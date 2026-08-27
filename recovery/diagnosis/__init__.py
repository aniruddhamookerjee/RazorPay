"""Layer 2 — diagnosis.

Turns a raw failure into (a) a cause and (b) a confidence, by two routes:

    taxonomy.py    Razorpay's documented reason strings -> our nine causes
    classifier.py  rule lookup first, local model only for what the rules miss
    segments.py    slices the batch into comparable groups
    patterns.py    is a segment failing more than baseline, really?

This package must never import `recovery.simulation` — that is the holdout
boundary, enforced by `tests/test_generator.py`.
"""

from recovery.diagnosis.classifier import Classifier, classify_reason
from recovery.diagnosis.patterns import Finding, apply_to_events, detect
from recovery.diagnosis.segments import Segment, issuing_bank, segments_for
from recovery.diagnosis.taxonomy import REASON_TO_CAUSE, cause_for_reason

__all__ = [
    "Classifier",
    "Finding",
    "apply_to_events",
    "detect",
    "classify_reason",
    "REASON_TO_CAUSE",
    "cause_for_reason",
    "Segment",
    "issuing_bank",
    "segments_for",
]
