"""Read captured webhook payloads and check they carry what Day 3 needs.

The point of Day 1 is not "a file arrived" — it is "a file arrived that the
diagnosis layer can actually work from". The fields that matter are the error
fields: if a captured `payment.failed` has no `error.reason`, the whole cause
taxonomy has nothing to key off and we need to know that on Day 1, not Day 3.

This module also prints the *vocabulary* found across all fixtures — the actual
strings Razorpay used. Build the Day 3 taxonomy from that list, not from what
we assume the codes are called.

Run:
    .venv/Scripts/python.exe -m recovery.fixtures
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from recovery.config import settings

# Razorpay nests the interesting object under payload.<entity>.entity.
KNOWN_ENTITIES = ("payment", "subscription", "invoice", "order", "refund")

# What the diagnosis layer (Day 3) reads off a failed payment.
REQUIRED_ERROR_FIELDS = ("error_code", "error_reason", "error_description")
# Useful but not fatal if absent.
OPTIONAL_ERROR_FIELDS = ("error_source", "error_step")


@dataclass
class FixtureProbe:
    """What one captured payload does and does not give us."""

    path: Path
    event: str | None = None
    entity: str | None = None
    amount_paise: int | None = None
    method: str | None = None
    bank: str | None = None
    card_network: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)

    @property
    def is_failure(self) -> bool:
        return bool(self.event and self.event.endswith(".failed"))

    @property
    def usable_for_diagnosis(self) -> bool:
        """A failure payload is usable only if it says *why* it failed."""
        if not self.is_failure:
            return True
        return bool(self.fields.get("error_reason") or self.fields.get("error_code"))


def _extract_entity(payload: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """Pull the primary entity out of a Razorpay webhook envelope."""
    container = payload.get("payload") or {}
    for name in KNOWN_ENTITIES:
        node = container.get(name)
        if isinstance(node, dict) and isinstance(node.get("entity"), dict):
            return name, node["entity"]
    return None, {}


def probe_payload(path: Path, payload: dict[str, Any]) -> FixtureProbe:
    entity_name, entity = _extract_entity(payload)

    card = entity.get("card") or {}
    result = FixtureProbe(
        path=path,
        event=payload.get("event"),
        entity=entity_name,
        amount_paise=entity.get("amount"),
        method=entity.get("method"),
        bank=entity.get("bank"),
        card_network=card.get("network"),
        fields={
            "error_code": entity.get("error_code"),
            "error_reason": entity.get("error_reason"),
            "error_description": entity.get("error_description"),
            "error_source": entity.get("error_source"),
            "error_step": entity.get("error_step"),
        },
    )

    if result.is_failure:
        result.missing = [
            name for name in REQUIRED_ERROR_FIELDS if not result.fields.get(name)
        ]
    return result


def load_fixtures(directory: Path | None = None) -> list[FixtureProbe]:
    """Probe every JSON payload in `directory` (default: the configured one)."""
    directory = directory or settings.fixtures_dir
    probes: list[FixtureProbe] = []
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            probe = FixtureProbe(path=path)
            probe.missing = ["<malformed JSON>"]
            probes.append(probe)
            continue
        probes.append(probe_payload(path, payload))
    return probes


def report(directory: Path | None = None) -> int:
    """Print a per-fixture report. Returns the count of unusable failures."""
    directory = directory or settings.fixtures_dir
    probes = load_fixtures(directory)

    if not probes:
        print(f"No payloads in {directory}/ yet.")
        print("Day 1 is not done until at least one payment.failed lands here.")
        return 0

    vocabulary: Counter[str] = Counter()
    unusable = 0

    for probe in probes:
        print(f"\n{probe.path.name}")
        print(f"  event   {probe.event}   entity={probe.entity}")
        print(f"  amount  {probe.amount_paise} paise   method={probe.method}")
        print(f"  segment bank={probe.bank}  network={probe.card_network}")

        present = {k: v for k, v in probe.fields.items() if v}
        if present:
            for key, value in present.items():
                print(f"  {key:<18} {value}")
        elif probe.is_failure:
            print("  (no error fields at all)")

        reason = probe.fields.get("error_reason")
        if reason:
            vocabulary[str(reason)] += 1

        if probe.missing:
            unusable += 1
            print(f"  MISSING: {', '.join(probe.missing)}")

    print("\n" + "=" * 60)
    failures = [p for p in probes if p.is_failure]
    print(f"{len(probes)} payload(s), {len(failures)} failure(s)")

    if vocabulary:
        print("\nerror_reason vocabulary seen (build the Day 3 taxonomy from this):")
        for reason, count in vocabulary.most_common():
            print(f"  {count:>3}x  {reason}")

    if unusable:
        print(
            f"\n{unusable} failure payload(s) carry no usable error fields. "
            "The diagnosis layer cannot key off these."
        )
    elif failures:
        print("\nAll failure payloads carry error fields. Day 1 goal met.")
    else:
        print("\nNo failure payload captured yet — that is still the Day 1 goal.")

    return unusable


if __name__ == "__main__":
    report()
