
# RepoGauge Comprehensive Bead Plan

This document is the execution plan for building **RepoGauge** from scratch as a **Python-first, local-first mining and evaluation tool** that turns one repository into:

1. a **private SWE-bench-style dataset** for that repository; and
2. a **generated harness adapter** that registers the repo and environment with the official SWE-bench harness.

This file is intentionally self-contained. Engineers should be able to execute the work without reopening the original strategy memo. The plan assumes the following product statement:

- v1 is **Python repos only**.
- v1 is **local-repo input first**.
- v1 only targets **single-parent commits** and primarily **single-commit or squash-merge bugfixes**.
- v1 prefers commits that already contain **regression tests or clearly necessary test-support changes**.
- v1 uses **deterministic validation** as the source of truth; LLMs may propose, but validators must prove.
- the artifact pair for arbitrary repos is **`dataset.jsonl` + generated `adapter/`**, not dataset-only.
- repository contents must **never leave the machine by default**; model calls are opt-in.

## Product scope and invariants

### In scope for MVP

- CLI-only workflow:
  - `repogauge mine /path/to/repo --out ./out`
  - `repogauge review ./out/candidates.jsonl`
  - `repogauge export ./out/reviewed.jsonl --dataset ./out/dataset`
  - `repogauge eval ./out/dataset/dataset.jsonl --gold`
- Mining candidate bugfix commits from the default branch or an explicit commit range.
- Materializing SWE-bench-style instances with:
  - `instance_id`
  - `repo`
  - `base_commit`
  - `problem_statement`
  - `version`
  - `patch`
  - `test_patch`
  - `FAIL_TO_PASS`
  - `PASS_TO_PASS`
  - optional metadata
- Generating a repo-specific adapter that patches the official harness runtime maps.
- Validating gold patches locally and through the official harness.
- Running experiment matrices across multiple solver adapters and evaluating them through the judge path.
- Producing per-run cost/quality reports and router-training data.

### Out of scope for MVP

- multi-language mining and evaluation
- automatic multi-commit PR reconstruction
- synthetic test generation
- hosted service / web product / database-backed UI
- fully generic parsing of arbitrary test runner text formats
- remote-only repo analysis
- benchmark publishing workflows

### Architectural invariants

- **Deterministic validators are authoritative.** LLMs can rank, classify, summarize, or rescue environment hints, but exported instances must pass deterministic validation.
- **The generated adapter is part of the product.** Treat `dataset.jsonl` without `adapter/` as incomplete for unseen repositories.
- **All outputs are local artifacts.** Re-runs and debugging must work from the artifact directory, not from transient memory.
- **Evaluation uses the official SWE-bench harness underneath.** RepoGauge may wrap and register data, but should not fork grading semantics without an explicit ADR.
- **The experiment runner separates solver execution from judging.** Solver throughput and Docker-heavy judging are different bottlenecks and must remain independently schedulable.
- **Every exported instance must have at least one stable `FAIL_TO_PASS` test and zero regressions in `PASS_TO_PASS`.**
- **Every long-running step must be resumable or restart-safe.** Mining, validation, solving, judging, and analysis all need stable manifests and idempotent file layout.

## Recommended repository layout

Use this structure instead of overloading a handful of modules. The original draft layout is directionally correct, but this version is easier to scale.

```text
repogauge/
  pyproject.toml
  README.md
  DESIGN.md
  docs/
    ADRs/
    schema/
    tutorials/
  repogauge/
    __init__.py
    cli.py
    config.py
    artifacts.py
    logging_utils.py
    manifest.py
    git_utils.py
    exec.py
    review.py
    mining/
      inspect.py
      classify.py
      scan.py
      score.py
      enrich.py
      materialize.py
      split_patch.py
      synthesize.py
    validation/
      env_detect.py
      dryrun.py
      testsel.py
      junit.py
      runner.py
      validate.py
      evidence.py
    export/
      dataset.py
      predictions.py
      adapter.py
      specs.py
    llm/
      base.py
      schemas.py
      prompts.py
      claude_cli.py
      claude_sdk.py
      codex_cli.py
      openai_responses.py
      opencode.py
      openai_compatible.py
    runner/
      matrix.py
      planner.py
      providers.py
      workspaces.py
      normalize_patch.py
      solvers.py
      scheduler.py
      judge.py
      telemetry.py
      analyze.py
      features.py
      router.py
    parsers/
      junit.py
  tests/
    unit/
    integration/
    golden/
    fixtures/
```

## Output artifact contract

For `repogauge mine /path/to/repo --out ./out`, the default artifact directory should eventually contain:

```text
out/
  repo_profile.json
  scan.jsonl
  candidates.jsonl
  review.md
  review.html
  reviewed.jsonl
  dataset/
    dataset.jsonl
    predictions.gold.jsonl
    validation.jsonl
    adapter/
      __init__.py
      repogauge_<repo>.py
      specs.json
  logs/
    inspect/
    validation/
    eval/
  manifests/
    mine.json
    export.json
    eval.json
```

For `repogauge run ./matrix.yaml --dataset ./artifact/dataset.jsonl --run-id q2_baseline`:

```text
runs/q2_baseline/
  matrix.yaml
  jobs.jsonl
  attempts.parquet
  predictions/
    <solver>.jsonl
  eval/
    <solver>/
      results.json
      instance_results.jsonl
      run_logs/
  analyze/
    summary.json
    report.html
    report.csv
    router_train.parquet
```

## Priority and type legend

- **P0**: blocks MVP or invalidates downstream work if wrong.
- **P1**: required for the first “credible” release, but can follow the critical path.
- **P2**: useful hardening or scalability work after the first end-to-end system exists.

- **spike**: investigate and commit an ADR or reference implementation.
- **task**: implement a bounded feature with production code and tests.
- **epic**: umbrella bead that may require multiple merge requests.

## Critical path overview

The shortest path to a credible v1 is:

1. freeze architecture and contracts;
2. build scaffolding, logging, artifacts, git, and subprocess utilities;
3. implement deterministic miner and review exports;
4. implement materialization and dataset export;
5. implement environment detection and four-run validation;
6. generate the adapter and integrate the official harness;
7. prove gold end-to-end resolution;
8. add experiment runner, judge queue, telemetry, analysis, and router baselines.

Dependency summary:

- `rg-mvp-000` -> `rg-mvp-001` -> `rg-mvp-002` -> `rg-mvp-003` -> `rg-mvp-004` -> `rg-mvp-005`
- mining path: `rg-mvp-005` -> `rg-mvp-010` -> `rg-mvp-011` -> `rg-mvp-012` -> `rg-mvp-013` -> `rg-mvp-014`
- materialization path: `rg-mvp-014` -> `rg-mvp-020` -> `rg-mvp-021` -> `rg-mvp-022` -> `rg-mvp-023` -> `rg-mvp-024`
- validation path: `rg-mvp-005` + `rg-mvp-024` -> `rg-mvp-030` -> `rg-mvp-031` -> `rg-mvp-032` -> `rg-mvp-033` -> `rg-mvp-034` -> `rg-mvp-035` -> `rg-mvp-036`
- harness path: `rg-mvp-024` + `rg-mvp-033` -> `rg-mvp-040` -> `rg-mvp-041` -> `rg-mvp-042` -> `rg-mvp-043`
- experiment path: `rg-mvp-042` -> `rg-mvp-050` -> `rg-mvp-051` -> `rg-mvp-052` -> `rg-mvp-053` -> `rg-mvp-054` -> `rg-mvp-055` -> `rg-mvp-056`
- analysis/router path: `rg-mvp-056` -> `rg-mvp-060` -> `rg-mvp-061` -> `rg-mvp-062` -> `rg-mvp-063`
- hardening path spans the whole system: `rg-mvp-064`, `rg-mvp-065`

---

## Freeze MVP architecture, scope boundaries, and non-goals

### ID
rg-mvp-000

### Priority
P0

### Type
spike

### Labels
architecture, adr, scope, mvp, harness

### Description
- Write the canonical architecture decision record for RepoGauge MVP.
- Freeze the non-goals so downstream engineers do not accidentally broaden scope into a platform project.
- Codify the artifact pair rule: **dataset plus generated adapter** is the minimum honest output for arbitrary repositories.
- Record the authoritative validation rule: **LLMs suggest, validators prove**.
- Record the queue split rule: **solver queue** and **judge queue** are separate subsystems.

### Subtasks
- Add `docs/ADRs/0001-mvp-architecture.md`.
- Add a one-page “What v1 is / is not” section to `README.md` and `DESIGN.md`.
- Add module-header comments to `repogauge/export/adapter.py`, `repogauge/validation/validate.py`, and `repogauge/runner/judge.py` so future contributors see the invariants in code.

### Likely Files
- `DESIGN.md`
- `README.md`
- `docs/ADRs/0001-mvp-architecture.md`
- `repogauge/export/adapter.py`
- `repogauge/validation/validate.py`
- `repogauge/runner/judge.py`

### Design
- The ADR should explicitly cover:
  - why MVP is Python-only;
  - why the adapter exists instead of a harness fork;
  - why validators are authoritative;
  - why the experiment runner separates solving from judging;
  - why repo contents are local by default and LLM calls are opt-in.
- Include at least one “future expansion” section that says multi-language and synthetic tests require new ADRs.

### Testing / Validation
- This is doc work, but reviewers should be able to answer:
  - what exact artifact makes arbitrary-repo evaluation possible?
  - what is allowed to call external models?
  - what breaks if the adapter is missing?
  - what breaks if the harness is unavailable?

### Gotchas
- If the ADR is vague, later work will quietly turn optional model assistance into a hidden requirement.
- If the artifact pair rule is not stated clearly, engineers may waste time chasing dataset-only compatibility for unsupported repos.

### Acceptance Criteria
- ADR is merged and linked from `README.md` and `DESIGN.md`.
- Reviewers can explain the MVP without the original strategy memo.
- The codebase contains visible invariant comments in the highest-risk modules.

---

## Define canonical contracts for instances, predictions, validation, and run artifacts

### ID
rg-mvp-001

### Priority
P0

### Type
task

### Labels
schema, contracts, dataset, predictions, validation

### Dependencies
- rg-mvp-000

