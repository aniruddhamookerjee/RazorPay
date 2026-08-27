"""Strip identifying values out of captured webhook payloads.

Fixtures exist to lock the payload **schema** — field names, nesting, types.
They do not need real values, and a captured payload carries several that should
not be published: the phone number typed into the checkout page, the Razorpay
account id, and per-payment object ids.

Redaction replaces those values with same-shaped placeholders, so the schema the
generator must match is preserved exactly while the identifying content is not.

Idempotent: running it twice changes nothing the second time.

Run:
    .venv/Scripts/python.exe -m scripts.redact_fixtures --check
    .venv/Scripts/python.exe -m scripts.redact_fixtures
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from recovery.config import settings

# Same-shaped stand-ins. Shape matters (a generator matching the schema has to
# produce plausible values); the specific content does not.
PLACEHOLDERS = {
    "contact": "+919876543210",
    "account_id": "acc_XXXXXXXXXXXXXX",
    "email": "test.customer@example.com",
}

# Razorpay object ids: keep the prefix so type is still readable, blank the rest.
ID_PATTERN = re.compile(r"^(pay|card|order|plink|inv|sub|plan|cust)_[A-Za-z0-9]+$")

# Keys whose values are replaced wherever they appear, at any depth.
REDACT_KEYS = {"contact", "account_id", "email"}
# Keys holding Razorpay object ids, redacted via ID_PATTERN.
ID_KEYS = {"id", "payment_id", "order_id", "card_id", "invoice_id", "token_id"}


def _redact_id(value: str) -> str:
    match = ID_PATTERN.match(value)
    if not match:
        return value
    return f"{match.group(1)}_XXXXXXXXXXXXXX"


def redact(node: Any) -> tuple[Any, int]:
    """Walk the payload, replacing identifying values. Returns (node, count)."""
    changes = 0

    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in REDACT_KEYS and isinstance(value, str) and value:
                replacement = PLACEHOLDERS[key]
                if value != replacement:
                    changes += 1
                out[key] = replacement
            elif key in ID_KEYS and isinstance(value, str):
                replacement = _redact_id(value)
                if value != replacement:
                    changes += 1
                out[key] = replacement
            else:
                out[key], sub = redact(value)
                changes += sub
        return out, changes

    if isinstance(node, list):
        result = []
        for item in node:
            redacted, sub = redact(item)
            result.append(redacted)
            changes += sub
        return result, changes

    return node, changes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report what would change without writing.",
    )
    args = parser.parse_args()

    paths = sorted(settings.fixtures_dir.glob("*.json"))
    if not paths:
        sys.exit(f"No payloads in {settings.fixtures_dir}/")

    total = 0
    for path in paths:
        original = json.loads(path.read_text(encoding="utf-8"))
        cleaned, changes = redact(original)
        total += changes
        verb = "would redact" if args.check else "redacted"
        print(f"{path.name}: {verb} {changes} value(s)")
        if changes and not args.check:
            path.write_text(
                json.dumps(cleaned, indent=2, sort_keys=True), encoding="utf-8"
            )

    print()
    if args.check:
        print(f"{total} value(s) would be redacted. Re-run without --check to apply.")
    else:
        print(f"{total} value(s) redacted across {len(paths)} file(s).")


if __name__ == "__main__":
    main()
