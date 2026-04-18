# RepoGauge Design

## What RepoGauge is

RepoGauge is a local, language-aware CLI for turning a single repository into
SWE-bench-style benchmark artifacts that can be evaluated by the official
SWE-bench harness.

The current product scope is intentionally narrow in v1:

- mine candidate bug-fix commits deterministically
- review and export SWE-bench-compatible dataset instances
- generate harness adapter registration for arbitrary repositories
- validate `FAIL_TO_PASS` and `PASS_TO_PASS` deterministically before export
- evaluate candidate patches against official semantics via `repogauge eval`

## Core architectural principles

### Local-first and scoped
- Repository contents are expected to remain local by default.
- Deterministic heuristics and local validation drive correctness.

### Separation of concerns
- mining/review/export is separate from judge execution.
- solver generation and scoring runs should be isolated from docker-heavy judging where possible.

### Deterministic contracts and artifact completeness
- Every exported dataset artifact has a corresponding generated harness adapter registration for unseen repositories.
- `FAIL_TO_PASS` and `PASS_TO_PASS` are evidence outputs of deterministic validation, not model suggestion.
- LLMs (if used) can propose only; they do not determine final export validity.

### Language adapter registry
RepoGauge routes language-specific detection, inspection, parsing, harness
export, and validation behavior through the `LanguageAdapter` registry in
`repogauge/lang/__init__.py`. The registry keeps the CLI and export pipeline
language-aware without scattering per-language conditionals across the rest of
the codebase. See
[ADR-0002](docs/ADRs/0002-language-adapter-registry.md) and
`docs/language_adapters.md` for the detailed contract and rollout notes.

### Resumability and observability
- Long-running commands should emit step-level manifests and persisted logs.
- Artifacts should be machine-readable and explainable without source-code access.

## Non-goals

- multi-commit PR reconstruction
- synthetic test generation
- remote-only or hosted service workflows
- broad platform orchestration beyond CLI flow

## Scope boundaries and invariants

- Local-first, privacy-preserving defaults
- explicit opt-in for remote model providers
- published dataset + adapter pair is the compatibility boundary for harness evaluation
- solver and judge pipelines remain decoupled as queue participants
- local reproducibility before performance optimization

## Reference decisions

- Architecture decision record: [docs/ADRs/0001-mvp-architecture.md](docs/ADRs/0001-mvp-architecture.md)
- Language adapter registry: [docs/ADRs/0002-language-adapter-registry.md](docs/ADRs/0002-language-adapter-registry.md)