### Description
- Define all serialization contracts that flow across commands:
  - `RepoProfile`
  - `ScanRow`
  - `CandidateRow`
  - `ReviewedCandidate`
  - `DatasetInstance`
  - `PredictionRow`
  - `ValidationRow`
  - `AdapterSpec`
  - `JobRow`
  - `AttemptRow`
  - `InstanceEvalRow`
- Make field names stable, versioned, and JSON/JSONL/Parquet-safe.

### Subtasks
- Create `docs/schema/contracts.md` as the source of truth.
- Implement `pydantic` models or typed dataclasses in `repogauge/config.py`, `repogauge/manifest.py`, `repogauge/export/specs.py`, and `repogauge/runner/telemetry.py`.
- Add a version field to every persisted top-level document.
- Freeze enum values for:
  - candidate state
  - file role
  - install strategy
  - test strategy
  - validation status
  - attempt exit reason
  - usage/cost source
  - harness outcome

### Likely Files
- `docs/schema/contracts.md`
- `repogauge/config.py`
- `repogauge/manifest.py`
- `repogauge/export/specs.py`
- `repogauge/runner/telemetry.py`
- `tests/unit/test_contracts.py`

### Design
- Unknown fields must be preserved where practical so older runs can still be analyzed by newer tools.
- JSONL rows should be append-only friendly.
- Parquet schemas should avoid nested structures that make later SQL or pandas analysis painful; flatten where possible.
- Mark which fields are authoritative:
  - authoritative: hashes, commit SHAs, patch text, test IDs, harness outcomes
  - advisory: heuristic scores, LLM confidence, problem statement provenance hints

### Testing / Validation
- Golden serialization tests for JSON and JSONL.
- Round-trip tests for Parquet schemas.
- Backward-compatibility test with a fixture missing newly added optional fields.

### Gotchas
- Do not let `FAIL_TO_PASS` and `PASS_TO_PASS` become type-unstable. RepoGauge should accept JSON strings on import if necessary, but should emit arrays consistently.
- Avoid over-nesting metadata blobs that later analysis cannot query efficiently.

### Acceptance Criteria
- A single contract doc exists and is referenced in review checklists.
- Engineers can implement commands without re-deciding field names.
- JSONL and Parquet round-trip tests pass.

---

## Create package scaffold and dependency boundaries

### ID
rg-mvp-002

### Priority
P0

### Type
task

### Labels
scaffold, packaging, architecture, modules

### Dependencies
- rg-mvp-001

### Description
- Create the production package layout and move away from a few large flat modules.
- Establish clear dependency directions:
  - `mining` cannot import `runner`
  - `validation` can depend on `git_utils` and `exec`, but not on experiment-specific modules
  - `runner` can depend on `export`, `validation`, and `llm`
  - `parsers` should stay generic and reusable

### Subtasks
- Create empty packages and `__init__.py` files.
- Add `pyproject.toml` entry points for `repogauge`.
- Add lint/type/test tooling configuration.
- Add a simple `tests/` hierarchy and fixtures folder.

### Likely Files
- `pyproject.toml`
- `repogauge/__init__.py`
- `repogauge/cli.py`
- all new package directories under `repogauge/`
- `tests/unit/`
- `tests/integration/`
- `tests/golden/`

### Design
- Prefer small single-purpose modules:
  - `mining/scan.py` for history traversal
  - `mining/score.py` for scoring only
  - `validation/runner.py` for command execution only
  - `export/adapter.py` for code generation only
- Avoid a god-object orchestrator early; use thin service functions and explicit context objects.

### Testing / Validation
- Import smoke test that touches every top-level package.
- CLI entry point test to ensure `python -m repogauge.cli --help` works.
- Type checker should pass on the empty scaffold before feature work starts.

### Gotchas
- Do not mix persisted schemas into utility modules; keep contract classes discoverable.
- Avoid circular imports by keeping config and contracts low in the dependency graph.

### Acceptance Criteria
- Package installs locally with `pip install -e .`.
- Entry point resolves.
- Skeleton modules exist with import tests and lint/type baseline passing.

---

## Define CLI surface, config loading, and output directory semantics

### ID
rg-mvp-003

### Priority
P0

### Type
task

### Labels
cli, config, ux, artifacts

### Dependencies
- rg-mvp-002

### Description
- Implement the command surface for:
  - `mine`
  - `review`
  - `export`
  - `eval`
  - `run`
  - `analyze`
  - `train-router`
- Define config precedence:
  1. built-in defaults
  2. config file
  3. environment variables
  4. CLI flags
- Define output directory behavior and resumability semantics.

### Subtasks
- Create `repogauge/cli.py`.
- Implement shared path resolution and “safe overwrite” checks.
- Add `--out`, `--config`, `--resume`, `--dry-run`, `--llm-mode`, and `--verbose`.
- Document command I/O contracts.

### Likely Files
- `repogauge/cli.py`
- `repogauge/config.py`
- `repogauge/artifacts.py`
- `README.md`
- `tests/unit/test_cli.py`

### Design
- Commands should be explicit about whether they write a new artifact tree or reuse an existing one.
- `review` should not mutate `candidates.jsonl`; it should produce `reviewed.jsonl`.
- `eval --gold` should derive predictions from dataset rows instead of requiring a separate input file.
- `run` should accept both a matrix file and ad hoc overrides for smoke-testing.

### Testing / Validation
- CLI tests for help output and invalid combinations.
- Path tests for:
  - existing non-empty directory without `--resume`
  - `--resume` into partially completed artifact tree
  - missing dataset file
- Snapshot tests for generated usage text.

### Gotchas
- Be careful not to let `mine` and `export` write conflicting copies of `dataset.jsonl`.
- Relative paths in matrix/config files must be resolved relative to the config file location, not the current shell directory.

### Acceptance Criteria
- All planned commands exist with stable flag names.
- Output directory semantics are documented and tested.
- Engineers can run end-to-end smoke flows without guessing file paths.

---

## Implement manifests, structured logging, and resumability primitives

### ID
rg-mvp-004

### Priority
P0

### Type
task

### Labels
logging, manifests, resumability, observability

### Dependencies
- rg-mvp-003

### Description
- Implement machine-readable manifests for every major command.
- Add structured logs and per-step status markers so long-running jobs can be resumed safely.
- Make the artifact tree explain what happened without opening source code.

### Subtasks
- Add `mine.json`, `export.json`, `eval.json`, and `run.json` manifest writers.
- Implement step-level statuses: `pending`, `running`, `succeeded`, `failed`, `skipped`.
- Add timestamps, version info, host metadata, and CLI arguments.
- Ensure logs are persisted under `logs/` and referenced from manifests.

### Likely Files
- `repogauge/manifest.py`
- `repogauge/logging_utils.py`
- `repogauge/artifacts.py`
- `tests/unit/test_manifest.py`

### Design
- Keep logs append-only where practical.
- Add a per-command “inputs hash” or equivalent provenance fingerprint to help detect accidental misuse of resumed directories.
- Emit both human-readable console logs and JSONL logs.
- Store child process command lines, exit codes, durations, and output paths.

### Testing / Validation
- Resume tests that simulate partial failure and rerun the same command.
- Manifest integrity tests that verify logs referenced in the manifest actually exist.
- Ensure JSON logs remain parseable even when subprocesses emit invalid UTF-8; sanitize safely.

### Gotchas
- Do not store secrets in manifests or logs.
- Avoid writing massive stdout/stderr blobs inline into manifests; reference file paths instead.

### Acceptance Criteria
- Every top-level command produces a manifest.
- A failed step can be retried without corrupting the artifact directory.
- Logs are sufficient to debug validation and harness failures offline.

---

## Build reusable git, worktree, patch, and subprocess utilities

### ID
rg-mvp-005

### Priority
P0

### Type
task

### Labels
git, worktree, patching, subprocess, sandbox

### Dependencies
- rg-mvp-004

### Description
- Implement the low-level primitives shared by miner, validator, and experiment runner:
  - resolve repo root and default branch
  - list commits and parents
  - extract diffs
  - create isolated git worktrees
  - apply and reverse patches
  - run commands with timeouts and captured output

### Subtasks
- Add `repogauge/git_utils.py` and `repogauge/exec.py`.
- Implement `git worktree` lifecycle helpers.
- Implement patch apply helpers for:
  - clean apply
  - reject apply
  - reverse apply
- Add command runner with cwd, env, timeout, stdout/stderr capture, and streaming log file support.

### Likely Files
- `repogauge/git_utils.py`
- `repogauge/exec.py`
- `tests/unit/test_git_utils.py`
- `tests/integration/test_worktrees.py`
- `tests/integration/test_patch_apply.py`

### Design
- Worktrees are preferable to repeated clone/checkouts because validation and solver jobs need isolation but benefit from object reuse.
- Patch helpers should preserve exact diff text; do not normalize before storing gold patches.
- Subprocess runner should return structured results, not raw tuples.

### Testing / Validation
- Integration tests on a synthetic git repo fixture:
  - default branch detection
  - single-parent and merge-commit detection
  - clean and failed patch apply
  - worktree create/remove lifecycle
- Timeout and signal-handling tests for subprocess runner.

### Gotchas
- Clean up worktrees aggressively after failure or they will accumulate and confuse later runs.
- `git apply` semantics vary depending on context lines and path prefix; use consistent flags and document them.

### Acceptance Criteria
- Shared utilities exist and are reused instead of shelling out ad hoc in each subsystem.
- Worktree and patch integration tests pass.
- Subprocess results are structured and logged.

---

## Implement deterministic repo inspection and `repo_profile.json`

### ID
rg-mvp-010

### Priority
P0

### Type
task

### Labels
mining, inspection, repo-profile, environment

### Dependencies
- rg-mvp-005

### Description
- Inspect a local Python repository and emit a deterministic `repo_profile.json`.
- Detect:
  - repo identity
  - branch and commit range defaults
  - package manager signals
  - test runner signals
  - Python version hints
  - obvious env files and CI configs

### Subtasks
- Read `pyproject.toml`, `setup.py`, `setup.cfg`, `requirements*.txt`, `tox.ini`, `noxfile.py`, `.python-version`, GitHub Actions, and similar files.
- Infer package style (`src/` layout vs flat package).
- Infer likely test directories.
- Emit confidence annotations and provenance for each detection.

