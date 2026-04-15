"""Validation pipeline primitives for deterministic task credibility.

Invariant:
- LLM outputs can suggest, but exported artifacts require deterministic validation.
- Validation must prove `FAIL_TO_PASS` and `PASS_TO_PASS` outcomes before dataset
  export is considered complete.
"""

# TODO(oss_repogauge): implement four-run validation and flake reconciliation.

