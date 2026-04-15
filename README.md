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

### In scope for MVP

- CLI-only workflows such as:
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

Current release state is scaffolded and in active development.

## Running repogauge against itself

```bash
scripts/gauge_self.sh
```

Options:

| Flag | Default | Description |
|---|---|---|
| `--out DIR` | `./out` | Root directory for all artifacts |
| `--max-commits N` | `100` | Commits to scan |
| `--decisions FILE` | _(none)_ | JSONL file of manual accept/reject decisions |

Outputs written under `--out`:

```
mine/repo_profile.json              repo identity and environment hints
mine/candidates.jsonl               all scanned commits with heuristic scores
review/reviewed.jsonl               accept/reject decisions
review/review.html                  human-readable review report
export/dataset/dataset.jsonl        SWE-bench-compatible instances
export/dataset/predictions.gold.jsonl
```

### E2E integration test

```bash
uv run python -m pytest tests/e2e/test_self_gauge.py -v
```

This runs the full mine → review → export pipeline against this repository and
validates every artifact at each stage.
