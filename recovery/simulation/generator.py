"""Seeded batch generator — the simulated event stream.

Produces `FailureEvent`s whose `raw_payload` matches the **captured** Razorpay
envelope (`fixtures/`) while carrying **documented** reason strings that test
mode never emits. That split is the provenance story: shape is real, vocabulary
is official, distribution is cited. Nothing is invented.

Also exposes `resolve()`, which decides whether a retry attempted at a given
time actually succeeds. That function is the answer key. It lives here, behind
the holdout boundary, and the agent never calls it — only the experiment
harness does, to score what the agent chose.

Determinism: everything derives from one seed via `numpy.random.Generator`.
Two runs with the same seed produce byte-identical batches, which is what makes
the paired-replay comparison on Day 6 valid.

Run:
    .venv/Scripts/python.exe -m recovery.simulation.generator --seed 42 --count 100
"""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from recovery.config import settings
from recovery.clock import EPOCH
from recovery.models import (
    AttemptStatus,
    Cause,
    ChargeAttempt,
    FailureEvent,
)
from recovery.simulation import params as P

# The envelope is not hand-written — it is loaded from a real captured payload
# and then overridden field by field. Hand-writing it drifted from the capture
# within an hour (missing fee, tax, acquirer_data, refund_status...), which is
# exactly the fiction the diagnosis layer must not be developed against.
_FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"


@lru_cache(maxsize=1)
def _envelope_template() -> dict[str, Any]:
    """The most recent captured `payment.failed` payload, as a template."""
    captured = sorted(_FIXTURES_DIR.glob("payment_failed__*.json"))
    if not captured:
        raise FileNotFoundError(
            f"No captured payload in {_FIXTURES_DIR}. The generator derives its "
            "envelope from a real capture — run scripts.create_payment_link and "
            "pay with a failing test card first (WORKPLAN.md Day 1)."
        )
    return json.loads(captured[-1].read_text(encoding="utf-8"))


@dataclass(frozen=True)
class GeneratedBatch:
    """A reproducible batch, its manifest, and the ground truth behind it.

    `truth` maps event_id -> the cause the generator actually used. Two things
    need it and neither is the agent:

    * the Day 6 harness, which must pass `true_cause` to `resolve()` to score
      what an arm chose;
    * the classifier evaluation, which measures diagnosis accuracy.

    It lives on the batch rather than on the events themselves precisely so it
    cannot leak: the classifier is handed `batch.events`, never the batch. The
    holdout boundary is about who can *read* this, and the answer is the
    harness and the evaluation, not the decision path.
    """

    events: tuple[FailureEvent, ...]
    seed: int
    manifest: dict[str, Any]
    truth: dict[str, Cause] = dataclass_field(default_factory=dict)


