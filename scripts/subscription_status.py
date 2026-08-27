"""Inspect a test-mode subscription and its invoices.

Day-1 companion to `setup_subscription.py`. Once the mandate is authorised you
need to watch state change — has it gone `active`, is there an invoice waiting,
did the charge land — and clicking around the dashboard for that is slow.

Deliberately read-only. Triggering the charge itself stays a dashboard action
("Charge this now" on the pending invoice): Razorpay exposes that as a UI
affordance and this script will not pretend to an API that may not behave the
way we assume. Read state here, press the button there.

Run:
    .venv/Scripts/python.exe -m scripts.subscription_status
    .venv/Scripts/python.exe -m scripts.subscription_status --id sub_xxxxxxxxxxxx
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import razorpay

from recovery.config import settings

STATE_FILE = Path(".razorpay_ids.json")

# Fields worth seeing at a glance; the full object is available with --raw.
SUBSCRIPTION_FIELDS = (
    "id",
    "status",
    "plan_id",
    "paid_count",
    "remaining_count",
    "total_count",
    "current_start",
    "current_end",
    "charge_at",
    "short_url",
)
INVOICE_FIELDS = ("id", "status", "amount", "amount_paid", "amount_due", "payment_id")


def _client() -> razorpay.Client:
    if not settings.razorpay_configured:
        sys.exit("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set in .env")
    if not settings.razorpay_key_id.startswith("rzp_test"):
        sys.exit("Refusing to run against non-test keys.")
    return razorpay.Client(
        auth=(settings.razorpay_key_id, settings.razorpay_key_secret)
    )


def _resolve_subscription_id(explicit: str | None) -> str:
    if explicit:
        return explicit
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        saved = state.get("latest_subscription_id")
        if saved:
            return saved
    sys.exit(
        "No subscription id given and none saved. Run scripts.setup_subscription "
        "first, or pass --id sub_xxxxxxxxxxxx"
    )


def _summarise(obj: dict[str, Any], fields: tuple[str, ...]) -> None:
    width = max(len(f) for f in fields)
    for field in fields:
        if field in obj:
            print(f"  {field:<{width}}  {obj[field]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", dest="subscription_id", default=None)
    parser.add_argument(
        "--raw", action="store_true", help="Dump the full JSON objects."
    )
    args = parser.parse_args()

    client = _client()
    subscription_id = _resolve_subscription_id(args.subscription_id)

    subscription = client.subscription.fetch(subscription_id)
    print(f"SUBSCRIPTION {subscription_id}")
    if args.raw:
        print(json.dumps(subscription, indent=2, sort_keys=True))
    else:
        _summarise(subscription, SUBSCRIPTION_FIELDS)

    status = subscription.get("status")
    if status == "created":
        print()
        print("  Not authorised yet — open the short_url above and complete it")
        print("  with a Razorpay test card. Nothing is chargeable until then.")

    invoices = client.invoice.all({"subscription_id": subscription_id})
    items = invoices.get("items", [])
    print()
    print(f"INVOICES ({len(items)})")
    if not items:
        print("  none yet")
    for invoice in items:
        print()
        if args.raw:
            print(json.dumps(invoice, indent=2, sort_keys=True))
        else:
            _summarise(invoice, INVOICE_FIELDS)

    pending = [i for i in items if i.get("status") in {"issued", "partially_paid"}]
    if pending:
        print()
        print("NEXT — in the Razorpay dashboard, open the pending invoice and use")
        print('  "Charge this now" to trigger a real charge attempt. Make sure the')
        print("  webhook app and tunnel are already running so the payload lands in")
        print("  fixtures/.")


if __name__ == "__main__":
    main()
