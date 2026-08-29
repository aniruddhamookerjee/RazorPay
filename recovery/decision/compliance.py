"""Hard constraints. Checked before anything is scored, never traded off.

The track's bar asks for "compliant escalation" and "stopping rules". This is
where both live, and the ordering is deliberate: the gate runs **before** the EV
policy, so a forbidden action is never even ranked. An action cannot be
justified into existence by a large enough expected value — that is the
difference between a constraint and a preference.

Every rejection is returned with a reason and written to the ledger as
`DecisionStatus.BLOCKED`. Nothing is silently dropped: the compliance panel in
the dashboard is built entirely out of actions the agent *refused* to take, and
a gate that dropped them quietly would have nothing to show.

⚠ **THESE THRESHOLDS NEED HUMAN VERIFICATION.**

The values below come from secondary sources, not from RBI circulars read
directly. RBI has revised e-mandate rules more than once, and a wrong threshold
here is exactly the error a payments judge spots instantly. They are grouped in
one block for that reason. Before submission, check each against the actual
circular and update `sources.md` §5.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from recovery.config import settings
from recovery.models import Action, Cause

# ---------------------------------------------------------------------------
# ⚠ UNVERIFIED — check against RBI circulars before submission
# ---------------------------------------------------------------------------

# RBI e-mandate framework: the customer must be notified ahead of a recurring
# debit. Commonly cited as 24 hours.
PRE_DEBIT_NOTICE_HOURS = 24

# Above this amount an e-mandate debit requires additional factor
# authentication and cannot be auto-debited. Commonly cited as Rs 15,000.
# Read from settings so it is configurable without a code change.
AFA_THRESHOLD_PAISE = settings.afa_threshold_inr * 100

# Quiet hours for customer messaging. TRAI regulates commercial communication
# timing; treated here as a conservative do-not-disturb window.
QUIET_HOURS = range(21, 24), range(0, 8)

# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComplianceContext:
    """What the gate needs to know about a case to judge an action."""

    amount_paise: int
    attempts_so_far: int
    cause: Cause
    first_failure_at: datetime
    scheduled_for: datetime
    pre_debit_notice_sent_at: datetime | None = None
    customer_opted_out: bool = False
    messaging_consent: bool = True
    mandate_active: bool = True


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str | None = None

    @staticmethod
    def ok() -> "Verdict":
        return Verdict(True)

    @staticmethod
    def block(reason: str) -> "Verdict":
        return Verdict(False, reason)


def _in_quiet_hours(when: datetime) -> bool:
    return any(when.hour in window for window in QUIET_HOURS)


def check(action: Action, ctx: ComplianceContext) -> Verdict:
    """May we take this action? Reasons are machine-readable for the ledger."""

    # Doing nothing is always permitted. Worth stating explicitly: it means the
    # gate can never leave the policy with no legal action at all.
    if action in (Action.WAIT, Action.STOP):
        return Verdict.ok()

    # An opt-out ends everything except stopping. This outranks every other
    # rule, including a case that would otherwise be highly recoverable.
    if ctx.customer_opted_out:
        return Verdict.block("customer_opted_out")

    if action in (Action.NOTIFY, Action.REQUEST_REAUTH):
        if not ctx.messaging_consent:
            return Verdict.block("no_messaging_consent")
        if _in_quiet_hours(ctx.scheduled_for):
            return Verdict.block("quiet_hours")
        return Verdict.ok()

    # -- debit attempts ----------------------------------------------------

    if action in (Action.RETRY_NOW, Action.RETRY_DELAYED):
        if not ctx.mandate_active or ctx.cause in (
            Cause.MANDATE_EXPIRED,
            Cause.MANDATE_REVOKED,
        ):
            # Not merely futile — debiting without a live mandate is the
            # compliance failure this whole framework exists to prevent.
            return Verdict.block("no_active_mandate")

        if ctx.amount_paise > AFA_THRESHOLD_PAISE:
            return Verdict.block("afa_required_above_threshold")

        if ctx.pre_debit_notice_sent_at is None:
            return Verdict.block("pre_debit_notice_not_sent")

        notice_age = ctx.scheduled_for - ctx.pre_debit_notice_sent_at
        if notice_age < timedelta(hours=PRE_DEBIT_NOTICE_HOURS):
            return Verdict.block("pre_debit_notice_too_recent")

        return Verdict.ok()

    return Verdict.ok()


# ---------------------------------------------------------------------------
# Stopping rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StopVerdict:
    should_stop: bool
    reason: str | None = None


def should_stop(ctx: ComplianceContext) -> StopVerdict:
    """Has this case run out of road, regardless of what it might be worth?

    Checked before the policy scores anything, so no amount of expected value
    can push a case past its caps. Reasons are distinct so the audit trail can
    say *which* limit ended it.
    """
    if ctx.customer_opted_out:
        return StopVerdict(True, "customer_opted_out")

    if ctx.attempts_so_far >= settings.max_attempts:
        return StopVerdict(True, "max_attempts_reached")

    window = timedelta(days=settings.recovery_window_days)
    if ctx.scheduled_for - ctx.first_failure_at >= window:
        return StopVerdict(True, "recovery_window_expired")

    if ctx.cause is Cause.HARD_DECLINE:
        return StopVerdict(True, "hard_decline")

    return StopVerdict(False)