### Likely Files
- `repogauge/mining/inspect.py`
- `repogauge/config.py`
- `tests/unit/test_inspect.py`
- `tests/fixtures/repos/`

### Design
- This stage must be deterministic and should not require model access.
- Output should include:
  - `repo_name`
  - `repo_root`
  - `default_branch`
  - `python_hints`
  - `install_hints`
  - `test_runner_hints`
  - `ci_files`
  - `test_paths`
  - `profile_warnings`
- Treat missing or conflicting signals as warnings, not fatal errors.

### Testing / Validation
- Fixture repos covering:
  - `pyproject.toml` with pytest
  - `tox.ini` only
  - `requirements-dev.txt` plus `setup.py`
  - conflicting Python version hints
- Snapshot tests for `repo_profile.json`.

### Gotchas
- Do not overfit to GitHub Actions; some repos use tox/nox or custom scripts.
- A repo can have multiple requirements files; preserve all candidates and defer selection to env detection.

### Acceptance Criteria
- `repo_profile.json` is deterministic across runs.
- Engineers can understand the repo’s likely install/test shape from the profile alone.
- Inspection tests cover the common Python packaging patterns.

---

## Define file-role taxonomy and path-based classifier

### ID
rg-mvp-011

### Priority
P0

### Type
task

### Labels
mining, classifier, paths, patch-splitting

### Dependencies
- rg-mvp-010

### Description
- Implement the canonical file-role classifier used by:
  - candidate scanning
  - patch splitting
  - validation test targeting
- Roles:
  - `prod`
  - `test`
  - `test_support`
  - `config_build`
  - `docs`
  - `generated_vendor`
  - `unknown`

### Subtasks
- Add path heuristics for common test patterns:
  - `tests/**`
  - `test/**`
  - `test_*.py`
  - `*_test.py`
  - `conftest.py`
  - fixture directories under tests
- Add deny patterns for:
  - vendored directories
  - generated snapshots
  - docs-only trees
- Add a confidence/provenance field per classification.

### Likely Files
- `repogauge/mining/classify.py`
- `repogauge/mining/split_patch.py`
- `repogauge/validation/testsel.py`
- `tests/unit/test_classify.py`

### Design
- This taxonomy is central to multiple subsystems; do not duplicate logic.
- Ambiguous files should remain `unknown` and optionally flow into LLM triage later.
- Classifier output should include both `role` and `reason` so reviewers can debug mistakes.

### Testing / Validation
- Unit tests for path patterns and edge cases.
- Golden tests on real-world-style file lists:
  - `pytest.ini`
  - `tox.ini`
  - `docs/conf.py`
  - fixtures outside the standard test root
  - benchmark/demo directories that look test-like

### Gotchas
- Not every `conftest.py` belongs in `test_patch`; only include it when the selected commit changed it and it is required for the regression test to run.
- Avoid labelling all config files as `test_support`; some are packaging-only noise.

### Acceptance Criteria
- File-role classification is centralized and tested.
- Patch splitting and scan scoring can import the same logic.
- Ambiguous files remain visible for human or LLM review instead of being silently misclassified.

---

## Build commit walker and diff extraction pipeline

### ID
rg-mvp-012

### Priority
P0

### Type
task

### Labels
mining, git-history, diff, scan

### Dependencies
- rg-mvp-005
- rg-mvp-011

### Description
- Walk recent history for the default branch or user-supplied range.
- Produce one `ScanRow` per candidate commit with raw diff statistics and file-role summaries.

### Subtasks
- Enumerate commits with metadata:
  - SHA
  - parent count
  - author date
  - subject/body
- Extract changed files, hunks, insertions/deletions, and rename/move signals.
- Attach file-role counts and high-level commit shape descriptors.

### Likely Files
- `repogauge/mining/scan.py`
- `repogauge/git_utils.py`
- `tests/unit/test_scan.py`
- `tests/integration/test_scan_realistic.py`

### Design
- Do not read the whole repo contents into memory for every commit; rely on git metadata and targeted diff extraction.
- Persist `scan.jsonl` even for rejected commits; later heuristics and LLM work need the full audit trail.
- Include enough raw metadata to explain downstream scores:
  - `n_prod_files`
  - `n_test_files`
  - `n_config_files`
  - `n_hunks`
  - `total_changed_lines`
  - `is_merge`
  - `is_revert`
  - `has_rename_only`

### Testing / Validation
- Synthetic repo fixtures with:
  - simple bugfix commit
  - merge commit
  - docs-only commit
  - rename-only commit
  - large refactor
- Snapshot test for `scan.jsonl`.

### Gotchas
- Git rename detection can be noisy; store both git’s rename flag and raw paths when possible.
- Some squash merges may have noisy commit messages; do not rely on message text alone.

### Acceptance Criteria
- `scan.jsonl` exists and includes every inspected commit.
- Scan output contains enough metadata to score and review candidates without rerunning git commands.
- Synthetic history tests pass.

---

## Implement hard reject filters and heuristic scoring

### ID
rg-mvp-013

### Priority
P0

### Type
task

### Labels
mining, scoring, heuristics, shortlist

### Dependencies
- rg-mvp-012

### Description
- Implement deterministic filtering and scoring for candidate bugfix commits.
- Encode the initial scoring policy described in the product plan.

### Subtasks
- Hard reject:
  - merge commits
  - reverts
  - docs-only changes
  - formatting-only changes
  - dependency-only bumps
  - generated/vendor-only diffs
  - rename-only changes
  - huge refactors
- Score positives:
  - touches prod and tests
  - bugfix-like message
  - small/medium patch
  - linked issue/PR signal
  - new assertions or new test functions

### Likely Files
- `repogauge/mining/score.py`
- `repogauge/mining/scan.py`
- `tests/unit/test_scoring.py`

### Design
- Persist both the final numeric score and a score breakdown list.
- Implement threshold bands:
  - `>= 8` auto-shortlist
  - `5-7.99` review / optional LLM queue
  - `< 5` reject
- Keep weights configurable but ship sane defaults in code and config.

### Testing / Validation
- Table-driven tests for each filter and score component.
- Golden tests that show why a commit scored the way it did.
- Regression tests to prevent accidental score drift after refactors.

### Gotchas
- Formatting-only commits are hard to detect from diff stats alone; use line-pattern heuristics but be conservative.
- Some real bugfixes do not touch tests. Rejecting them is acceptable for MVP because evaluation quality matters more than recall.

### Acceptance Criteria
- `candidates.jsonl` includes score, score breakdown, and decision band.
- Reviewers can explain every shortlist/reject result from stored metadata.
- Scoring tests cover both positives and negatives.

---

## Generate human review artifacts and acceptance workflow

### ID
rg-mvp-014

### Priority
P0

### Type
task

### Labels
review, curation, ux, miner

### Dependencies
- rg-mvp-013

### Description
- Turn scored candidates into human-readable review artifacts.
- Provide a low-friction workflow for curators to accept, reject, or annotate candidates before export.

### Subtasks
- Generate `review.md` and `review.html`.
- Include per-candidate:
  - commit SHA and message
  - score breakdown
  - changed files grouped by role
  - extracted linked issue/PR references if available
  - placeholder for reviewer notes and status
- Implement `repogauge review` to convert review decisions into `reviewed.jsonl`.

### Likely Files
- `repogauge/review.py`
- `repogauge/mining/score.py`
- `tests/unit/test_review.py`

### Design
- Keep the review artifact static and portable; do not require a web server.
- `reviewed.jsonl` should include reviewer action, notes, and optional overrides such as “force include despite no test change”.
- Preserve original scores even if a human overrides the candidate state.

### Testing / Validation
- Snapshot tests for markdown and HTML outputs.
- Round-trip test: `candidates.jsonl` -> review input -> `reviewed.jsonl`.
- Ensure unicode and long commit messages render correctly.

### Gotchas
- Do not make human review mandatory for all workflows; batch export should still be possible for automated experiments.
- Keep HTML generation simple; avoid a templating dependency explosion for MVP.

### Acceptance Criteria
- Curators can inspect a shortlist without opening the repo or raw JSONL.
- Review decisions are persisted separately from original candidate data.
- `review` command works in both manual and scripted flows.

---

## Add optional issue/PR enrichment from GitHub metadata

### ID
rg-mvp-015

### Priority
P1

### Type
task

### Labels
mining, github, enrichment, optional

### Dependencies
- rg-mvp-014

### Description
- Add optional enrichment that resolves linked issue and PR metadata from GitHub.
- Improve problem statements and review context when network access and repository remotes are available.

### Subtasks
- Parse issue/PR references from commit messages and merge metadata.
- Resolve repository remote URL to owner/repo.
- Fetch title/body/URLs for linked issues and PRs when the user opts in.
- Cache enrichment results in the artifact directory.

### Likely Files
- `repogauge/mining/enrich.py`
- `repogauge/config.py`
- `tests/unit/test_enrich.py`

### Design
- This subsystem must be optional and degrade cleanly.
- Store provenance for each enriched field:
  - `from_issue`
  - `from_pr`
  - `from_commit`
  - `from_llm`
- Respect rate limits and unauthenticated fallback behavior.

### Testing / Validation
- Unit tests for URL/reference parsing.
- Mocked API tests for successful and failed enrichment.
- Cache reuse test.

### Gotchas
- Private or offline repos may not have resolvable GitHub metadata; do not make this a dependency for problem statement generation.
- Avoid storing access tokens or raw API headers in logs.

### Acceptance Criteria
- Enrichment can be enabled or disabled explicitly.
- Missing GitHub metadata does not block mining.
- Review and synthesis code can consume enriched data when available.

---

## Build LLM abstraction and deterministic triage schema for mining

### ID
rg-mvp-016

### Priority
P1

### Type
task

### Labels
llm, mining, triage, schema

### Dependencies
- rg-mvp-013

### Description
- Implement the model abstraction used for optional candidate triage, file-role rescue, and problem statement drafting.
- Enforce structured JSON output and schema validation.

### Subtasks
- Create `repogauge/llm/base.py`, `schemas.py`, and `prompts.py`.
- Define the triage response schema:
  - `is_bugfix_eval`
  - `confidence`
  - `reason`
  - `prod_files`
  - `test_files`
  - `test_support_files`
  - `problem_statement_draft`
  - `environment_hints`
