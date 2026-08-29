"""Run the measurement.

    .venv/Scripts/python.exe -m recovery.experiment --seeds 30 --cycles 3
    .venv/Scripts/python.exe -m recovery.experiment --no-churn-penalty
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from recovery.decision import CostModel
from recovery.experiment.harness import check_plausibility, run_experiment
from recovery.models import Arm


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds", type=int, default=30)
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--batch", type=int, default=2_000)
    parser.add_argument(
        "--no-churn-penalty",
        action="store_true",
        help="Report with the softest assumption switched off (WORKPLAN 4.3).",
    )
    parser.add_argument("--out", default=None, help="Write results to JSON.")
    args = parser.parse_args()

    costs = CostModel(churn_penalty_enabled=not args.no_churn_penalty)

    started = time.time()
    result = run_experiment(
        seeds=args.seeds, cycles=args.cycles, batch_size=args.batch, costs=costs
    )
    elapsed = time.time() - started

    print(
        f"{args.seeds} seeds x {args.cycles} cycles x {args.batch} events"
        f"   churn penalty {'ON' if costs.churn_penalty_enabled else 'OFF'}"
    )
    print(f"ran in {elapsed:.1f}s\n")

    summary = result.summary()
    header = f"{'arm':<12}{'net Rs':>14}{'gross Rs':>14}{'recovery':>11}{'attempts':>11}{'wasted':>10}"
    print(header)
    print("-" * len(header))
    for arm in (Arm.NAIVE, Arm.FIXED_3X, Arm.AGENT):
        s = summary[arm.value]
        print(
            f"{arm.value:<12}{s['net_paise'] / 100:>14,.0f}{s['gross_paise'] / 100:>14,.0f}"
            f"{s['recovery_rate']:>10.1%}{s['attempts']:>11,.0f}{s['wasted_attempts']:>10,.0f}"
        )

    print("\nPAIRED DIFFERENCES (same events, same coin flips, 95% CI)")
    for comparison in result.comparisons:
        print(f"  {comparison.describe()}")

    violations = sum(s["compliance_violations"] for s in summary.values())
    print(f"\ncompliance violations: {violations}  (target 0)")

    warning = check_plausibility(result)
    if warning:
        print(f"\n!! PLAUSIBILITY CHECK: {warning}")
    else:
        print("\nplausibility check: uplift is inside the published 15-40% band")

    if args.out:
        Path(args.out).write_text(
            json.dumps(
                {
                    "seeds": result.seeds,
                    "cycles": result.cycles,
                    "batch_size": result.batch_size,
                    "churn_penalty_enabled": result.churn_penalty_enabled,
                    "summary": summary,
                    "comparisons": [
                        {
                            "label": c.label,
                            "mean_difference_paise": c.mean_difference_paise,
                            "ci_low": c.ci_low,
                            "ci_high": c.ci_high,
                            "relative_uplift": c.relative_uplift,
                            "significant": c.significant,
                        }
                        for c in result.comparisons
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
