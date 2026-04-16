"""Judge-queue evaluation orchestration.

Invariant:
- Solver execution and judge execution must stay decoupled.
- The judge queue should only consume normalized predictions and output official
  SWE-bench-compatible outcomes and logs.
"""

# TODO(oss_repogauge): implement batched judge scheduling and report persistence.
