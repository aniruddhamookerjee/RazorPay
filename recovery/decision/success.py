"""p(success) — a Beta-Binomial belief that learns from what actually happened.

**Why Beta-Binomial and not logistic regression.** Cells are sparse: a segment
may carry three observations in cycle one. A regression on that produces a
confident nonsense estimate; a Beta posterior with a weak prior produces a
sensible one with honest error bars. It is also explainable to a judge in a
sentence — "we start from published rates and update with what we see" — which
matters for a layer that decides where money goes.

**What a cell is.** `(cause, action, delay_bucket, attempt_number)`. Delay is
bucketed rather than continuous because the agent must *learn* which delays work
and continuous delay would give it a curve it never earned.

**Cold start** (WORKPLAN.md §4.2). On cycle 1 every cell is empty and the model
returns the prior. That is correct behaviour, not a bug — but it means a
single-batch demo shows an agent that is cause-aware and cost-aware yet has
learned nothing. `is_cold` and `evidence_count` make that visible rather than
letting it be quietly assumed away, and the Day 6 harness runs multiple cycles
so the learning is demonstrated instead of claimed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from recovery.decision.priors import (
    CANDIDATE_DELAYS_HOURS,
    PRIOR_STRENGTH,
    prior_rate,
)
from recovery.models import Action, AttemptStatus, Cause

Cell = tuple[Cause, Action, int, int]


def delay_bucket(delay_hours: int) -> int:
    """Snap a delay onto the nearest candidate the agent reasons about."""
    return min(CANDIDATE_DELAYS_HOURS, key=lambda h: abs(h - delay_hours))


@dataclass(frozen=True)
class Belief:
    """A posterior over one cell, with the evidence behind it."""

    mean: float
    low: float
    high: float
    successes: int
    trials: int
    is_cold: bool

    @property
    def evidence_count(self) -> int:
        return self.trials

    def describe(self) -> str:
        if self.is_cold:
            return f"{self.mean:.1%} (prior only, no observations)"
        return (
            f"{self.mean:.1%} [{self.low:.1%}-{self.high:.1%}] "
            f"from {self.successes}/{self.trials}"
        )


@dataclass
class SuccessModel:
    """Beta-Binomial posterior per cell, updated only from observed outcomes.

    Never reads the simulator. The only way a number enters this model is
    `observe()`, called with an outcome that actually happened.
    """

    prior_strength: float = PRIOR_STRENGTH
    # Plain dicts, not defaultdicts: a defaultdict inserts on *read*, so merely
    # asking for a belief grew the model. Harmless for correctness, but the
    # pooling loop below walks every cell, and Day 6 replays ~2.7M decisions.
    _successes: dict[Cell, int] = field(default_factory=dict, repr=False)
    _trials: dict[Cell, int] = field(default_factory=dict, repr=False)
    # Cells grouped by (cause, action, bucket) so pooling does not scan
    # everything that has ever been observed.
    _by_group: dict[tuple[Cause, Action, int], list[Cell]] = field(
        default_factory=dict, repr=False
    )

    # -- learning ----------------------------------------------------------

    def observe(
        self,
        *,
        cause: Cause,
        action: Action,
        delay_hours: int,
        attempt_number: int,
        outcome: AttemptStatus,
    ) -> None:
        """Record one real outcome. The only way evidence enters the model."""
        if outcome is AttemptStatus.PENDING:
            return  # not resolved yet; nothing to learn
        bucket = delay_bucket(delay_hours)
        cell = (cause, action, bucket, attempt_number)
        if cell not in self._trials:
            self._trials[cell] = 0
            self._successes[cell] = 0
            self._by_group.setdefault((cause, action, bucket), []).append(cell)
        self._trials[cell] += 1
        if outcome is AttemptStatus.SUCCEEDED:
            self._successes[cell] += 1

    # -- belief ------------------------------------------------------------

    def belief(
        self,
        *,
        cause: Cause,
        action: Action,
        delay_hours: int = 0,
        attempt_number: int = 2,
    ) -> Belief:
        """Posterior for one cell.

        Evidence is pooled across attempt numbers when the exact cell is thin:
        a third attempt at 72h is rare, but second attempts at 72h are not, and
        the delay is the part carrying most of the signal. Pooled evidence is
        discounted so it cannot masquerade as direct observation.
        """
        bucket = delay_bucket(delay_hours)
        exact: Cell = (cause, action, bucket, attempt_number)

        successes = float(self._successes.get(exact, 0))
        trials = float(self._trials.get(exact, 0))
        direct_trials = int(trials)

        if trials < 10:
            for cell in self._by_group.get((cause, action, bucket), ()):
                if cell[3] == attempt_number:
                    continue
                trials += 0.5 * self._trials[cell]
                successes += 0.5 * self._successes[cell]

        rate = prior_rate(cause, action)

        # A structurally impossible action stays impossible. Without this a few
        # coincidental successes could talk the agent into retrying a revoked
        # mandate — and a revoked mandate is a compliance problem, not just a
        # wasted attempt.
        if rate == 0.0 and cause in (Cause.MANDATE_EXPIRED, Cause.MANDATE_REVOKED):
            if action in (Action.RETRY_NOW, Action.RETRY_DELAYED):
                return Belief(0.0, 0.0, 0.0, 0, direct_trials, direct_trials == 0)

        alpha = rate * self.prior_strength + successes
        beta = (1.0 - rate) * self.prior_strength + (trials - successes)

        total = alpha + beta
        mean = alpha / total
        # Normal approximation to the Beta interval. Adequate here and cheaper
        # than the exact quantile inside a hot loop.
        spread = 1.96 * (mean * (1 - mean) / (total + 1)) ** 0.5

        return Belief(
            mean=mean,
            low=max(0.0, mean - spread),
            high=min(1.0, mean + spread),
            successes=int(successes),
            trials=direct_trials,
            is_cold=direct_trials == 0,
        )

    # -- introspection -----------------------------------------------------

    @property
    def total_observations(self) -> int:
        return sum(self._trials.values())

    @property
    def is_cold(self) -> bool:
        """True when nothing has been observed at all — cycle 1."""
        return self.total_observations == 0

    def learned_best_delay(self, cause: Cause) -> tuple[int, Belief] | None:
        """The delay the agent currently thinks is best for this cause.

        This is the demo-able artefact of learning: run several cycles and watch
        it move from an arbitrary starting point onto the delay that genuinely
        works. Returns None while the agent has no evidence to prefer one.
        """
        scored = [
            (delay, self.belief(cause=cause, action=Action.RETRY_DELAYED, delay_hours=delay))
            for delay in CANDIDATE_DELAYS_HOURS
        ]
        informed = [(d, b) for d, b in scored if not b.is_cold]
        if not informed:
            return None
        return max(informed, key=lambda pair: pair[1].mean)
