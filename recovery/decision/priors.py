"""What the agent believes before it has observed anything.

**This is not a copy of the simulator's parameters, and the difference matters.**

`recovery/simulation/params.py` holds the exact recovery curves that decide
whether a simulated retry succeeds. This module holds what a real team could
know on day one: coarse published base rates, with wide uncertainty, and *no
timing shape at all*.

That asymmetry is the entire experiment. The simulator knows that
insufficient-funds recovery peaks at three to five days. The agent does not —
it starts believing every delay is equally good and has to **learn** the timing
from observed outcomes. If we seeded it with the true curve, the agent would be
reciting the answer key and the measured uplift would prove nothing.

So the numbers below are deliberately:

* **coarse** — one base rate per cause, not a curve
* **flat over delay** — no delay preference is encoded anywhere here
* **weakly held** — small pseudo-counts, so a few dozen real observations
  overwhelm the prior rather than being swamped by it

Sources are the same public benchmarks cited in `sources.md` §2, because that
is genuinely where a team would start. Reading the same public report as the
simulator's author is not a leak; being handed their config file would be.
"""

from __future__ import annotations

from dataclasses import dataclass

from recovery.models import Action, Cause

# Candidate delays the policy scores, in hours. The agent has no idea which of
# these is good for which cause — that is what it learns.
CANDIDATE_DELAYS_HOURS: tuple[int, ...] = (0, 2, 24, 72, 120)

# How much a prior is "worth" in observations. Low on purpose: after ~30 real
# outcomes in a cell the data dominates. Too high and the agent never learns;
# too low and early cycles are noise-driven.
PRIOR_STRENGTH: float = 12.0


@dataclass(frozen=True)
class CausePrior:
    """Belief about one cause before any evidence.

    `retry_rate` is the chance a retry works *at some point in the window* —
    published recovery rates are quoted that way, not per-attempt-per-hour.
    """

    retry_rate: float
    reauth_rate: float
    notify_rate: float
    source: str


# Published bands (sources.md §2): expired cards recover 50-70% via card update,
# insufficient funds 40-60% on retry, fraud/hard declines 20-40%.
#   https://www.digitalapplied.com/blog/failed-payment-recovery-dunning-playbook-2026
#
# Midpoints are used rather than optimistic ends. Where a cause has no published
# figure the value is conservative and marked — the sensitivity sweep on Day 7
# is what makes these defensible, not their precision.
PRIORS: dict[Cause, CausePrior] = {
    Cause.INSUFFICIENT_FUNDS: CausePrior(
        retry_rate=0.50,
        reauth_rate=0.10,
        notify_rate=0.15,
        source="40-60% band, midpoint",
    ),
    # Published 50-70% is recovery *overall*, which for a dead card happens by
    # the customer supplying a new one. Retrying the same expired card is not
    # what that number measures, so retry is held low and re-auth high.
    Cause.CARD_EXPIRED: CausePrior(
        retry_rate=0.10,
        reauth_rate=0.55,
        notify_rate=0.30,
        source="50-70% band attributed to card update, not repetition",
    ),
    Cause.BANK_UNAVAILABLE: CausePrior(
        retry_rate=0.60,
        reauth_rate=0.05,
        notify_rate=0.05,
        source="transient; no published figure, conservative",
    ),
    Cause.NETWORK_TIMEOUT: CausePrior(
        retry_rate=0.60,
        reauth_rate=0.05,
        notify_rate=0.05,
        source="transient; no published figure, conservative",
    ),
    Cause.HARD_DECLINE: CausePrior(
        retry_rate=0.06,
        reauth_rate=0.10,
        notify_rate=0.12,
        source="20-40% band is fraud holds generally; a firm decline is lower",
    ),
    Cause.AUTHENTICATION_REQUIRED: CausePrior(
        retry_rate=0.15,
        reauth_rate=0.45,
        notify_rate=0.25,
        source="needs customer action; no published figure",
    ),
    # Structural, not empirical: without a valid mandate there is nothing to
    # debit. This is the one place the agent may start out certain, and it is
    # certain from the definition of a mandate rather than from data.
    Cause.MANDATE_EXPIRED: CausePrior(
        retry_rate=0.0,
        reauth_rate=0.40,
        notify_rate=0.20,
        source="structural: no valid mandate, no debit",
    ),
    Cause.MANDATE_REVOKED: CausePrior(
        retry_rate=0.0,
        reauth_rate=0.20,
        notify_rate=0.10,
        source="structural, and re-auth after revocation is a weaker ask",
    ),
    # An undiagnosed failure. Pessimistic on purpose — an agent that treats
    # "we don't know" as an average case will happily retry dead mandates that
    # happened to arrive with a generic reason string.
    Cause.UNKNOWN: CausePrior(
        retry_rate=0.20,
        reauth_rate=0.15,
        notify_rate=0.10,
        source="no diagnosis; deliberately pessimistic",
    ),
}


def prior_rate(cause: Cause, action: Action) -> float:
    """The agent's starting belief for one (cause, action) pair.

    Flat over delay by design — see the module docstring. `retry_now` and
    `retry_delayed` start out identical, and only observed outcomes can ever
    separate them.
    """
    belief = PRIORS.get(cause, PRIORS[Cause.UNKNOWN])

    if action in (Action.RETRY_NOW, Action.RETRY_DELAYED):
        return belief.retry_rate
    if action is Action.REQUEST_REAUTH:
        return belief.reauth_rate
    if action is Action.NOTIFY:
        return belief.notify_rate
    # WAIT and STOP collect nothing by themselves.
    return 0.0
