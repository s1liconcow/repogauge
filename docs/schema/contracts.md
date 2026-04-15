# RepoGauge Contract Schema

`RepoGauge` uses stable, versioned artifacts so downstream stages can reason about
results without re-reading source code.

## Current schema version

- `REPOGAUGE_SCHEMA_VERSION = "0.1.0"`

## General principles

- Keep schema rows JSONL-friendly.
- Keep JSONL append-only.
- Preserve unknown fields where possible by allowing optional `metadata` maps.
- Distinguish **authoritative** fields (hashes, patch text, test IDs, outcomes)
  from **advisory** fields (scores, confidences, provenance hints).

## Canonical record types

All records include `schema_version` and `repo` where applicable.

### `RepoProfile`

- `schema_version: str`
- `repo: str`
- `default_branch: str`
- `python_version: str | null`
- `install_cmds: list[str]`
- `test_cmds: list[str]`
- `package_manager: str | null`
- `source_path: str`
- `updated_at: str`
- `metadata: object`

### `ScanRow`

- `schema_version: str`
- `id: str`
- `repo: str`
- `commit: str`
- `parent_commit: str | null`
- `diff: str`
- `files_touched: list[str]`
- `changed_lines: int`
- `heuristic_score: float`
- `state: "discovered" | "rejected" | "shortlist"`
- `metadata: object`

### `CandidateRow`

- `schema_version: str`
- `id: str`
- `repo: str`
- `source_scan: str`
- `review_state: "open" | "accepted" | "rejected"`
- `problem_statement: str | null`
- `file_roles: object`
- `evidence: list[str]`
- `metadata: object`

### `ReviewedCandidate`

- `schema_version: str`
- `id: str`
- `candidate_id: str`
- `repo: str`
- `reviewer_notes: str`
- `state: "accepted" | "rejected"`
- `reason: str | null`
- `metadata: object`

### `DatasetInstance`

- `schema_version: str`
- `instance_id: str`
- `repo: str`
- `base_commit: str`
- `problem_statement: str`
- `version: str`
- `patch: str`
- `test_patch: str`
- `FAIL_TO_PASS: list[str]`
- `PASS_TO_PASS: list[str]`
- `metadata: object`

### `PredictionRow`

- `schema_version: str`
- `instance_id: str`
- `model_name_or_path: str`
- `model_patch: str`
- `solver_id: str | null`
- `prompt_hash: str | null`
- `metadata: object`

### `ValidationRow`

- `schema_version: str`
- `instance_id: str`
- `status: "pending" | "succeeded" | "failed" | "flaky"`
- `fail_to_pass: list[str]`
- `pass_to_pass: list[str]`
- `flake_runs: int`
- `outcome_summary: object`
- `metadata: object`

### `AdapterSpec`

- `schema_version: str`
- `repo: str`
- `version: str`
- `docker_specs: object`
- `install_cmds: list[str]`
- `test_cmds: list[str]`
- `module_name: str`
- `metadata: object`

### `JobRow`

- `schema_version: str`
- `job_id: str`
- `instance_id: str`
- `solver_id: str`
- `status: "queued" | "running" | "succeeded" | "failed" | "timed_out" | "budget_exceeded" | "invalid_patch"`
- `started_at: str | null`
- `ended_at: str | null`
- `attempts: int`
- `metadata: object`

### `AttemptRow`

- `schema_version: str`
- `attempt_id: str`
- `job_id: str`
- `instance_id: str`
- `solver_id: str`
- `duration_ms: int`
- `exit_reason: str`
- `model_patch: str | null`
- `usage: object`
- `cost: object`
- `metadata: object`

### `InstanceEvalRow`

- `schema_version: str`
- `instance_id: str`
- `solver_id: str`
- `model_patch: str | null`
- `harness_outcome: "passed" | "failed" | "error" | "timeout"`
- `resolved: bool`
- `resolved_at: str | null`
- `failure_reason: str | null`
- `metadata: object`
