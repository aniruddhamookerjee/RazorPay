"""Razorpay's documented reason strings, mapped onto our nine causes.

Source: <https://razorpay.com/docs/payments/payment-gateway/rainy-day/errors/error-reasons/>

**Why this is a table and not a model.** Test mode only ever returns the generic
`payment_failed` (verified across three captures — `sources.md` §7), so the
vocabulary could not be learned from data. It comes from the documented list,
which is also why the mapping is auditable: every row is a published string.

**The organising rule.** A cause earns its own category only if it implies a
*different best action*. Two reasons that both mean "wait and retry" belong in
one bucket no matter how differently the bank phrases them. That is why daily
limits sit with insufficient funds (both: the money isn't available *right now*)
and why a blocked card sits with hard declines (both: stop, this will not work).

**Known gap — merchant/integration errors.** Codes like `invalid_order_id`,
`merchant_not_activated` and `input_validation_failed` are our bugs, not the
customer's problem. Retrying cannot help, and they need an ops alert rather than
a recovery action. There is no category for that, so they map to `UNKNOWN`
rather than being forced into a cause they do not belong to. In a recurring
charge batch they should not appear at all; if they show up in volume, that is
itself a finding.
"""

from __future__ import annotations

from recovery.models import Cause

# ---------------------------------------------------------------------------
# The mapping
# ---------------------------------------------------------------------------

_BY_CAUSE: dict[Cause, tuple[str, ...]] = {
    # The money is not there *right now*. Waiting is the entire remedy — which
    # is why limit breaches live here too: a daily cap clears tomorrow.
    Cause.INSUFFICIENT_FUNDS: (
        "insufficient_funds",
        "credit_limit_exceeded",
        "transaction_limit_exceeded",
        "transaction_daily_limit_exceeded",
        "transaction_daily_count_exceeded",
        "transaction_frequency_limit_exceeded",
        "funds_blocked_by_mandate",
    ),
    # The instrument is stale. No amount of retrying fixes it; the customer has
    # to supply a new card, so the action is re-authentication, not repetition.
    Cause.CARD_EXPIRED: (
        "card_expired",
        "incorrect_card_expiry_date",
        "credit_limit_expired",
    ),
    # No usable standing permission to debit. Retrying is pointless.
    Cause.MANDATE_EXPIRED: (
        "mandate_creation_expired",
        "mandate_creation_timeout",
    ),
    # Permission actively refused or absent. Retrying is also a compliance risk,
    # not merely a waste — which is why it is separate from MANDATE_EXPIRED.
    Cause.MANDATE_REVOKED: (
        "mandate_creation_declined",
        "mandate_creation_failed",
        "reqauth_mandate_not_acknowledged",
        "recurring_payment_not_enabled",
        "upi_autopay_not_supported_on_psp",
    ),
    # Somebody else's infrastructure is having a bad hour. Transient: a short
    # delay is the cheapest thing that works.
    Cause.BANK_UNAVAILABLE: (
        "bank_not_available",
        "bank_cutoff_in_progress",
        "bank_technical_error",
        "issuer_technical_error",
        "gateway_technical_error",
        "upi_app_technical_error",
        "psp_not_available",
        "psp_app_not_available",
        "server_error",
        "payment_declined_due_to_high_traffic",
    ),
    # No clean answer came back. Often already resolved; may surface later as a
    # late authorisation, which `definitions.md` counts as recovered.
    Cause.NETWORK_TIMEOUT: (
        "payment_timed_out",
        "request_timed_out",
        "payment_session_expired",
        "payment_collect_request_expired",
        "invalid_response_from_gateway",
        "deemed_transaction",
        "duplicate_rrn_found",
    ),
    # A firm no: fraud, blocks, restrictions, permanent ineligibility. Every
    # further attempt spends money and goodwill for nothing.
    Cause.HARD_DECLINE: (
        "card_declined",
        "payment_declined",
        "debit_declined",
        "authorisation_declined_by_psp",
        "debit_instrument_blocked",
        "debit_instrument_inactive",
        "payment_risk_check_failed",
        "compliance_violation",
        "card_number_invalid",
        "card_type_invalid",
        "card_network_not_enabled",
        "bank_account_invalid",
        "bank_account_validation_failed",
        "transaction_on_vpa_restricted",
        "international_transaction_not_allowed",
        "payment_method_not_enabled",
        "credit_not_permitted",
        "beneficiary_account_does_not_exist",
        "beneficiary_account_dormant",
        "user_not_eligible",
        "payment_cancelled",
    ),
    # The customer must prove it is them. A retry alone almost never helps;
    # the action is to route them through authentication again.
    Cause.AUTHENTICATION_REQUIRED: (
        "authentication_failed",
        "incorrect_otp",
        "otp_expired",
        "otp_attempts_exceeded",
        "incorrect_pin",
        "incorrect_atm_pin",
        "pin_attempts_exceeded",
        "pin_not_set",
        "card_not_enrolled",
        "incorrect_cvv",
        "incorrect_card_details",
        "incorrect_cardholder_name",
        "invalid_vpa",
        "vpa_resolution_failed",
        "psp_not_registered",
        "invalid_device",
        "mobile_number_invalid",
        "user_not_registered_for_netbanking",
    ),
}

REASON_TO_CAUSE: dict[str, Cause] = {
    reason: cause for cause, reasons in _BY_CAUSE.items() for reason in reasons
}

# Documented codes we deliberately refuse to guess at. `payment_failed` is the
# important one: it is Razorpay's catch-all, it is the *only* thing test mode
# returns, and mapping it to a cause would be inventing information. A generic
# failure must diagnose as UNKNOWN so the decision layer treats it cautiously
# rather than confidently retrying the wrong way.
DELIBERATELY_UNKNOWN: frozenset[str] = frozenset(
    {
        "payment_failed",
        "payment_pending",
        "payment_pending_approval",
        "collect_request_pending",
        "record_not_found",
        "verification_failed",
        # Merchant/integration errors — our bug, not a recovery case.
        "invalid_request",
        "input_validation_failed",
        "invalid_order_id",
        "invalid_amount",
        "invalid_currency",
        "invalid_email",
        "invalid_mobile_number",
        "invalid_user_details",
        "merchant_not_activated",
        "live_mode_not_enabled",
        "bank_not_enabled",
        "order_already_paid",
        "order_amount_mismatch",
        "order_payment_method_mismatch",
        "payment_amount_tampered",
        "duplicate_request",
        "capture_failed",
        "mismatch_in_transaction_details",
        "amount_less_than_minimum_amount",
    }
)


def cause_for_reason(reason: str | None) -> Cause | None:
    """Look up a documented reason string.

    Returns `None` when the string is not in the table at all — that is the
    signal for the model fallback. Returns `Cause.UNKNOWN` when the string *is*
    known but is one we refuse to guess at, which is a different thing and must
    not be sent to the model to be guessed at anyway.
    """
    if not reason:
        return Cause.UNKNOWN

    key = reason.strip().lower()
    if key in DELIBERATELY_UNKNOWN:
        return Cause.UNKNOWN
    return REASON_TO_CAUSE.get(key)


def coverage() -> dict[str, int]:
    """How many documented strings each cause covers. Used by the CLI report."""
    counts = {cause.value: len(reasons) for cause, reasons in _BY_CAUSE.items()}
    counts["unknown (deliberate)"] = len(DELIBERATELY_UNKNOWN)
    return counts
