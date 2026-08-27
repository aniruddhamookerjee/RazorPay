"""Tests for the virtual clock and the append-only ledger.

The ledger tests are the ones that matter: "append-only" is a claim the project
makes to judges, so it needs to be enforced rather than asserted. These prove
UPDATE and DELETE are refused at the database level, not merely absent from the
Python API.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from recovery.clock import EPOCH, VirtualClock
from recovery.ledger import Ledger
from recovery.models import (
    Action,
    ActionScore,
    Arm,
    AttemptStatus,
    Cause,
    ClassificationSource,
    DecisionStatus,
    LedgerEntry,
)

NOW = datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def ledger(tmp_path) -> Ledger:
    return Ledger(f"sqlite:///{tmp_path / 'test.db'}")


def make_entry(**overrides) -> LedgerEntry:
    defaults = dict(
        event_id="evt_1",
        decision_id="dec_1",
        subscription_id="sub_1",
        customer_id="cust_1",
        cause=Cause.INSUFFICIENT_FUNDS,
        classified_by=ClassificationSource.RULE,
        confidence=0.8,
        error_reason="insufficient_funds",
        action=Action.RETRY_DELAYED,
        status=DecisionStatus.EXECUTED,
        attempt_number=2,
        amount_paise=49_900,
        outcome=AttemptStatus.PENDING,
        recorded_at=NOW,
        virtual_time=EPOCH,
    )
    defaults.update(overrides)
    return LedgerEntry(**defaults)


# -- clock -----------------------------------------------------------------


def test_clock_only_moves_forward() -> None:
    clock = VirtualClock()
    clock.advance(days=2)
    with pytest.raises(ValueError):
        clock.advance(days=-1)
    with pytest.raises(ValueError):
        clock.advance_to(EPOCH)


def test_window_boundary_is_exclusive() -> None:
    """Day 7 exactly is outside the window — `definitions.md` section 1."""
    clock = VirtualClock()
    clock.advance(days=6, hours=23)
    assert clock.within_window(EPOCH, days=7) is True
    clock.advance(hours=1)
    assert clock.within_window(EPOCH, days=7) is False


def test_at_does_not_move_the_clock() -> None:
    clock = VirtualClock()
    scheduled = clock.at(days=3)
    assert scheduled == EPOCH + timedelta(days=3)
    assert clock.now == EPOCH


# -- ledger ----------------------------------------------------------------


def test_append_and_read_back(ledger: Ledger) -> None:
    entry = make_entry(
        scores=(
            ActionScore(action=Action.RETRY_NOW, expected_value_paise=-200.0, p_success=0.04),
            ActionScore(
                action=Action.RETRY_DELAYED,
                expected_value_paise=13_800.0,
                p_success=0.31,
                delay_hours=72,
            ),
        )
    )
    seq = ledger.append(entry)
    assert seq == 1

    rows = ledger.all()
    assert len(rows) == 1
    # The whole EV table survives the round trip, not just the chosen action.
    assert len(rows[0].scores) == 2
    assert rows[0].cause is Cause.INSUFFICIENT_FUNDS


def test_update_is_refused_at_the_database(ledger: Ledger) -> None:
    ledger.append(make_entry())
    with pytest.raises(DatabaseError, match="append-only"):
        with ledger.engine.begin() as conn:
            conn.execute(text("UPDATE ledger SET recovered_paise = 999999"))


def test_delete_is_refused_at_the_database(ledger: Ledger) -> None:
    ledger.append(make_entry())
    with pytest.raises(DatabaseError, match="append-only"):
        with ledger.engine.begin() as conn:
            conn.execute(text("DELETE FROM ledger"))


def test_blocked_and_stopped_rows_are_kept(ledger: Ledger) -> None:
    """The compliance panel is built from rows that did NOT charge anyone."""
    ledger.append_many(
        [
            make_entry(event_id="e1", status=DecisionStatus.EXECUTED),
            make_entry(
                event_id="e2",
                status=DecisionStatus.BLOCKED,
                action=Action.RETRY_NOW,
                stop_reason="rbi_pre_debit_notification_missing",
            ),
            make_entry(
                event_id="e3",
                status=DecisionStatus.STOPPED,
                action=Action.STOP,
                stop_reason="max_attempts_reached",
            ),
        ]
    )
    counts = ledger.count_by("status")
    assert counts == {"executed": 1, "blocked": 1, "stopped": 1}


def test_totals_lead_with_net(ledger: Ledger) -> None:
    ledger.append_many(
        [
            make_entry(event_id="e1", recovered_paise=49_900, cost_paise=200),
            make_entry(event_id="e2", recovered_paise=0, cost_paise=200),
        ]
    )
    totals = ledger.totals()
    assert totals["gross_paise"] == 49_900
    assert totals["cost_paise"] == 400
    assert totals["net_paise"] == 49_500


def test_idempotency_key_blocks_double_charge(ledger: Ledger) -> None:
    """A crash mid-run must not let the same attempt be written twice."""
    ledger.append(make_entry(event_id="e1", idempotency_key="sub_1:cycle0:attempt2"))
    with pytest.raises(Exception):
        ledger.append(make_entry(event_id="e2", idempotency_key="sub_1:cycle0:attempt2"))


def test_subscription_history_is_ordered(ledger: Ledger) -> None:
    for i in range(1, 4):
        ledger.append(make_entry(event_id=f"e{i}", attempt_number=i))
    history = ledger.for_subscription("sub_1")
    assert [h.attempt_number for h in history] == [1, 2, 3]
