"""Does the result survive being wrong about the world?

The single sharpest question this project can be asked is *"you invented the
numbers, so of course your agent won"*. A point estimate cannot answer it. A
**range** can: vary the assumptions the result depends on, and report where the
advantage holds and where it stops.

Two families of parameter are swept, and the distinction matters:

* **Agent-side** (`churn penalty`, `attempt cost`, `LTV`) — what the agent
  believes. Getting these wrong makes the agent behave badly.
* **World-side** (`delay sensitivity`, `base recovery rates`) — how the
  simulated world actually works. Getting these wrong makes the *measurement*
  wrong, which is far more serious.

The world-side sweep is the important one and the one most projects skip.
Our agent's advantage comes largely from retrying at better times, and the value
of timing is set by a delay curve we wrote ourselves. If real-world recovery is
flatter over delay than we assumed, that advantage should shrink — and this
module reports by how much rather than hoping nobody asks.

Run:
    .venv/Scripts/python.exe -m recovery.experiment.sensitivity
    .venv/Scripts/python.exe -m recovery.experiment.sensitivity --seeds 10
"""

from __future__ import annotations

import argparse
import copy
import json
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from recovery.decision import CostModel
from recovery.experiment.harness import run_experiment
from recovery.models import Arm
from recovery.simulation import params as P


@contextmanager
def flattened_delay_curves(strength: float):
    """Temporarily blend the delay multipliers toward flat.

    `strength=1.0` leaves the world as configured; `0.0` removes delay
    sensitivity entirely, so *when* you retry stops mattering. Everything in
    between interpolates.

    This is the honest stress test for a timing-based agent: if the advantage
    vanishes at low strength, then the result is a statement about our delay
    curve rather than about recovery.
    """
    original = copy.deepcopy(P.RECOVERY)
    try:
        for cause, curve in P.RECOVERY.items():
            mean = sum(curve.delay_multiplier.values()) / len(curve.delay_multiplier)
            flattened = {
                hours: mean + strength * (value - mean)
                for hours, value in curve.delay_multiplier.items()
            }
            object.__setattr__(curve, "delay_multiplier", flattened)
        yield
    finally:
        P.RECOVERY.clear()
        P.RECOVERY.update(original)


@contextmanager
def scaled_base_rates(factor: float):
    """Scale every cause's base recovery rate. Tests an easier or harder world."""
    original = copy.deepcopy(P.RECOVERY)
    try:
        for curve in P.RECOVERY.values():
            object.__setattr__(curve, "base", min(1.0, curve.base * factor))
        yield
    finally:
        P.RECOVERY.clear()
        P.RECOVERY.update(original)


@dataclass
class SweepPoint:
    parameter: str
    value: float
    uplift_full: float
    uplift_retry_only: float
    agent_net_paise: float
    baseline_net_paise: float
    significant: bool

    def describe(self) -> str:
        mark = "" if self.significant else "  (n.s.)"
        return (
            f"  {self.parameter:<22} {self.value:>6.2f}   "
            f"full {self.uplift_full:>+7.1%}   retry-only {self.uplift_retry_only:>+7.1%}{mark}"
        )


def _measure(*, seeds: int, cycles: int, batch: int, costs: CostModel) -> tuple:
    result = run_experiment(seeds=seeds, cycles=cycles, batch_size=batch, costs=costs)
    full = next(c for c in result.comparisons if c.label == "agent vs fixed-3x")
    retry = next(
        c for c in result.comparisons if c.label == "agent (retry-only) vs fixed-3x"
    )
    summary = result.summary()
    return (
        full.relative_uplift,
        retry.relative_uplift,
        summary[Arm.AGENT.value]["net_paise"],
        summary[Arm.FIXED_3X.value]["net_paise"],
        full.significant,
    )


def sweep(*, seeds: int = 8, cycles: int = 2, batch: int = 800) -> list[SweepPoint]:
    points: list[SweepPoint] = []

    def record(parameter: str, value: float, measured: tuple) -> None:
        uplift_full, uplift_retry, agent_net, base_net, significant = measured
        points.append(
            SweepPoint(
                parameter=parameter,
                value=value,
                uplift_full=uplift_full,
                uplift_retry_only=uplift_retry,
                agent_net_paise=agent_net,
                baseline_net_paise=base_net,
                significant=significant,
            )
        )

    # -- world-side: does timing actually matter? -------------------------
    for strength in (0.0, 0.25, 0.5, 0.75, 1.0):
        with flattened_delay_curves(strength):
            record(
                "delay_sensitivity",
                strength,
                _measure(seeds=seeds, cycles=cycles, batch=batch, costs=CostModel()),
            )

    # -- world-side: an easier or harder world ----------------------------
    for factor in (0.5, 0.75, 1.0, 1.25):
        with scaled_base_rates(factor):
            record(
                "base_rate_scale",
                factor,
                _measure(seeds=seeds, cycles=cycles, batch=batch, costs=CostModel()),
            )

    # -- agent-side: the softest number in the model ----------------------
    record(
        "churn_penalty_off",
        0.0,
        _measure(
            seeds=seeds,
            cycles=cycles,
            batch=batch,
            costs=CostModel(churn_penalty_enabled=False),
        ),
    )
    for months in (3, 12, 36):
        record(
            "ltv_months",
            months,
            _measure(
                seeds=seeds,
                cycles=cycles,
                batch=batch,
                costs=CostModel(ltv_months=months),
            ),
        )
    for cost in (0, 200, 2_000):
        record(
            "attempt_cost_paise",
            cost,
            _measure(
                seeds=seeds,
                cycles=cycles,
                batch=batch,
                costs=CostModel(attempt_cost_paise=cost),
            ),
        )

    return points


def crossover(points: list[SweepPoint], parameter: str) -> str:
    """Where does the advantage stop holding for this parameter?"""
    relevant = [p for p in points if p.parameter == parameter]
    if not relevant:
        return "no data"
    losing = [p for p in relevant if p.uplift_full <= 0 or not p.significant]
    if not losing:
        return f"holds across every value tested ({relevant[0].value}-{relevant[-1].value})"
    edge = max(p.value for p in losing)
    return f"stops holding at or below {parameter} = {edge}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds", type=int, default=8)
    parser.add_argument("--cycles", type=int, default=2)
    parser.add_argument("--batch", type=int, default=800)
    parser.add_argument("--out", default="sensitivity.json")
    args = parser.parse_args()

    print(
        f"SENSITIVITY SWEEP  {args.seeds} seeds x {args.cycles} cycles "
        f"x {args.batch} events per point\n"
    )
    points = sweep(seeds=args.seeds, cycles=args.cycles, batch=args.batch)

    current = None
    for point in points:
        if point.parameter != current:
            current = point.parameter
            print()
        print(point.describe())

    print("\nWHERE THE ADVANTAGE STOPS HOLDING")
    for parameter in (
        "delay_sensitivity",
        "base_rate_scale",
        "ltv_months",
        "attempt_cost_paise",
    ):
        print(f"  {parameter:<22} {crossover(points, parameter)}")

    off = next(p for p in points if p.parameter == "churn_penalty_off")
    print(
        f"\nWith the churn penalty switched off entirely: "
        f"full {off.uplift_full:+.1%}, retry-only {off.uplift_retry_only:+.1%}"
    )

    Path(args.out).write_text(
        json.dumps([p.__dict__ for p in points], indent=2), encoding="utf-8"
    )
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