- Add response caching keyed by prompt hash and input hash.

### Likely Files
- `repogauge/llm/base.py`
- `repogauge/llm/schemas.py`
- `repogauge/llm/prompts.py`
- `repogauge/mining/classify.py`
- `repogauge/mining/synthesize.py`
- `tests/unit/test_llm_schema.py`

### Design
- Model support must be string-based, not hardcoded enum-only.
- The abstraction should carry:
  - model name
  - provider kind
  - prompt version
  - usage/cost info if available
  - raw request/response references if locally cached
- Fail closed: if schema validation fails, drop the LLM hint instead of half-using it.

### Testing / Validation
- Schema validation tests for valid and invalid model output.
- Cache hit/miss tests.
- Ensure no LLM path is invoked when `--llm-mode off`.

### Gotchas
- Never let LLM triage silently override deterministic reject reasons without leaving an audit trail.
- Prompt and schema versioning must be explicit so experiment comparisons remain meaningful.

### Acceptance Criteria
- Optional triage path exists with structured outputs and caching.
- The miner remains useful with LLMs fully disabled.
- All LLM-derived fields are marked as advisory.

---

## Orchestrate candidate materialization from reviewed commits

### ID
rg-mvp-020

### Priority
P0

### Type
task

### Labels
materialization, export, orchestration

### Dependencies
- rg-mvp-014

### Description
- Convert accepted reviewed candidates into materialized instance work items.
- Each work item should gather all information needed for patch splitting, synthesis, env detection, and validation.

### Subtasks
- Implement a materializer that loads `reviewed.jsonl` or accepted `candidates.jsonl`.
- Resolve the base commit as the candidate commit’s single parent.
- Attach scan metadata, enriched metadata, and optional LLM hints.
- Produce an in-memory or on-disk intermediate structure that downstream steps consume.

### Likely Files
- `repogauge/mining/materialize.py`
- `repogauge/export/dataset.py`
- `tests/unit/test_materialize.py`

### Design
- Materialization should stop early for commits that violate v1 constraints:
  - not single-parent
  - patch cannot be extracted cleanly
  - empty prod diff or empty test diff after split
- Record explicit rejection reasons so the user can inspect what was lost between review and export.

### Testing / Validation
- Unit tests for base commit resolution.
- Fixture tests for accepted/rejected candidates.
- Ensure intermediate representation retains all audit metadata.

### Gotchas
- A candidate that looks good in the scan may still fail once file-role split is applied; preserve traceability back to the original candidate row.
- Keep the intermediate structure serializable for debugging.

### Acceptance Criteria
- Accepted candidates become materialization work items with explicit base commit and provenance.
- Invalid candidates fail with actionable reasons.
- Materialization is deterministic for a given input artifact.

---

## Implement production-vs-test patch split with exact diff preservation

### ID
rg-mvp-021

### Priority
P0

### Type
task

### Labels
patch, split, dataset, diffs

### Dependencies
- rg-mvp-011
- rg-mvp-020

### Description
- Split a fixing commit into:
  - `patch`: production-code diff only
  - `test_patch`: tests plus required test-support files
- Preserve exact unified diff text and apply semantics.

### Subtasks
- Parse commit diffs into per-file hunks.
- Classify files using the centralized file-role taxonomy.
- Build two patch blobs with correct headers and context.
- Reject commits where split results are empty or clearly nonsensical.

### Likely Files
- `repogauge/mining/split_patch.py`
- `repogauge/git_utils.py`
- `tests/unit/test_split_patch.py`
- `tests/golden/test_split_patch_golden.py`

### Design
- File-level split is acceptable for MVP; do not attempt hunk-level split within a single file unless a later ADR justifies it.
- `test_support` files belong in `test_patch` only when they are plausibly required to execute the changed regression test.
- Preserve original path casing and git diff headers.

### Testing / Validation
- Golden tests on representative commits:
  - prod + tests
  - prod + conftest
  - prod + pytest.ini
  - ambiguous helper module
- Apply both generated patches in fresh worktrees to verify syntax and patch integrity.

### Gotchas
- Renames across prod/test boundaries are risky; reject them in MVP rather than implementing partial rename logic.
- Empty `patch` or empty `test_patch` should usually reject the instance.

### Acceptance Criteria
- `patch` and `test_patch` are generated as valid unified diffs.
- Split logic is shared and tested with gold fixtures.
- Failed or ambiguous splits are rejected with reason codes.

---

## Synthesize issue-style problem statements with provenance

### ID
rg-mvp-022

### Priority
P0

### Type
task

### Labels
problem-statement, synthesis, dataset

### Dependencies
- rg-mvp-020

### Description
- Generate the `problem_statement` field for each dataset instance.
- Follow the priority order:
  1. linked issue title/body
  2. PR title/body
  3. commit message plus diff summary
  4. LLM-generated issue-style restatement

### Subtasks
- Implement deterministic synthesis from issue/PR/commit text.
- Add optional LLM restatement when deterministic text is weak or absent.
- Record provenance and source snippets in metadata.

### Likely Files
- `repogauge/mining/synthesize.py`
- `repogauge/mining/enrich.py`
- `repogauge/llm/prompts.py`
- `tests/unit/test_synthesize.py`

### Design
- The output should read like a GitHub issue, not a commit message.
- Prefer concise factual statements:
  - observed behavior
  - reproduction condition
  - expected behavior
- Avoid leaking gold fix details into the statement.
- Include `problem_statement_source` in metadata.

### Testing / Validation
- Snapshot tests for deterministic synthesis.
- LLM-off tests to confirm useful fallback without model access.
- Review generated statements against a small hand-labeled fixture set.

### Gotchas
- Commit messages often mention the fix instead of the user-visible bug. Restate from the bug perspective whenever possible.
- Do not include stack traces unless they materially help and were present in the source artifact.

### Acceptance Criteria
- Every exportable instance has a non-empty problem statement.
- Provenance for the statement is stored.
- The statement remains useful with no model access.

---

## Compute environment signature and stable `version` key

### ID
rg-mvp-023

### Priority
P0

### Type
task

### Labels
environment, versioning, specs, dataset

### Dependencies
- rg-mvp-010
- rg-mvp-020

### Description
- Define and implement the stable environment signature used for the dataset `version` field and harness adapter lookup.

### Subtasks
- Identify relevant inputs:
  - detected package version if available
  - Python version hint
  - test runner
  - install strategy
  - dependency signature hash
- Produce a normalized key like:
  - `0.9.2__py311__pytest__reqhash_<hash>`
- Group compatible instances by identical signature.

### Likely Files
- `repogauge/export/specs.py`
- `repogauge/validation/env_detect.py`
- `tests/unit/test_version_key.py`

### Design
- The key should be stable enough to maximize image reuse, but granular enough to avoid false sharing between incompatible environments.
- Include a canonical hash input order so the same repo produces the same key across machines.
- If no package version exists, use a repo-local sentinel such as `repover_unknown`.

### Testing / Validation
- Determinism tests on repeated computation.
- Collision tests on fixture repos with small env variations.
- Grouping tests that show compatible instances reuse the same key.

### Gotchas
- Avoid hashing absolute file paths or machine-local state; signatures must be portable.
- Do not encode too much incidental data or every instance will get its own useless `version` bucket.

### Acceptance Criteria
- `version` is deterministic and documented.
- Instances with identical environment signatures share a key.
- The adapter generator can consume the same signature object without recomputation drift.

---

## Export SWE-bench-style dataset rows and gold predictions

### ID
rg-mvp-024

### Priority
P0

### Type
task

### Labels
dataset, export, predictions, swebench

### Dependencies
- rg-mvp-021
- rg-mvp-022
- rg-mvp-023

### Description
- Emit the local dataset and gold predictions in the expected SWE-bench-style shape.
- Include extra metadata fields that RepoGauge needs for debugging and curation.

### Subtasks
- Generate `dataset/dataset.jsonl`.
- Generate `dataset/predictions.gold.jsonl`.
- Ensure `instance_id` is stable and collision-resistant.
- Store `created_at`, provenance metadata, and validation placeholders.

### Likely Files
- `repogauge/export/dataset.py`
- `repogauge/export/predictions.py`
- `tests/unit/test_dataset_export.py`
- `tests/unit/test_predictions_export.py`

### Design
- `instance_id` should follow the repo-prefixed form:
  - `owner__repo-rg-<shortsha>`
- `PredictionRow.model_patch` for gold predictions should be the instance’s `patch`.
- Export arrays for `FAIL_TO_PASS` and `PASS_TO_PASS`; avoid emitting JSON-encoded strings unless compatibility import requires it.

### Testing / Validation
- JSONL schema tests.
- Snapshot tests for example dataset rows.
- Round-trip tests to load dataset and predictions back into typed contracts.

### Gotchas
- Leave `FAIL_TO_PASS` and `PASS_TO_PASS` empty only before validation; do not mark an instance exportable until validation fills them in.
- Keep extra metadata under predictable keys; avoid dumping arbitrary internal state blobs.

### Acceptance Criteria
- RepoGauge emits syntactically valid dataset and prediction rows.
- `instance_id`, `version`, `patch`, and `test_patch` are stable.
- Export artifacts can be loaded by later validation and harness steps.

---

## Implement deterministic environment detection heuristics

### ID
rg-mvp-030

### Priority
P0

### Type
task

### Labels
validation, environment, install, heuristics

### Dependencies
- rg-mvp-010
- rg-mvp-024

### Description
- Convert repo inspection hints into a concrete install and test environment plan for validation.
- This is the Level 1 deterministic part of the environment ladder.

### Subtasks
- Infer:
  - Python executable/version target
  - install commands
  - optional build commands
  - base test command
- Support common Python repo shapes:
  - `pyproject.toml`
  - `setup.py`
  - `requirements-dev.txt`
  - extras like `.[test]` or `.[dev]`
  - direct pytest invocation
  - tox/nox fallback

### Likely Files
- `repogauge/validation/env_detect.py`
- `repogauge/export/specs.py`
- `tests/unit/test_env_detect.py`

### Design
- Output an `EnvPlan` object with:
  - `python_version`
  - `pre_install`
  - `install`
  - `build`
  - `test_cmd_base`
  - `strategy_name`
  - `confidence`
