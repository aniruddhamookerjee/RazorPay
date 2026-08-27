"""Measure how well the classifier diagnoses — as an evaluation, never at runtime.

WORKPLAN.md Day 3: *"Classifier accuracy measured against generator labels as an
evaluation only — labels stay out of the runtime path."*

That caveat is the whole point. The generator knows every event's true cause, and
a classifier could reach 100% trivially by reading it. So the labels live on
`GeneratedBatch.truth`, the classifier is handed `batch.events`, and only this
module puts the two together.

It lives in `recovery/experiment/` rather than `recovery/diagnosis/` for exactly
that reason: the diagnosis package is on the decision path and is forbidden from
importing the simulation. Putting the evaluation next to the code it grades would
have broken the holdout boundary — the test catches it.

Run:
    .venv/Scripts/python.exe -m recovery.experiment.evaluate
    .venv/Scripts/python.exe -m recovery.experiment.evaluate --model   (slow)
"""

from __future__ import annotations

import argparse
from collections import Counter

from recovery.diagnosis.classifier import Classifier
from recovery.diagnosis.patterns import Finding, apply_to_events, detect
from recovery.diagnosis.segments import segments_for
from recovery.models import Cause, FailureEvent
from recovery.simulation.generator import BatchGenerator


def score(events: tuple[FailureEvent, ...], truth: dict[str, Cause]) -> dict[str, object]:
    """Accuracy overall and per cause, plus the confusions that actually occur."""
    correct = 0
    totals: Counter[Cause] = Counter()
    rights: Counter[Cause] = Counter()
    confusions: Counter[tuple[Cause, Cause]] = Counter()

    for event in events:
        actual = truth.get(event.event_id)
        if actual is None:
            continue
        totals[actual] += 1
        if event.cause is actual:
            correct += 1
            rights[actual] += 1
        else:
            confusions[(actual, event.cause)] += 1

    n = sum(totals.values())
    return {
        "n": n,
        "accuracy": correct / n if n else 0.0,
        "per_cause": {
            cause.value: {"n": totals[cause], "accuracy": rights[cause] / totals[cause]}
            for cause in totals
        },
        "confusions": confusions,
    }


def verify_findings(
    events: tuple[FailureEvent, ...],
    truth: dict[str, Cause],
    findings: list[Finding],
) -> list[tuple[str, float, float]]:
    """Check each reported pattern against ground truth rather than our diagnoses.

    A finding claims "this segment over-produces cause C". Measured against our
    own labels that is partly circular — a systematic misclassification would
    manufacture the same pattern. Measured against the generator's real labels,
    it says whether the pattern is real.

    Returns (description, rate we claimed, rate in truth).
    """
    results: list[tuple[str, float, float]] = []
    for finding in findings:
        if not finding.significant or finding.lift <= 1.5:
            continue
        target = str(finding.segment)
        members = [
            e for e in events if target in {str(s) for s in segments_for(e)}
        ]
        if not members:
            continue
        actual = sum(1 for e in members if truth.get(e.event_id) is finding.cause)
        results.append(
            (
                f"{finding.segment} -> {finding.cause.value}",
                finding.rate_segment,
                actual / len(members),
            )
        )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--count", type=int, default=10_000)
    parser.add_argument(
        "--model",
        action="store_true",
        help="Enable the local-model fallback (slow; rules cover the batch anyway).",
    )
    args = parser.parse_args()

    batch = BatchGenerator(args.seed).generate(args.count)
    classifier = Classifier(use_model=args.model)
    classified = classifier.classify_all(batch.events)

    findings = detect(classified)
    sharpened = apply_to_events(classified, findings)
    result = score(sharpened, batch.truth)

    print(f"CLASSIFIER EVALUATION   seed={args.seed}   n={result['n']}")
    print(f"  overall accuracy  {result['accuracy']:.1%}")
    print(f"  classifier        {classifier.stats}")

    if result["accuracy"] > 0.99:
        print()
        print("  NOTE: near-perfect accuracy is expected and is NOT evidence that")
        print("  the classifier is good. The generator emits documented Razorpay")
        print("  codes and the taxonomy maps documented codes, so this is a closed")
        print("  loop by construction. What it does prove is that the two tables")
        print("  agree — which is exactly the bug that slipped through on Day 3,")
        print("  when five invented codes silently classified as UNKNOWN.")
        print("  Real-world accuracy is unmeasurable here: test mode only ever")
        print("  returned the generic payment_failed (sources.md section 7).")

    print("\nper cause:")
    per_cause: dict = result["per_cause"]  # type: ignore[assignment]
    for cause, stats in sorted(per_cause.items(), key=lambda kv: -kv[1]["n"]):
        print(f"  {stats['accuracy']:>6.1%}  n={stats['n']:>6}  {cause}")

    confusions: Counter = result["confusions"]  # type: ignore[assignment]
    if confusions:
        print("\nconfusions (true -> diagnosed):")
        for (actual, diagnosed), n in confusions.most_common(10):
            print(f"  {n:>6}  {actual.value} -> {diagnosed.value}")
    else:
        print("\nno confusions.")

    significant = [f for f in findings if f.significant]
    print(f"\npattern detection: {len(findings)} tests, {len(significant)} significant")
    for finding in significant[:5]:
        print(f"  {finding.describe()}")

    verified = verify_findings(sharpened, batch.truth, findings)
    if verified:
        print("\nfindings checked against ground truth:")
        for label, claimed, actual in verified:
            mark = "ok " if abs(claimed - actual) < 0.05 else "OFF"
            print(f"  {mark} {label}: claimed {claimed:.1%}, true {actual:.1%}")

    corroborated = sum(1 for e in sharpened if e.segment)
    print(f"\n{corroborated} diagnoses corroborated by a segment pattern")


if __name__ == "__main__":
    main()
