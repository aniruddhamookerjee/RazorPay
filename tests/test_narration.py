"""Tests for the LLM surfaces.

These are the only two places a model touches the system, and neither is on the
money path. What matters is that the model cannot make the system say something
untrue, so most of these test the guards rather than the generation.

Each guard exists because of something the model actually did during the Day 7
quality check, noted inline.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from recovery.decision import CostModel, Policy, SuccessModel
from recovery.diagnosis import Classifier
from recovery.execution import RecoveryRunner, SimulatedExecutor
from recovery.ledger import Ledger
from recovery.models import Cause
from recovery.narration import (
    Channel,
    Language,
    MessageWriter,
    Narrator,
    facts,
    looks_like_hinglish,
    render,
)
from recovery.narration.messages import _has_invented_numbers
from recovery.simulation.generator import BatchGenerator


@pytest.fixture(scope="module")
def entries():
    batch = BatchGenerator(42).generate(60)
    db = Path(tempfile.mkdtemp()) / "n.db"
    runner = RecoveryRunner(
        classifier=Classifier(use_model=False),
        policy=Policy(success=SuccessModel(), costs=CostModel()),
        executor=SimulatedExecutor(oracle=lambda *a: False),
        ledger=Ledger(f"sqlite:///{db}"),
    )
    return runner.run_event(batch.events[0])


# -- message guards --------------------------------------------------------


def test_invented_numbers_are_rejected() -> None:
    """The model produced a message citing a date that appears nowhere in the
    facts it was given."""
    assert _has_invented_numbers("balance on the 15th")
    assert _has_invented_numbers("retry in 3 days")
    assert not _has_invented_numbers("Rs {amount} ka payment fail hua")
    assert not _has_invented_numbers("Hi {name}, please update your card")


def test_hinglish_detector_rejects_plain_english() -> None:
    """Asked plainly for Hinglish, the model returned fluent English every time.

    Shipping that as Hinglish would be worse than shipping English and saying so.
    """
    assert not looks_like_hinglish(
        "Hi Aarav, your payment could not be completed. Please top up."
    )
    assert looks_like_hinglish(
        "Aarav ji, aapka payment nahi ho paya kyunki balance thoda kam tha."
    )


def test_a_raw_cause_code_never_reaches_a_customer() -> None:
    """One generated message read 'bank_unavailable se fail hua'."""
    writer = MessageWriter(use_model=True)
    with patch("httpx.post") as post:
        post.return_value.raise_for_status = lambda: None
        post.return_value.json.return_value = {
            "message": {"content": "Payment failed: bank_unavailable se"}
        }
        text = writer.template(Cause.BANK_UNAVAILABLE, Language.ENGLISH)
    assert "bank_unavailable" not in text
    assert writer.fallbacks_used == 1


def test_a_non_hinglish_reply_is_rejected() -> None:
    writer = MessageWriter(use_model=True)
    with patch("httpx.post") as post:
        post.return_value.raise_for_status = lambda: None
        post.return_value.json.return_value = {
            "message": {"content": "Hello, your payment failed. Please update it."}
        }
        text = writer.template(Cause.CARD_EXPIRED, Language.HINGLISH)
    assert writer.hinglish_rejections == 1
    assert looks_like_hinglish(text), "the fallback must itself be Hinglish"


def test_messages_fall_back_when_the_model_is_down() -> None:
    writer = MessageWriter(use_model=True)
    with patch("httpx.post", side_effect=OSError("ollama down")):
        text = writer.write(cause=Cause.CARD_EXPIRED, name="Priya", amount_paise=129900)
    assert "Priya" in text and "1,299" in text
    assert writer.fallbacks_used == 1


def test_placeholders_are_filled_locally_not_by_the_model() -> None:
    """Amount and name must never depend on the model getting them right."""
    writer = MessageWriter(use_model=False)
    a = writer.write(cause=Cause.CARD_EXPIRED, name="Priya", amount_paise=129900)
    b = writer.write(cause=Cause.CARD_EXPIRED, name="Aarav", amount_paise=49900)
    assert "1,299" in a and "Priya" in a
    assert "499" in b and "Aarav" in b


def test_templates_are_cached_per_cause_and_language() -> None:
    """Roughly 15s per call; per-customer generation across 10k events cannot work."""
    writer = MessageWriter(use_model=True)
    with patch.object(MessageWriter, "_ask_model", return_value="Hi {name}") as mock:
        for _ in range(20):
            writer.write(cause=Cause.CARD_EXPIRED, name="X", amount_paise=100)
    assert mock.call_count == 1


def test_every_cause_has_a_description_and_an_ask() -> None:
    """A missing entry would raise inside message generation at demo time."""
    from recovery.narration.messages import ACTION_ASKS, CAUSE_DESCRIPTIONS

    for cause in Cause:
        assert cause in CAUSE_DESCRIPTIONS
        assert cause in ACTION_ASKS


# -- narration -------------------------------------------------------------


def test_narration_states_the_stop_reason(entries) -> None:
    """Why we stopped is the part that proves the agent is bounded."""
    text = render(entries)
    assert "Rs" in text
    assert any(
        phrase in text for phrase in ("stopped because", "recovered", "took no action")
    )


def test_facts_come_only_from_the_ledger(entries) -> None:
    f = facts(entries)
    assert f["subscription_id"] == entries[0].subscription_id
    assert f["cause"] == entries[0].cause.value
    assert f["amount_paise"] == entries[0].amount_paise


def test_narration_rejects_invented_numbers(entries) -> None:
    """A fluent sentence with a wrong figure is worse than a clumsy correct one."""
    narrator = Narrator(use_model=True)
    with patch("httpx.post") as post:
        post.return_value.raise_for_status = lambda: None
        post.return_value.json.return_value = {
            "message": {"content": "We retried 47 times and recovered Rs 999999."}
        }
        text = narrator.narrate(entries)
    assert "47" not in text
    assert narrator.rejections == 1
    assert text == render(entries)


def test_narration_accepts_a_faithful_rewrite(entries) -> None:
    faithful = "The charge did not go through and we stopped."
    narrator = Narrator(use_model=True)
    with patch("httpx.post") as post:
        post.return_value.raise_for_status = lambda: None
        post.return_value.json.return_value = {"message": {"content": faithful}}
        assert narrator.narrate(entries) == faithful
    assert narrator.rejections == 0


def test_narration_works_without_a_model(entries) -> None:
    assert Narrator(use_model=False).narrate(entries) == render(entries)


def test_empty_history_does_not_crash() -> None:
    assert render([]) == "No actions recorded."
    assert Narrator(use_model=False).narrate([]) == "No actions recorded."