- Keep this stage deterministic and auditable.
- Prefer simpler commands first:
  - `pip install -e .`
  - add `pytest` only when clearly missing
  - use tox only if direct pytest is not viable

### Testing / Validation
- Fixture repos for the main packaging patterns.
- Assert generated command lists are ordered and deterministic.
- Negative tests for missing dependency files or contradictory hints.

### Gotchas
- Do not blindly install every requirements file; some repos have incompatible or obsolete lockfiles.
- Tox and nox can hide lots of environment setup complexity; use them as explicit fallback, not default.

### Acceptance Criteria
- Common Python repo patterns produce deterministic `EnvPlan` outputs.
- Strategy names and provenance are persisted.
- The next dry-run stage can consume the plan without ad hoc inference.

---

## Add dry-run correction ladder and environment rescue flow

### ID
rg-mvp-031

### Priority
P0

### Type
task

### Labels
validation, dry-run, environment, fallback

### Dependencies
- rg-mvp-030

### Description
- Implement the Level 2 and Level 3 parts of the environment ladder:
  - deterministic dry-run correction
  - optional LLM rescue when deterministic attempts fail

### Subtasks
- Run install and a smoke-test on HEAD or a validation worktree.
- Deterministic corrections:
  - upgrade `pip`, `setuptools`, `wheel`
  - try extras like `.[test]`, `.[dev]`, `.[tests]`
  - switch from `pytest` to `python -m pytest`
  - fall back to tox/nox where supported
- Optional LLM rescue based on config files and failure logs.

### Likely Files
- `repogauge/validation/dryrun.py`
- `repogauge/validation/env_detect.py`
- `repogauge/llm/prompts.py`
- `tests/integration/test_dryrun.py`

### Design
- Persist every attempted plan and its outcome.
- Promote the first successful corrected plan to the canonical instance/group environment plan.
- Rescue prompts should include only the minimal local signals needed and must be opt-in.

### Testing / Validation
- Integration tests with fixture repos that require:
  - extras install
  - `python -m pytest`
  - pip toolchain upgrade
- Mocked LLM rescue tests.
- Ensure exact chosen strategy is cached and reused.

### Gotchas
- Dry-run correction can accidentally turn into unbounded guesswork. Limit attempts and document the search order.
- Do not let a successful rescue path leak into unrelated environment signatures without proof they are compatible.

### Acceptance Criteria
- Failed deterministic plans can recover through a bounded correction ladder.
- Successful plans are cached and reused.
- LLM rescue remains optional and auditable.

---

## Build targeted test selection and command generation

### ID
rg-mvp-032

### Priority
P0

### Type
task

### Labels
validation, tests, targeting, pytest

### Dependencies
- rg-mvp-021
- rg-mvp-030

### Description
- Determine which tests to run for validation and how to invoke them.
- Focus validation on the tests touched by `test_patch`, plus minimal related support when needed.

### Subtasks
- Extract changed test files from `test_patch`.
- Generate targeted commands:
  - file-level pytest targets
  - optional node IDs when directly recoverable
- Fall back to package- or suite-level commands when file-level execution is impossible.

### Likely Files
- `repogauge/validation/testsel.py`
- `repogauge/validation/env_detect.py`
- `tests/unit/test_testsel.py`

### Design
- The validator should prefer the narrowest command that still exercises the regression tests.
- Store both:
  - `targeted_test_cmd`
  - `targeted_test_inputs`
- Keep targeting conservative; running a slightly larger set is acceptable if it stays reproducible.

### Testing / Validation
- Unit tests for path-to-command generation.
- Integration tests with:
  - standard pytest layout
  - nested package tests
  - changed `conftest.py`
- Ensure outputs are stable across platforms.

### Gotchas
- Test helper changes may require running more than the directly changed file.
- Some repos rely on cwd-sensitive invocation; record and reuse working-directory decisions.

### Acceptance Criteria
- Validation can produce a targeted command for common pytest repositories.
- Target selection metadata is persisted.
- The command is compatible with JUnit forcing in the next stage.

---

## Force JUnit XML output and define the generic parser contract

### ID
rg-mvp-033

### Priority
P0

### Type
task

### Labels
validation, junit, parser, harness

### Dependencies
- rg-mvp-032

### Description
- Standardize on JUnit XML as the primary per-test result format for v1.
- Define how RepoGauge will force its creation and parse test IDs robustly.

### Subtasks
- Add JUnit-related command injection for pytest:
  - `--junitxml=<path>`
- Define canonical output paths in worktrees and logs.
- Implement low-level XML parsing rules for:
  - passed
  - failed
  - errored
  - skipped
  - xfail/xpass handling policy

### Likely Files
- `repogauge/validation/junit.py`
- `repogauge/parsers/junit.py`
- `repogauge/validation/testsel.py`
- `tests/unit/test_junit_parser.py`

### Design
- Do not parse free-form stdout when a JUnit XML file is available.
- Canonicalize test IDs to the string form exported in dataset rows.
- Decide and document how to treat skipped or xfailed tests:
  - they are not `FAIL_TO_PASS`
  - they may be excluded from `PASS_TO_PASS` depending on policy

### Testing / Validation
- Unit tests with handcrafted JUnit XML fixtures.
- Tests for missing XML, malformed XML, and partial XML writes.
- Ensure the parser can still extract useful failure reasons for logs.

### Gotchas
- Pytest plugins can emit differing classname/name combinations in JUnit XML; canonicalization must be tolerant.
- XML files can be truncated on crash. Treat that as a run failure, not a partial success.

### Acceptance Criteria
- A single generic JUnit parser exists and is used by validation and harness grading integration.
- JUnit paths and parsing rules are documented.
- XML parser tests cover the major pytest cases.

---

## Implement four-run validation controller

### ID
rg-mvp-034

### Priority
P0

### Type
task

### Labels
validation, runner, execution, stability

### Dependencies
- rg-mvp-005
- rg-mvp-031
- rg-mvp-033

### Description
- Implement the authoritative validation controller that executes:
  1. base sanity
  2. base + test_patch
  3. base + patch + test_patch
  4. reruns for flake detection

### Subtasks
- Create isolated worktrees per run or a carefully resettable worktree plan.
- Apply patches in the correct order.
- Run install/build/test commands.
- Capture per-run:
  - command lists
  - exit codes
  - durations
  - JUnit path
  - parsed test results
  - raw logs

### Likely Files
- `repogauge/validation/runner.py`
- `repogauge/validation/validate.py`
- `repogauge/exec.py`
- `tests/integration/test_validate_runs.py`

### Design
- Treat Run 1 as a repo sanity gate; if the base repo cannot execute at all, reject early with a clear reason.
- Run 2 and Run 3 must use the same targeted test command for comparability.
- Prefer clean worktree recreation or guaranteed hard reset between runs to avoid contamination.

### Testing / Validation
- Integration tests on miniature repos with known fail/pass transitions.
- Simulate patch apply failure, install failure, and missing JUnit output.
- Verify that reruns repeat the exact same environment plan and test command.

### Gotchas
- Reusing the same worktree carelessly can leak installed editable-package state or modified files across runs.
- Always record whether a failure is infra/environment vs test semantics.

### Acceptance Criteria
- The validator performs all planned runs with durable logs and parsed outputs.
- Failures are categorized and persisted.
- The run controller is deterministic for the same input instance and environment plan.

---

## Compute `FAIL_TO_PASS`, `PASS_TO_PASS`, and flake rejection policy

### ID
rg-mvp-035

### Priority
P0

### Type
task

### Labels
validation, semantics, ftop, ptop, flake

### Dependencies
- rg-mvp-034

### Description
- Turn the results of Run 2 and Run 3 into official instance semantics.
- Enforce acceptance rules for exportability.

### Subtasks
- Compute:
  - `FAIL_TO_PASS` = fail in Run 2, pass in Run 3
  - `PASS_TO_PASS` = pass in Run 2, pass in Run 3
- Compare reruns to detect instability.
- Reject instances when:
  - no `FAIL_TO_PASS`
  - any `PASS_TO_PASS` regression
  - outcomes differ across reruns
  - patches do not apply cleanly
  - validator or harness infrastructure fails irrecoverably

### Likely Files
- `repogauge/validation/validate.py`
- `repogauge/export/dataset.py`
- `tests/unit/test_fail_to_pass.py`

### Design
- Store complete per-run test ID sets, not just the final derived lists.
- Preserve rejected instances in `validation.jsonl` with explicit reason codes.
- Keep flake policy strict for MVP; recall is less important than benchmark trustworthiness.

### Testing / Validation
- Table-driven tests for all outcome combinations.
- Regression tests for flaky rerun scenarios.
- Ensure stable ordering of exported test ID lists.

### Gotchas
- Do not include skipped tests in `PASS_TO_PASS` unless the policy explicitly allows it.
- A repo may report different node IDs across runs if cwd or package import roots differ; canonicalize consistently upstream.

### Acceptance Criteria
- Exported instances have stable, non-empty `FAIL_TO_PASS`.
- Rejected instances include clear reason codes.
- FTOP/PTOP derivation is fully covered by tests.

---

## Export validation evidence, logs, and failure taxonomy

### ID
rg-mvp-036

### Priority
P0

### Type
task

### Labels
validation, evidence, logs, debugging

### Dependencies
- rg-mvp-035

### Description
- Persist the complete validation audit trail so users can understand why instances were accepted or rejected.

### Subtasks
- Emit `dataset/validation.jsonl`.
- Save log bundles under `logs/validation/<instance_id>/`.
- Define a failure taxonomy:
  - `base_repo_unrunnable`
  - `env_install_failed`
  - `test_targeting_failed`
  - `test_patch_apply_failed`
  - `patch_apply_failed`
  - `no_fail_to_pass`
  - `pass_to_pass_regression`
  - `flaky_outcomes`
  - `missing_junit`
  - `unknown_validator_failure`

### Likely Files
- `repogauge/validation/evidence.py`
- `repogauge/validation/validate.py`
- `tests/unit/test_validation_evidence.py`

### Design
- Validation rows should include enough data for later filtering and analysis:
  - run status
  - chosen environment strategy
  - chosen test strategy
  - test counts
  - file paths to logs
- Separate concise row fields from bulky artifacts.