class BatchGenerator:
    """Generates failure events with cited distributions."""

    def __init__(self, seed: int, population: P.Population | None = None) -> None:
        self.seed = seed
        self.rng = np.random.default_rng(seed)
        self.population = population or P.Population()

    # -- sampling ----------------------------------------------------------

    def _pick(self, choices: tuple, weights: tuple[float, ...]):
        return choices[self.rng.choice(len(choices), p=np.array(weights) / sum(weights))]

    def _pick_cause(self, *, issuer: str, amount_paise: int) -> Cause:
        """Sample a cause, applying planted segment effects."""
        causes = list(P.CAUSE_WEIGHTS)
        weights = np.array([P.CAUSE_WEIGHTS[c] for c in causes], dtype=float)

        band = self._amount_band(amount_paise)
        for effect in P.SEGMENT_EFFECTS:
            observed = issuer if effect.dimension == "issuer" else band
            if observed == effect.value and effect.cause in causes:
                weights[causes.index(effect.cause)] *= effect.multiplier

        weights /= weights.sum()
        return causes[self.rng.choice(len(causes), p=weights)]

    @staticmethod
    def _amount_band(amount_paise: int) -> str:
        # The high band starts at the RBI additional-factor-auth threshold, so
        # the planted auth effect lands where a real one plausibly would.
        if amount_paise >= 1_500_000:
            return "high"
        if amount_paise >= 100_000:
            return "mid"
        return "low"

    # -- payload -----------------------------------------------------------

    def _payload(
        self,
        *,
        attempt: ChargeAttempt,
        reason: str,
        network: str,
        issuer: str,
        occurred_at: datetime,
    ) -> dict[str, Any]:
        """A real captured envelope with the varying fields overridden.

        Every field we do not set keeps whatever the real capture had, so the
        shape cannot drift from production even as this generator grows.
        """
        unix = int(occurred_at.timestamp())
        payload = deepcopy(_envelope_template())
        payload["created_at"] = unix

        entity = payload["payload"]["payment"]["entity"]
        entity.update(
            {
                "id": f"pay_{self.rng.integers(10**13, 10**14)}",
                "amount": attempt.amount_paise,
                "status": "failed",
                "order_id": f"order_{self.rng.integers(10**13, 10**14)}",
                "captured": False,
                "card_id": f"card_{self.rng.integers(10**13, 10**14)}",
                "error_code": "BAD_REQUEST_ERROR",
                "error_description": reason.replace("_", " ").capitalize(),
                "error_source": (
                    "bank" if ("bank" in reason or "funds" in reason) else "gateway"
                ),
                "error_step": "payment_authorization",
                "error_reason": reason,
                "created_at": unix,
            }
        )

        card = entity.get("card")
        if isinstance(card, dict):
            card.update(
                {
                    "id": entity["card_id"],
                    "network": network,
                    "issuer": issuer,
                    "last4": f"{self.rng.integers(0, 10000):04d}",
                }
            )

        return payload

    # -- generation --------------------------------------------------------

    def generate(self, count: int, *, cycle: int = 0) -> GeneratedBatch:
        events: list[FailureEvent] = []
        truth: dict[str, Cause] = {}

        for i in range(count):
            amount = int(
                self._pick(
                    self.population.amount_choices_paise, self.population.amount_weights
                )
            )
            network = str(
                self._pick(self.population.networks, self.population.network_weights)
            )
            issuer = str(
                self._pick(self.population.issuers, self.population.issuer_weights)
            )
            cause = self._pick_cause(issuer=issuer, amount_paise=amount)
            reason = str(self.rng.choice(P.REASON_STRINGS[cause]))

            # Charges spread across a 30-day billing month, at realistic hours.
            occurred_at = EPOCH + timedelta(
                days=int(self.rng.integers(0, 30)),
                hours=int(self.rng.integers(0, 24)),
                minutes=int(self.rng.integers(0, 60)),
            )

            attempt = ChargeAttempt(
                subscription_id=f"sub_sim_{i:06d}",
                customer_id=f"cust_sim_{i:06d}",
                mandate_id=f"mandate_sim_{i:06d}",
                amount_paise=amount,
                attempt_number=1,
                status=AttemptStatus.FAILED,
                created_at=occurred_at,
                virtual_time=occurred_at,
                bank=None,
                card_network=network,
                method="card",
                is_live=False,
            )

            event = FailureEvent(
                    attempt=attempt,
                    error_code="BAD_REQUEST_ERROR",
                    error_reason=reason,
                    error_description=reason.replace("_", " ").capitalize(),
                    # Diagnosis is Day 3's job. The generator does not label.
                    cause=Cause.UNKNOWN,
                    occurred_at=occurred_at,
                    virtual_time=occurred_at,
                    raw_payload=self._payload(
                        attempt=attempt,
                        reason=reason,
                        network=network,
                        issuer=issuer,
                        occurred_at=occurred_at,
                    ),
            )
            events.append(event)
            truth[event.event_id] = cause

        return GeneratedBatch(
            events=tuple(events),
            seed=self.seed,
            truth=truth,
            manifest={
                "seed": self.seed,
                "count": count,
                "cycle": cycle,
                "epoch": EPOCH.isoformat(),
                "population": self.population.__dict__,
                "cause_weights": {c.value: w for c, w in P.CAUSE_WEIGHTS.items()},
            },
        )

    def generate_split(
        self,
        *,
        volume_count: int = 10_000,
        live_count: int | None = None,
        cycle: int = 0,
    ) -> tuple[GeneratedBatch, GeneratedBatch]:
        """Produce the volume batch and the live subset in one draw.

        Two batches with different jobs (WORKPLAN.md Day 2):

        * **volume** — large enough that pattern detection can make a
          significance claim that means something. Day 3 Part B needs this;
          a 100-event batch would put 3 rows in a segment cell.
        * **live subset** — small, and flagged `is_live`, so Day 5 executes it
          against real Razorpay test APIs. Capped by `LIVE_SUBSET_SIZE` in
          `.env` so a bug cannot fan out into hundreds of real API calls.

        The subset is drawn from the *same* generated population rather than
        generated separately, so it is a genuine sample of the batch and not a
        second distribution that happens to look similar.
        """
        live_count = live_count or settings.live_subset_size
        if live_count > volume_count:
            raise ValueError("live subset cannot be larger than the volume batch")

        batch = self.generate(volume_count, cycle=cycle)

        # Sample without replacement so the subset is representative.
        picked = set(
            self.rng.choice(volume_count, size=live_count, replace=False).tolist()
        )

        volume_events: list[FailureEvent] = []
        live_events: list[FailureEvent] = []
        for i, event in enumerate(batch.events):
            if i in picked:
                live_events.append(
                    event.model_copy(
                        update={
                            "attempt": event.attempt.model_copy(
                                update={"is_live": True}
                            )
                        }
                    )
                )
            else:
                volume_events.append(event)

        def manifest(kind: str, events: tuple[FailureEvent, ...]) -> dict[str, Any]:
            return {
                **batch.manifest,
                "kind": kind,
                "count": len(events),
                "at_risk_paise": sum(e.attempt.amount_paise for e in events),
            }

        volume = GeneratedBatch(
            events=tuple(volume_events),
            seed=self.seed,
            truth={e.event_id: batch.truth[e.event_id] for e in volume_events},
            manifest=manifest("volume", tuple(volume_events)),
        )
        live = GeneratedBatch(
            events=tuple(live_events),
            seed=self.seed,
            truth={e.event_id: batch.truth[e.event_id] for e in live_events},
            manifest=manifest("live_subset", tuple(live_events)),
        )
        return volume, live


