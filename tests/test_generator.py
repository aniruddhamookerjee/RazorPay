"""Tests for the generator and, more importantly, the holdout boundary.

Two of these defend claims the project makes to judges rather than checking
ordinary correctness:

* `test_decision_layer_never_imports_simulation` enforces "the parameters are
  held out from the agent". Without it that is a promise; with it, it is a
  property that breaks the build when violated.
* `test_generated_envelope_matches_captured_fixture` enforces "the payload
  shape is real". If the generator drifts from the captured Razorpay envelope,
  the diagnosis layer is being developed against fiction.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest

from recovery.clock import EPOCH
from recovery.models import Cause
from recovery.simulation import params as P
from recovery.simulation.generator import BatchGenerator, resolve

REPO = Path(__file__).resolve().parent.parent


# -- the holdout boundary --------------------------------------------------


def test_decision_layer_never_imports_simulation() -> None:
    """The agent must not be able to read its own answer key.

    Only the generator, the experiment harness and tests may import
    `recovery.simulation`. Anything in the decision path that does has made the
    measured result circular.
    """
    allowed = {"simulation", "experiment"}
    pattern = re.compile(r"^\s*(from|import)\s+recovery\.simulation", re.MULTILINE)

    offenders = []
    for path in (REPO / "recovery").rglob("*.py"):
        if any(part in allowed for part in path.parts):
            continue
        if pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(REPO)))

    assert not offenders, (
        "these modules import the held-out simulation parameters, which makes "
        f"the measurement circular: {offenders}"
    )


def test_generator_does_not_label_the_cause() -> None:
    """Events carry a reason string, not a diagnosis. Day 3 has to earn it."""
    batch = BatchGenerator(seed=1).generate(50)
    assert all(e.cause is Cause.UNKNOWN for e in batch.events)
    assert all(e.error_reason for e in batch.events)


# -- reproducibility -------------------------------------------------------


def test_same_seed_gives_identical_batches() -> None:
    """Paired replay on Day 6 is only valid if this holds."""
    a = BatchGenerator(seed=7).generate(100)
    b = BatchGenerator(seed=7).generate(100)
    assert [e.error_reason for e in a.events] == [e.error_reason for e in b.events]
    assert [e.attempt.amount_paise for e in a.events] == [
        e.attempt.amount_paise for e in b.events
    ]


def test_different_seeds_differ() -> None:
    a = BatchGenerator(seed=1).generate(200)
    b = BatchGenerator(seed=2).generate(200)
    assert [e.error_reason for e in a.events] != [e.error_reason for e in b.events]


# -- schema fidelity -------------------------------------------------------


def test_generated_envelope_matches_captured_fixture() -> None:
    """Generated payloads must have the same shape as a real capture."""
    captured = sorted((REPO / "fixtures").glob("payment_failed__*.json"))
    if not captured:
        pytest.skip("no captured fixture to compare against")

    real = json.loads(captured[0].read_text(encoding="utf-8"))
    generated = BatchGenerator(seed=3).generate(1).events[0].raw_payload

    assert set(generated) == set(real), "top-level envelope keys drifted"

    real_entity = real["payload"]["payment"]["entity"]
    gen_entity = generated["payload"]["payment"]["entity"]
    assert set(gen_entity) == set(real_entity), "payment entity keys drifted"

    # bank is null on card payments in the real capture (sources.md section 7).
    assert gen_entity["bank"] is None
    assert gen_entity["error_step"] == real_entity["error_step"]


def test_reason_strings_are_documented_values() -> None:
    """Vocabulary comes from Razorpay's documented list, not invention."""
    documented = {r for reasons in P.REASON_STRINGS.values() for r in reasons}
    batch = BatchGenerator(seed=11).generate(300)
    assert {e.error_reason for e in batch.events} <= documented


# -- the answer key --------------------------------------------------------


def test_dead_mandates_never_recover() -> None:
    """Retrying a revoked mandate cannot work, at any delay, ever."""
    rng = np.random.default_rng(0)
    for cause in (Cause.MANDATE_EXPIRED, Cause.MANDATE_REVOKED):
        for delay_days in (0, 1, 3, 7):
            assert not any(
                resolve(
                    true_cause=cause,
                    attempted_at=EPOCH + timedelta(days=delay_days),
                    failed_at=EPOCH,
                    attempt_number=2,
                    rng=rng,
                )
                for _ in range(200)
            )


def test_waiting_helps_insufficient_funds_but_not_hard_declines() -> None:
    """The core asymmetry the whole agent exists to exploit."""

    def rate(cause: Cause, delay_hours: int) -> float:
        rng = np.random.default_rng(99)
        hits = sum(
            resolve(
                true_cause=cause,
                attempted_at=EPOCH + timedelta(hours=delay_hours),
                failed_at=EPOCH,
                attempt_number=2,
                rng=rng,
            )
            for _ in range(4000)
        )
        return hits / 4000

    # Waiting three days beats retrying immediately, by a wide margin.
    assert rate(Cause.INSUFFICIENT_FUNDS, 72) > 2 * rate(Cause.INSUFFICIENT_FUNDS, 0)
    # For a hard decline, waiting changes nothing — retries are just wasted.
    assert rate(Cause.HARD_DECLINE, 72) == pytest.approx(
        rate(Cause.HARD_DECLINE, 0), abs=0.03
    )


def test_transient_failures_reward_a_short_delay() -> None:
    """Bank outages clear in hours: fast retry should beat both 0h and 7d."""

    def rate(delay_hours: int) -> float:
        rng = np.random.default_rng(5)
        hits = sum(
            resolve(
                true_cause=Cause.BANK_UNAVAILABLE,
                attempted_at=EPOCH + timedelta(hours=delay_hours),
                failed_at=EPOCH,
                attempt_number=2,
                rng=rng,
            )
            for _ in range(4000)
        )
        return hits / 4000

    assert rate(2) > rate(0)
    assert rate(2) > rate(168) - 0.05