### Testing / Validation
- Schema tests for `validation.jsonl`.
- Filesystem tests verifying log paths exist.
- Snapshot tests for common reject reasons.

### Gotchas
- Avoid storing megabytes of raw stdout in JSONL rows.
- Keep reason codes stable once public; analyses and dashboards will depend on them.

### Acceptance Criteria
- Every attempted instance has a validation row and log bundle.
- Failure reasons are normalized.
- Users can debug rejected candidates from artifacts alone.

---

## Generate harness adapter registration code and serialized specs

### ID
rg-mvp-040

### Priority
P0

### Type
task

### Labels
harness, adapter, codegen, swebench

### Dependencies
- rg-mvp-024
- rg-mvp-033
- rg-mvp-036

### Description
- Generate the repo-specific adapter code that registers new repo/version/environment entries with the official harness at runtime.

### Subtasks
- Produce `dataset/adapter/specs.json` containing normalized repo/version specs.
- Produce Python code under `dataset/adapter/` that:
  - registers `MAP_REPO_TO_EXT`
  - registers `MAP_REPO_VERSION_TO_SPECS`
  - registers `MAP_REPO_TO_PARSER`
- Add a stable import entry point for `repogauge eval`.

### Likely Files
- `repogauge/export/adapter.py`
- `repogauge/export/specs.py`
- `tests/unit/test_adapter_codegen.py`

### Design
- Treat codegen as deterministic output from `specs.json`.
- Keep the generated module tiny and explicit rather than clever.
- Generated specs should include:
  - python version
  - pre-install/install/build commands
  - test command
  - parser name
- Repo identifier should match the dataset `repo` field exactly.

### Testing / Validation
- Golden tests for generated code text.
- Import test for generated adapter in an isolated temp directory.
- Ensure re-running codegen does not produce diff churn when inputs are unchanged.

### Gotchas
- Do not couple the adapter to internal RepoGauge runtime objects; generated code should depend only on stable public harness and parser import paths.
- Missing repo/version map entries will fail late and opaquely; validate them before writing code.

### Acceptance Criteria
- Adapter code and specs are generated deterministically.
- `repogauge eval` can import the generated adapter.
- Unit tests cover map registration and idempotent codegen.

---

## Implement generic JUnit parser bridge into harness grading

### ID
rg-mvp-041

### Priority
P0

### Type
task

### Labels
harness, parser, junit, grading

### Dependencies
- rg-mvp-040

### Description
- Bridge RepoGauge’s JUnit parsing strategy into the parser interface expected by the official harness.

### Subtasks
- Define `parse_repogauge_junit` or equivalent callable.
- Make sure the generated adapter points the repo to that parser.
- Normalize parser output into the structure the harness expects for grading.

### Likely Files
- `repogauge/parsers/junit.py`
- `repogauge/export/adapter.py`
- `tests/unit/test_harness_parser_bridge.py`

### Design
- Keep this parser focused on the pytest+JUnit contract; do not try to support arbitrary test runners in v1.
- Include helpful error messaging when the expected XML is absent or malformed.
- Reuse the same canonicalization logic from validation so test IDs match exactly.

### Testing / Validation
- Unit tests with fixture XML and expected harness-facing outputs.
- Compatibility test that imports the parser in the same way the adapter will.
- Negative tests for malformed XML and missing files.

### Gotchas
- If validation and harness grading canonicalize test IDs differently, gold patches will appear to fail even when they are correct.
- Keep the parser dependency-light; it may run inside harness-managed environments.

### Acceptance Criteria
- Generated adapters can reference the parser successfully.
- Parser output is consistent with validation-time semantics.
- Harness-facing compatibility tests pass.

---

## Implement `repogauge eval` wrapper and gold mode

### ID
rg-mvp-042

### Priority
P0

### Type
task

### Labels
eval, harness, cli, wrapper

### Dependencies
- rg-mvp-040
- rg-mvp-041

### Description
- Implement the CLI wrapper that imports the generated adapter and dispatches to the official harness for local evaluation.

### Subtasks
- Resolve dataset path and adjacent adapter path.
- Import the generated adapter module dynamically.
- Generate gold predictions on the fly when `--gold` is supplied.
- Pass through relevant harness flags such as worker counts and cache controls.

### Likely Files
- `repogauge/cli.py`
- `repogauge/export/adapter.py`
- `repogauge/runner/judge.py`
- `tests/integration/test_eval_wrapper.py`

### Design
- `repogauge eval` should feel like a thin, honest wrapper:
  1. register specs
  2. load dataset
  3. load predictions
  4. call the harness
- Log the exact harness invocation and output locations.
- Support user-supplied predictions files in addition to gold mode.

### Testing / Validation
- Integration smoke test against a tiny fixture dataset and stub adapter.
- CLI tests for `--gold` and explicit `--predictions`.
- Ensure missing adapter or mismatched repo/version emits actionable errors.

### Gotchas
- Avoid hiding harness exceptions behind generic RepoGauge errors; preserve the original failure context.
- Dynamic import paths can be brittle; normalize and test path handling on temp dirs.

### Acceptance Criteria
- `repogauge eval` can evaluate gold predictions through the official harness path.
- Wrapper logs make clear that the official harness is being used underneath.
- Error handling points users at missing adapter/spec issues quickly.

---

## Add end-to-end gold resolution test suites

### ID
rg-mvp-043

### Priority
P0

### Type
task

### Labels
integration, gold, harness, release-gate

### Dependencies
- rg-mvp-042

### Description
- Build the release-gate test suite proving that gold predictions resolve every exported fixture instance through the official harness wrapper.

### Subtasks
- Create at least one miniature Python fixture repo with:
  - bugfix commit
  - regression test
  - stable FTOP/PTOP behavior
- Run the full chain:
  - mine
  - export
  - validate
  - generate adapter
  - eval --gold

### Likely Files
- `tests/integration/test_end_to_end_gold.py`
- `tests/fixtures/repos/<fixture_repo>/`
- `tests/golden/`

### Design
- This is the credibility test for MVP. Keep fixture repos tiny and deterministic.
- Prefer one or two crystal-clear fixture repos over many flaky, realistic ones.
- Store expected outputs as golden artifacts.

### Testing / Validation
- Full e2e integration in CI for at least one fixture repo.
- Optional slower local test suite for multiple fixtures.
- Assert that all exported instances resolve under gold predictions.

### Gotchas
- If this suite is missing, the team may ship a miner that produces pretty JSON but not actually valid benchmark tasks.
- Keep fixture dependencies lightweight enough for CI.

### Acceptance Criteria
- The e2e gold suite passes in CI.
- It proves the artifact pair is sufficient to evaluate an unseen repo locally.
- Gold patches resolve all exported fixture instances.

---

## Define matrix schema, planner, and run manifests

### ID
rg-mvp-050

### Priority
P1

### Type
task

### Labels
runner, matrix, planner, experiments

### Dependencies
- rg-mvp-042

### Description
- Implement `matrix.yaml` parsing and job expansion for experiment runs.

### Subtasks
- Define config sections:
  - dataset
  - execution
  - fairness
  - providers
  - solvers
- Expand into jobs of shape:
  - `(instance_id, solver_id, seed)`
- Persist `runs/<run_id>/matrix.yaml` and `jobs.jsonl`.

### Likely Files
- `repogauge/runner/matrix.py`
- `repogauge/runner/planner.py`
- `repogauge/config.py`
- `tests/unit/test_matrix.py`

### Design
- Keep provider and solver definitions separate.
- Support instance filtering and repeat seeds.
- Normalize relative paths relative to the matrix file location.
- Include prompt/tool policy hashes in planned jobs.

### Testing / Validation
- YAML schema tests.
- Job expansion tests with filters and repeat seeds.
- Determinism tests ensuring stable job order when shuffle is disabled.

### Gotchas
- Resist the urge to put secret values directly in matrix files; use env var references.
- Keep the planned job row flat and auditable.

### Acceptance Criteria
- A matrix file can be loaded, validated, and expanded into jobs.
- Job manifests are persisted.
- Planned jobs carry enough metadata for reproducibility and analysis.

---

## Implement provider/solver abstraction and secrets resolution

### ID
rg-mvp-051

### Priority
P1

### Type
task

### Labels
runner, providers, solvers, auth

### Dependencies
- rg-mvp-050

### Description
- Implement the abstraction layer that separates:
  - provider transport/config
  - solver behavior/config
- Resolve secrets from environment variables and optional local files.

### Subtasks
- Define provider kinds:
  - `anthropic_api`
  - `openai_responses`
  - `codex_cli`
  - `opencode_server`
  - `openai_compatible`
- Define solver config fields:
  - `id`
  - `adapter`
  - `provider`
  - `model`
  - reasoning/budget knobs
- Add validation for missing env vars or incompatible adapter/provider combinations.

### Likely Files
- `repogauge/runner/providers.py`
- `repogauge/runner/solvers.py`
- `repogauge/config.py`
- `tests/unit/test_providers.py`

### Design
- Model names must remain plain strings.
- Solver config should be immutable once a run starts; persist the resolved form into run manifests.
- Add secret redaction helpers for logs.

### Testing / Validation
- Config validation tests.
- Environment variable resolution tests.
- Negative tests for mismatched adapter/provider combinations.

### Gotchas
- Do not let adapters reach back into raw YAML structures at runtime; resolve and validate once.
- Never log full auth headers or API keys.

### Acceptance Criteria
- Providers and solvers are separate, validated concepts.
- Runs fail early and clearly when secrets are missing.
- Resolved solver configs are persisted for reproducibility.

---

## Build attempt workspace preparation and patch normalization

### ID
rg-mvp-052

### Priority
P1

### Type
task

### Labels
runner, workspaces, patches, normalization

### Dependencies
- rg-mvp-005
- rg-mvp-050

### Description
- Prepare isolated per-attempt workspaces at `base_commit`.
- Normalize model outputs into unified diffs suitable for judging.

### Subtasks
- Create per-job git worktrees.
- Materialize the agent instruction pack:
  - problem statement
  - allowed tools
  - expected output format
- Support both patch-returning and file-editing solvers.
- Normalize the final patch via `git diff` if needed.

### Likely Files
- `repogauge/runner/workspaces.py`
- `repogauge/runner/normalize_patch.py`
- `tests/integration/test_attempt_workspace.py`

