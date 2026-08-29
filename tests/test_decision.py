"""Tests for the decision layer — the part that decides where money goes.

The heaviest-tested layer in the project, because it is the only one whose bugs
cost real money. Three groups carry more weight than ordinary correctness:

* **caps** — `test_caps_cannot_be_exceeded_by_any_input` fuzzes the whole
  parameter space. A stopping rule that holds for the cases we thought of is
  not a stopping rule.
* **the gate before the score** — a forbidden action must never be selectable,
  no matter how valuable. That is what separates a constraint from a preference.
* **the anti-leak property** — `test_priors_are_flat_over_delay` proves the
  agent was not handed the answer. Its whole measured advantage on retry timing
  depends on having learned it.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from recovery.clock import EPOCH
from recovery.config import settings
from recovery.decision import ComplianceContext, CostModel, Policy, SuccessModel, check
from recovery.decision.compliance import AFA_THRESHOLD_PAISE, should_stop
from recovery.decision.costs import zero_cost_model
from recovery.decision.priors import CANDIDATE_DELAYS_HOURS, prior_rate
from recovery.diagnosis import Classifier
from recovery.models import (
    Action,
    AttemptStatus,
    Cause,
    DecisionStatus,
)
from recovery.simulation.generator import BatchGenerator, resolve

NOW = datetime(2026, 1, 10, 10, 0, tzinfo=timezone.utc)
NOTICE = NOW - timedelta(hours=30)


@pytest.fixture(scope="module")
def events():
    batch = BatchGenerator(42).generate(3_000)
    return Classifier(use_model=False).classify_all(batch.events)


@pytest.fixture(scope="module")
def by_cause(events):
    out: dict[Cause, object] = {}
    for event in events:
        out.setdefault(event.cause, event)
    return out


def fresh_policy(costs: CostModel | None = None) -> Policy:
    return Policy(success=SuccessModel(), costs=costs or CostModel())


def decide(policy, event, **kwargs):
    params = dict(
        now=NOW,
        attempts_so_far=1,
        first_failure_at=NOW - timedelta(hours=2),
        pre_debit_notice_sent_at=NOTICE,
    )
    params.update(kwargs)
    return policy.decide(event, **params)


# -- the anti-leak property ------------------------------------------------


def test_priors_are_flat_over_delay() -> None:
    """The agent must not start out knowing which delay works.

    Its entire measured advantage on retry timing rests on having learned this
    from outcomes. If the priors encoded a delay preference, the result would be
    the answer key read back to us.
    """
    for cause in Cause:
        rates = {
            prior_rate(cause, Action.RETRY_NOW),
            prior_rate(cause, Action.RETRY_DELAYED),
        }
        assert len(rates) == 1, f"{cause} starts with a delay preference"


def test_cold_model_cannot_distinguish_delays(by_cause) -> None:
    model = SuccessModel()
    beliefs = {
        model.belief(
            cause=Cause.INSUFFICIENT_FUNDS, action=Action.RETRY_DELAYED, delay_hours=d
        ).mean
        for d in CANDIDATE_DELAYS_HOURS
    }
    assert len(beliefs) == 1
    assert model.is_cold


# -- learning --------------------------------------------------------------


def test_the_agent_learns_the_retry_window() -> None:
    """Cold it retries immediately; with evidence it waits for payday.

    This is WORKPLAN.md §4.2 — the cold-start hole — demonstrated rather than
    asserted. The simulator's true peak for insufficient funds is 120h.
    """
    model = SuccessModel()
    rng = np.random.default_rng(0)

    assert model.learned_best_delay(Cause.INSUFFICIENT_FUNDS) is None

    for _ in range(300):
        for delay in CANDIDATE_DELAYS_HOURS:
            succeeded = resolve(
                true_cause=Cause.INSUFFICIENT_FUNDS,
                attempted_at=EPOCH + timedelta(hours=delay),
                failed_at=EPOCH,
                attempt_number=2,
                rng=rng,
            )
            model.observe(
                cause=Cause.INSUFFICIENT_FUNDS,
                action=Action.RETRY_NOW if delay == 0 else Action.RETRY_DELAYED,
                delay_hours=delay,
                attempt_number=2,
                outcome=AttemptStatus.SUCCEEDED if succeeded else AttemptStatus.FAILED,
            )

    best_delay, belief = model.learned_best_delay(Cause.INSUFFICIENT_FUNDS)
    assert best_delay >= 72, f"expected a multi-day window, learned {best_delay}h"
    assert not belief.is_cold


def test_pending_outcomes_teach_nothing() -> None:
    model = SuccessModel()
    model.observe(
        cause=Cause.INSUFFICIENT_FUNDS,
        action=Action.RETRY_NOW,
        delay_hours=0,
        attempt_number=2,
        outcome=AttemptStatus.PENDING,
    )
    assert model.is_cold


# -- EV arithmetic ---------------------------------------------------------


def test_ev_is_probability_times_amount_minus_costs() -> None:
    costs = CostModel(attempt_cost_paise=200, churn_penalty_enabled=False, ltv_months=0)
    model = SuccessModel()
    belief = model.belief(
        cause=Cause.INSUFFICIENT_FUNDS, action=Action.RETRY_NOW, attempt_number=2
    )
    breakdown = costs.cost_of(Action.RETRY_NOW, attempts_so_far=1, amount_paise=49_900)
    assert breakdown.churn_penalty_paise == 0.0
    assert breakdown.direct_paise == 200
    expected = belief.mean * 49_900 - 200
    assert expected == pytest.approx(belief.mean * 49_900 - breakdown.total_paise)


def test_ltv_scales_with_the_charge() -> None:
    """A flat LTV made the churn penalty exceed the gain on small charges,
    and the agent refused to retry anything cheap. Regression guard."""
    costs = CostModel()
    small = costs.cost_of(Action.RETRY_DELAYED, attempts_so_far=1, amount_paise=19_900)
    large = costs.cost_of(Action.RETRY_DELAYED, attempts_so_far=1, amount_paise=1_999_900)
    assert small.churn_penalty_paise < large.churn_penalty_paise
    # And the small charge must still be worth retrying at a plausible rate.
    assert 0.5 * 19_900 - small.total_paise > 0


def test_stop_scores_exactly_zero(by_cause) -> None:
    """`stop` is the bar every action must clear, so it must be free."""
    policy = fresh_policy()
    decision = decide(policy, by_cause[Cause.INSUFFICIENT_FUNDS])
    stop = next(s for s in decision.scores if s.action is Action.STOP)
    assert stop.expected_value_paise == 0.0


def test_the_whole_table_is_retained(by_cause) -> None:
    """A decision you cannot interrogate is not auditable."""
    decision = decide(fresh_policy(), by_cause[Cause.INSUFFICIENT_FUNDS])
    assert len(decision.scores) >= 5
    assert any(s.action is Action.STOP for s in decision.scores)
    assert all(s.rationale for s in decision.scores)


# -- the answers that must fall out, not be hardcoded ----------------------


def test_dead_mandates_never_yield_a_retry(by_cause) -> None:
    """No rule says 'mandate expired means re-auth'. It has to fall out."""
    policy = fresh_policy()
    for cause in (Cause.MANDATE_EXPIRED, Cause.MANDATE_REVOKED):
        decision = decide(policy, by_cause[cause])
        assert decision.chosen_action not in (Action.RETRY_NOW, Action.RETRY_DELAYED)


def test_hard_declines_stop(by_cause) -> None:
    decision = decide(fresh_policy(), by_cause[Cause.HARD_DECLINE])
    assert decision.chosen_action is Action.STOP
    assert decision.status is DecisionStatus.STOPPED
    assert decision.stop_reason == "hard_decline"


def test_expired_cards_prefer_reauth_over_retry(by_cause) -> None:
    decision = decide(fresh_policy(), by_cause[Cause.CARD_EXPIRED])
    assert decision.chosen_action is Action.REQUEST_REAUTH


def test_no_positive_value_is_distinct_from_a_cap(by_cause) -> None:
    """'Nothing was worth doing' and 'we hit the limit' are different facts."""
    decision = decide(fresh_policy(), by_cause[Cause.MANDATE_EXPIRED])
    if decision.chosen_action is Action.STOP:
        assert decision.stop_reason in {
            "no_action_with_positive_value",
            "hard_decline",
        }


# -- compliance ------------------------------------------------------------


def base_ctx(**kwargs) -> ComplianceContext:
    params = dict(
        amount_paise=49_900,
        attempts_so_far=1,
        cause=Cause.INSUFFICIENT_FUNDS,
        first_failure_at=NOW - timedelta(hours=2),
        scheduled_for=NOW,
        pre_debit_notice_sent_at=NOTICE,
    )
    params.update(kwargs)
    return ComplianceContext(**params)


def test_debit_above_the_afa_threshold_is_blocked() -> None:
    verdict = check(Action.RETRY_NOW, base_ctx(amount_paise=AFA_THRESHOLD_PAISE + 1))
    assert not verdict.allowed
    assert verdict.reason == "afa_required_above_threshold"


def test_debit_without_pre_debit_notice_is_blocked() -> None:
    assert check(
        Action.RETRY_NOW, base_ctx(pre_debit_notice_sent_at=None)
    ).reason == "pre_debit_notice_not_sent"
    assert check(
        Action.RETRY_NOW, base_ctx(pre_debit_notice_sent_at=NOW - timedelta(hours=2))
    ).reason == "pre_debit_notice_too_recent"


def test_messaging_needs_consent_and_respects_quiet_hours() -> None:
    assert check(Action.NOTIFY, base_ctx(messaging_consent=False)).reason == (
        "no_messaging_consent"
    )
    late = NOW.replace(hour=23)
    assert check(Action.NOTIFY, base_ctx(scheduled_for=late)).reason == "quiet_hours"


def test_opt_out_blocks_everything_except_stopping() -> None:
    ctx = base_ctx(customer_opted_out=True)
    for action in (Action.RETRY_NOW, Action.NOTIFY, Action.REQUEST_REAUTH):
        assert not check(action, ctx).allowed
    assert check(Action.STOP, ctx).allowed
    assert check(Action.WAIT, ctx).allowed


def test_blocked_actions_are_scored_and_logged_not_dropped(by_cause) -> None:
    """The compliance panel is built from actions the agent refused to take."""
    decision = decide(
        fresh_policy(), by_cause[Cause.INSUFFICIENT_FUNDS], messaging_consent=False
    )
    blocked = [s for s in decision.scores if s.blocked_by]
    assert blocked, "expected messaging actions to be blocked"
    assert all(s.rationale and s.blocked_by for s in blocked)


def test_a_blocked_action_can_never_be_chosen(by_cause) -> None:
    """Constraint, not preference: no EV is large enough to buy past the gate."""
    policy = fresh_policy()
    for event in list(by_cause.values()):
        decision = policy.decide(
            event,
            now=NOW,
            attempts_so_far=1,
            first_failure_at=NOW - timedelta(hours=2),
            pre_debit_notice_sent_at=None,  # every debit is illegal
            messaging_consent=False,  # every message is illegal
        )
        chosen = next(
            (s for s in decision.scores if s.action is decision.chosen_action), None
        )
        if chosen is not None:
            assert chosen.blocked_by is None


# -- stopping rules --------------------------------------------------------


def test_caps_cannot_be_exceeded_by_any_input(by_cause) -> None:
    """Fuzzed. A rule that holds only for cases we imagined is not a rule."""
    rng = random.Random(7)
    policy = fresh_policy()
    events = list(by_cause.values())

    for _ in range(600):
        event = rng.choice(events)
        attempts = rng.randint(0, 8)
        age_hours = rng.randint(0, 24 * 14)
        decision = policy.decide(
            event,
            now=NOW,
            attempts_so_far=attempts,
            first_failure_at=NOW - timedelta(hours=age_hours),
            pre_debit_notice_sent_at=NOW - timedelta(hours=rng.randint(0, 72)),
            customer_opted_out=rng.random() < 0.2,
            messaging_consent=rng.random() < 0.8,
        )

        if attempts >= settings.max_attempts:
            assert decision.chosen_action is Action.STOP
        if age_hours >= 24 * settings.recovery_window_days:
            assert decision.chosen_action is Action.STOP
        if decision.status is DecisionStatus.STOPPED:
            assert decision.stop_reason


def test_each_stopping_rule_names_itself() -> None:
    assert should_stop(base_ctx(attempts_so_far=99)).reason == "max_attempts_reached"
    assert should_stop(
        base_ctx(first_failure_at=NOW - timedelta(days=30))
    ).reason == "recovery_window_expired"
    assert should_stop(base_ctx(customer_opted_out=True)).reason == "customer_opted_out"
    assert should_stop(base_ctx(cause=Cause.HARD_DECLINE)).reason == "hard_decline"
    assert not should_stop(base_ctx()).should_stop


def test_retries_are_never_scheduled_past_the_window(by_cause) -> None:
    decision = decide(
        fresh_policy(),
        by_cause[Cause.INSUFFICIENT_FUNDS],
        first_failure_at=NOW - timedelta(days=6, hours=12),
    )
    if decision.scheduled_for:
        window = timedelta(days=settings.recovery_window_days)
        assert decision.scheduled_for - (NOW - timedelta(days=6, hours=12)) < window


# -- the sensitivity escape hatch ------------------------------------------


def test_decisions_stay_sane_with_the_churn_penalty_off(by_cause) -> None:
    """Day 7 reports results with this disabled; it must still work."""
    policy = fresh_policy(CostModel(churn_penalty_enabled=False))
    for cause, event in by_cause.items():
        decision = decide(policy, event)
        assert decision.chosen_action in set(Action)
        if cause in (Cause.MANDATE_EXPIRED, Cause.MANDATE_REVOKED):
            assert decision.chosen_action not in (Action.RETRY_NOW, Action.RETRY_DELAYED)


def test_zero_cost_model_makes_action_cheap_but_not_illegal(by_cause) -> None:
    policy = fresh_policy(zero_cost_model())
    decision = decide(policy, by_cause[Cause.INSUFFICIENT_FUNDS])
    assert decision.chosen_action is not Action.STOP
    # Dead mandates stay refused regardless of how cheap acting becomes.
    dead = decide(policy, by_cause[Cause.MANDATE_REVOKED])
    assert dead.chosen_action not in (Action.RETRY_NOW, Action.RETRY_DELAYED)
