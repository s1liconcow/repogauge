# ADR-0001: RepoGauge MVP architecture, scope, and constraints

## Status

Accepted

## Context

RepoGauge is being built as a local CLI-first tool that turns a target repository into:

- SWE-bench-style dataset instances for the repo
- a generated `swebench` harness adapter that enables official grading for that repo

The project has strong pressure points around correctness, reproducibility, and
operational cost. This decision records hard boundaries and non-goals to prevent
scope drift.

## Decision

1. MVP is Python-only and local-repo-first.
2. The artifact pair for arbitrary repositories is **`dataset.jsonl` + generated adapter code**.
3. Deterministic validation is the source of truth; any language-model output is advisory.
4. Solving and judging are separate queue stages.
5. By default, repository contents stay local and model usage is opt-in.

## Rationale

### 1) Python-only MVP

SWE-bench grading and local evaluation are already most mature for Python tooling.
Concentrating on Python in v1 keeps behavior deterministic and testable while still
proving the full end-to-end path.

### 2) Keep the generated adapter as part of the product

For unseen repositories, a dataset alone is not enough to satisfy the harness.
The repo/version registration required by the official harness must be produced and
packaged alongside dataset artifacts so evaluation is runnable end-to-end.

### 3) Validators are authoritative

Heuristics and models can propose candidate tasks, enrich problem statements, or
suggest environment hints. They must not decide final validity alone. Only validated
patch + test outcomes (including `FAIL_TO_PASS` and `PASS_TO_PASS`) determine what is
exportable.

### 4) Separate solver and judge pipelines

Solver execution (patch generation) and judge execution (Docker-heavy grading) have
different resource and performance profiles. They must be separately queue-managed for
reliability and scheduling control.

### 5) Privacy-by-default operation

Private repos are local data by default. Remote model calls happen only with explicit
opt-in, so users can run deterministic local pipelines without any external model
dependency.

## Scope boundaries

### In scope for v1

- CLI entry points for mine/review/export/eval flows
- Deterministic commit scan and candidate filtering
- Dataset materialization with `FAIL_TO_PASS` and `PASS_TO_PASS`
- Generated adapter integration for official harness grading
- Deterministic validation and smoke-grade offline checks

### Out of scope for v1

- Multi-language support
- Synthetic test generation
- Multi-commit PR reconstruction
- Hosted service / UI / DB-backed workflow orchestration

## Invariants

- `adapter + dataset` is the minimum export unit for arbitrary repo support.
- `dataset` without corresponding adapter is incomplete for official grading.
- Any approved exported instance must satisfy:
  - at least one `FAIL_TO_PASS`
  - no `PASS_TO_PASS` regressions
  - stable outcomes across repeated validation runs

## Future expansion (required ADR before enabling)

- Multi-language support and parser generalization
- Network-enabled solver runs and remote dataset enrichment
- Gateway-based provider federation
- Cloud services or centralized orchestration

Any proposal that adds these capabilities must begin with a new ADR and an explicit
risk assessment.

## Consequences

- Contributors can judge v1 viability quickly from documentation alone.
- Implementation work is anchored to deterministic artifacts and reproducible
  validation.
- Evaluation compatibility stays aligned with the official harness instead of
  diverging into a custom grading format.

