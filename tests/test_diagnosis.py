"""Tests for the diagnosis layer.

Several of these exist because of bugs they actually caught during Day 3, noted
inline. The two that carry the most weight:

* `test_every_generated_reason_round_trips` — the generator once emitted
  invented codes (`gateway_error`, `payment_timeout`) that are not in Razorpay's
  documented vocabulary. Everything classified as UNKNOWN and a planted segment
  effect vanished. Nothing failed; the numbers were just quietly wrong.
* `test_detector_stays_quiet_on_a_null_batch` — a detector that finds signal is
  unremarkable. One that stays silent on noise is the credible one.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from recovery.diagnosis import Classifier, issuing_bank, segments_for
from recovery.diagnosis.patterns import _benjamini_hochberg, detect
from recovery.diagnosis.taxonomy import (
    DELIBERATELY_UNKNOWN,
    REASON_TO_CAUSE,
    cause_for_reason,
)
from recovery.models import Cause, ClassificationSource
from recovery.simulation import params as P
from recovery.simulation.generator import BatchGenerator


@pytest.fixture(scope="module")
def classified():
    batch = BatchGenerator(42).generate(10_000)
    return Classifier(use_model=False).classify_all(batch.events)


# -- taxonomy --------------------------------------------------------------


def test_every_generated_reason_round_trips() -> None:
    """Generator vocabulary must be real documented codes, and map correctly.

    Caught the Day 3 bug: five invented codes classified as UNKNOWN, which
    silently erased a planted segment effect.
    """
    mismatches = [
        (reason, intended, cause_for_reason(reason))
        for intended, reasons in P.REASON_STRINGS.items()
        for reason in reasons
        if cause_for_reason(reason) is not intended
    ]
    assert not mismatches, f"generator emits codes the taxonomy misreads: {mismatches}"


def test_generic_reasons_are_never_guessed() -> None:
    """`payment_failed` is Razorpay's catch-all and the only thing test mode
    returns. Mapping it to a cause would be inventing information."""
    for reason in ("payment_failed", "payment_pending", "record_not_found"):
        assert cause_for_reason(reason) is Cause.UNKNOWN


def test_unseen_reason_returns_none_not_unknown() -> None:
    """None means 'ask the model'; UNKNOWN means 'we refuse to guess'.

    Collapsing the two would either send generic codes to the model to be
    guessed at, or never use the fallback at all.
    """
    assert cause_for_reason("some_code_razorpay_added_last_week") is None
    assert cause_for_reason("payment_failed") is Cause.UNKNOWN


def test_taxonomy_has_no_overlap_between_mapped_and_unknown() -> None:
    assert not (set(REASON_TO_CAUSE) & set(DELIBERATELY_UNKNOWN))


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("insufficient_funds", Cause.INSUFFICIENT_FUNDS),
        ("transaction_daily_limit_exceeded", Cause.INSUFFICIENT_FUNDS),
        ("card_expired", Cause.CARD_EXPIRED),
        ("card_declined", Cause.HARD_DECLINE),
        ("debit_instrument_blocked", Cause.HARD_DECLINE),
        ("payment_risk_check_failed", Cause.HARD_DECLINE),
        ("bank_not_available", Cause.BANK_UNAVAILABLE),
        ("gateway_technical_error", Cause.BANK_UNAVAILABLE),
        ("payment_timed_out", Cause.NETWORK_TIMEOUT),
        ("incorrect_otp", Cause.AUTHENTICATION_REQUIRED),
        ("mandate_creation_expired", Cause.MANDATE_EXPIRED),
        ("mandate_creation_declined", Cause.MANDATE_REVOKED),
    ],
)
def test_rule_mapping(reason: str, expected: Cause) -> None:
    assert cause_for_reason(reason) is expected


def test_a_blocked_card_is_a_hard_decline_not_an_expiry() -> None:
    """The local model got this wrong in testing (stolen card -> card_expired).

    The distinction is not academic: expiry routes to re-authentication, a hard
    decline routes to stop. Getting it backwards spends attempts on a card that
    will never work.
    """
    assert cause_for_reason("debit_instrument_blocked") is Cause.HARD_DECLINE
    assert cause_for_reason("payment_risk_check_failed") is Cause.HARD_DECLINE


# -- classifier ------------------------------------------------------------


def test_rules_classify_the_whole_batch(classified) -> None:
    assert all(e.classified_by is ClassificationSource.RULE for e in classified)
    assert not any(e.cause is Cause.UNKNOWN for e in classified)


def test_model_failure_degrades_to_unknown() -> None:
    """A hung or missing model must not take down a 10k-event batch."""
    classifier = Classifier(use_model=True)
    with patch("httpx.post", side_effect=OSError("ollama is not running")):
        cause, source = classifier.classify_reason("a_totally_novel_code")
    assert cause is Cause.UNKNOWN
    assert classifier.model_failures == 1


def test_model_answers_are_marked_and_scored_lower() -> None:
    """The ledger must show which diagnoses were guessed rather than looked up."""
    classifier = Classifier(use_model=True)
    with patch.object(Classifier, "_ask_model", return_value=Cause.BANK_UNAVAILABLE):
        event = BatchGenerator(1).generate(1).events[0]
        event = event.model_copy(update={"error_reason": "novel_code_xyz"})
        result = classifier.classify(event)
    assert result.classified_by is ClassificationSource.LLM
    assert result.confidence < 0.95  # below the rule path, deliberately


def test_reason_strings_are_cached() -> None:
    classifier = Classifier(use_model=True)
    with patch.object(Classifier, "_ask_model", return_value=Cause.UNKNOWN) as mock:
        for _ in range(5):
            classifier.classify_reason("same_novel_code")
    assert mock.call_count == 1


# -- segments --------------------------------------------------------------


def test_issuing_bank_reads_the_right_field_per_method() -> None:
    """The field trap: bank lives somewhere different for each payment method."""
    assert issuing_bank({"method": "card", "card": {"issuer": "DCBL"}, "bank": None}) == "DCBL"
    assert issuing_bank({"method": "netbanking", "bank": "HDFC", "card": None}) == "HDFC"
    assert issuing_bank({"method": "upi", "bank": "SBIN"}) == "SBIN"
    assert issuing_bank({"method": "wallet", "wallet": "paytm"}) == "paytm"


def test_unknown_issuer_is_none_not_a_fake_bank() -> None:
    """An 'unknown' bucket would report a failure rate as if it were a real bank."""
    assert issuing_bank({"method": "card", "card": {}}) is None
    assert issuing_bank({"method": "card", "card": {"issuer": ""}}) is None


def test_segments_include_issuer_for_card_payments() -> None:
    event = BatchGenerator(42).generate(1).events[0]
    dimensions = {s.dimension for s in segments_for(event)}
    assert "issuer" in dimensions
    assert {"method", "network", "amount_band", "time_band"} <= dimensions


# -- pattern detection -----------------------------------------------------


def test_benjamini_hochberg_is_monotone_and_bounded() -> None:
    q, flags = _benjamini_hochberg([0.001, 0.01, 0.04, 0.3, 0.9], fdr=0.10)
    assert all(0.0 <= v <= 1.0 for v in q)
    assert q == sorted(q)
    assert flags[0] is True and flags[-1] is False


def test_detector_finds_the_planted_effects(classified) -> None:
    significant = [f for f in detect(classified) if f.significant]
    found = {(str(f.segment), f.cause) for f in significant if f.lift > 1.5}

    assert ("issuer=SYNTHETIC_BANK_A", Cause.BANK_UNAVAILABLE) in found
    assert ("amount_band=high", Cause.AUTHENTICATION_REQUIRED) in found

    # And the measured lift should be close to what was planted.
    bank = next(
        f for f in significant
        if str(f.segment) == "issuer=SYNTHETIC_BANK_A" and f.cause is Cause.BANK_UNAVAILABLE
    )
    assert 2.0 < bank.lift < 3.5  # planted at 2.8x


def test_detector_stays_quiet_on_a_null_batch() -> None:
    """The credibility test. Finding signal is easy; staying silent is not."""
    with patch("recovery.simulation.params.SEGMENT_EFFECTS", ()):
        batch = BatchGenerator(42).generate(10_000)
    events = Classifier(use_model=False).classify_all(batch.events)
    findings = detect(events)

    assert [f for f in findings if f.significant] == []

    # And show what the correction bought: uncorrected, this same noise would
    # have produced a fistful of confident "findings".
    uncorrected = [f for f in findings if f.p_value < 0.05]
    assert len(uncorrected) > 0, "expected some raw p < 0.05 on pure noise"


def test_small_segments_are_not_tested(classified) -> None:
    """A segment of 3 events can show a 100% rate and pass any test."""
    findings = detect(classified, min_support=500)
    assert all(f.n_segment >= 500 for f in findings)
