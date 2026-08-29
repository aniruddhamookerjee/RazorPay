"""The arms the agent is measured against.

Two baselines, and the second is the one that matters:

* **naive** — one retry, fixed delay. What a first implementation does.
* **fixed 3x** — three retries on a fixed schedule. Closer to what merchants
  actually run, and the honest comparison. Beating only the naive arm would be
  beating a strawman, and a result against a strawman is not a result.

**Both baselines respect the compliance gate and the stopping rules.** That is a
deliberate choice and worth defending: it would flatter the agent to let the
baselines debit without a pre-debit notice or past their caps, and the claim we
want is "the cause-aware *strategy* recovers more", not "the agent is the only
arm we bothered to make lawful".

They differ from the agent in exactly one respect — how they choose. No causes,
no costs, no learning: a schedule, applied to everyone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from recovery.config import settings
from recovery.decision import compliance
from recovery.decision.compliance import ComplianceContext
from recovery.decision.costs import CostModel
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

# The industry default: retry every three days.
#   https://gr4vy.com/posts/payment-retry-logic-explained-smart-retries-for-failed-transactions-in-2026/
FIXED_SCHEDULE_HOURS: tuple[int, ...] = (72, 72, 72)
NAIVE_DELAY_HOURS = 24


@dataclass
class ScheduledPolicy:
    """Retry on a fixed schedule, ignoring cause and cost entirely.

    Exposes the same surface as `decision.Policy` — including a `success` model —
    so the runner cannot tell the arms apart. The model is fed outcomes and
    never consulted: baselines observe the world and learn nothing from it,
    which is precisely the behaviour being measured against.
    """

    schedule_hours: tuple[int, ...]
    arm: Arm
    success: SuccessModel = field(default_factory=SuccessModel)
    costs: CostModel = field(default_factory=CostModel)

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
        arm: Arm | None = None,
    ) -> Decision:
        first_failure_at = first_failure_at or event.occurred_at
        arm = arm or self.arm

        ctx = ComplianceContext(
            amount_paise=event.attempt.amount_paise,
            attempts_so_far=attempts_so_far,
            contacts_so_far=contacts_so_far,
            cause=event.cause,
            first_failure_at=first_failure_at,
            scheduled_for=now,
            pre_debit_notice_sent_at=pre_debit_notice_sent_at,
            customer_opted_out=customer_opted_out,
            messaging_consent=messaging_consent,
            mandate_active=mandate_active,
        )

        def stop(reason: str) -> Decision:
            return Decision(
                event_id=event.event_id,
                chosen_action=Action.STOP,
                status=DecisionStatus.STOPPED,
                stop_reason=reason,
                scores=(
                    ActionScore(
                        action=Action.STOP,
                        expected_value_paise=0.0,
                        p_success=0.0,
                        rationale=f"stopped: {reason}",
                    ),
                ),
                decided_at=now,
                virtual_time=now,
                arm=arm,
                policy_version=f"baseline-{self.arm.value}",
            )

        verdict = compliance.should_stop(ctx)
        if verdict.should_stop:
            return stop(verdict.reason or "stopped")

        # Out of scheduled retries.
        index = attempts_so_far - 1
        if index >= len(self.schedule_hours):
            return stop("schedule_exhausted")

        delay = self.schedule_hours[index]
        scheduled = now + timedelta(hours=delay)

        if scheduled - first_failure_at >= timedelta(
            days=settings.recovery_window_days
        ):
            return stop("recovery_window_expired")

        retry_ctx = ComplianceContext(
            **{**ctx.__dict__, "scheduled_for": scheduled}
        )
        retry_verdict = compliance.check(Action.RETRY_DELAYED, retry_ctx)

        if not retry_verdict.allowed:
            # A baseline has no notion of earning the right to debit, so a
            # blocked retry is simply a blocked retry. The one exception is the
            # pre-debit notice, which even a fixed schedule has to send — a real
            # merchant on a fixed schedule still complies. Without this the
            # baselines would be unable to debit at all and the comparison would
            # be meaningless rather than merely unfavourable to them.
            if retry_verdict.reason in {
                "pre_debit_notice_not_sent",
                "pre_debit_notice_too_recent",
            } and contacts_so_far < settings.max_contacts:
                notice_ctx = ComplianceContext(**{**ctx.__dict__, "scheduled_for": now})
                if compliance.check(Action.NOTIFY, notice_ctx).allowed:
                    return Decision(
                        event_id=event.event_id,
                        chosen_action=Action.NOTIFY,
                        status=DecisionStatus.EXECUTED,
                        scores=(
                            ActionScore(
                                action=Action.NOTIFY,
                                expected_value_paise=0.0,
                                p_success=0.0,
                                cost_paise=self.costs.cost_of(
                                    Action.NOTIFY,
                                    attempts_so_far=attempts_so_far,
                                    amount_paise=event.attempt.amount_paise,
                                ).direct_paise,
                                churn_penalty_paise=self.costs.cost_of(
                                    Action.NOTIFY,
                                    attempts_so_far=attempts_so_far,
                                    amount_paise=event.attempt.amount_paise,
                                ).churn_penalty_paise,
                                rationale="fixed schedule: pre-debit notice",
                            ),
                        ),
                        scheduled_for=now,
                        decided_at=now,
                        virtual_time=now,
                        arm=arm,
                        policy_version=f"baseline-{self.arm.value}",
                    )
            return stop(f"blocked:{retry_verdict.reason}")

        # Costed exactly like the agent's actions. Without this the baselines
        # retried for free while the agent paid, so "net" was not comparable —
        # and the bias ran AGAINST the agent, which is the direction that would
        # have made a favourable result look better than it was.
        breakdown = self.costs.cost_of(
            Action.RETRY_DELAYED,
            attempts_so_far=attempts_so_far,
            amount_paise=event.attempt.amount_paise,
        )
        return Decision(
            event_id=event.event_id,
            chosen_action=Action.RETRY_DELAYED,
            status=DecisionStatus.EXECUTED,
            scores=(
                ActionScore(
                    action=Action.RETRY_DELAYED,
                    expected_value_paise=0.0,
                    p_success=0.0,
                    cost_paise=breakdown.direct_paise,
                    churn_penalty_paise=breakdown.churn_penalty_paise,
                    delay_hours=delay,
                    rationale=f"fixed schedule: retry {attempts_so_far} at +{delay}h",
                ),
            ),
            scheduled_for=scheduled,
            decided_at=now,
            virtual_time=now,
            arm=arm,
            policy_version=f"baseline-{self.arm.value}",
        )


def naive_policy(costs: CostModel | None = None) -> ScheduledPolicy:
    """One retry, 24h later. What a first implementation looks like."""
    return ScheduledPolicy(
        schedule_hours=(NAIVE_DELAY_HOURS,), arm=Arm.NAIVE, costs=costs or CostModel()
    )


def fixed_3x_policy(costs: CostModel | None = None) -> ScheduledPolicy:
    """Three retries, three days apart. The comparison that means something."""
    return ScheduledPolicy(
        schedule_hours=FIXED_SCHEDULE_HOURS, arm=Arm.FIXED_3X, costs=costs or CostModel()
    )
