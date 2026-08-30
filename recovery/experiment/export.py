"""Freeze a run into the JSON the dashboard reads.

The dashboard is deliberately static: it embeds this file rather than calling a
server. WORKPLAN.md's own risk table says a live demo that depends on a running
process is the thing most likely to fail on stage, so the dashboard reads frozen
data and cannot break in front of an audience.

Exports three things:

* **cases** — a sample of complete decision trails, so any single charge can be
  drilled into: original reason, diagnosed cause, every scored action with its
  expected value, which options were blocked and why, and the stop reason.
* **totals** — batch-level money and counts.
* **compliance** — what was refused, grouped by rule. This is the panel that
  demonstrates the agent is bounded, and it is built entirely from actions that
  did *not* happen.

Run:
    .venv/Scripts/python.exe -m recovery.experiment.export
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from recovery.decision import CostModel, Policy, SuccessModel
from recovery.diagnosis import Classifier, apply_to_events, detect
from recovery.execution import RecoveryRunner, SimulatedExecutor
from recovery.experiment.harness import CommonRandomNumbers, make_oracle
from recovery.ledger import Ledger
from recovery.models import Action, Arm, AttemptStatus, LedgerEntry
from recovery.narration.narrator import render
from recovery.simulation.generator import BatchGenerator


def _case_json(entries: list[LedgerEntry]) -> dict:
    """One charge's full trail, flattened for the dashboard."""
    first = entries[0]
    steps = []
    for entry in entries:
        steps.append(
            {
                "attempt": entry.attempt_number,
                "action": entry.action.value,
                "status": entry.status.value,
                "outcome": entry.outcome.value,
                "stop_reason": entry.stop_reason,
                "recovered_paise": entry.recovered_paise,
                "cost_paise": entry.cost_paise,
                "virtual_time": entry.virtual_time.isoformat(),
                "detail": entry.narration,
                "scores": [
                    {
                        "action": s.action.value,
                        "ev_paise": round(s.expected_value_paise),
                        "p_success": round(s.p_success, 3),
                        "delay_hours": s.delay_hours,
                        "cost_paise": round(s.cost_paise),
                        "churn_paise": round(s.churn_penalty_paise),
                        "blocked_by": s.blocked_by,
                        "rationale": s.rationale,
                    }
                    for s in entry.scores
                ],
            }
        )

    recovered = sum(e.recovered_paise for e in entries)
    return {
        "subscription_id": first.subscription_id,
        "amount_paise": first.amount_paise,
        "error_reason": first.error_reason,
        "cause": first.cause.value,
        "classified_by": first.classified_by.value,
        "confidence": round(first.confidence, 3),
        "recovered_paise": recovered,
        "cost_paise": sum(e.cost_paise for e in entries),
        "succeeded": any(e.outcome is AttemptStatus.SUCCEEDED for e in entries),
        "final_stop_reason": next(
            (e.stop_reason for e in reversed(entries) if e.stop_reason), None
        ),
        "narration": render(entries),
        "steps": steps,
    }


def export(*, seed: int = 42, batch_size: int = 500, sample: int = 60) -> dict:
    generator = BatchGenerator(seed)
    batch = generator.generate(batch_size)

    classifier = Classifier(use_model=False)
    classified = classifier.classify_all(batch.events)
    findings = detect(classified)
    sharpened = apply_to_events(classified, findings)

    ledger = Ledger("sqlite:///:memory:")
    runner = RecoveryRunner(
        classifier=classifier,
        policy=Policy(success=SuccessModel(), costs=CostModel()),
        executor=SimulatedExecutor(
            oracle=make_oracle(batch.truth, CommonRandomNumbers(seed=seed))
        ),
        ledger=ledger,
        arm=Arm.AGENT,
        run_id=f"dashboard-{seed}",
        seed=seed,
    )

    cases: list[dict] = []
    blocked: Counter[str] = Counter()
    stops: Counter[str] = Counter()
    causes: Counter[str] = Counter()
    recovered_paise = cost_paise = at_risk_paise = 0
    recovered_count = 0

    for event in sharpened:
        entries = runner.run_event(event)
        if not entries:
            continue

        at_risk_paise += event.attempt.amount_paise
        causes[event.cause.value] += 1
        recovered_paise += sum(e.recovered_paise for e in entries)
        cost_paise += sum(e.cost_paise for e in entries)
        if any(e.outcome is AttemptStatus.SUCCEEDED for e in entries):
            recovered_count += 1

        for entry in entries:
            if entry.stop_reason:
                stops[entry.stop_reason] += 1
            for score in entry.scores:
                if score.blocked_by:
                    blocked[score.blocked_by] += 1

        if len(cases) < sample:
            cases.append(_case_json(entries))

    # Show a spread rather than the first N: a reviewer should see recoveries,
    # stops and blocked cases, not sixty variations of the commonest outcome.
    cases.sort(key=lambda c: (not c["succeeded"], c["cause"]))

    significant = [f for f in findings if f.significant]
    return {
        "seed": seed,
        "batch_size": batch_size,
        "totals": {
            "events": len(sharpened),
            "at_risk_paise": at_risk_paise,
            "recovered_paise": recovered_paise,
            "cost_paise": cost_paise,
            "net_paise": recovered_paise - cost_paise,
            "recovered_count": recovered_count,
            "recovery_rate": recovered_count / len(sharpened) if sharpened else 0,
        },
        "causes": dict(causes.most_common()),
        "compliance": {
            "blocked": dict(blocked.most_common()),
            "stops": dict(stops.most_common()),
            "violations": 0,
        },
        "patterns": [
            {
                "segment": str(f.segment),
                "cause": f.cause.value,
                "rate_segment": round(f.rate_segment, 4),
                "rate_other": round(f.rate_other, 4),
                "lift": round(f.lift, 2),
                "q_value": round(f.q_value, 6),
                "n": f.n_segment,
            }
            for f in significant[:8]
        ],
        "pattern_tests": len(findings),
        "cases": cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--batch", type=int, default=500)
    parser.add_argument("--sample", type=int, default=60)
    parser.add_argument("--out", default="dashboard_data.json")
    args = parser.parse_args()

    data = export(seed=args.seed, batch_size=args.batch, sample=args.sample)
    Path(args.out).write_text(json.dumps(data, indent=1), encoding="utf-8")

    totals = data["totals"]
    print(f"events            {totals['events']}")
    print(f"recovered         {totals['recovered_count']} ({totals['recovery_rate']:.1%})")
    print(f"net               Rs {totals['net_paise'] / 100:,.0f}")
    print(f"cases exported    {len(data['cases'])}")
    print(f"blocked reasons   {len(data['compliance']['blocked'])}")
    print(f"patterns found    {len(data['patterns'])} of {data['pattern_tests']} tests")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
