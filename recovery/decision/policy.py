"""The EV policy — score every action, keep the whole table, pick the best.

    EV(action) = p_success x amount - direct_cost - churn_penalty

**Why this is not a lookup table.** A cause-to-action map is table stakes;
Razorpay already ships smart retries. What a table cannot do is give a
principled answer on the ambiguous cases, or explain *why* it declined the
alternatives. Here the familiar answers fall out of the arithmetic rather than
being hardcoded:

* a dead mandate has p ≈ 0 on retry, so re-auth outscores it without any rule
  saying "mandate expired means re-auth";
* a hard decline drives every attempt negative, so `stop` wins on its own;
* insufficient funds prefers a delayed retry once the agent has *observed* that
  waiting works — which it has to learn, since its priors are flat over delay.

**Order of operations**, and each step is load-bearing:

1. stopping rules — caps first, so nothing can be bought past them
2. compliance gate — per action, before scoring
3. score the survivors
4. pick the highest EV; `stop` is always in the running at exactly zero

Because `stop` scores zero and is never blocked, an action is only ever taken
when it beats doing nothing. That is what makes "we do not waste attempts" a
property of the design rather than a hope.

The full table is retained on the `Decision`, including blocked actions with
their reasons. A decision you cannot interrogate is not auditable.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from recovery.config import settings
from recovery.decision import compliance
from recovery.decision.compliance import ComplianceContext
from recovery.decision.costs import CostModel
from recovery.decision.priors import CANDIDATE_DELAYS_HOURS

# Contact actions get a shorter, denser delay ladder than debits. The point is
# only to step over a quiet-hours window, not to sit on a message for days —
# an unsent dunning message loses value fast.
CONTACT_DELAYS_HOURS: tuple[int, ...] = (0, 3, 6, 9, 12)
from recovery.decision.success import SuccessModel
from recovery.models import (
    Action,
    ActionScore,
    Arm,
    Cause,
    Decision,
    DecisionStatus,
    FailureEvent,
)

POLICY_VERSION = "0.2.0"


@dataclass
class Policy:
    """Cause-aware, cost-aware, compliance-bounded action selection."""

    success: SuccessModel
    costs: CostModel
    # When set, the policy may only retry (plus the pre-debit notice a retry
    # legally requires). Used for the retry-only arm, so the smart-retry gain
    # can be quoted against the published band without the extra channels
    # inflating it.
    retry_only: bool = False

    # -- scoring -----------------------------------------------------------

    def _score(
        self,
        *,
        action: Action,
        cause: Cause,
        amount_paise: int,
        attempts_so_far: int,
        delay_hours: int,
        ctx: ComplianceContext,
    ) -> ActionScore:
        if action is Action.STOP:
            # The baseline every other action must beat. Exactly zero: stopping
            # neither earns nor costs, and that is the whole point of it.
            # Returned before scoring — a belief about "do nothing" is meaningless
            # and this runs in the Day 6 hot loop.
            return ActionScore(
                action=Action.STOP,
                expected_value_paise=0.0,
                p_success=0.0,
                rationale="do nothing further; the bar every other action must clear",
            )

        verdict = compliance.check(action, ctx)

        belief = self.success.belief(
            cause=cause,
            action=action,
            delay_hours=delay_hours,
            attempt_number=attempts_so_far + 1,
        )
        breakdown = self.costs.cost_of(
            action,
            attempts_so_far=attempts_so_far,
            amount_paise=amount_paise,
        )

        expected = belief.mean * amount_paise - breakdown.total_paise

        if not verdict.allowed:
            # Scored anyway, then blocked. Showing what a forbidden action
            # would have been worth is the honest way to present a constraint:
            # it makes the cost of compliance visible instead of hiding it.
            return ActionScore(
                action=action,
                expected_value_paise=expected,
                p_success=belief.mean,
                cost_paise=breakdown.direct_paise,
                churn_penalty_paise=breakdown.churn_penalty_paise,
                delay_hours=delay_hours,
                blocked_by=verdict.reason,
                rationale=f"blocked: {verdict.reason}",
            )

        return ActionScore(
            action=action,
            expected_value_paise=expected,
            p_success=belief.mean,
            cost_paise=breakdown.direct_paise,
            churn_penalty_paise=breakdown.churn_penalty_paise,
            delay_hours=delay_hours,
            rationale=belief.describe(),
        )

    # -- decision ----------------------------------------------------------

    def decide(
        self,
        event: FailureEvent,
        *,
        now: datetime,
        attempts_so_far: int = 1,
        contacts_so_far: int = 0,
        first_failure_at: datetime | None = None,
        pre_debit_notice_sent_at: datetime | None = None,
        customer_opted_out: bool = False,
        messaging_consent: bool = True,
        mandate_active: bool = True,
        arm: Arm = Arm.AGENT,
    ) -> Decision:
        """Choose one bounded action, and record why every other was rejected."""
        first_failure_at = first_failure_at or event.occurred_at
        amount = event.attempt.amount_paise
        cause = event.cause

        base_ctx = ComplianceContext(
            amount_paise=amount,
            attempts_so_far=attempts_so_far,
            contacts_so_far=contacts_so_far,
            cause=cause,
            first_failure_at=first_failure_at,
            scheduled_for=now,
            pre_debit_notice_sent_at=pre_debit_notice_sent_at,
            customer_opted_out=customer_opted_out,
            messaging_consent=messaging_consent,
            mandate_active=mandate_active,
        )

        # 1. Caps first. Nothing can be bought past them.
        stop_verdict = compliance.should_stop(base_ctx)
        if stop_verdict.should_stop:
            return Decision(
                event_id=event.event_id,
                chosen_action=Action.STOP,
                status=DecisionStatus.STOPPED,
                stop_reason=stop_verdict.reason,
                scores=(
                    ActionScore(
                        action=Action.STOP,
                        expected_value_paise=0.0,
                        p_success=0.0,
                        rationale=f"stopped: {stop_verdict.reason}",
                    ),
                ),
                decided_at=now,
                virtual_time=now,
                arm=arm,
                policy_version=POLICY_VERSION,
            )

        # 2 & 3. Score every candidate, gate included.
        scores: list[ActionScore] = [
            self._score(
                action=Action.STOP,
                cause=cause,
                amount_paise=amount,
                attempts_so_far=attempts_so_far,
                delay_hours=0,
                ctx=base_ctx,
            )
        ]

        for delay in CANDIDATE_DELAYS_HOURS:
            action = Action.RETRY_NOW if delay == 0 else Action.RETRY_DELAYED
            scheduled = now + timedelta(hours=delay)
            # A retry scheduled past the window is not a candidate at all.
            # Read from settings, not a literal: compliance.should_stop uses the
            # same setting, and the two drifting apart would let the scheduler
            # book retries the stopping rule would refuse.
            if scheduled - first_failure_at >= timedelta(
                days=settings.recovery_window_days
            ):
                continue
            scores.append(
                self._score(
                    action=action,
                    cause=cause,
                    amount_paise=amount,
                    attempts_so_far=attempts_so_far,
                    delay_hours=delay,
                    ctx=replace(base_ctx, scheduled_for=scheduled),
                )
            )

        # Contact actions are scored at delays too, not only at "now". Scoring
        # them only at zero meant a case that failed at 11pm had its
        # notification blocked by quiet hours outright, rather than scheduled
        # for the morning — 430 blocked actions in a 500-event batch, and the
        # recovery those cases might have produced simply lost.
        contact_actions = (
            (Action.NOTIFY,) if self.retry_only else (Action.REQUEST_REAUTH, Action.NOTIFY)
        )
        for action in contact_actions:
            for delay in CONTACT_DELAYS_HOURS:
                scheduled = now + timedelta(hours=delay)
                if scheduled - first_failure_at >= timedelta(
                    days=settings.recovery_window_days
                ):
                    continue
                scores.append(
                    self._score(
                        action=action,
                        cause=cause,
                        amount_paise=amount,
                        attempts_so_far=attempts_so_far,
                        delay_hours=delay,
                        ctx=replace(base_ctx, scheduled_for=scheduled),
                    )
                )

        scores.append(
            self._score(
                action=Action.WAIT,
                cause=cause,
                amount_paise=amount,
                attempts_so_far=attempts_so_far,
                delay_hours=0,
                ctx=base_ctx,
            )
        )

        # 4. Best allowed action. `stop` is always available at zero, so an
        # action is taken only when it genuinely beats doing nothing.
        allowed = [s for s in scores if s.blocked_by is None]
        best = max(allowed, key=lambda s: s.expected_value_paise)

        scores.sort(key=lambda s: (s.blocked_by is not None, -s.expected_value_paise))

        if best.action is Action.STOP:
            # Distinct from a cap firing: nothing was worth doing. Recorded as a
            # real decision so the ledger can tell the two apart.
            return Decision(
                event_id=event.event_id,
                chosen_action=Action.STOP,
                status=DecisionStatus.STOPPED,
                stop_reason="no_action_with_positive_value",
                scores=tuple(scores),
                decided_at=now,
                virtual_time=now,
                arm=arm,
                policy_version=POLICY_VERSION,
            )

        return Decision(
            event_id=event.event_id,
            chosen_action=best.action,
            status=DecisionStatus.EXECUTED,
            scores=tuple(scores),
            scheduled_for=now + timedelta(hours=best.delay_hours),
            decided_at=now,
            virtual_time=now,
            arm=arm,
            policy_version=POLICY_VERSION,
        )
