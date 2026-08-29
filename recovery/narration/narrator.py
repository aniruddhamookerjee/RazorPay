"""Turning ledger rows into an explanation a person can read.

The ledger is complete but not legible: a row carries nine scored actions, two
timestamps, a confidence and a stop reason. That is the right thing to store and
the wrong thing to show. This module renders one case's history as prose.

**The deterministic renderer is the primary path, not the fallback.** It is
built first and always runs; the model is offered the same facts and asked to
make them read better. That ordering matters for an audit trail — a narration
that hallucinated a reason the agent did not have would be worse than no
narration at all, so the facts are assembled from the ledger and the model is
never the source of them.

Anything the model returns is checked against the facts before it is used. If it
introduces a number that is not in the row, it is discarded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import httpx

from recovery.config import settings
from recovery.models import Action, AttemptStatus, DecisionStatus, LedgerEntry

_ACTION_PHRASES = {
    Action.RETRY_NOW: "retried the charge immediately",
    Action.RETRY_DELAYED: "scheduled another attempt",
    Action.REQUEST_REAUTH: "asked the customer to re-authorise",
    Action.NOTIFY: "notified the customer",
    Action.WAIT: "waited",
    Action.STOP: "stopped",
}

_STOP_PHRASES = {
    "max_attempts_reached": "the attempt limit was reached",
    "max_contacts_reached": "the messaging limit was reached",
    "recovery_window_expired": "the seven-day recovery window closed",
    "hard_decline": "the bank declined the payment outright, so further attempts would be wasted",
    "customer_opted_out": "the customer opted out",
    "no_action_with_positive_value": "no remaining action was worth more than it would cost",
    "schedule_exhausted": "the fixed schedule ran out of retries",
}

_BLOCK_PHRASES = {
    "no_active_mandate": "there was no active mandate to debit against",
    "afa_required_above_threshold": "the amount needed additional authentication",
    "pre_debit_notice_not_sent": "the required 24-hour notice had not been sent",
    "pre_debit_notice_too_recent": "the 24-hour notice period had not elapsed",
    "quiet_hours": "it fell inside customer quiet hours",
    "no_messaging_consent": "the customer had not consented to messaging",
    "customer_opted_out": "the customer had opted out",
    "exceeds_mandate_maximum": "it exceeded the mandate's agreed ceiling",
    "mandate_debit_limit_reached": "the mandate's debit limit for the period was used up",
}


def facts(entries: list[LedgerEntry]) -> dict[str, object]:
    """Everything true about one case, pulled from the ledger and nowhere else."""
    if not entries:
        return {}

    first = entries[0]
    recovered = sum(e.recovered_paise for e in entries)
    cost = sum(e.cost_paise for e in entries)
    blocked: dict[str, int] = {}
    for entry in entries:
        for score in entry.scores:
            if score.blocked_by:
                blocked[score.blocked_by] = blocked.get(score.blocked_by, 0) + 1

    return {
        "subscription_id": first.subscription_id,
        "amount_paise": first.amount_paise,
        "error_reason": first.error_reason,
        "cause": first.cause.value,
        "classified_by": first.classified_by.value,
        "confidence": first.confidence,
        "segment": None,
        "actions": [e.action.value for e in entries],
        "stop_reason": next(
            (e.stop_reason for e in reversed(entries) if e.stop_reason), None
        ),
        "recovered_paise": recovered,
        "cost_paise": cost,
        "succeeded": any(e.outcome is AttemptStatus.SUCCEEDED for e in entries),
        "blocked": blocked,
    }


def render(entries: list[LedgerEntry]) -> str:
    """A readable account of one case, built only from what the ledger says."""
    if not entries:
        return "No actions recorded."

    f = facts(entries)
    amount = f["amount_paise"] / 100  # type: ignore[operator]
    lines: list[str] = []

    lines.append(
        f"A charge of Rs {amount:,.0f} failed with the reason "
        f"'{f['error_reason']}', which we diagnosed as {f['cause'].replace('_', ' ')} "  # type: ignore[union-attr]
        f"(by {f['classified_by']}, confidence {f['confidence']:.0%})."
    )

    taken = [
        e for e in entries if e.action not in (Action.STOP, Action.WAIT)
    ]
    if taken:
        steps = ", then ".join(_ACTION_PHRASES[e.action] for e in taken)
        lines.append(f"We {steps}.")
    else:
        lines.append("We took no action.")

    blocked = f["blocked"]
    if blocked:  # type: ignore[truthy-bool]
        reasons = [
            _BLOCK_PHRASES.get(reason, reason.replace("_", " "))
            for reason in list(blocked)[:2]  # type: ignore[arg-type]
        ]
        lines.append(
            "Some options were unavailable because " + " and ".join(reasons) + "."
        )

    if f["succeeded"]:
        lines.append(
            f"The charge was recovered: Rs {f['recovered_paise'] / 100:,.0f} "  # type: ignore[operator]
            f"collected against Rs {f['cost_paise'] / 100:,.2f} of cost."
        )
    else:
        stop = f["stop_reason"]
        why = _STOP_PHRASES.get(stop, stop.replace("_", " ")) if stop else "the case ended"  # type: ignore[union-attr,arg-type]
        lines.append(
            f"Nothing was recovered and we stopped because {why}. "
            f"Rs {f['cost_paise'] / 100:,.2f} was spent trying."  # type: ignore[operator]
        )

    return " ".join(lines)


_SYSTEM = """You rewrite a payment-recovery audit note so a non-technical \
colleague can read it.

Rules:
- Use ONLY the facts given. Never add a number, reason or action that is not there.
- 2 to 4 sentences. Plain English. No jargon, no bullet points, no headings.
- Keep every rupee figure exactly as given.
- Output only the rewritten note."""


@dataclass
class Narrator:
    """Deterministic rendering first; the model only makes it read better."""

    use_model: bool = True
    model_calls: int = 0
    rejections: int = 0
    _cache: dict[str, str] = field(default_factory=dict, repr=False)

    def narrate(self, entries: list[LedgerEntry]) -> str:
        deterministic = render(entries)
        if not self.use_model or not entries:
            return deterministic

        polished = self._ask_model(deterministic)
        return polished or deterministic

    def _ask_model(self, note: str) -> str | None:
        if note in self._cache:
            return self._cache[note]

        self.model_calls += 1
        try:
            response = httpx.post(
                f"{settings.ollama_host}/api/chat",
                timeout=settings.llm_timeout_seconds,
                json={
                    "model": settings.llm_model,
                    "messages": [
                        {"role": "system", "content": _SYSTEM},
                        {"role": "user", "content": note},
                    ],
                    "stream": False,
                    "options": {"temperature": 0.2},
                },
            )
            response.raise_for_status()
            text = response.json()["message"]["content"].strip().strip('"')
        except Exception:
            return None

        if not self._is_faithful(note, text):
            self.rejections += 1
            return None

        self._cache[note] = text
        return text

    @staticmethod
    def _is_faithful(source: str, candidate: str) -> bool:
        """Reject anything that invents a number the ledger did not contain.

        A narration is part of an audit trail. A fluent sentence with a wrong
        figure in it is worse than a clumsy sentence with the right one.
        """
        if not candidate or len(candidate) > 1_200:
            return False

        def numbers(text: str) -> set[str]:
            return {n.replace(",", "") for n in re.findall(r"\d[\d,]*\.?\d*", text)}

        return numbers(candidate) <= numbers(source)

    @property
    def stats(self) -> dict[str, int]:
        return {
            "model_calls": self.model_calls,
            "rejections": self.rejections,
            "cached": len(self._cache),
        }
