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

See the architecture docs:

- [docs/ADRs/0001-mvp-architecture.md](docs/ADRs/0001-mvp-architecture.md)
- [DESIGN.md](DESIGN.md)

## CLI surface (scaffold)

- `repogauge mine PATH --out DIR`
- `repogauge review CANDIDATES --out DIR`
- `repogauge export REVIEWED --dataset DIR`
- `repogauge eval DATASET --gold`
- `repogauge run MATRIX`
- `repogauge analyze RUN`
- `repogauge train-router RUN`

Global behavior:

- `--config`: merges config files over built-in defaults.
- `--out`: sets output directory root.
- `--resume`: continues from existing outputs where possible.
- `--dry-run`: validates parameters without writing artifacts.
- `--llm-mode`: `off`, `local_only`, or `allow_remote`.

Current release state is scaffolded and in active development.