### Design
- Preserve the raw solver output separately from the normalized patch.
- Patch normalization should verify:
  - patch applies cleanly to `base_commit`
  - no unexpected binary files
  - no modifications outside the repo root
- Include basic patch stats for telemetry.

### Testing / Validation
- Integration tests for:
  - direct diff output
  - edited-file capture
  - invalid patch rejection
- Ensure workspace cleanup and reuse are correct.

### Gotchas
- Some solvers may leave untracked files or change line endings; normalization needs a clear policy.
- Normalize paths carefully on Windows-style shells even if MVP support is Unix-first.

### Acceptance Criteria
- Every solver attempt yields either a normalized patch or an explicit failure reason.
- Raw and normalized outputs are both persisted.
- Workspaces are isolated and reproducible.

---

## Build solver queue orchestration and adapter base interfaces

### ID
rg-mvp-053

### Priority
P1

### Type
task

### Labels
runner, scheduler, solver-queue, adapters

### Dependencies
- rg-mvp-051
- rg-mvp-052

### Description
- Implement the solver-side scheduler, concurrency controls, and adapter base interface.

### Subtasks
- Define the solver adapter interface:
  - prepare request
  - execute attempt
  - stream or collect telemetry
  - finalize output
- Add:
  - global ready queue
  - per-provider semaphores
  - per-provider rate limiters
  - per-solver budget guards
- Persist attempt lifecycle events.

### Likely Files
- `repogauge/runner/scheduler.py`
- `repogauge/runner/solvers.py`
- `repogauge/runner/telemetry.py`
- `tests/unit/test_scheduler.py`

### Design
- Solver queue is independent from judge queue.
- Keep retries bounded and explicit.
- Include attempt states:
  - `queued`
  - `running`
  - `succeeded`
  - `failed`
  - `timed_out`
  - `budget_exceeded`
  - `invalid_patch`

### Testing / Validation
- Scheduler unit tests with fake adapters.
- Concurrency and rate-limit tests.
- Retry-policy tests.

### Gotchas
- Do not let judge load leak into solver concurrency decisions.
- Budget checks must happen before and after attempts; some providers only reveal usage at completion.

### Acceptance Criteria
- Solver jobs can be scheduled independently of judging.
- Adapter base interface is stable and documented.
- Attempt state transitions are persisted and test-covered.

---

## Implement concrete solver adapters for Anthropic, OpenAI/Codex, OpenCode, and OpenAI-compatible backends

### ID
rg-mvp-054

### Priority
P1

### Type
epic

### Labels
runner, adapters, anthropic, openai, codex, opencode, kimi

### Dependencies
- rg-mvp-016
- rg-mvp-053

### Description
- Implement the first batch of production solver adapters.

### Subtasks
- `claude_sdk.py`:
  - primary programmable Anthropic path
  - structured attempt telemetry
- `claude_cli.py`:
  - convenience local adapter
- `openai_responses.py`:
  - direct OpenAI Responses API path
- `codex_cli.py`:
  - non-interactive CLI path with JSON events
- `opencode.py`:
  - `opencode serve` preferred, CLI fallback optional
- `openai_compatible.py`:
  - generic base URL + API key adapter for Kimi or gateway deployments

### Likely Files
- `repogauge/llm/claude_sdk.py`
- `repogauge/llm/claude_cli.py`
- `repogauge/llm/openai_responses.py`
- `repogauge/llm/codex_cli.py`
- `repogauge/llm/opencode.py`
- `repogauge/llm/openai_compatible.py`
- `tests/unit/test_adapter_<provider>.py`

### Design
- Each adapter should return a common `AttemptResult` structure:
  - raw output reference
  - normalized patch or failure
  - usage
  - latency
  - tool counts if available
  - cost if reported
- Keep provider-specific parsing isolated inside each adapter.
- Prefer exact provider telemetry over local estimates.

### Testing / Validation
- Mocked API/CLI tests for each adapter.
- Contract tests that assert all adapters populate the common fields consistently.
- Offline tests verifying `--llm-mode off` bypasses all adapters.

### Gotchas
- CLI adapters are brittle if you parse human text; prefer JSON or structured output modes wherever possible.
- Different providers expose usage/cost differently; missing fields must be represented explicitly rather than guessed silently.

### Acceptance Criteria
- At least one adapter each for Anthropic, OpenAI/Codex, OpenCode, and OpenAI-compatible backends works behind the common interface.
- Adapters emit consistent attempt records.
- Provider-specific quirks are isolated and documented.

---

## Implement judge queue orchestration and batched harness evaluation

### ID
rg-mvp-055

### Priority
P1

### Type
task

### Labels
judge, harness, scheduler, docker

### Dependencies
- rg-mvp-042
- rg-mvp-053

### Description
- Build the judge-side scheduler that consumes normalized predictions and evaluates them through the harness in batches.

### Subtasks
- Queue `(instance_id, solver_id, model_patch)` jobs.
- Write per-solver predictions JSONL files.
- Batch evaluations to reuse dataset adapter and Docker cache.
- Persist per-instance harness outcomes and log paths.

### Likely Files
- `repogauge/runner/judge.py`
- `repogauge/export/predictions.py`
- `repogauge/runner/scheduler.py`
- `tests/integration/test_judge_queue.py`

### Design
- Judge concurrency should be lower than solver concurrency by default.
- Prefer batching by solver and dataset adapter to maximize cache reuse.
- Do not spawn one harness process per attempt unless no better batching option exists.
- Persist both aggregate `results.json` and normalized `instance_results.jsonl`.

### Testing / Validation
- Integration smoke test with a tiny dataset and fake predictions.
- Ensure batched judge runs still preserve per-attempt traceability.
- Failure tests for harness crash, Docker issues, and malformed predictions.

### Gotchas
- Docker-heavy evaluation can saturate the host quickly; defaults must stay conservative.
- Keep solver and judge queues logically separate even if the first implementation runs them in one process.

### Acceptance Criteria
- Predictions can be judged in batches with per-instance results persisted.
- Judge logs reference the underlying harness runs.
- The scheduler does not conflate solver throughput with judge throughput.

---

## Normalize telemetry, usage, cost, and parquet attempt logs

### ID
rg-mvp-056

### Priority
P1

### Type
task

### Labels
telemetry, parquet, cost, usage, analytics

### Dependencies
- rg-mvp-054
- rg-mvp-055

### Description
- Normalize attempt and evaluation telemetry into analysis-friendly parquet and JSONL outputs.

### Subtasks
- Define `attempts.parquet` schema with:
  - instance metadata
  - solver config
  - timing
  - exit reason
  - patch stats
  - usage
  - cost
  - judge outcome
- Implement tiered usage/cost sourcing:
  - exact API
  - exact CLI/session telemetry
  - estimated catalog fallback

### Likely Files
- `repogauge/runner/telemetry.py`
- `repogauge/runner/analyze.py`
- `tests/unit/test_telemetry.py`

### Design
- Store `usage_source` and `cost_source` explicitly.
- Flatten fields for easy SQL/pandas use.
- Include hashes of prompt/tool policy/config for reproducibility.
- Keep attempt rows append-only where possible.

### Testing / Validation
- Parquet schema tests.
- Merge tests that join solver results with judge outcomes.
- Ensure missing usage/cost fields remain well-defined, not NaN soup.

### Gotchas
- Do not mix currency estimates from multiple price catalogs without a clear timestamp/version.
- Some providers report usage late or approximately; mark that provenance honestly.

### Acceptance Criteria
- Every attempt yields a normalized row suitable for analysis.
- Cost and usage provenance are explicit.
- Parquet outputs are query-friendly and stable.

---

## Build analyzer join logic and summary metric engine

### ID
rg-mvp-060

### Priority
P1

### Type
task

### Labels
analysis, metrics, reports, benchmarking

### Dependencies
- rg-mvp-056

### Description
- Merge attempt telemetry with judge outcomes and compute the benchmark metrics the organization actually cares about.

### Subtasks
- Compute:
  - raw resolution rate
  - cost per resolved issue
  - latency per resolved issue
  - expensive-vs-cheap coverage
  - exclusive expensive win rate
  - marginal cost per extra resolve
  - Pareto frontier inputs
- Support grouping by solver, repo, environment cluster, and bug category.

### Likely Files
- `repogauge/runner/analyze.py`
- `tests/unit/test_analyze_metrics.py`

### Design
- Keep the metric engine deterministic and side-effect free.
- Define clear resolution semantics:
  - “resolved” means the judge says the patch passes FTOP/PTOP conditions
- Include confidence intervals or at least sample counts where group sizes are small.

### Testing / Validation
- Table-driven metric tests on synthetic result sets.
- Regression tests for divide-by-zero and missing data cases.
- Validate that summary metrics match hand-computed fixtures.

### Gotchas
- Cost-per-resolve is undefined when resolves are zero; present explicit null/inf semantics instead of garbage numbers.
- Do not compare solvers across runs unless prompt/tool/config hashes match or the report flags the mismatch.

### Acceptance Criteria
- Analyzer can produce trustworthy summary metrics from attempt + judge data.
- Metrics are tested against known fixtures.
- Grouping dimensions are explicit and documented.

---

## Generate HTML, CSV, JSON, and Parquet reports

### ID
rg-mvp-061

### Priority
P1

### Type
task

### Labels
analysis, html, csv, reporting

### Dependencies
- rg-mvp-060

### Description
- Produce the first useful benchmark report for humans and downstream tools.

### Subtasks
- Emit:
  - `analyze/summary.json`
  - `analyze/report.csv`
  - `analyze/report.parquet`
  - `analyze/report.html`
- Include sections for:
  - top-line metrics
  - solver comparison table
  - marginal win analysis
  - cost/latency tradeoffs
  - failure reason breakdown
  - unresolved task sample list

### Likely Files
- `repogauge/runner/analyze.py`
- `repogauge/review.py` or a small HTML template helper
- `tests/unit/test_report_generation.py`

### Design
- HTML should be static, portable, and easy to email or archive.
- CSV/Parquet should contain machine-readable equivalents of the summary tables.
- Preserve drill-down links to local logs and per-instance artifacts where possible.

