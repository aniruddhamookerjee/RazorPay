"""What an action costs — including the cost that is not on any invoice.

Three components:

    attempt_cost        the fee for trying
    notification_cost   the fee for messaging
    churn_penalty       P(customer leaves | attempts so far) x lifetime value

The third is doing the heaviest lifting in the whole model and is the least
sourceable number in the project (`sources.md` §3, WORKPLAN.md §4.3). It is what
makes the agent stop early, which is what produces the "fewer wasted attempts"
result. So it is built to be **switched off**: `CostModel(churn_penalty_enabled=False)`
zeroes it, and Day 7 reports the agent's advantage both ways. If the advantage
survives with the softest assumption disabled, that is worth more than any
amount of arguing about its value.

Every figure here is an assumption, not a citation, and is labelled as one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from recovery.models import Action

# ASSUMPTION — no published Razorpay figure used. Swept on Day 7.
DEFAULT_ATTEMPT_COST_PAISE = 200
DEFAULT_NOTIFICATION_COST_PAISE = 25

# Lifetime value is expressed as a MULTIPLE OF THE CHARGE, not a flat rupee
# figure, and that was a bug worth recording. A flat Rs 12,000 LTV against a
# Rs 199/month subscription implies a sixty-month lifetime; the resulting churn
# penalty (Rs 120) then exceeded the expected gain on a 50% recovery (Rs 99.50),
# so the agent refused to retry small charges at all. Scaling with the charge
# keeps the ratio sane across a book that spans Rs 199 to Rs 19,999.
DEFAULT_LTV_MONTHS = 12

# ASSUMPTION, and the softest number in the model. Pestering a customer who
# already failed once costs goodwill; the cost rises with each attempt.
#
# CALIBRATED, NOT CITED — and the distinction matters. The first values tried
# here (0.01 / 0.03 / 0.07) made the churn penalty on a contact action
# 0.24 x the charge, which is structurally larger than the ~0.15 a notification
# can be expected to recover. The agent therefore never notified, so the RBI
# pre-debit notice was never sent, so every debit stayed blocked, and the whole
# batch recovered 1.4% at a net loss.
#
# That is not a finding about payment recovery. Published recovery rates run
# 30-70% (sources.md section 2), so an agent recovering 1.4% is evidence the
# parameters are wrong rather than evidence the world is. These values are set
# so that behaviour lands in the published range — an external sanity check, not
# a citation, and the honest description is "calibrated against a benchmark".
#
# Because they are chosen rather than sourced, they carry the heaviest weight in
# the Day 7 sensitivity sweep, and every headline is also reported with the
# churn penalty switched off entirely.
DEFAULT_CHURN_BY_ATTEMPTS: dict[int, float] = {0: 0.000, 1: 0.003, 2: 0.008, 3: 0.020}


@dataclass(frozen=True)
class CostBreakdown:
    """Itemised so the audit trail can show the arithmetic, not just a total."""

    direct_paise: float
    churn_penalty_paise: float

    @property
    def total_paise(self) -> float:
        return self.direct_paise + self.churn_penalty_paise


@dataclass(frozen=True)
class CostModel:
    """Costs of taking an action. All parameters overridable, all can be zero."""

    attempt_cost_paise: int = DEFAULT_ATTEMPT_COST_PAISE
    notification_cost_paise: int = DEFAULT_NOTIFICATION_COST_PAISE
    # Expected lifetime in billing cycles. LTV = ltv_months x charge amount.
    ltv_months: int = DEFAULT_LTV_MONTHS
    churn_by_attempts: dict[int, float] = field(
        default_factory=lambda: dict(DEFAULT_CHURN_BY_ATTEMPTS)
    )
    # Day 7 reports results with this off, so the agent's advantage can be
    # judged without the least defensible parameter in play.
    churn_penalty_enabled: bool = True

    def churn_probability(self, attempts_so_far: int) -> float:
        if not self.churn_penalty_enabled:
            return 0.0
        if attempts_so_far in self.churn_by_attempts:
            return self.churn_by_attempts[attempts_so_far]
        # Beyond the tabulated range, hold at the worst known value rather than
        # extrapolating a number nobody sourced.
        return max(self.churn_by_attempts.values())

    def lifetime_value_paise(self, amount_paise: int) -> int:
        """LTV for a customer paying `amount_paise` per cycle."""
        return self.ltv_months * amount_paise

    def cost_of(
        self,
        action: Action,
        *,
        attempts_so_far: int,
        amount_paise: int,
    ) -> CostBreakdown:
        """What taking `action` costs right now, for a charge of this size."""
        # Doing nothing costs nothing. WAIT and STOP are genuinely free, which
        # is what lets STOP win on its own when everything else is negative.
        if action in (Action.WAIT, Action.STOP):
            return CostBreakdown(0.0, 0.0)

        if action is Action.NOTIFY:
            direct = float(self.notification_cost_paise)
        elif action is Action.REQUEST_REAUTH:
            # A re-auth request is a message plus friction we do not price.
            direct = float(self.notification_cost_paise)
        else:
            direct = float(self.attempt_cost_paise)

        # Only actions that touch the customer carry much churn risk. A retry
        # the customer never sees is cheap in goodwill; a third dunning message
        # is not. Retries carry a small weight rather than none, because a
        # declined card still pings the customer's own banking app.
        contact_weight = 1.0 if action in (Action.NOTIFY, Action.REQUEST_REAUTH) else 0.25
        churn = (
            self.churn_probability(attempts_so_far + 1)
            - self.churn_probability(attempts_so_far)
        ) * contact_weight * self.lifetime_value_paise(amount_paise)

        return CostBreakdown(direct_paise=direct, churn_penalty_paise=max(0.0, churn))


def zero_cost_model() -> CostModel:
    """Every cost off. Used by the sensitivity sweep as the extreme bound."""
    return CostModel(
        attempt_cost_paise=0,
        notification_cost_paise=0,
        ltv_months=0,
        churn_penalty_enabled=False,
    )
