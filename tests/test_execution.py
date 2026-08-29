"""Tests for the execution layer.

The live-executor guards carry the most weight here. `LiveExecutor` makes real
API calls, and a bug that fanned out across 10,000 events would be the worst
outcome available in this project — so each guard is tested on its own, because
"one of the four will catch it" is not a safety argument.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from recovery.decision import CostModel, Policy, SuccessModel
from recovery.diagnosis import Classifier
from recovery.execution import (
    LiveExecutor,
    RecoveryRunner,
    SimulatedExecutor,
    idempotency_key,
)
from recovery.ledger import Ledger
from recovery.models import Action, Arm, AttemptStatus, DecisionStatus
from recovery.simulation.generator import BatchGenerator

NOW = datetime(2026, 1, 10, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def ledger():
    return Ledger(f"sqlite:///{Path(tempfile.mkdtemp()) / 'test.db'}")


@pytest.fixture(scope="module")
def batch():
    return BatchGenerator(42).generate(300)


def always(result: bool):
    return lambda event, action, when, attempt: result


def make_runner(ledger, oracle, **kwargs):
    return RecoveryRunner(
        classifier=Classifier(use_model=False),
        policy=Policy(success=SuccessModel(), costs=CostModel()),
        executor=SimulatedExecutor(oracle=oracle),
        ledger=ledger,
        run_id="test",
        seed=42,
        **kwargs,
    )


# -- idempotency -----------------------------------------------------------


def test_idempotency_key_is_deterministic() -> None:
    a = idempotency_key(subscription_id="sub_1", cycle=0, step=2)
    b = idempotency_key(subscription_id="sub_1", cycle=0, step=2)
    assert a == b
    assert a != idempotency_key(subscription_id="sub_1", cycle=1, step=2)
    assert a != idempotency_key(subscription_id="sub_1", cycle=0, step=3)


def test_the_database_refuses_a_duplicate_execution(ledger, batch) -> None:
    """A crash mid-run then a re-run must not charge anyone twice.

    Enforced by a unique constraint, not by a check someone remembered to write.
    """
    runner = make_runner(ledger, always(False))
    entries = runner.run_event(batch.events[0])
    keyed = [e for e in entries if e.idempotency_key]
    assert keyed

    # run_event returns rows without writing them; run() does the writing. So
    # the first append is the original, and the second is the replay a crashed
    # run would produce.
    ledger.append(keyed[0])
    with pytest.raises(Exception):
        ledger.append(keyed[0])


# -- live executor guards --------------------------------------------------


def live_call_args(event):
    return dict(
        event=event,
        action=Action.RETRY_NOW,
        attempted_at=NOW,
        attempt_number=2,
        step=0,
        cycle=0,
        cost_paise=200,
    )


def test_live_executor_is_off_by_default(batch) -> None:
    """Firing at a real API must always be a deliberate act."""
    executor = LiveExecutor()
    assert executor.enabled is False
    result = executor.execute(**live_call_args(batch.events[0]))
    assert result.detail == "refused: live_executor_disabled"
    assert executor.calls_made == 0


def test_live_executor_refuses_simulated_events(batch) -> None:
    """The most important guard: a simulated event must never reach a real API."""
    executor = LiveExecutor(enabled=True)
    event = batch.events[0]
    assert not event.attempt.is_live
    result = executor.execute(**live_call_args(event))
    assert result.detail == "refused: event_not_in_live_subset"
    assert executor.calls_made == 0


def test_live_executor_refuses_non_test_keys(batch) -> None:
    _, live = BatchGenerator(42).generate_split(volume_count=200, live_count=10)
    executor = LiveExecutor(enabled=True)
    with patch("recovery.execution.executors.settings") as fake:
        fake.razorpay_key_id = "rzp_live_dangerous"
        fake.live_subset_size = 100
        result = executor.execute(**live_call_args(live.events[0]))
    assert result.detail == "refused: not_a_test_key"
    assert executor.calls_made == 0


def test_live_executor_respects_its_call_cap() -> None:
    _, live = BatchGenerator(42).generate_split(volume_count=400, live_count=20)
    executor = LiveExecutor(enabled=True, max_calls=2)
    with patch("httpx.post") as post:
        post.return_value.status_code = 200
        post.return_value.json.return_value = {"id": "plink_x", "short_url": "http://x"}
        for event in live.events[:5]:
            executor.execute(**live_call_args(event))
    assert executor.calls_made == 2
    assert executor.refusals.get("live_call_cap_reached") == 3


def test_live_executor_treats_a_duplicate_reference_as_idempotency(batch) -> None:
    """Razorpay rejecting a repeated reference_id is the guard working."""
    _, live = BatchGenerator(42).generate_split(volume_count=200, live_count=10)
    executor = LiveExecutor(enabled=True)
    with patch("httpx.post") as post:
        post.return_value.status_code = 400
        post.return_value.text = '{"error":{"description":"reference_id already exists"}}'
        result = executor.execute(**live_call_args(live.events[0]))
    assert result.outcome is AttemptStatus.PENDING
    assert result.cost_paise == 0  # a refused duplicate costs nothing
    assert "duplicate" in (result.detail or "")


def test_live_executor_survives_a_network_failure(batch) -> None:
    _, live = BatchGenerator(42).generate_split(volume_count=200, live_count=10)
    executor = LiveExecutor(enabled=True)
    with patch("httpx.post", side_effect=OSError("connection reset")):
        result = executor.execute(**live_call_args(live.events[0]))
    assert result.outcome is AttemptStatus.FAILED
    assert "transport error" in (result.detail or "")


# -- the runner ------------------------------------------------------------


def test_every_pass_writes_a_ledger_row_including_stops(ledger, batch) -> None:
    """'We stopped, and here is which rule stopped us' cannot be the missing row."""
    runner = make_runner(ledger, always(False))
    runner.run(batch.events[:50])

    rows = ledger.all()
    assert rows
    stopped = [r for r in rows if r.status is DecisionStatus.STOPPED]
    assert stopped, "expected some cases to stop"
    assert all(r.stop_reason for r in stopped)


def test_the_ledger_row_carries_the_whole_trail(ledger, batch) -> None:
    runner = make_runner(ledger, always(True))
    runner.run(batch.events[:20])

    row = ledger.all()[0]
    assert row.error_reason           # the original reason, verbatim
    assert row.cause                  # what we diagnosed it as
    assert row.classified_by          # how we arrived at that
    assert row.scores                 # the whole EV table, not just the winner
    assert row.idempotency_key
    assert row.run_id == "test"


def test_outcomes_feed_back_into_the_model(ledger, batch) -> None:
    """The only channel by which evidence reaches the agent."""
    runner = make_runner(ledger, always(True))
    assert runner.policy.success.is_cold
    runner.run(batch.events[:40])
    assert not runner.policy.success.is_cold
    assert runner.policy.success.total_observations > 0


def test_a_notification_earns_the_right_to_debit(ledger, batch) -> None:
    """RBI requires notice before a debit, so something must be able to send it.

    Without this the gate was a wall rather than a step: no action could satisfy
    the pre-debit requirement, so every debit was blocked forever and the batch
    recovered almost nothing.
    """
    runner = make_runner(ledger, always(False))
    entries = []
    for event in batch.events[:80]:
        entries.extend(runner.run_event(event, pre_debit_notice_sent_at=None))

    actions = [e.action for e in entries]
    assert Action.NOTIFY in actions or Action.REQUEST_REAUTH in actions
    # Some case must get past the notice requirement to an actual debit.
    assert any(a in (Action.RETRY_NOW, Action.RETRY_DELAYED) for a in actions)


def test_the_runner_cannot_loop_forever(ledger, batch) -> None:
    """Belt and braces over the policy's own caps."""
    runner = make_runner(ledger, always(False))
    for event in batch.events[:30]:
        entries = runner.run_event(event)
        assert len(entries) <= 10


