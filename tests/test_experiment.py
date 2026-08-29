"""Tests for the measurement itself.

The measurement is the claim. If the harness is wrong, every number in the
write-up is wrong, and it is the one component whose bugs would not show up as
a crash — only as a flattering result. So these test the *fairness* properties
rather than the arithmetic:

* the same decision gets the same coin in every arm
* a different decision gets an independent coin
* baselines pay the same costs and obey the same rules
* the agent learns across cycles and the baselines do not
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from recovery.decision import CostModel, Policy, SuccessModel
from recovery.diagnosis import Classifier
from recovery.experiment.baselines import fixed_3x_policy, naive_policy
from recovery.experiment.harness import (
    CommonRandomNumbers,
    check_plausibility,
    paired_difference,
    run_arm,
    run_experiment,
)
from recovery.models import Action, Arm, Cause
from recovery.simulation.generator import BatchGenerator

NOW = datetime(2026, 1, 10, 10, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def batch():
    return BatchGenerator(1).generate(200)


# -- common random numbers -------------------------------------------------


def test_the_same_decision_gets_the_same_coin() -> None:
    """A difference between arms must be strategy, never luck."""
    crn = CommonRandomNumbers(seed=5)
    args = dict(event_id="evt_1", action=Action.RETRY_DELAYED, delay_hours=72, attempt=2)
    assert crn.uniform(**args) == crn.uniform(**args)


def test_a_different_decision_gets_an_independent_coin() -> None:
    """An arm must not be rewarded merely for doing something different."""
    crn = CommonRandomNumbers(seed=5)
    base = crn.uniform(
        event_id="evt_1", action=Action.RETRY_DELAYED, delay_hours=72, attempt=2
    )
    assert base != crn.uniform(
        event_id="evt_1", action=Action.RETRY_DELAYED, delay_hours=24, attempt=2
    )
    assert base != crn.uniform(
        event_id="evt_2", action=Action.RETRY_DELAYED, delay_hours=72, attempt=2
    )


def test_coins_are_uniform_enough_to_be_coins() -> None:
    crn = CommonRandomNumbers(seed=1)
    draws = [
        crn.uniform(
            event_id=f"evt_{i}", action=Action.RETRY_NOW, delay_hours=0, attempt=2
        )
        for i in range(4_000)
    ]
    assert all(0.0 <= d < 1.0 for d in draws)
    assert 0.47 < sum(draws) / len(draws) < 0.53


# -- fairness between arms -------------------------------------------------


def test_baselines_pay_the_same_costs_as_the_agent(batch) -> None:
    """Free retries for the baselines would make `net` incomparable.

    Caught on the first harness run: the baselines reported net == gross while
    the agent paid every cost, biasing the comparison in the agent's favour.
    """
    costs = CostModel()
    policy = fixed_3x_policy(costs=costs)
    event = Classifier(use_model=False).classify(batch.events[0])

    decision = policy.decide(
        event,
        now=NOW,
        attempts_so_far=1,
        first_failure_at=NOW - timedelta(hours=2),
        pre_debit_notice_sent_at=NOW - timedelta(hours=30),
    )
    if decision.chosen_action in (Action.RETRY_NOW, Action.RETRY_DELAYED):
        score = decision.scores[0]
        assert score.cost_paise > 0, "a baseline retry must not be free"


def test_baselines_obey_the_stopping_rules(batch) -> None:
    """It would flatter the agent to be the only lawful arm."""
    policy = fixed_3x_policy()
    event = Classifier(use_model=False).classify(batch.events[0])
    decision = policy.decide(
        event,
        now=NOW,
        attempts_so_far=99,
        first_failure_at=NOW - timedelta(hours=2),
        pre_debit_notice_sent_at=NOW - timedelta(hours=30),
    )
    assert decision.chosen_action is Action.STOP
    assert decision.stop_reason == "max_attempts_reached"


def test_baselines_never_retry_a_dead_mandate() -> None:
    # Drawn from a wider batch: revoked mandates are 2% of the distribution, so
    # a 200-event sample often contains none at all.
    classifier = Classifier(use_model=False)
    events = [classifier.classify(e) for e in BatchGenerator(1).generate(1_500).events]
    dead = next(
        e
        for e in events
        if e.cause in (Cause.MANDATE_REVOKED, Cause.MANDATE_EXPIRED)
    )
    decision = fixed_3x_policy().decide(
        dead,
        now=NOW,
        attempts_so_far=1,
        first_failure_at=NOW - timedelta(hours=2),
        pre_debit_notice_sent_at=NOW - timedelta(hours=30),
    )
    assert decision.chosen_action is not Action.RETRY_DELAYED


def test_baselines_do_not_learn(batch) -> None:
    """The thing being measured against is a schedule, not a learner."""
    classifier = Classifier(use_model=False)
    policy = fixed_3x_policy()
    before = policy.decide(
        classifier.classify(batch.events[0]),
        now=NOW,
        attempts_so_far=1,
        first_failure_at=NOW - timedelta(hours=2),
        pre_debit_notice_sent_at=NOW - timedelta(hours=30),
    )
    for _ in range(200):
        policy.success.observe(
            cause=Cause.INSUFFICIENT_FUNDS,
            action=Action.RETRY_DELAYED,
            delay_hours=120,
            attempt_number=2,
            outcome=__import__(
                "recovery.models", fromlist=["AttemptStatus"]
            ).AttemptStatus.SUCCEEDED,
        )
    after = policy.decide(
        classifier.classify(batch.events[0]),
        now=NOW,
        attempts_so_far=1,
        first_failure_at=NOW - timedelta(hours=2),
        pre_debit_notice_sent_at=NOW - timedelta(hours=30),
    )
    assert before.chosen_action is after.chosen_action
    assert before.scores[0].delay_hours == after.scores[0].delay_hours


# -- paired differences ----------------------------------------------------


def test_paired_difference_is_zero_for_identical_arms() -> None:
    from recovery.experiment.harness import ArmResult

    a = [ArmResult(arm=Arm.AGENT, recovered_paise=100, cost_paise=10) for _ in range(5)]
    b = [ArmResult(arm=Arm.NAIVE, recovered_paise=100, cost_paise=10) for _ in range(5)]
    comparison = paired_difference(a, b, "identical")
    assert comparison.mean_difference_paise == 0
    assert not comparison.significant


def test_an_interval_spanning_zero_is_not_significant() -> None:
    from recovery.experiment.harness import ArmResult

    a = [
        ArmResult(arm=Arm.AGENT, recovered_paise=v, cost_paise=0)
        for v in (0, 500, -400, 900, -800)
    ]
    b = [ArmResult(arm=Arm.NAIVE, recovered_paise=0, cost_paise=0) for _ in range(5)]
    assert not paired_difference(a, b, "noisy").significant


# -- the whole experiment --------------------------------------------------


def test_experiment_runs_and_reports_every_arm() -> None:
    result = run_experiment(seeds=2, cycles=2, batch_size=120)
    assert set(result.arms) == {
        Arm.NAIVE,
        Arm.FIXED_3X,
        Arm.AGENT_RETRY_ONLY,
        Arm.AGENT,
    }
    assert len(result.comparisons) == 4
    summary = result.summary()
    assert summary[Arm.AGENT.value]["recovery_rate"] > 0


def test_all_arms_see_the_same_events() -> None:
    result = run_experiment(seeds=2, cycles=1, batch_size=120)
    counts = {arm: runs[0].events for arm, runs in result.arms.items()}
    assert len(set(counts.values())) == 1


def test_zero_compliance_violations() -> None:
    """Not a metric to optimise — an invariant. Any non-zero value is a bug."""
    result = run_experiment(seeds=2, cycles=1, batch_size=120)
    assert all(
        s["compliance_violations"] == 0 for s in result.summary().values()
    )


def test_the_plausibility_check_flags_an_implausible_result() -> None:
    """A result that is too good is evidence against itself."""
    from recovery.experiment.harness import ArmResult, Comparison, ExperimentResult

    fake = ExperimentResult(
        arms={a: [ArmResult(arm=a)] for a in Arm},
        comparisons=[
            # The plausibility check judges the retry-only arm: the published
            # band measures retry timing against retry timing.
            Comparison(
                "agent (retry-only) vs fixed-3x", 1e9, 1e9, 1e9, 30, relative_uplift=5.0
            )
        ],
        seeds=30,
        cycles=3,
        batch_size=2000,
        churn_penalty_enabled=True,
    )
    warning = check_plausibility(fake)
    assert warning and "double" in warning


def test_the_churn_penalty_can_be_switched_off_end_to_end() -> None:
    """WORKPLAN §4.3 — the headline must be reportable without the soft number."""
    result = run_experiment(
        seeds=2, cycles=1, batch_size=120, costs=CostModel(churn_penalty_enabled=False)
    )
    assert result.churn_penalty_enabled is False
    assert result.summary()[Arm.AGENT.value]["net_paise"] != 0


# -- sensitivity -----------------------------------------------------------


def test_flattening_the_delay_curve_actually_flattens_it() -> None:
    """The sweep's central manipulation must do what it claims.

    If this were a no-op, the finding that the agent's advantage does not come
    from retry timing would be an artefact of a broken context manager rather
    than a result.
    """
    from recovery.experiment.sensitivity import flattened_delay_curves
    from recovery.simulation import params as P

    before = dict(P.RECOVERY[Cause.INSUFFICIENT_FUNDS].delay_multiplier)
    with flattened_delay_curves(0.0):
        during = dict(P.RECOVERY[Cause.INSUFFICIENT_FUNDS].delay_multiplier)
        assert len(set(round(v, 9) for v in during.values())) == 1
    after = dict(P.RECOVERY[Cause.INSUFFICIENT_FUNDS].delay_multiplier)

    assert len(set(round(v, 9) for v in before.values())) > 1
    assert after == before, "the sweep must restore the world it borrowed"


def test_scaling_base_rates_restores_afterwards() -> None:
    from recovery.experiment.sensitivity import scaled_base_rates
    from recovery.simulation import params as P

    before = P.RECOVERY[Cause.INSUFFICIENT_FUNDS].base
    with scaled_base_rates(0.5):
        assert P.RECOVERY[Cause.INSUFFICIENT_FUNDS].base == pytest.approx(before * 0.5)
    assert P.RECOVERY[Cause.INSUFFICIENT_FUNDS].base == before


def test_retry_only_agent_never_requests_reauth(batch) -> None:
    """The like-for-like arm must actually be restricted, or the comparison
    against the published retry-timing band means nothing."""
    from recovery.decision import Policy, SuccessModel

    classifier = Classifier(use_model=False)
    policy = Policy(success=SuccessModel(), costs=CostModel(), retry_only=True)
    for event in batch.events[:60]:
        decision = policy.decide(
            classifier.classify(event),
            now=NOW,
            attempts_so_far=1,
            first_failure_at=NOW - timedelta(hours=2),
            pre_debit_notice_sent_at=NOW - timedelta(hours=30),
        )
        assert decision.chosen_action is not Action.REQUEST_REAUTH
