"""FastAPI app that receives Razorpay webhooks.

Day-1 job (WORKPLAN.md): stand this up behind a `cloudflared` tunnel, point a
Razorpay test-mode webhook at `/webhook`, and capture one real `payment.failed`
and one `subscription.charged` payload into `fixtures/`. Those files are the
schema ground truth the batch generator must match — the variety comes from the
generator, the *shape* comes from here.

Run:
    uvicorn recovery.webhook:app --reload --port 8000
    cloudflared tunnel --url http://localhost:8000
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request

from recovery.config import settings

app = FastAPI(
    title="Revenue Recovery — webhook capture",
    version="0.1.0",
    description="Receives Razorpay test-mode webhooks and archives raw payloads.",
)
router = APIRouter()


def verify_signature(body: bytes, signature: str | None) -> bool:
    """Razorpay signs the raw body with the webhook secret (HMAC-SHA256).

    Verification is skipped only when no secret is configured, so a payload can
    still be captured before the dashboard webhook is fully wired up.
    """
    if not settings.razorpay_webhook_secret:
        return True
    if not signature:
        return False
    expected = hmac.new(
        settings.razorpay_webhook_secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def archive_payload(event: str, payload: dict[str, Any]) -> str:
    """Write the raw payload to `fixtures/` and return the filename."""
    settings.fixtures_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_event = event.replace(".", "_") or "unknown"
    path = settings.fixtures_dir / f"{safe_event}__{stamp}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path.name


@router.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "env": settings.env,
        "razorpay_configured": settings.razorpay_configured,
        "signature_verification": bool(settings.razorpay_webhook_secret),
    }


@router.post("/webhook")
async def webhook(
    request: Request,
    x_razorpay_signature: str | None = Header(default=None),
    x_razorpay_event_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Verify, archive, acknowledge.

    Nothing is processed inline: Razorpay retries on a non-2xx, so this handler
    stays trivial and the pipeline reads from `fixtures/` separately.
    """
    body = await request.body()

    if not verify_signature(body, x_razorpay_signature):
        raise HTTPException(status_code=400, detail="invalid signature")

    try:
        payload: dict[str, Any] = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="malformed JSON") from exc

    event = payload.get("event", "unknown")
    filename = archive_payload(event, payload)

    return {
        "received": True,
        "event": event,
        "event_id": x_razorpay_event_id,
        "fixture": filename,
    }


app.include_router(router)
