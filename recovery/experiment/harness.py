"""The measurement. Three arms, identical events, honest error bars.

**Paired replay with common random numbers.** Every arm sees the same events,
and the coin that decides whether a given action succeeds is derived from a hash
of `(seed, event, action, delay, attempt)` rather than drawn from a stream. Two
consequences, both essential:

* the same decision always gets the same outcome in every arm, so a difference
  between arms is a difference in *strategy*, never in luck;
* a *different* decision gets an independent draw, so an arm is not rewarded for
  simply doing something else.

Production A/B would split the population three ways instead. In a simulator,
pairing is strictly better: it removes between-arm variance and gives three
times the effective sample for the same compute. We therefore report the
**paired difference** — agent minus baseline, per event — not two independent
means, and the confidence interval is on that difference.

**Multi-cycle.** The agent carries its success model across cycles; the
baselines do not learn by construction. A single-cycle run would show a
cold-start agent with flat priors and hide the entire learning claim, which is
WORKPLAN.md §4.2.

**Sanity ceiling.** Published smart-retry uplift over fixed schedules is 15-40%.
A result far outside that band is evidence of a bug — a parameter leak, or a
strawman baseline — before it is evidence of success. The harness says so.
"""

from __future__ import annotations

import hashlib
import statistics
from dataclasses import dataclass, field
from datetime import datetime

from recovery.decision import CostModel, Policy, SuccessModel
from recovery.diagnosis import Classifier
from recovery.execution import RecoveryRunner, SimulatedExecutor
from recovery.experiment.baselines import fixed_3x_policy, naive_policy
from recovery.ledger import Ledger
from recovery.models import Action, Arm, Cause, FailureEvent
from recovery.simulation import params as P
from recovery.simulation.generator import BatchGenerator, resolve

_COLLECTING = {
    Action.RETRY_NOW,
    Action.RETRY_DELAYED,
    Action.REQUEST_REAUTH,
    Action.NOTIFY,
}


class CommonRandomNumbers:
    """Deterministic coin flips shared across arms.

    A stream would give arm two a different draw purely because arm one had
    consumed one first. Hashing the decision instead means the coin belongs to
    the decision, not to the order in which arms happened to run.
    """

    def __init__(self, seed: int) -> None:
        self.seed = seed

    def uniform(
        self, *, event_id: str, action: Action, delay_hours: int, attempt: int
    ) -> float:
        raw = f"{self.seed}|{event_id}|{action.value}|{delay_hours}|{attempt}"
        digest = hashlib.blake2b(raw.encode(), digest_size=8).digest()
        return int.from_bytes(digest, "big") / float(1 << 64)


def make_oracle(truth: dict[str, Cause], crn: CommonRandomNumbers):
    """Resolve an action against ground truth using the shared coin."""

    class _FixedDraw:
        """Adapter: `resolve` wants an rng, we have one predetermined number."""

        def __init__(self, value: float) -> None:
            self._value = value

        def random(self) -> float:
            return self._value

    def oracle(
        event: FailureEvent, action: Action, attempted_at: datetime, attempt: int
    ) -> bool:
        if action not in _COLLECTING:
            return False
        delay_hours = int(
            (attempted_at - event.occurred_at).total_seconds() // 3600
        )
        draw = crn.uniform(
            event_id=event.event_id,
            action=action,
            delay_hours=delay_hours,
            attempt=attempt,
        )
        return resolve(
            true_cause=truth[event.event_id],
            attempted_at=attempted_at,
            failed_at=event.occurred_at,
            attempt_number=attempt,
            rng=_FixedDraw(draw),
        )

    return oracle


@dataclass
class ArmResult:
    arm: Arm
    recovered_paise: int = 0
    cost_paise: int = 0
    recovered_count: int = 0
    events: int = 0
    attempts: int = 0
    wasted_attempts: int = 0
    compliance_violations: int = 0
    # Per-event net, so paired differences can be computed against another arm.
    per_event_net: dict[str, int] = field(default_factory=dict)

    @property
    def net_paise(self) -> int:
        return self.recovered_paise - self.cost_paise

    @property
    def recovery_rate(self) -> float:
        return self.recovered_count / self.events if self.events else 0.0

    @property
    def attempts_per_recovery(self) -> float:
        return self.attempts / self.recovered_count if self.recovered_count else 0.0


