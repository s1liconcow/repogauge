"""Repo/instance adapter generation for SWE-bench evaluator integration.

Invariant:
- Do not emit dataset-only exports for unseen repositories.
- For arbitrary repos, exported artifacts must include generated adapter code and
  adapter specs so harness registration is complete.
"""

# TODO(oss_repogauge): implement adapter template rendering and spec emission.

