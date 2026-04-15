# repogauge

## v1 Scope and non-goals

RepoGauge v1 is a **Python-only, local-first CLI** for creating local SWE-bench-style
evaluation tasks from a repository and evaluating patches with the official SWE-bench harness.

What v1 is:

- Mine and shortlist candidate bugfix commits with deterministic heuristics.
- Export SWE-bench-compatible `dataset.jsonl` artifacts.
- Generate a repository-specific harness adapter so official evaluation can run
  against previously unsupported repos.
- Validate gold patches and `FAIL_TO_PASS` / `PASS_TO_PASS` outcomes deterministically.

What v1 is not:

- Multi-language generality.
- Multi-commit PR reconstruction.
- Synthetic test generation.
- Hosted service or database-backed workflow orchestration.
- Remote model calls by default.

See the MVP architecture decision record:

- [docs/ADRs/0001-mvp-architecture.md](docs/ADRs/0001-mvp-architecture.md)
