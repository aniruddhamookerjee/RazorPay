"""Carrying out a decision — for real, or in simulation.

Two backends behind one interface:

    SimulatedExecutor   resolves outcomes from an injected oracle
    LiveExecutor        makes real calls against Razorpay test mode

**Why the oracle is injected rather than imported.** The simulated executor
needs to know whether a retry succeeded, and that answer lives in
`recovery/simulation/`. Importing it here would put the answer key on the
execution path and break the holdout boundary. So the oracle is a callable
handed in by the experiment harness, which is allowed to know. This module
never learns where outcomes come from.

**Idempotency.** Every execution carries a key derived from
`(subscription, cycle, attempt)`. The ledger has a unique constraint on it, so a
crash mid-run followed by a re-run cannot charge anyone twice — the second write
is rejected by the database rather than by a check someone remembered to write.

**Live safety.** `LiveExecutor` makes real API calls, and a bug that fans out
across 10,000 events would be the worst outcome available in this project. Four
independent guards, each sufficient on its own:

1. the event must carry `is_live` — set on ~100 of 10,000 by the generator
2. the key must start `rzp_test`
3. a hard per-run call cap
4. `enabled=False` by default, so firing live is always a deliberate act
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

import httpx

from recovery.config import settings
from recovery.models import Action, AttemptStatus, FailureEvent

API = "https://api.razorpay.com/v1"

# Actions that move money or reach a customer. WAIT and STOP are absent by
# design: they are decisions not to act, and executing them is a no-op that
# still gets a ledger row.
_EFFECTFUL = {
    Action.RETRY_NOW,
    Action.RETRY_DELAYED,
    Action.REQUEST_REAUTH,
    Action.NOTIFY,
}


def idempotency_key(*, subscription_id: str, cycle: int, attempt_number: int) -> str:
    """Deterministic, so a replay collides with itself instead of double-charging."""
    return f"{subscription_id}:c{cycle}:a{attempt_number}"


@dataclass(frozen=True)
class ExecutionResult:
    outcome: AttemptStatus
    recovered_paise: int = 0
    cost_paise: int = 0
    razorpay_payment_id: str | None = None
    idempotency_key: str | None = None
    is_live: bool = False
    detail: str | None = None


class Executor(Protocol):
    def execute(
        self,
        *,
        event: FailureEvent,
        action: Action,
        attempted_at: datetime,
        attempt_number: int,
        cycle: int,
        cost_paise: int,
    ) -> ExecutionResult: ...


# Given (event, action, when, attempt) -> did it succeed? Supplied by the
# harness; this module deliberately does not know how it decides.
OutcomeOracle = Callable[[FailureEvent, Action, datetime, int], bool]


@dataclass
class SimulatedExecutor:
    """Executes against an injected oracle. Never touches the network."""

    oracle: OutcomeOracle

    def execute(
        self,
        *,
        event: FailureEvent,
        action: Action,
        attempted_at: datetime,
        attempt_number: int,
        cycle: int,
        cost_paise: int,
    ) -> ExecutionResult:
        key = idempotency_key(
            subscription_id=event.attempt.subscription_id,
            cycle=cycle,
            attempt_number=attempt_number,
        )

        if action not in _EFFECTFUL:
            # A decision not to act is still an execution: it produces a ledger
            # row, costs nothing, and recovers nothing.
            return ExecutionResult(
                outcome=AttemptStatus.PENDING,
                idempotency_key=key,
                detail=f"{action.value}: no action taken",
            )

        succeeded = self.oracle(event, action, attempted_at, attempt_number)

        # Only a debit collects money. A notification or re-auth request that
        # "succeeds" means the customer acted, which is modelled as collection
        # too — `definitions.md` §3 tags these separately as assisted recovery.
        return ExecutionResult(
            outcome=AttemptStatus.SUCCEEDED if succeeded else AttemptStatus.FAILED,
            recovered_paise=event.attempt.amount_paise if succeeded else 0,
            cost_paise=cost_paise,
            idempotency_key=key,
            detail=f"{action.value}: simulated",
        )


@dataclass
class LiveExecutor:
    """Executes against Razorpay test mode. Off unless explicitly enabled.

    A "retry" is a fresh Payment Link for the same amount rather than a
    subscription re-charge, because Subscriptions is not enabled on this account
    (`sources.md` §6). It is a weaker claim — "we retry through the Payments
    API" rather than "we re-charge a mandate" — but it is a real API call
    against real infrastructure, which is what the live subset exists to prove.
    """

    enabled: bool = False
    max_calls: int = field(default_factory=lambda: settings.live_subset_size)
    calls_made: int = 0
    refusals: dict[str, int] = field(default_factory=dict)

    def _refuse(self, reason: str, key: str) -> ExecutionResult:
        self.refusals[reason] = self.refusals.get(reason, 0) + 1
        return ExecutionResult(
            outcome=AttemptStatus.PENDING, idempotency_key=key, detail=f"refused: {reason}"
        )

    def execute(
        self,
        *,
        event: FailureEvent,
        action: Action,
        attempted_at: datetime,
        attempt_number: int,
        cycle: int,
        cost_paise: int,
    ) -> ExecutionResult:
        key = idempotency_key(
            subscription_id=event.attempt.subscription_id,
            cycle=cycle,
            attempt_number=attempt_number,
        )

        if not self.enabled:
            return self._refuse("live_executor_disabled", key)
        if not event.attempt.is_live:
            # The single most important guard: a simulated event must never
            # reach a real API. The generator flags ~100 of 10,000.
            return self._refuse("event_not_in_live_subset", key)
        if not settings.razorpay_key_id.startswith("rzp_test"):
            return self._refuse("not_a_test_key", key)
        if self.calls_made >= self.max_calls:
            return self._refuse("live_call_cap_reached", key)
        if action not in _EFFECTFUL:
            return ExecutionResult(
                outcome=AttemptStatus.PENDING,
                idempotency_key=key,
                detail=f"{action.value}: no action taken",
            )

        self.calls_made += 1

        body = {
            "amount": event.attempt.amount_paise,
            "currency": "INR",
            "accept_partial": False,
            "description": f"Recovery {action.value} attempt {attempt_number}",
            "customer": {
                "name": "Test Customer",
                "email": "test.customer@example.com",
                "contact": "+919876543210",
            },
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
            "reference_id": key,
            "notes": {
                "project": "ai-revenue-recovery",
                "idempotency_key": key,
                "cause": event.cause.value,
                "action": action.value,
            },
        }

        try:
            response = httpx.post(
                f"{API}/payment_links",
                auth=(settings.razorpay_key_id, settings.razorpay_key_secret),
                json=body,
                timeout=30,
            )
        except Exception as exc:  # network is not the agent's fault
            return ExecutionResult(
                outcome=AttemptStatus.FAILED,
                cost_paise=cost_paise,
                idempotency_key=key,
                is_live=True,
                detail=f"transport error: {type(exc).__name__}",
            )

        if response.status_code >= 400:
            # Razorpay rejects a duplicate reference_id, which is idempotency
            # working rather than an error worth panicking about.
            duplicate = "reference_id" in response.text
            return ExecutionResult(
                outcome=AttemptStatus.PENDING if duplicate else AttemptStatus.FAILED,
                cost_paise=0 if duplicate else cost_paise,
                idempotency_key=key,
                is_live=True,
                detail=(
                    "duplicate reference_id: already attempted"
                    if duplicate
                    else f"HTTP {response.status_code}: {response.text[:120]}"
                ),
            )

        created = response.json()
        # The link exists; whether the customer pays is not known synchronously.
        # PENDING is the honest status — the webhook resolves it later.
        return ExecutionResult(
            outcome=AttemptStatus.PENDING,
            cost_paise=cost_paise,
            razorpay_payment_id=created.get("id"),
            idempotency_key=key,
            is_live=True,
            detail=f"payment link created: {created.get('short_url')}",
        )
