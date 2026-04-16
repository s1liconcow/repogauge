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
- [docs/junit_parser_contract.md](docs/junit_parser_contract.md)

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
| `--enrich-github` | disabled | Enable optional GitHub issue/PR metadata enrichment |
| `--max-commits N` | `100` | Commits to scan |
| `--github-token TOKEN` | `$(GITHUB_TOKEN)` | Token used for GitHub API calls |
| `--github-enrichment-cache PATH` | `<out>/github_enrichment_cache.json` | Optional local cache for enrichment responses |
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

### Command artifact contract (current scaffold)

For `--out` directory `./out`, the scaffold writes command-specific artifacts:

- `manifest.json`
  - command metadata and step status for each run invocation
- `events.jsonl`
  - machine-readable execution events for the same run
- `repo_profile.json`, `scan.jsonl`, `candidates.jsonl` for `mine`
- `reviewed.jsonl`, `review.md`, `review.html` for `review`
- `materialized.jsonl`, `materialization_rejections.jsonl`, `dataset/dataset.jsonl`,
  `dataset/predictions.gold.jsonl`, `adapter_<repo>.py`, `specs.json` for `export`
- `validation.jsonl` for `eval`

This list reflects what the v0.1 scaffold guarantees today; future stages
extend it to include run-level and analysis artifacts.

### E2E integration test

```bash
uv run python -m pytest tests/e2e/test_self_gauge.py -v
```

This runs the full mine → review → export pipeline against this repository and
validates every artifact at each stage.

## Quickstart

Clone the repo (or use a local checkout you already have), install with `uv`, and
follow the workflow below for a fast offline smoke path.

```bash
uv sync --group dev
uv run repogauge mine /path/to/repo --out ./out/mine --llm-mode off
uv run repogauge review ./out/mine/candidates.jsonl --out ./out/review --llm-mode off
uv run repogauge export ./out/review/reviewed.jsonl --out ./out/export --llm-mode off
uv run repogauge eval ./out/export/dataset/dataset.jsonl --gold --llm-mode off
```

For a runnable matrix run using only local behavior, use the included
`examples/matrix.yaml` and a dataset from your export step:

```bash
uv run repogauge run examples/matrix.yaml --dataset ./out/export/dataset/dataset.jsonl --out ./out/run
```

### Tutorials and examples

- [Mining a repo → review → export](docs/tutorials/mine-review-export.md)
- [Running a solver matrix](docs/tutorials/run-matrix.md)
- [Troubleshooting guide](docs/tutorials/troubleshooting.md)
- [Example matrix + outputs](examples/matrix.yaml)
