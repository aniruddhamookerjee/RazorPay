"""The loop that ties every layer together and writes the audit trail.

For one failed charge:

    diagnose  ->  decide  ->  execute  ->  record  ->  learn

and repeat until the case succeeds, hits a cap, or the window closes. Every pass
writes exactly one ledger row, including the passes where nothing was done —
"we stopped, and here is which rule stopped us" is the row that proves the agent
is bounded, so it cannot be the row that is missing.

**Where learning happens.** After each resolved outcome the runner feeds it back
into the success model. That is the only channel through which evidence reaches
the agent: no simulator parameter is ever read here.

**Virtual time.** The loop advances a `VirtualClock` by whatever delay the
policy chose, so a seven-day recovery window replays in milliseconds. Live
executions happen in real time but are stamped with both clocks, so they sit on
the same timeline as the simulated batch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from recovery.clock import VirtualClock
from recovery.config import settings
from recovery.decision.policy import Policy
from recovery.diagnosis.classifier import Classifier
from recovery.execution.executors import Executor
from recovery.ledger import Ledger
from recovery.models import (
    Action,
    Arm,
    AttemptStatus,
    DecisionStatus,
    FailureEvent,
    LedgerEntry,
)

# A retry the policy schedules is only *attempted* once the clock reaches it.
_DEBIT_ACTIONS = {Action.RETRY_NOW, Action.RETRY_DELAYED}

# Actions that inform the customer ahead of a debit. Under the RBI e-mandate
# framework a debit needs 24h notice, and the first end-to-end run showed why
# this mapping matters: without it, nothing in the system could ever *send* the
# notice, so every debit was blocked forever and the agent was deadlocked into
# never retrying anything. A notification IS the pre-debit notice — wiring the
# two together turns the constraint from a wall into a step in the sequence.
_NOTICE_ACTIONS = {Action.NOTIFY, Action.REQUEST_REAUTH}


@dataclass
class RunStats:
    events: int = 0
    entries: int = 0
    recovered_paise: int = 0
    cost_paise: int = 0
    recovered_count: int = 0
    stop_reasons: dict[str, int] = field(default_factory=dict)
    blocked: dict[str, int] = field(default_factory=dict)

    @property
    def net_paise(self) -> int:
        return self.recovered_paise - self.cost_paise

    @property
    def recovery_rate(self) -> float:
        return self.recovered_count / self.events if self.events else 0.0


@dataclass
class RecoveryRunner:
    """Runs a batch end to end and records every action in the ledger."""

    classifier: Classifier
    policy: Policy
    executor: Executor
    ledger: Ledger
    arm: Arm = Arm.AGENT
    run_id: str | None = None
    seed: int | None = None

    def run_event(
        self,
        event: FailureEvent,
        *,
        cycle: int = 0,
        pre_debit_notice_sent_at: datetime | None = None,
        customer_opted_out: bool = False,
        messaging_consent: bool = True,
    ) -> list[LedgerEntry]:
        """Work one case until it succeeds, stops, or runs out of window."""
        classified = self.classifier.classify(event)
        first_failure_at = classified.occurred_at

        clock = VirtualClock(now=first_failure_at)
        entries: list[LedgerEntry] = []
        # Debits and contacts are budgeted separately. Counting a notification
        # against the retry cap left every case with a single real retry:
        # notify -> retry -> stop, which is most of why the first end-to-end run
        # recovered so little.
        attempts = 1  # the original charge already failed once
        contacts = 0
        passes = 0
        # Tracked as state rather than a parameter: the agent can earn the right
        # to debit by notifying first, which is the whole point of the rule.
        notice_sent_at = pre_debit_notice_sent_at

        while True:
            decision = self.policy.decide(
                classified,
                now=clock.now,
                attempts_so_far=attempts,
                contacts_so_far=contacts,
                first_failure_at=first_failure_at,
                pre_debit_notice_sent_at=notice_sent_at,
                customer_opted_out=customer_opted_out,
                messaging_consent=messaging_consent,
                arm=self.arm,
            )

            chosen = next(
                (s for s in decision.scores if s.action is decision.chosen_action),
                None,
            )
            delay_hours = chosen.delay_hours if chosen else 0
            cost = int(chosen.cost_paise + chosen.churn_penalty_paise) if chosen else 0

            # The clock moves to when the action actually happens. Doing this
            # before execution is what makes the delay real rather than notional.
            attempted_at = clock.at(hours=delay_hours)

            result = self.executor.execute(
                event=classified,
                action=decision.chosen_action,
                attempted_at=attempted_at,
                attempt_number=attempts + 1,
                step=passes,
                cycle=cycle,
                cost_paise=cost,
            )

            entry = LedgerEntry(
                event_id=classified.event_id,
                decision_id=decision.decision_id,
                subscription_id=classified.attempt.subscription_id,
                customer_id=classified.attempt.customer_id,
                cause=classified.cause,
                classified_by=classified.classified_by,
                confidence=classified.confidence,
                error_reason=classified.error_reason,
                action=decision.chosen_action,
                status=decision.status,
                stop_reason=decision.stop_reason,
                scores=decision.scores,
                attempt_number=attempts,
                amount_paise=classified.attempt.amount_paise,
                recovered_paise=result.recovered_paise,
                cost_paise=result.cost_paise,
                outcome=result.outcome,
                razorpay_payment_id=result.razorpay_payment_id,
                idempotency_key=result.idempotency_key,
                arm=self.arm,
                run_id=self.run_id,
                seed=self.seed,
                cycle=cycle,
                is_live=result.is_live,
                recorded_at=datetime.now(tz=first_failure_at.tzinfo),
                virtual_time=attempted_at,
                narration=result.detail,
            )
            entries.append(entry)

            # Feed the outcome back. This is the ONLY path by which the agent
            # learns anything — no simulator parameter is read anywhere here.
            if decision.chosen_action in _DEBIT_ACTIONS or decision.chosen_action in (
                Action.REQUEST_REAUTH,
                Action.NOTIFY,
            ):
                self.policy.success.observe(
                    cause=classified.cause,
                    action=decision.chosen_action,
                    delay_hours=delay_hours,
                    attempt_number=attempts + 1,
                    outcome=result.outcome,
                )

            # A notification satisfies the pre-debit requirement from now on.
            if (
                decision.chosen_action in _NOTICE_ACTIONS
                and result.outcome is not AttemptStatus.SUCCEEDED
            ):
                notice_sent_at = attempted_at

            if decision.status is DecisionStatus.STOPPED:
                break
            if result.outcome is AttemptStatus.SUCCEEDED:
                break

            if decision.chosen_action in _DEBIT_ACTIONS:
                attempts += 1
            elif decision.chosen_action in _NOTICE_ACTIONS:
                contacts += 1

            clock.advance_to(attempted_at)
            passes += 1

            # Belt and braces. The policy enforces all of these, but a runner
            # that could spin forever on a policy bug is not something to ship —
            # and now that some actions do not advance `attempts`, a pass cap is
            # the guard that actually terminates the loop.
            if attempts > settings.max_attempts:
                break
            if passes >= settings.max_attempts + settings.max_contacts + 2:
                break
            if clock.now - first_failure_at >= timedelta(
                days=settings.recovery_window_days
            ):
                break

        return entries

    def run(
        self, events: tuple[FailureEvent, ...], *, cycle: int = 0, **kwargs
    ) -> RunStats:
        """Run a whole batch, writing every row to the ledger."""
        stats = RunStats(events=len(events))
        buffer: list[LedgerEntry] = []

        for event in events:
            for entry in self.run_event(event, cycle=cycle, **kwargs):
                buffer.append(entry)
                stats.entries += 1
                stats.recovered_paise += entry.recovered_paise
                stats.cost_paise += entry.cost_paise
                if entry.outcome is AttemptStatus.SUCCEEDED:
                    stats.recovered_count += 1
                if entry.stop_reason:
                    stats.stop_reasons[entry.stop_reason] = (
                        stats.stop_reasons.get(entry.stop_reason, 0) + 1
                    )
                for score in entry.scores:
                    if score.blocked_by:
                        stats.blocked[score.blocked_by] = (
                            stats.blocked.get(score.blocked_by, 0) + 1
                        )

        self.ledger.append_many(buffer)
        return stats
