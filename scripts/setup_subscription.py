"""Create a Razorpay **test-mode** plan + subscription, ready to authorise.

Day-1 job (WORKPLAN.md): we need one real subscription so that charging it
produces genuine webhook payloads. Those payloads are the schema ground truth
for the batch generator — the *shape* comes from here, the variety comes from
the generator.

Doing this from a script rather than the dashboard means it is reproducible and
re-runnable when the first subscription gets into a state you would rather
abandon than untangle.

Run:
    .venv/Scripts/python.exe -m scripts.setup_subscription
    .venv/Scripts/python.exe -m scripts.setup_subscription --amount 49900 --period daily

Then open the printed `short_url` in a browser and authorise the mandate with a
Razorpay test card. Nothing is chargeable until that happens.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import razorpay

from recovery.config import settings

# Written next to the repo so a re-run can reuse the plan instead of piling up
# near-identical ones. Git-ignored: these ids are per-developer, not shared.
STATE_FILE = Path(".razorpay_ids.json")


def _client() -> razorpay.Client:
    """Build the SDK client, refusing anything that is not test mode.

    The single genuinely bad outcome available on Day 1 is running this against
    live keys, so the guard is a hard exit rather than a warning.
    """
    if not settings.razorpay_configured:
        sys.exit("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set in .env")

    if not settings.razorpay_key_id.startswith("rzp_test"):
        sys.exit(
            f"Refusing to run: key id {settings.razorpay_key_id[:12]}... is not a "
            "test key. Switch the Razorpay dashboard to Test Mode and regenerate."
        )

    return razorpay.Client(
        auth=(settings.razorpay_key_id, settings.razorpay_key_secret)
    )


def _load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def _save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def create_plan(
    client: razorpay.Client, *, amount_paise: int, period: str, interval: int
) -> dict[str, Any]:
    """Create a billing plan.

    `period="daily"` is the default on purpose: a monthly plan makes every
    repeat-charge experiment a month-long wait, which we do not have.
    """
    return client.plan.create(
        {
            "period": period,
            "interval": interval,
            "item": {
                "name": f"Recovery test plan ({amount_paise} paise / {period})",
                "amount": amount_paise,
                "currency": "INR",
                "description": "Test-mode plan for the revenue recovery agent.",
            },
            "notes": {"project": "ai-revenue-recovery", "mode": "test"},
        }
    )


def create_subscription(
    client: razorpay.Client, *, plan_id: str, total_count: int
) -> dict[str, Any]:
    """Create a subscription against `plan_id`.

    `customer_notify=0` keeps Razorpay from emailing the test customer on every
    charge attempt — we are about to fail these deliberately and repeatedly.
    """
    return client.subscription.create(
        {
            "plan_id": plan_id,
            "total_count": total_count,
            "quantity": 1,
            "customer_notify": 0,
            "notes": {"project": "ai-revenue-recovery", "mode": "test"},
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--amount",
        type=int,
        default=49_900,
        help="Charge amount in paise (default 49900 = Rs 499).",
    )
    parser.add_argument(
        "--period",
        default="daily",
        choices=["daily", "weekly", "monthly", "yearly"],
        help="Billing period (default daily, so charges come round quickly).",
    )
    parser.add_argument("--interval", type=int, default=1)
    parser.add_argument(
        "--total-count",
        type=int,
        default=12,
        help="How many cycles the subscription runs for.",
    )
    parser.add_argument(
        "--new-plan",
        action="store_true",
        help="Force a fresh plan instead of reusing the saved one.",
    )
    args = parser.parse_args()

    client = _client()
    state = _load_state()

    plan_id = state.get("plan_id")
    if plan_id and not args.new_plan:
        print(f"Reusing saved plan {plan_id} (pass --new-plan to force a new one)")
    else:
        plan = create_plan(
            client,
            amount_paise=args.amount,
            period=args.period,
            interval=args.interval,
        )
        plan_id = plan["id"]
        state["plan_id"] = plan_id
        print(f"Created plan {plan_id}  ({args.amount} paise / {args.period})")

    subscription = create_subscription(
        client, plan_id=plan_id, total_count=args.total_count
    )

    state.setdefault("subscription_ids", []).append(subscription["id"])
    state["latest_subscription_id"] = subscription["id"]
    _save_state(state)

    print(f"Created subscription {subscription['id']}  status={subscription['status']}")
    print(f"Saved ids to {STATE_FILE}")
    print()
    print("NEXT — authorise the mandate (nothing is chargeable until you do):")
    print(f"  {subscription.get('short_url')}")
    print()
    print("Use a Razorpay test card on that page. Then check status with:")
    print("  .venv/Scripts/python.exe -m scripts.subscription_status")


if __name__ == "__main__":
    main()
