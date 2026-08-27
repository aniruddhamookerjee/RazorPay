"""Rule-first classifier, with a local model only for what the rules miss.

Order of resort, and the reasoning behind it:

1. **Rule table** (`taxonomy.py`) — deterministic, auditable, instant. Every
   documented Razorpay reason resolves here.
2. **Local model** — only for strings the table has never seen: free text, a
   new code Razorpay added, a mangled value.
3. **`UNKNOWN`** — when the model is unavailable, too slow, or unsure.

**Why the model is kept on a short leash.** Measured on this build's
`qwen2.5:7b-instruct` (28 Aug), structured output was reliable — 8/8 responses
were valid enum members — but the *judgements* were not:

    "the card has been reported stolen"  ->  card_expired      (should be hard_decline)
    "payment_failed"                     ->  insufficient_funds (should be unknown)

The second is the dangerous one: handed a meaningless generic string, the model
confidently guessed the most common cause instead of admitting it did not know.
A wrong cause here becomes a wrong action on real money three layers down.

So: the prompt states explicitly that `unknown` is a correct and preferred
answer; `payment_failed` and friends never reach the model at all (they resolve
to `UNKNOWN` in the table); and every model answer is recorded with
`ClassificationSource.LLM` so the audit trail shows which diagnoses were guessed
rather than looked up.

**Cost.** ~6s per call locally, so results are cached by reason string. Unique
strings in a batch number in the dozens; events number in the tens of thousands.
No model call ever happens inside the experiment loop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx

from recovery.config import settings
from recovery.diagnosis.taxonomy import cause_for_reason
from recovery.models import Cause, ClassificationSource, FailureEvent

_ENUM = [c.value for c in Cause]

_SCHEMA = {
    "type": "object",
    "properties": {"cause": {"type": "string", "enum": _ENUM}},
    "required": ["cause"],
}

_SYSTEM = """You classify payment failure reasons for an Indian payment gateway.

Answer with exactly one cause from the allowed list.

Definitions:
- insufficient_funds: money not available right now (low balance, limit hit)
- card_expired: the card or credit line has lapsed and must be replaced
- mandate_expired: standing debit permission has lapsed
- mandate_revoked: standing debit permission was refused or withdrawn
- bank_unavailable: the bank, issuer or gateway is down or erroring
- network_timeout: no clear answer came back in time
- hard_decline: a firm, permanent refusal (fraud, blocked or stolen card,
  restricted or ineligible account, risk check failed)
- authentication_required: the customer must authenticate (OTP, PIN, CVV, 3DS)
- unknown: the reason is generic, ambiguous, or does not clearly fit above

Rules:
- "unknown" is a correct and preferred answer when the reason is generic or you
  are not confident. Do not guess the most common cause.
- A stolen, blocked or fraud-flagged card is hard_decline, never card_expired.
- Answer only with the JSON object."""


@dataclass
class Classifier:
    """Classifies failure events. Rules first, model only as a fallback."""

    use_model: bool = True
    _cache: dict[str, tuple[Cause, ClassificationSource]] = field(
        default_factory=dict, repr=False
    )
    model_calls: int = 0
    model_failures: int = 0

    # -- one reason string -------------------------------------------------

    def classify_reason(self, reason: str | None) -> tuple[Cause, ClassificationSource]:
        """Return (cause, how we got there) for one reason string."""
        if not reason:
            return Cause.UNKNOWN, ClassificationSource.UNCLASSIFIED

        key = reason.strip().lower()
        if key in self._cache:
            return self._cache[key]

        mapped = cause_for_reason(key)
        if mapped is not None:
            result = (mapped, ClassificationSource.RULE)
        elif self.use_model:
            result = (self._ask_model(reason), ClassificationSource.LLM)
        else:
            result = (Cause.UNKNOWN, ClassificationSource.UNCLASSIFIED)

        self._cache[key] = result
        return result

    def _ask_model(self, reason: str) -> Cause:
        """Ask the local model. Any failure degrades to UNKNOWN, never raises.

        A hung or missing model must not take down a 10,000-event batch — the
        rule table already covers every documented string, so losing the
        fallback costs coverage on novel strings only.
        """
        self.model_calls += 1
        try:
            response = httpx.post(
                f"{settings.ollama_host}/api/chat",
                timeout=settings.llm_timeout_seconds,
                json={
                    "model": settings.llm_model,
                    "messages": [
                        {"role": "system", "content": _SYSTEM},
                        {"role": "user", "content": f"Payment failure reason: {reason!r}"},
                    ],
                    "format": _SCHEMA,
                    "stream": False,
                    "options": {"temperature": 0},
                },
            )
            response.raise_for_status()
            value = json.loads(response.json()["message"]["content"])["cause"]
            return Cause(value)
        except Exception:
            self.model_failures += 1
            return Cause.UNKNOWN

    # -- events ------------------------------------------------------------

    def classify(self, event: FailureEvent) -> FailureEvent:
        """Return a copy of `event` carrying a cause and its source.

        Confidence here is diagnostic confidence only — how sure we are of the
        *label*. Pattern detection sharpens it later; the decision layer reads
        it, but never reads how the outcome was generated.
        """
        cause, source = self.classify_reason(event.error_reason)

        if source is ClassificationSource.RULE:
            confidence = 0.95 if cause is not Cause.UNKNOWN else 0.30
        elif source is ClassificationSource.LLM:
            # Deliberately capped below the rule path: a model answer is a
            # guess with a schema around it, and the ledger should show that.
            confidence = 0.60 if cause is not Cause.UNKNOWN else 0.20
        else:
            confidence = 0.0

        return event.model_copy(
            update={"cause": cause, "classified_by": source, "confidence": confidence}
        )

    def classify_all(self, events: tuple[FailureEvent, ...]) -> tuple[FailureEvent, ...]:
        return tuple(self.classify(e) for e in events)

    @property
    def stats(self) -> dict[str, int]:
        return {
            "unique_reasons": len(self._cache),
            "model_calls": self.model_calls,
            "model_failures": self.model_failures,
        }


_default = Classifier()


def classify_reason(reason: str | None) -> tuple[Cause, ClassificationSource]:
    """Module-level convenience using a shared cache."""
    return _default.classify_reason(reason)