# ---------------------------------------------------------------------------
# The answer key
# ---------------------------------------------------------------------------


def resolve(
    *,
    true_cause: Cause,
    attempted_at: datetime,
    failed_at: datetime,
    attempt_number: int,
    rng: np.random.Generator,
) -> bool:
    """Does a retry attempted at `attempted_at` succeed?

    Called only by the experiment harness to score what an arm chose. The agent
    never sees this — if it did, the whole measurement would be circular.
    """
    delay_hours = max(0, int((attempted_at - failed_at).total_seconds() // 3600))
    curve = P.RECOVERY.get(true_cause, P.RECOVERY[Cause.UNKNOWN])
    p = curve.p_success(delay_hours=delay_hours, attempt_number=attempt_number)

    p *= P.WEEKDAY_MULTIPLIER[attempted_at.weekday()]
    if true_cause is Cause.INSUFFICIENT_FUNDS and attempted_at.day in P.PAYDAY_DAYS:
        p *= P.PAYDAY_MULTIPLIER

    return bool(rng.random() < min(1.0, p))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--out", default=None, help="Write events to this JSON file.")
    parser.add_argument(
        "--split",
        action="store_true",
        help="Emit the 10k volume batch plus the live subset (WORKPLAN.md Day 2).",
    )
    args = parser.parse_args()

    if args.split:
        volume, live = BatchGenerator(args.seed).generate_split(
            volume_count=args.count if args.count > 1000 else 10_000
        )
        for label, b in (("volume", volume), ("live subset", live)):
            print(
                f"{label:<12} {len(b.events):>6} events   "
                f"Rs {b.manifest['at_risk_paise'] / 100:>14,.2f} at risk"
            )
        assert all(e.attempt.is_live for e in live.events)
        assert not any(e.attempt.is_live for e in volume.events)
        print()
        print("is_live flags verified")
        return

    batch = BatchGenerator(args.seed).generate(args.count)

    counts: dict[str, int] = {}
    for event in batch.events:
        counts[event.error_reason or "?"] = counts.get(event.error_reason or "?", 0) + 1

    print(f"seed={batch.seed}  events={len(batch.events)}")
    print("\nreason distribution:")
    for reason, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>5}  {reason}")

    total = sum(e.attempt.amount_paise for e in batch.events)
    print(f"\nat risk: {total} paise  (Rs {total / 100:,.2f})")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "manifest": batch.manifest,
                    "events": [e.model_dump(mode="json") for e in batch.events],
                },
                fh,
                indent=2,
            )
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
