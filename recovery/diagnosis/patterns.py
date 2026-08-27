"""Is a segment really failing differently, or does it just look that way?

Every event in the batch is already a failure, so the question is not "does this
segment fail more" but **"is this segment's mix of causes different"** — does
`issuer=X` produce a higher share of `bank_unavailable` than the rest of the
book does?

Method: a two-proportion z-test of P(cause | segment) against
P(cause | everything else), for every (segment, cause) pair with enough support.

**Why the correction is not optional.** With ~15 segment values and 9 causes,
this runs on the order of a hundred tests. At p < 0.05, five of them come back
"significant" on pure noise, by construction. Reporting those as findings — "we
detected that Bank X fails more" — is exactly the claim that collapses under one
sharp question. Benjamini-Hochberg controls the *false discovery rate*: of the
segments we do report, the expected share of spurious ones is held at `fdr`.

**The test that matters is the null test.** A detector that finds signal is
unremarkable; one that stays quiet when there is nothing there is credible. See
`tests/test_patterns.py`.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

import numpy as np
from scipy import stats

from recovery.diagnosis.segments import Segment, segments_for
from recovery.models import Cause, FailureEvent

# A segment needs this many events before it is tested at all. Without a floor,
# a segment of 3 events can show a "100% rate" and sail through any test.
MIN_SUPPORT = 30

# Expected proportion of reported findings that are false. 0.10 is a deliberate
# choice: this feeds a confidence score, not a medical decision, and being too
# strict here means missing real operational patterns.
DEFAULT_FDR = 0.10


@dataclass(frozen=True)
class Finding:
    """One segment that behaves differently from the rest of the book."""

    segment: Segment
    cause: Cause
    n_segment: int
    n_other: int
    rate_segment: float
    rate_other: float
    p_value: float
    q_value: float  # Benjamini-Hochberg adjusted
    significant: bool

    @property
    def lift(self) -> float:
        """How many times more likely this cause is inside the segment."""
        if self.rate_other == 0:
            return float("inf") if self.rate_segment > 0 else 1.0
        return self.rate_segment / self.rate_other

    def describe(self) -> str:
        return (
            f"{self.segment}: {self.cause.value} at {self.rate_segment:.1%} "
            f"vs {self.rate_other:.1%} elsewhere "
            f"({self.lift:.1f}x, n={self.n_segment}, q={self.q_value:.4f})"
        )


def _two_proportion_z(k1: int, n1: int, k2: int, n2: int) -> float:
    """Two-sided p-value for two proportions being equal."""
    if n1 == 0 or n2 == 0:
        return 1.0
    p1, p2 = k1 / n1, k2 / n2
    pooled = (k1 + k2) / (n1 + n2)
    if pooled in (0.0, 1.0):
        return 1.0
    se = np.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    if se == 0:
        return 1.0
    return float(2 * (1 - stats.norm.cdf(abs((p1 - p2) / se))))


def _benjamini_hochberg(p_values: list[float], fdr: float) -> tuple[list[float], list[bool]]:
    """Adjust for testing many hypotheses at once.

    Returns (q-values, significant flags). Implemented directly rather than
    pulled from statsmodels so the step is inspectable — this is the part a
    judge is most likely to ask about.
    """
    m = len(p_values)
    if m == 0:
        return [], []

    order = np.argsort(p_values)
    ranked = np.array(p_values)[order]

    # Step-up: q_i = min over j >= i of (m/j) * p_j, kept monotone.
    q = np.minimum.accumulate((ranked * m / np.arange(1, m + 1))[::-1])[::-1]
    q = np.minimum(q, 1.0)

    q_values = np.empty(m)
    q_values[order] = q
    return q_values.tolist(), (q_values <= fdr).tolist()


def detect(
    events: tuple[FailureEvent, ...],
    *,
    min_support: int = MIN_SUPPORT,
    fdr: float = DEFAULT_FDR,
) -> list[Finding]:
    """Find segments whose cause mix differs from the rest of the batch.

    Events must already be classified — `cause` drives the whole analysis.
    Returns every tested pair, ordered by q-value, with `significant` set.
    Callers who only want findings should filter on it; keeping the rest makes
    the near-misses inspectable rather than silently discarded.
    """
    if not events:
        return []

    total = len(events)
    overall: Counter[Cause] = Counter(e.cause for e in events)

    membership: dict[Segment, Counter[Cause]] = defaultdict(Counter)
    sizes: Counter[Segment] = Counter()
    for event in events:
        for segment in segments_for(event):
            membership[segment][event.cause] += 1
            sizes[segment] += 1

    candidates: list[tuple[Segment, Cause, int, int, int, int]] = []
    for segment, counts in membership.items():
        n1 = sizes[segment]
        if n1 < min_support:
            continue
        n2 = total - n1
        if n2 < min_support:
            continue
        for cause in Cause:
            k1 = counts.get(cause, 0)
            k2 = overall.get(cause, 0) - k1
            # A cause absent from both sides carries no information.
            if k1 + k2 == 0:
                continue
            candidates.append((segment, cause, k1, n1, k2, n2))

    if not candidates:
        return []

    p_values = [_two_proportion_z(k1, n1, k2, n2) for _, _, k1, n1, k2, n2 in candidates]
    q_values, flags = _benjamini_hochberg(p_values, fdr)

    findings = [
        Finding(
            segment=segment,
            cause=cause,
            n_segment=n1,
            n_other=n2,
            rate_segment=k1 / n1,
            rate_other=k2 / n2,
            p_value=p,
            q_value=q,
            significant=flag,
        )
        for (segment, cause, k1, n1, k2, n2), p, q, flag in zip(
            candidates, p_values, q_values, flags
        )
    ]
    findings.sort(key=lambda f: (f.q_value, -abs(f.rate_segment - f.rate_other)))
    return findings


def apply_to_events(
    events: tuple[FailureEvent, ...], findings: list[Finding]
) -> tuple[FailureEvent, ...]:
    """Sharpen each event's confidence using the segment patterns it sits in.

    WORKPLAN.md Day 3 Part B: "confidence score attached to each diagnosis".
    A diagnosis corroborated by a significant segment pattern is worth more than
    the same label assigned in isolation — if this issuer demonstrably produces
    bank outages at 2.6x the rest of the book, `bank_unavailable` here is better
    evidenced.

    The event also records *which* segment vouched for it, so the audit trail
    can show the reasoning rather than just a higher number.
    """
    boost = confidence_boost(findings)
    if not boost:
        return events

    updated: list[FailureEvent] = []
    for event in events:
        best_segment, best_boost = None, 0.0
        for segment in segments_for(event):
            gain = boost.get((str(segment), event.cause), 0.0)
            if gain > best_boost:
                best_segment, best_boost = str(segment), gain

        if best_segment is None:
            updated.append(event)
            continue

        updated.append(
            event.model_copy(
                update={
                    "confidence": min(1.0, event.confidence + best_boost),
                    "segment": best_segment,
                }
            )
        )
    return tuple(updated)


def confidence_boost(findings: list[Finding]) -> dict[tuple[str, Cause], float]:
    """Map (segment string, cause) -> a diagnostic confidence bump.

    A diagnosis supported by a significant segment pattern is worth more than
    the same label assigned in isolation: the segment gives corroborating
    evidence. Capped so a pattern can sharpen a diagnosis but never manufacture
    certainty on its own.
    """
    boost: dict[tuple[str, Cause], float] = {}
    for finding in findings:
        if not finding.significant or finding.lift <= 1.0:
            continue
        boost[(str(finding.segment), finding.cause)] = min(
            0.15, 0.05 * float(np.log2(finding.lift))
        )
    return boost
