"""Create a test-mode Payment Link, so a real charge can be attempted.

Day-1 job (WORKPLAN.md), via the route that actually works on this account.

**Why not Subscriptions?** Razorpay Subscriptions is a separately-entitled
product and this account does not have it: `/v1/plans` and `/v1/subscriptions`
return 401 while `/v1/payments`, `/v1/orders` and `/v1/payment_links` return
200. Enabling it needs full account activation (KYC), which runs longer than
the time we have.

That costs us less than it looks. Day 1's goal was never "own a subscription" —
it was **lock the real payload schema**. A failed payment returns the same
`error.code` / `error.reason` / `error.step` fields whether it came from a
subscription cycle or a one-off link. Subscription *semantics* (mandate, cycle,
attempt number) are modelled in `recovery/models.py` and simulated; the payload
*shape* is grounded in a real capture from here.

Payment Links are used rather than raw Orders because Razorpay hosts the
payment page — we get a `short_url` to open in a browser instead of having to
embed and serve Checkout.js ourselves.

Run:
    .venv/Scripts/python.exe -m scripts.create_payment_link
    .venv/Scripts/python.exe -m scripts.create_payment_link --amount 49900
    .venv/Scripts/python.exe -m scripts.create_payment_link --list

Then open the printed URL and pay with a **failing** test card to produce a
`payment.failed` webhook. Make sure the webhook app and tunnel are running
first, or the payload is gone.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import httpx

from recovery.config import settings

API = "https://api.razorpay.com/v1"
STATE_FILE = Path(".razorpay_ids.json")

# A deliberately fake customer. Test mode never contacts anyone, and `notify`
# is switched off below as well, but there is no reason to put a real person's
# details into a payment record even in test mode.
#
# The contact avoids runs of repeated digits: Razorpay rejects numbers like
# +919999999999 with "Recurring digits in customer contact are disallowed".
TEST_CUSTOMER = {
    "name": "Test Customer",
    "email": "test.customer@example.com",
    "contact": "+919876543210",
}


def _auth() -> tuple[str, str]:
    if not settings.razorpay_configured:
        sys.exit("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set in .env")
    if not settings.razorpay_key_id.startswith("rzp_test"):
        sys.exit(
            f"Refusing to run: key id {settings.razorpay_key_id[:12]}... is not a "
            "test key. Switch the dashboard to Test Mode and regenerate."
        )
    return settings.razorpay_key_id, settings.razorpay_key_secret


def _load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def _save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def create_payment_link(amount_paise: int, *, reference: str | None) -> dict[str, Any]:
    """Create a hosted payment page for `amount_paise`."""
    body: dict[str, Any] = {
        "amount": amount_paise,
        "currency": "INR",
        "accept_partial": False,
        "description": "Revenue recovery agent — test-mode charge attempt",
        "customer": TEST_CUSTOMER,
        # Nothing is sent to anyone. We are about to fail these deliberately.
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
        "notes": {"project": "ai-revenue-recovery", "mode": "test"},
    }
    if reference:
        body["reference_id"] = reference

    response = httpx.post(f"{API}/payment_links", auth=_auth(), json=body, timeout=30)
    if response.status_code >= 400:
        sys.exit(f"Razorpay returned {response.status_code}: {response.text}")
    return response.json()


def list_payment_links() -> list[dict[str, Any]]:
    response = httpx.get(f"{API}/payment_links", auth=_auth(), timeout=30)
    if response.status_code >= 400:
        sys.exit(f"Razorpay returned {response.status_code}: {response.text}")
    return response.json().get("payment_links", [])


def _print_links(links: list[dict[str, Any]]) -> None:
    if not links:
        print("No payment links yet.")
        return
    for link in links:
        print(
            f"  {link.get('id'):<24} {str(link.get('status')):<10} "
            f"{link.get('amount')} paise   {link.get('short_url')}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--amount",
        type=int,
        default=49_900,
        help="Amount in paise (default 49900 = Rs 499).",
    )
    parser.add_argument(
        "--reference",
        default=None,
        help="Your own reference id, echoed back on the payment.",
    )
    parser.add_argument(
        "--list", action="store_true", help="List existing links and exit."
    )
    args = parser.parse_args()

    if args.list:
        _print_links(list_payment_links())
        return

    link = create_payment_link(args.amount, reference=args.reference)

    state = _load_state()
    state.setdefault("payment_link_ids", []).append(link["id"])
    state["latest_payment_link_id"] = link["id"]
    _save_state(state)

    print(f"Created payment link {link['id']}  status={link.get('status')}")
    print(f"Saved ids to {STATE_FILE}")
    print()
    print("BEFORE opening the link, make sure both of these are running:")
    print("  uvicorn recovery.webhook:app --reload --port 8000")
    print("  cloudflared tunnel --url http://localhost:8000")
    print("...and that the tunnel URL is registered as a webhook in the dashboard")
    print("with payment.failed and payment.captured subscribed.")
    print()
    print("NOW open this and pay:")
    print(f"  {link.get('short_url')}")
    print()
    print("Use a FAILING test card to produce the payload we actually need.")
    print("Razorpay's current test card list is at:")
    print("  https://razorpay.com/docs/payments/payments/test-card-details/")
    print()
    print("Then check what landed:")
    print("  .venv/Scripts/python.exe -m recovery.fixtures")


if __name__ == "__main__":
    main()