def test_success_ends_the_case(ledger, batch) -> None:
    runner = make_runner(ledger, always(True))
    for event in batch.events[:30]:
        entries = runner.run_event(event)
        succeeded = [e for e in entries if e.outcome is AttemptStatus.SUCCEEDED]
        if succeeded:
            assert entries[-1].outcome is AttemptStatus.SUCCEEDED


def test_wait_and_stop_cost_nothing(ledger, batch) -> None:
    runner = make_runner(ledger, always(False))
    runner.run(batch.events[:60])
    for row in ledger.all():
        if row.action in (Action.WAIT, Action.STOP):
            assert row.cost_paise == 0
            assert row.recovered_paise == 0


def test_net_is_gross_minus_cost(ledger, batch) -> None:
    runner = make_runner(ledger, always(True))
    stats = runner.run(batch.events[:40])
    totals = ledger.totals()
    assert totals["net_paise"] == totals["gross_paise"] - totals["cost_paise"]
    assert stats.net_paise == totals["net_paise"]


def test_arm_is_recorded_for_the_day_6_comparison(ledger, batch) -> None:
    runner = make_runner(ledger, always(False), arm=Arm.FIXED_3X)
    runner.run(batch.events[:20])
    assert all(r.arm is Arm.FIXED_3X for r in ledger.all())
    assert ledger.all(arm=Arm.AGENT) == []


# -- regressions from the Day 5 audit --------------------------------------


def test_a_notification_does_not_consume_a_retry(ledger, batch) -> None:
    """'Max 3 retry attempts' means debits, not every action.

    Counting a notification against the retry cap produced the sequence
    notify -> retry -> stop, leaving exactly one real retry per case. Recovery
    on a 500-event batch was 8.4%; separating the budgets took it to 18.4%.
    """
    runner = make_runner(ledger, always(False))
    entries = []
    for event in batch.events[:120]:
        entries.extend(runner.run_event(event))

    by_sub: dict[str, list] = {}
    for entry in entries:
        by_sub.setdefault(entry.subscription_id, []).append(entry)

    saw_multi_retry = False
    for rows in by_sub.values():
        debits = [
            r for r in rows if r.action in (Action.RETRY_NOW, Action.RETRY_DELAYED)
        ]
        # A notification must not have advanced the debit counter.
        for row in rows:
            if row.action in (Action.NOTIFY, Action.REQUEST_REAUTH):
                assert row.attempt_number <= len(debits) + 1
        if len(debits) >= 2:
            saw_multi_retry = True

    assert saw_multi_retry, "a case should be able to retry more than once"


def test_contacts_are_capped_separately(ledger, batch) -> None:
    """Separating the budgets must not mean unlimited messaging."""
    from recovery.config import settings

    runner = make_runner(ledger, always(False))
    for event in batch.events[:120]:
        entries = runner.run_event(event)
        contacts = [
            e for e in entries
            if e.action in (Action.NOTIFY, Action.REQUEST_REAUTH)
        ]
        assert len(contacts) <= settings.max_contacts


def test_idempotency_keys_are_unique_within_a_case(ledger, batch) -> None:
    """Keyed on step, not debit attempt.

    When contacts stopped incrementing the attempt counter, an attempt-keyed
    value collided between a notification and the retry after it, and the
    ledger's unique constraint rejected the write.
    """
    runner = make_runner(ledger, always(False))
    for event in batch.events[:60]:
        entries = runner.run_event(event)
        keys = [e.idempotency_key for e in entries]
        assert len(keys) == len(set(keys))