@dataclass
class Comparison:
    """A paired difference with a confidence interval on the difference."""

    label: str
    mean_difference_paise: float
    ci_low: float
    ci_high: float
    n_seeds: int
    relative_uplift: float

    @property
    def significant(self) -> bool:
        return self.ci_low > 0 or self.ci_high < 0

    def describe(self) -> str:
        marker = "" if self.significant else "  (interval spans zero)"
        return (
            f"{self.label}: {self.mean_difference_paise / 100:+,.0f} Rs per run "
            f"[{self.ci_low / 100:+,.0f}, {self.ci_high / 100:+,.0f}] "
            f"= {self.relative_uplift:+.1%}{marker}"
        )


def run_arm(
    *,
    events: tuple[FailureEvent, ...],
    truth: dict[str, Cause],
    policy,
    arm: Arm,
    seed: int,
    cycles: int,
    costs: CostModel,
    classifier: Classifier,
) -> ArmResult:
    """Replay one arm over the batch for `cycles` billing cycles."""
    result = ArmResult(arm=arm)
    ledger = Ledger("sqlite:///:memory:")

    for cycle in range(cycles):
        crn = CommonRandomNumbers(seed=seed * 1000 + cycle)
        runner = RecoveryRunner(
            classifier=classifier,
            policy=policy,
            executor=SimulatedExecutor(oracle=make_oracle(truth, crn)),
            ledger=ledger,
            arm=arm,
            run_id=f"{arm.value}-s{seed}-c{cycle}",
            seed=seed,
        )
        for event in events:
            entries = runner.run_event(event, cycle=cycle)
            recovered = sum(e.recovered_paise for e in entries)
            cost = sum(e.cost_paise for e in entries)
            debits = sum(
                1
                for e in entries
                if e.action in (Action.RETRY_NOW, Action.RETRY_DELAYED)
            )

            result.events += 1
            result.recovered_paise += recovered
            result.cost_paise += cost
            result.attempts += debits
            if recovered:
                result.recovered_count += 1
            else:
                # `definitions.md` §4: an attempt is wasted only if the case was
                # never recovered. Knowable only in hindsight, which is correct.
                result.wasted_attempts += debits

            key = f"{event.event_id}:c{cycle}"
            result.per_event_net[key] = recovered - cost

    return result


def paired_difference(
    treatment: list[ArmResult], control: list[ArmResult], label: str
) -> Comparison:
    """Per-seed net difference, with a t-interval on the mean difference."""
    diffs = [t.net_paise - c.net_paise for t, c in zip(treatment, control)]
    n = len(diffs)
    mean = statistics.fmean(diffs)

    if n < 2:
        return Comparison(label, mean, mean, mean, n, 0.0)

    stderr = statistics.stdev(diffs) / (n**0.5)
    # 1.96 is close enough for n >= 30, which is the documented run size.
    margin = 1.96 * stderr
    control_net = statistics.fmean([c.net_paise for c in control]) or 1
    return Comparison(
        label=label,
        mean_difference_paise=mean,
        ci_low=mean - margin,
        ci_high=mean + margin,
        n_seeds=n,
        relative_uplift=mean / abs(control_net),
    )


