"""Slice the batch into comparable groups for pattern detection.

A "pattern" claim means: *this group fails more than the rest of the book, by
more than chance explains*. That needs groups, and groups need dimensions that
are actually populated.

**The field trap this module exists to avoid.** Razorpay reports the bank in a
different place depending on how the customer paid:

    method == "card"        -> card.issuer     (bank is null)
    method == netbanking/upi -> bank           (card is null)
    method == "wallet"      -> wallet          (both null)

Verified against the real captured payload (`sources.md` §7), which has
`bank: None`, `card.issuer: "DCBL"`. Segmenting naively on `bank` would drop
every card payment into a single null bucket — one meaningless group, no signal,
and hours lost wondering whether the statistics were broken when the data was
fine and the field was wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from recovery.models import FailureEvent

# Above this the RBI additional-factor-authentication requirement applies, so
# the boundary is a real regulatory line rather than a round number.
AFA_THRESHOLD_PAISE = 1_500_000
MID_BAND_PAISE = 100_000


def issuing_bank(entity: dict[str, Any]) -> str | None:
    """The bank behind a payment, wherever the payload happens to put it.

    Returns `None` when genuinely unknown. That is deliberate: an unknown
    issuer must be *excluded* from bank segmentation, not collected into an
    `"unknown"` bank that then reports a failure rate as if it were a real
    institution.
    """
    method = (entity.get("method") or "").lower()

    if method == "card":
        return (entity.get("card") or {}).get("issuer") or None
    if method == "wallet":
        return entity.get("wallet") or None
    return entity.get("bank") or None


def amount_band(amount_paise: int) -> str:
    if amount_paise >= AFA_THRESHOLD_PAISE:
        return "high"
    if amount_paise >= MID_BAND_PAISE:
        return "mid"
    return "low"


def time_band(hour: int) -> str:
    """Coarse buckets. Bank batch windows and human behaviour both cluster."""
    if 0 <= hour < 6:
        return "night"
    if 6 <= hour < 12:
        return "morning"
    if 12 <= hour < 18:
        return "afternoon"
    return "evening"


@dataclass(frozen=True)
class Segment:
    """One dimension/value pair an event belongs to, e.g. issuer=DCBL."""

    dimension: str
    value: str

    def __str__(self) -> str:
        return f"{self.dimension}={self.value}"


def segments_for(event: FailureEvent) -> tuple[Segment, ...]:
    """Every group this event belongs to.

    One event belongs to several segments at once (its issuer, its network, its
    amount band...). Each is tested separately, which is precisely why the
    multiple-comparison correction in `patterns.py` is not optional.
    """
    entity: dict[str, Any] = {}
    if event.raw_payload:
        entity = (
            event.raw_payload.get("payload", {}).get("payment", {}).get("entity", {})
        )

    found: list[Segment] = []

    bank = issuing_bank(entity)
    if bank:
        found.append(Segment("issuer", str(bank)))

    method = entity.get("method") or event.attempt.method
    if method:
        found.append(Segment("method", str(method)))

    network = (entity.get("card") or {}).get("network") or event.attempt.card_network
    if network:
        found.append(Segment("network", str(network)))

    found.append(Segment("amount_band", amount_band(event.attempt.amount_paise)))
    found.append(Segment("time_band", time_band(event.occurred_at.hour)))
    found.append(Segment("weekday", event.occurred_at.strftime("%a")))
    found.append(Segment("attempt_number", str(event.attempt.attempt_number)))

    return tuple(found)
