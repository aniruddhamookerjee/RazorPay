# fixtures/

Raw Razorpay **test-mode** webhook payloads, committed verbatim as the schema
ground truth for the batch generator (WORKPLAN.md, Day 1).

Captured by running the webhook app behind a tunnel and triggering a charge
from the Razorpay dashboard:

```
uvicorn recovery.webhook:app --reload --port 8000
cloudflared tunnel --url http://localhost:8000
```

Files are named `<event>__<UTC timestamp>.json`, e.g.
`payment_failed__20260827T141203Z.json`.

Needed before Day 2: one `payment.failed`.

**Source is Payment Links, not Subscriptions.** Subscriptions is not enabled on
this test account (`/v1/plans` -> 401). A failed payment carries the same
`error.code` / `error.reason` / `error.step` fields, which is all the diagnosis
layer reads. See `sources.md` section 6 for the full note.

Create a link to charge against:

```
.venv/Scripts/python.exe -m scripts.create_payment_link
```

Then open the printed `short_url` and pay with a **failing** test card.

Test mode only — no real customer data, ever.
