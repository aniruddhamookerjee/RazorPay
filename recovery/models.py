"""Typed contracts shared by every layer.

One event's journey through the system:

    ChargeAttempt  -> what we tried to collect (and what happened)
    FailureEvent   -> a failed attempt, plus its diagnosed cause
    Decision       -> the ranked action set with EV per action, and what won
    LedgerEntry    -> the append-only audit row: decision + outcome + money

Amounts are integer **paise** everywhere, matching Razorpay's API. Floats are
for probabilities and expected values only, never for a booked amount.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


class Base(BaseModel):
    """Frozen by default: nothing in the audit path is mutated in place."""

    model_config = ConfigDict(frozen=True, extra="forbid", use_enum_values=False)


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------


class AttemptStatus(str, Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Cause(str, Enum):
    """Diagnosed reason a charge failed (Day 3 taxonomy).

    Kept deliberately small: each cause must imply a *different* best action,
    otherwise it earns nothing and belongs in an existing bucket.
    """

    INSUFFICIENT_FUNDS = "insufficient_funds"
    CARD_EXPIRED = "card_expired"
    MANDATE_EXPIRED = "mandate_expired"
    MANDATE_REVOKED = "mandate_revoked"
    BANK_UNAVAILABLE = "bank_unavailable"
    NETWORK_TIMEOUT = "network_timeout"
    HARD_DECLINE = "hard_decline"
    AUTHENTICATION_REQUIRED = "authentication_required"
    UNKNOWN = "unknown"


class ClassificationSource(str, Enum):
    """How the cause was arrived at. Every event carries this."""

    RULE = "rule"
    LLM = "llm"
    UNCLASSIFIED = "unclassified"


class Action(str, Enum):
    """The action set the EV policy scores over (Day 4)."""

    RETRY_NOW = "retry_now"
    RETRY_DELAYED = "retry_delayed"
    REQUEST_REAUTH = "request_reauth"
    NOTIFY = "notify"
    WAIT = "wait"
    STOP = "stop"


class DecisionStatus(str, Enum):
    """Why the chosen action is what it is.

    `BLOCKED` and `STOPPED` are logged, never silently dropped — the compliance
    panel in the dashboard is built entirely out of these rows.
    """

    EXECUTED = "executed"
    BLOCKED = "blocked"
    STOPPED = "stopped"


class Arm(str, Enum):
    """Experiment arm (Day 6). Paired replay: all arms see identical events."""

    NAIVE = "naive"
    FIXED_3X = "fixed_3x"
    AGENT = "agent"


# --------------------------------------------------------------------------
# Layer 1 — event
# --------------------------------------------------------------------------


class ChargeAttempt(Base):
    """One attempt to collect one recurring charge."""

    attempt_id: str = Field(default_factory=lambda: _new_id("att"))
    subscription_id: str
    customer_id: str
    mandate_id: str | None = None

    amount_paise: int = Field(gt=0)
    currency: str = "INR"

    attempt_number: int = Field(default=1, ge=1)
    status: AttemptStatus = AttemptStatus.PENDING

    # Real wall-clock time and simulated time are both kept: the live subset
    # runs in real time while the batch replays a 7-day window in seconds.
    created_at: datetime
    virtual_time: datetime

    # Segment dimensions for pattern detection (Day 3 Part B).
    bank: str | None = None
    card_network: str | None = None
    method: str | None = None

    is_live: bool = False
    razorpay_payment_id: str | None = None


class FailureEvent(Base):
    """A failed `ChargeAttempt` plus its diagnosis.

    The raw `error_code` / `error_reason` are kept verbatim: the ledger has to
    show the original text next to the cause we mapped it onto.
    """

    event_id: str = Field(default_factory=lambda: _new_id("evt"))
    attempt: ChargeAttempt

    error_code: str | None = None
    error_reason: str | None = None
    error_description: str | None = None

    cause: Cause = Cause.UNKNOWN
    classified_by: ClassificationSource = ClassificationSource.UNCLASSIFIED
    # Confidence in the *diagnosis*, sharpened by pattern detection when a
    # segment is significant after Benjamini-Hochberg correction.
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    segment: str | None = None

    occurred_at: datetime
    virtual_time: datetime

    # The webhook payload this was built from, for replay and schema checks.
    raw_payload: dict[str, Any] | None = None


# --------------------------------------------------------------------------
# Layer 3 — decision
# --------------------------------------------------------------------------


class ActionScore(Base):
    """One row of the EV table shown per decision in the UI.

    EV(action) = p_success * amount - attempt_cost - p_churn * ltv
    """

    action: Action
    expected_value_paise: float
    p_success: float = Field(ge=0.0, le=1.0)
    cost_paise: float = 0.0
    churn_penalty_paise: float = 0.0
    delay_hours: int = 0
    # Set when the compliance gate rejects an otherwise-scored action.
    blocked_by: str | None = None
    rationale: str | None = None


class Decision(Base):
    """The policy's output for one `FailureEvent`."""

    decision_id: str = Field(default_factory=lambda: _new_id("dec"))
    event_id: str

    chosen_action: Action
    status: DecisionStatus = DecisionStatus.EXECUTED
    # Which stopping rule or compliance rule fired, when one did.
    stop_reason: str | None = None

    # Every scored action, not just the winner — the whole table is auditable.
    scores: tuple[ActionScore, ...] = ()

    scheduled_for: datetime | None = None
    decided_at: datetime
    virtual_time: datetime

    arm: Arm = Arm.AGENT
    policy_version: str = "0.1.0"


# --------------------------------------------------------------------------
# Layer 4 — audit
# --------------------------------------------------------------------------


class LedgerEntry(Base):
    """Append-only audit row. Never updated, never deleted.

    Carries the full trail for one decision: the original error, the cause we
    diagnosed, the EV table, what we did (or why we did not), and the money.
    """

    entry_id: str = Field(default_factory=lambda: _new_id("led"))
    seq: int | None = None

    event_id: str
    decision_id: str
    subscription_id: str
    customer_id: str

    cause: Cause
    classified_by: ClassificationSource
    confidence: float = Field(ge=0.0, le=1.0)
    error_reason: str | None = None

    action: Action
    status: DecisionStatus
    stop_reason: str | None = None
    scores: tuple[ActionScore, ...] = ()

    attempt_number: int = Field(ge=1)
    amount_paise: int = Field(gt=0)
    recovered_paise: int = 0
    cost_paise: int = 0

    outcome: AttemptStatus = AttemptStatus.PENDING
    razorpay_payment_id: str | None = None
    # Guards against a mid-run crash double-charging anyone (Day 5).
    idempotency_key: str | None = None

    arm: Arm = Arm.AGENT
    run_id: str | None = None
    seed: int | None = None
    cycle: int = 0

    is_live: bool = False
    recorded_at: datetime
    virtual_time: datetime

    narration: str | None = None

    @property
    def net_paise(self) -> int:
        """Net recovery — the honest headline number (WORKPLAN.md 4.1)."""
        return self.recovered_paise - self.cost_paise
