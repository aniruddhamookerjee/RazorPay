"""Smoke tests for the scaffold.

The tests that matter come on Day 4 — the compliance gate, the stopping rules
and the EV maths. These only prove the skeleton holds together.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from recovery.config import settings
from recovery.models import (
    Action,
    ActionScore,
    AttemptStatus,
    Cause,
    ChargeAttempt,
    ClassificationSource,
    Decision,
    DecisionStatus,
    FailureEvent,
    LedgerEntry,
)
from recovery.webhook import app

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def make_event() -> FailureEvent:
    attempt = ChargeAttempt(
        subscription_id="sub_test",
        customer_id="cust_test",
        amount_paise=49900,
        status=AttemptStatus.FAILED,
        created_at=NOW,
        virtual_time=NOW,
    )
    return FailureEvent(
        attempt=attempt,
        error_reason="NOT_ENOUGH_BALANCE",
        cause=Cause.INSUFFICIENT_FUNDS,
        classified_by=ClassificationSource.RULE,
        confidence=0.9,
        occurred_at=NOW,
        virtual_time=NOW,
    )


def test_event_carries_cause_and_source() -> None:
    event = make_event()
    assert event.cause is Cause.INSUFFICIENT_FUNDS
    assert event.classified_by is ClassificationSource.RULE
    assert event.attempt.amount_paise == 49900


def test_decision_keeps_the_whole_ev_table() -> None:
    event = make_event()
    scores = (
        ActionScore(action=Action.RETRY_NOW, expected_value_paise=-200.0, p_success=0.04),
        ActionScore(
            action=Action.RETRY_DELAYED,
            expected_value_paise=13800.0,
            p_success=0.31,
            delay_hours=72,
        ),
        ActionScore(action=Action.STOP, expected_value_paise=0.0, p_success=0.0),
    )
    decision = Decision(
        event_id=event.event_id,
        chosen_action=Action.RETRY_DELAYED,
        scores=scores,
        decided_at=NOW,
        virtual_time=NOW,
    )
    assert len(decision.scores) == 3
    assert max(decision.scores, key=lambda s: s.expected_value_paise).action is (
        decision.chosen_action
    )


def test_ledger_entry_nets_off_costs() -> None:
    entry = LedgerEntry(
        event_id="evt_1",
        decision_id="dec_1",
        subscription_id="sub_test",
        customer_id="cust_test",
        cause=Cause.INSUFFICIENT_FUNDS,
        classified_by=ClassificationSource.RULE,
        confidence=0.9,
        action=Action.RETRY_DELAYED,
        status=DecisionStatus.EXECUTED,
        attempt_number=2,
        amount_paise=49900,
        recovered_paise=49900,
        cost_paise=200,
        outcome=AttemptStatus.SUCCEEDED,
        recorded_at=NOW,
        virtual_time=NOW,
    )
    assert entry.net_paise == 49700


def test_health_endpoint() -> None:
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"


def test_webhook_archives_payload(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "fixtures_dir", tmp_path)
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "")

    payload = {"event": "payment.failed", "payload": {}}
    with TestClient(app) as client:
        response = client.post("/webhook", content=json.dumps(payload))

    assert response.status_code == 200
    assert response.json()["event"] == "payment.failed"
    assert (tmp_path / response.json()["fixture"]).exists()


def test_webhook_rejects_bad_signature(monkeypatch) -> None:
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "shhh")
    with TestClient(app) as client:
        response = client.post("/webhook", json={"event": "payment.failed"})
    assert response.status_code == 400