@dataclass
class ExperimentResult:
    arms: dict[Arm, list[ArmResult]]
    comparisons: list[Comparison]
    seeds: int
    cycles: int
    batch_size: int
    churn_penalty_enabled: bool

    def summary(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for arm, runs in self.arms.items():
            out[arm.value] = {
                "net_paise": statistics.fmean([r.net_paise for r in runs]),
                "gross_paise": statistics.fmean([r.recovered_paise for r in runs]),
                "cost_paise": statistics.fmean([r.cost_paise for r in runs]),
                "recovery_rate": statistics.fmean([r.recovery_rate for r in runs]),
                "attempts": statistics.fmean([r.attempts for r in runs]),
                "wasted_attempts": statistics.fmean([r.wasted_attempts for r in runs]),
                "compliance_violations": sum(r.compliance_violations for r in runs),
            }
        return out


def run_experiment(
    *,
    seeds: int = 30,
    cycles: int = 3,
    batch_size: int = 2_000,
    costs: CostModel | None = None,
) -> ExperimentResult:
    """The headline measurement."""
    costs = costs or CostModel()
    classifier = Classifier(use_model=False)
    arms: dict[Arm, list[ArmResult]] = {
        Arm.NAIVE: [],
        Arm.FIXED_3X: [],
        Arm.AGENT_RETRY_ONLY: [],
        Arm.AGENT: [],
    }

    for seed in range(seeds):
        batch = BatchGenerator(seed).generate(batch_size)
        events, truth = batch.events, batch.truth

        policies = {
            Arm.NAIVE: naive_policy(costs=costs),
            Arm.FIXED_3X: fixed_3x_policy(costs=costs),
            # A fresh model per seed: the agent learns within a run, not across
            # independent replications, which would be leaking across seeds.
            Arm.AGENT_RETRY_ONLY: Policy(
                success=SuccessModel(), costs=costs, retry_only=True
            ),
            Arm.AGENT: Policy(success=SuccessModel(), costs=costs),
        }

        for arm, policy in policies.items():
            arms[arm].append(
                run_arm(
                    events=events,
                    truth=truth,
                    policy=policy,
                    arm=arm,
                    seed=seed,
                    cycles=cycles,
                    costs=costs,
                    classifier=classifier,
                )
            )

    comparisons = [
        paired_difference(arms[Arm.AGENT], arms[Arm.NAIVE], "agent vs naive"),
        paired_difference(arms[Arm.AGENT], arms[Arm.FIXED_3X], "agent vs fixed-3x"),
        # The like-for-like number: retry timing against retry timing, which is
        # what the published 15-40% band actually measures.
        paired_difference(
            arms[Arm.AGENT_RETRY_ONLY],
            arms[Arm.FIXED_3X],
            "agent (retry-only) vs fixed-3x",
        ),
        paired_difference(arms[Arm.FIXED_3X], arms[Arm.NAIVE], "fixed-3x vs naive"),
    ]

    return ExperimentResult(
        arms=arms,
        comparisons=comparisons,
        seeds=seeds,
        cycles=cycles,
        batch_size=batch_size,
        churn_penalty_enabled=costs.churn_penalty_enabled,
    )


def check_plausibility(result: ExperimentResult) -> str | None:
    """Is the headline inside the published band, or suspiciously outside it?"""
    # Judged on the retry-only arm: the published band measures retry timing
    # against retry timing, so comparing the full multi-channel agent to it
    # would flag a difference in scope as if it were a difference in quality.
    agent_vs_fixed = next(
        c
        for c in result.comparisons
        if c.label == "agent (retry-only) vs fixed-3x"
    )
    low, high = P.PLAUSIBLE_UPLIFT_BAND
    uplift = agent_vs_fixed.relative_uplift

    if uplift > high * 2:
        return (
            f"uplift {uplift:.1%} is more than double the published "
            f"{low:.0%}-{high:.0%} band — suspect a parameter leak or a strawman "
            "baseline before believing it"
        )
    if uplift > high:
        return (
            f"uplift {uplift:.1%} is above the published {low:.0%}-{high:.0%} "
            "band. Not implausible, but say so rather than quoting it as though "
            "it were typical"
        )
    if uplift < 0:
        return f"agent is losing to the fixed baseline ({uplift:.1%})"
    if uplift < low:
        return (
            f"uplift {uplift:.1%} is below the published {low:.0%}-{high:.0%} band"
        )
    return None
