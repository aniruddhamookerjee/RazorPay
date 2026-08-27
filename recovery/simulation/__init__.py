"""Ground truth for the simulated world.

**Nothing outside this package may import from it at runtime.**

This is the "held out" defence from WORKPLAN.md: the parameters that decide
whether a simulated retry succeeds live here, and the agent must never read
them. If the policy could see these numbers it would be discovering its own
answer key, and the measured result would prove nothing.

The boundary is physical (a separate package) and tested — see
`tests/test_holdout_boundary.py`, which fails if any decision-layer module
imports `recovery.simulation`.

Legitimate importers: the generator, the experiment harness, and tests.
"""
