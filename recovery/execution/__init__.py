"""Layer 4 — execution and audit.

    executors.py  simulated and live backends behind one interface
    runner.py     diagnose -> decide -> execute -> record -> learn

Must never import `recovery.simulation`. The simulated executor needs to know
whether a retry worked, but that answer arrives as an injected oracle rather
than an import, so the answer key stays off the execution path.
"""

from recovery.execution.executors import (
    ExecutionResult,
    Executor,
    LiveExecutor,
    OutcomeOracle,
    SimulatedExecutor,
    idempotency_key,
)
from recovery.execution.runner import RecoveryRunner, RunStats

__all__ = [
    "ExecutionResult",
    "Executor",
    "LiveExecutor",
    "OutcomeOracle",
    "RecoveryRunner",
    "RunStats",
    "SimulatedExecutor",
    "idempotency_key",
]
