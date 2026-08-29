"""Layer 3 — decision.

    priors.py      what the agent believes before observing anything
    success.py     Beta-Binomial p(success), learned from outcomes only
    costs.py       attempt fee, notification cost, churn penalty
    compliance.py  hard constraints and stopping rules
    policy.py      score every action, keep the table, pick the best

This package must never import `recovery.simulation`. Its priors come from the
same *public* benchmarks the simulator's author read, deliberately coarser and
flat over delay — the agent has to learn timing from evidence rather than being
handed the answer. Enforced by `tests/test_generator.py`.
"""

from recovery.decision.compliance import ComplianceContext, check, should_stop
from recovery.decision.costs import CostModel, zero_cost_model
from recovery.decision.policy import Policy
from recovery.decision.success import SuccessModel

__all__ = [
    "ComplianceContext",
    "CostModel",
    "Policy",
    "SuccessModel",
    "check",
    "should_stop",
    "zero_cost_model",
]
