"""Cited parameters for the simulated world. HELD OUT from the agent.

Every number here carries its source inline. See `sources.md` for the full
citation ledger and the honesty argument behind it.

**On source quality — read this before quoting any number.** These come from
subscription-billing vendor reports and industry write-ups, not peer-reviewed
research. They are directionally credible and publicly checkable, which is what
makes them better than invention, but they are not precise. That is exactly why
the project reports a **sensitivity band** rather than a point estimate: the
claim is "our agent wins across this range of plausible assumptions", not "the
insufficient-funds recovery rate is 0.47".

Nothing here is read by the decision layer. It exists to generate outcomes the
agent then has to learn from, the same way it would learn from production.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from recovery.models import Cause

# ---------------------------------------------------------------------------
# Cause distribution
# ---------------------------------------------------------------------------
# Insufficient funds is "nearly half of all failures"; the remainder splits
# across expired cards, declines, technical failures and authentication.
#   https://baremetrics.com/blog/recover-failed-payments-save-lost-revenue
#   https://www.digitalapplied.com/blog/failed-payment-recovery-dunning-playbook-2026
#
# The split *within* the non-insufficient-funds remainder is less well sourced
# than the headline. Treated as an assumption and swept — see sources.md section 1.
CAUSE_WEIGHTS: dict[Cause, float] = {
    Cause.INSUFFICIENT_FUNDS: 0.45,
    Cause.CARD_EXPIRED: 0.15,
    Cause.HARD_DECLINE: 0.12,
    Cause.AUTHENTICATION_REQUIRED: 0.10,
    Cause.BANK_UNAVAILABLE: 0.07,
    Cause.NETWORK_TIMEOUT: 0.05,
    Cause.MANDATE_EXPIRED: 0.04,
    Cause.MANDATE_REVOKED: 0.02,
}

# ---------------------------------------------------------------------------
# Razorpay's documented reason strings, per cause
# ---------------------------------------------------------------------------
# Test mode only ever returns the generic `payment_failed` (verified across
# three captures, 27 Aug — see sources.md section 7), so the vocabulary comes
# from Razorpay's documented list instead:
#   https://razorpay.com/docs/payments/payment-gateway/rainy-day/errors/error-reasons/
REASON_STRINGS: dict[Cause, tuple[str, ...]] = {
    Cause.INSUFFICIENT_FUNDS: ("insufficient_funds",),
    Cause.CARD_EXPIRED: ("card_expired",),
    Cause.HARD_DECLINE: ("card_declined", "authorisation_declined_by_psp"),
    Cause.AUTHENTICATION_REQUIRED: ("authentication_failed", "invalid_otp"),
    Cause.BANK_UNAVAILABLE: ("gateway_error",),
    Cause.NETWORK_TIMEOUT: ("payment_timeout",),
    Cause.MANDATE_EXPIRED: ("mandate_expired",),
    Cause.MANDATE_REVOKED: ("mandate_revoked",),
}


@dataclass(frozen=True)
class RecoveryCurve:
    """How recoverable a cause is, and how that depends on waiting.

    `base` is the probability a well-timed retry succeeds at all.
    `delay_multiplier` maps hours-of-delay to a factor on `base`, so the shape
    of "waiting helps / waiting is pointless" is per-cause rather than global.
    """

    base: float
    delay_multiplier: dict[int, float]
    # Each further attempt on the same cycle is worth less than the last.
    attempt_decay: float = 0.6
    note: str = ""

    def p_success(self, *, delay_hours: int, attempt_number: int) -> float:
        nearest = min(self.delay_multiplier, key=lambda h: abs(h - delay_hours))
        p = self.base * self.delay_multiplier[nearest]
        p *= self.attempt_decay ** max(0, attempt_number - 2)
        return max(0.0, min(1.0, p))


# ---------------------------------------------------------------------------
# Recovery probability by cause and timing
# ---------------------------------------------------------------------------
# Headline recovery rates by decline type — expired cards 50-70% on retry,
# insufficient funds 40-60%, fraud/hard declines 20-40%:
#   https://www.digitalapplied.com/blog/failed-payment-recovery-dunning-playbook-2026
#
# Timing shape — insufficient-funds retries spaced 3-5 days to align with
# payday cycles (1st and 15th); retrying at 24h rather than 2h improved
# recovery by 6.5% (Paddle); Tue-Thu outperform weekends:
#   https://gr4vy.com/posts/payment-retry-logic-explained-smart-retries-for-failed-transactions-in-2026/
#   https://solidgate.com/blog/smart-retries-for-revenue-recovery/
RECOVERY: dict[Cause, RecoveryCurve] = {
    # Waiting is the whole game: the account refills on payday.
    Cause.INSUFFICIENT_FUNDS: RecoveryCurve(
        base=0.50,
        delay_multiplier={0: 0.18, 2: 0.22, 24: 0.45, 72: 0.90, 120: 1.00, 168: 0.85},
        note="3-5 day spacing aligns with payday; immediate retry mostly wasted",
    ),
    # A dead card stays dead until the customer updates it — retrying cannot
    # fix it, but a re-auth request can. High ceiling, unreachable by retry.
    Cause.CARD_EXPIRED: RecoveryCurve(
        base=0.08,
        delay_multiplier={0: 1.0, 24: 1.0, 72: 1.0, 168: 1.0},
        attempt_decay=1.0,
        note="retry near-useless; recovery comes via re-auth, not repetition",
    ),
    # Transient. Short delay is nearly free and works; long delay adds nothing.
    Cause.BANK_UNAVAILABLE: RecoveryCurve(
        base=0.75,
        delay_multiplier={0: 0.30, 2: 0.95, 24: 1.00, 72: 0.95, 168: 0.90},
        attempt_decay=0.8,
        note="outage clears in hours; this is where fast retry earns its keep",
    ),
    Cause.NETWORK_TIMEOUT: RecoveryCurve(
        base=0.80,
        delay_multiplier={0: 0.60, 1: 0.95, 2: 1.00, 24: 0.95, 168: 0.85},
        attempt_decay=0.8,
        note="often already resolved; may surface as a late authorisation",
    ),
    # Hard decline: the bank means it. Retrying burns attempts and goodwill.
    Cause.HARD_DECLINE: RecoveryCurve(
        base=0.04,
        delay_multiplier={0: 1.0, 24: 1.0, 72: 1.0, 168: 1.0},
        attempt_decay=1.0,
        note="20-40% band in sources is for fraud holds generally; a firm "
        "decline sits at the bottom of it",
    ),
    Cause.AUTHENTICATION_REQUIRED: RecoveryCurve(
        base=0.12,
        delay_multiplier={0: 1.0, 24: 1.0, 168: 1.0},
        attempt_decay=1.0,
        note="needs the customer to authenticate; retry alone rarely helps",
    ),
    # Retrying a dead mandate can never work. This is the clearest case where a
    # cause-aware agent should refuse to spend an attempt at all.
    Cause.MANDATE_EXPIRED: RecoveryCurve(
        base=0.0,
        delay_multiplier={0: 0.0, 24: 0.0, 168: 0.0},
        attempt_decay=1.0,
        note="structurally impossible: no valid mandate, no debit",
    ),
    Cause.MANDATE_REVOKED: RecoveryCurve(
        base=0.0,
        delay_multiplier={0: 0.0, 24: 0.0, 168: 0.0},
        attempt_decay=1.0,
        note="customer withdrew permission; retrying is also a compliance risk",
    ),
    Cause.UNKNOWN: RecoveryCurve(
        base=0.20,
        delay_multiplier={0: 0.7, 24: 1.0, 72: 1.0, 168: 0.9},
        note="fallback for unmapped reasons",
    ),
}

# ---------------------------------------------------------------------------
# Day-of-week and payday effects
# ---------------------------------------------------------------------------
# "Tuesday through Thursday typically see the highest approval rates for
# retries, while weekends see the lowest."
#   https://gr4vy.com/posts/payment-retry-logic-explained-smart-retries-for-failed-transactions-in-2026/
WEEKDAY_MULTIPLIER: dict[int, float] = {
    0: 0.98,  # Monday
    1: 1.05,  # Tuesday
    2: 1.05,  # Wednesday
    3: 1.03,  # Thursday
    4: 0.97,  # Friday
    5: 0.85,  # Saturday
    6: 0.82,  # Sunday
}

# Paydays (1st and 15th) drive approval spikes for insufficient-funds declines.
PAYDAY_DAYS: tuple[int, ...] = (1, 2, 3, 15, 16, 17)
PAYDAY_MULTIPLIER: float = 1.35

# ---------------------------------------------------------------------------
# Costs
# ---------------------------------------------------------------------------
# ASSUMPTION — not sourced. Swept in the sensitivity analysis (sources.md 3).
ATTEMPT_COST_PAISE: int = 200
NOTIFICATION_COST_PAISE: int = 25
CUSTOMER_LTV_PAISE: int = 1_200_000

# ASSUMPTION, and the softest number in the model (WORKPLAN.md 4.3). The whole
# analysis is reported at churn_penalty = 0 as well, so the agent's advantage
# can be judged with this switched off entirely.
CHURN_PROBABILITY_BY_ATTEMPTS: dict[int, float] = {0: 0.00, 1: 0.01, 2: 0.03, 3: 0.07}

# ---------------------------------------------------------------------------
# Planted segment effects
# ---------------------------------------------------------------------------
# Synthetic by construction. The point is testing whether the detector recovers
# a known signal and stays silent on noise — never a claim about a real bank.
@dataclass(frozen=True)
class SegmentEffect:
    dimension: str
    value: str
    cause: Cause
    multiplier: float
    note: str = ""


SEGMENT_EFFECTS: tuple[SegmentEffect, ...] = (
    SegmentEffect(
        dimension="issuer",
        value="SYNTHETIC_BANK_A",
        cause=Cause.BANK_UNAVAILABLE,
        multiplier=2.8,
        note="planted: detector must find this above baseline",
    ),
    SegmentEffect(
        dimension="amount_band",
        value="high",
        cause=Cause.AUTHENTICATION_REQUIRED,
        multiplier=2.2,
        note="planted: mirrors AFA friction above the RBI threshold",
    ),
)

# ---------------------------------------------------------------------------
# Sanity ceiling
# ---------------------------------------------------------------------------
# Published smart-retry uplift over fixed schedules is 15-40%:
#   https://solidgate.com/blog/smart-retries-for-revenue-recovery/
#   https://gr4vy.com/posts/payment-retry-logic-explained-smart-retries-for-failed-transactions-in-2026/
#
# If our agent reports an uplift far outside this band, suspect a bug — most
# likely a leak of these parameters into the policy, or a strawman baseline —
# before believing the result. Asserted in the experiment harness on Day 6.
PLAUSIBLE_UPLIFT_BAND: tuple[float, float] = (0.15, 0.40)


@dataclass(frozen=True)
class Population:
    """Shape of the simulated merchant book."""

    subscriptions: int = 10_000
    amount_choices_paise: tuple[int, ...] = (19_900, 49_900, 99_900, 249_900, 1_999_900)
    amount_weights: tuple[float, ...] = (0.30, 0.35, 0.20, 0.10, 0.05)
    # Card networks and synthetic issuers for the segment matrix. Note the
    # captured payload has bank=None on card payments (sources.md section 7),
    # so cards are segmented on issuer/network rather than `bank`.
    networks: tuple[str, ...] = ("Visa", "MasterCard", "RuPay", "Amex")
    network_weights: tuple[float, ...] = (0.45, 0.35, 0.18, 0.02)
    issuers: tuple[str, ...] = field(
        default=("SYNTHETIC_BANK_A", "SYNTHETIC_BANK_B", "SYNTHETIC_BANK_C")
    )
    issuer_weights: tuple[float, ...] = (0.30, 0.40, 0.30)
    # Fraction of scheduled charges that fail at all. Card failure rates run
    # near 15%: https://baremetrics.com/blog/involuntary-churn
    failure_rate: float = 0.15