### Testing / Validation
- Snapshot tests for HTML and CSV outputs.
- Ensure reports render sensibly with missing optional sections.
- Large-run smoke test to confirm report generation stays performant.

### Gotchas
- Avoid front-end creep. Static HTML with embedded data is enough for MVP.
- Keep local file links relative so reports remain portable inside a run directory.

### Acceptance Criteria
- `repogauge analyze` produces a useful static report bundle.
- Report artifacts are deterministic for the same inputs.
- Users can answer “which solver is best under budget X?” from the outputs.

---

## Extract leak-free task features and cluster instances

### ID
rg-mvp-062

### Priority
P1

### Type
task

### Labels
features, clustering, analysis, router

### Dependencies
- rg-mvp-024
- rg-mvp-060

### Description
- Compute task features that are available before or during a cheap probe and do not leak gold-fix knowledge.

### Subtasks
- Extract features such as:
  - problem statement length
  - repo size and package layout
  - environment complexity
  - likely edit breadth from static retrieval
  - stack trace present/absent
  - number of touched test files in the historical candidate
  - cheap probe outcomes, if enabled
- Add cluster labels for reporting.

### Likely Files
- `repogauge/runner/features.py`
- `repogauge/runner/analyze.py`
- `tests/unit/test_features.py`

### Design
- Separate strictly pre-solve features from post-attempt probe features.
- Explicitly exclude leak-prone fields such as the true changed-file count from the historical gold patch when training routers.
- Feature definitions should be versioned.

### Testing / Validation
- Unit tests for feature extraction on fixture instances.
- Leakage review checklist.
- Ensure cluster labels are reproducible for the same feature version.

### Gotchas
- Historical mining metadata can accidentally leak target information if you include too much detail from the gold fix.
- Keep features interpretable; the first router will be a tabular model, not a latent embedding system.

### Acceptance Criteria
- Feature extraction is deterministic and documented.
- Leak-prone fields are excluded from router inputs.
- Reports can stratify results by useful task clusters.

---

## Export router training data and implement baseline routing policies

### ID
rg-mvp-063

### Priority
P1

### Type
task

### Labels
router, policy, training-data, analysis

### Dependencies
- rg-mvp-062

### Description
- Export router training data and evaluate simple escalation policies before training a learned router.

### Subtasks
- Emit `router_train.parquet` with:
  - task features
  - solver outcomes
  - costs
  - latencies
  - prompt/tool policy hashes
- Implement baseline policies:
  - always cheap
  - always expensive
  - cheap then escalate on failure
  - cheap then escalate on invalid patch/timeout/no-progress

### Likely Files
- `repogauge/runner/router.py`
- `repogauge/runner/features.py`
- `tests/unit/test_router_baselines.py`

### Design
- Baseline policies are often good enough and should be part of every report.
- `router_train.parquet` should support offline policy evaluation without rerunning solvers.
- Define oracle comparisons carefully and document any assumptions.

### Testing / Validation
- Policy simulation tests on synthetic matrix outcomes.
- Parquet schema tests.
- Ensure baseline outputs match hand-computed fixtures.

### Gotchas
- Do not jump straight to ML before proving the value of simple escalation.
- Route labels should include “likely unsolved” where appropriate to avoid forcing false choices between cheap and expensive.

### Acceptance Criteria
- Router training data is exported.
- Baseline policies are implemented and reportable.
- Users can compare simple routing strategies offline.

---

## Implement router training pipeline and offline policy evaluation

### ID
rg-mvp-064

### Priority
P2

### Type
task

### Labels
router, training, ml, offline-eval

### Dependencies
- rg-mvp-063

### Description
- Train the first supervised router and evaluate it offline against baseline policies.

### Subtasks
- Implement a gradient-boosted tree baseline.
- Train on leak-free features only.
- Evaluate:
  - resolve rate
  - average cost
  - p95 latency
  - regret vs oracle
  - escalation rate
- Persist the trained model and feature version metadata.

### Likely Files
- `repogauge/runner/router.py`
- `tests/unit/test_router_training.py`

### Design
- Keep the training path simple and reproducible.
- Add dataset split controls and seed handling.
- Separate feature extraction from model training so future models can reuse the same data.

### Testing / Validation
- Deterministic training smoke test on a tiny synthetic dataset.
- Offline policy-eval tests.
- Ensure the training pipeline fails clearly when required features are missing.

### Gotchas
- Do not present router gains without comparing against baseline escalation policies.
- Keep the training story modular; production routing can come later.

### Acceptance Criteria
- A basic router can be trained and evaluated offline.
- Model artifacts include feature and dataset versioning.
- Reports can compare learned routing with simple baselines.

---

## Enforce security, privacy, and offline-by-default model controls

### ID
rg-mvp-065

### Priority
P0

### Type
task

### Labels
security, privacy, llm, policy, offline

### Dependencies
- rg-mvp-003
- rg-mvp-016

### Description
- Implement the controls that make RepoGauge safe to use on private repos.

### Subtasks
- Add a global `--llm-mode` or equivalent config:
  - `off`
  - `local_only`
  - `allow_remote`
- Redact secrets from logs and manifests.
- Require explicit opt-in before sending repository contents to remote providers.
- Add network/tool policy settings to experiment runner fairness configs.

### Likely Files
- `repogauge/config.py`
- `repogauge/logging_utils.py`
- `repogauge/llm/base.py`
- `repogauge/runner/providers.py`
- `README.md`
- `tests/unit/test_privacy_controls.py`

### Design
- Default posture should be “useful without any model access”.
- Emit visible warnings when a run will send content to a remote model provider.
- Consider a future gateway mode, but do not hardwire it into MVP.

### Testing / Validation
- Tests that remote adapters are unreachable when `--llm-mode off`.
- Secret redaction tests.
- Config validation tests for incompatible privacy settings.

### Gotchas
- CLI adapters may inherit shell environment unexpectedly; sanitize where practical.
- Logs often leak through exception traces; review failure paths carefully.

### Acceptance Criteria
- RepoGauge can run end-to-end without remote model access.
- Remote model use requires explicit opt-in.
- Secrets and auth details are not written to artifacts.

---

## Complete docs, examples, CI, performance caching, and release hardening

### ID
rg-mvp-066

### Priority
P0

### Type
epic

### Labels
docs, ci, release, caching, performance

### Dependencies
- rg-mvp-043
- rg-mvp-056
- rg-mvp-065

### Description
- Ship the operational hardening work that turns the implementation into a credible OSS release.

### Subtasks
- Docs:
  - `README.md` quickstart
  - tutorial: mining one repo
  - tutorial: running a solver matrix
  - troubleshooting guide
  - schema and artifact reference
- CI:
  - lint
  - type-check
  - unit tests
  - integration tests
  - e2e gold test
- Performance:
  - environment signature caching
  - Docker layer/cache guidance
  - worktree reuse where safe
  - response caching for model calls
- Release:
  - versioning policy
  - changelog
  - example `matrix.yaml`
  - example `config.yaml`

### Likely Files
- `README.md`
- `docs/tutorials/*.md`
- `.github/workflows/*.yml`
- `pyproject.toml`
- `examples/config.yaml`
- `examples/matrix.yaml`
- `tests/`

### Design
- CI should separate fast and slow lanes.
- Cache invalidation rules must be documented, especially for environment signatures and model response caches.
- Release notes should call out the product guarantees:
  - deterministic miner available with models off
  - generated adapter included
  - gold patches resolve exported fixtures

### Testing / Validation
- CI pipeline green on merge.
- Smoke test from a clean machine or clean container.
- Dry-run docs review: a new engineer should be able to follow tutorials without verbal guidance.

### Gotchas
- Do not bury the adapter requirement deep in docs; make it obvious in quickstarts.
- Keep examples tiny and reproducible. Nothing undermines trust faster than a broken tutorial.

### Acceptance Criteria
- The repo has runnable quickstarts and examples.
- CI covers the critical path.
- Release artifacts and docs are sufficient for an external user to succeed.

---

# Recommended implementation order

Use this order unless staffing constraints force parallelization:

1. `rg-mvp-000` to `rg-mvp-005`
2. `rg-mvp-010` to `rg-mvp-014`
3. `rg-mvp-020` to `rg-mvp-024`
4. `rg-mvp-030` to `rg-mvp-036`
5. `rg-mvp-040` to `rg-mvp-043`
6. `rg-mvp-050` to `rg-mvp-056`
7. `rg-mvp-060` to `rg-mvp-066`

Parallelization guidance:

- One engineer can own the **miner path** (`010`-`024`).
- One engineer can own the **validator path** (`030`-`036`).
- One engineer can own the **harness/eval path** (`040`-`043`).
- One engineer can own the **runner/adapters path** (`050`-`056`).
- One engineer can own the **analysis/router/docs path** (`060`-`066`).

# Release gates

Do not call MVP complete until all of the following are true:

- every exported instance has at least one stable `FAIL_TO_PASS`;
- no exported instance has `PASS_TO_PASS` regressions;
- `repogauge eval --gold` resolves all exported fixture instances through the official harness wrapper;
- a clean machine with git, Docker, Python, and optional model CLI access can run the system end to end;
- model use is optional and explicitly opt-in;
- experiment runs produce solver predictions, judge results, parquet attempt logs, and a useful static HTML report.

# Suggested staffing notes for junior engineers

- Favor **determinism over recall** at every decision point. False negatives are acceptable in v1; false positives that produce invalid benchmark tasks are not.
- Keep persisted outputs **boring and explicit**. JSONL + Parquet + static HTML are features, not limitations.
- Leave a visible audit trail for every nontrivial decision:
  - why the commit was shortlisted
  - why a file was classified as test support
  - why a problem statement came from issue text vs LLM
  - why an environment plan was chosen
  - why an instance was rejected
- Resist platform creep:
  - no service
  - no database
  - no reactive UI
  - no hidden remote dependency
- When in doubt, ask: **“Would an external user understand this artifact without reading our code?”** If not, improve the artifact or the docs.

# Open questions worth ADRs only if they become blocking

These are not MVP blockers yet, but should trigger ADRs if someone tries to implement them:

- hunk-level patch splitting within a single changed file
- multi-commit PR reconstruction
- non-pytest or non-JUnit primary parsing
- network-enabled solver runs
- hosted gateway mode
- multi-language repository support
- Windows-first local execution support

