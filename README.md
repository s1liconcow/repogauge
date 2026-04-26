# RepoGauge

**Stop picking coding agents on vibes. Benchmark them on your own codebase.**

RepoGauge turns a supported repository into a SWE-bench-style evaluation suite
and runs AI coding agents against it locally. Today that includes Python, Go,
JavaScript/TypeScript, Java, and Rust. You get real pass/fail numbers and real
dollar costs — not a third-party leaderboard on someone else's problems.

## Why this exists

Pick-an-agent decisions today look like this:

- A new model drops. Someone tries it for a day. "It feels better."
- A provider ships an update. Something silently regresses. You don't notice
  until a teammate complains a week later.
- You pay for three different coding assistants because nobody has the data
  to justify dropping any of them.
- The public leaderboards are on SWE-bench Verified — 500 Django/sympy tasks
  that look nothing like your codebase.

RepoGauge replaces the vibes with a reproducible measurement:

- **Can this agent actually ship code in *your* repo?** Mines real bugfix
  commits from your history, turns each into a SWE-bench task with the gold
  patch and failing tests, and runs the agent against it. A task is "solved"
  only if the agent's patch makes the same tests pass that the human fix did.
- **What does it cost?** Every attempt records input/output tokens, cache
  reads, USD spend, wall-clock time, and turn count. Aggregated per solver.
- **Did the provider regress?** Rerun the same matrix on the same dataset
  next month. Diff the pass rate and the cost. Hard numbers, not feelings.
- **Is the cheap model good enough for the easy tasks?** Train a tiny
  cost-aware router over task features so you only pay for a premium model
  when the task actually needs it.

Everything runs locally. Your repo contents never leave your machine unless
you explicitly point a solver at a remote provider, and even then only the
per-task prompt is sent — not the whole tree.

## Who this is for

- **Eng leads** deciding which coding agent to standardize on.
- **Platform teams** deciding which agents to make available and how to
  budget for them.
- **Provider watchdogs** who want a reproducible canary that catches silent
  model regressions.
- **Anyone curious** whether the premium model is actually worth 10× the
  cheap one for their particular codebase.

## Quickstart

Install with [`uv`](https://github.com/astral-sh/uv):

```bash
uv sync --group dev
```

The full pipeline is five commands: **export → eval → run → analyze → train**.
Each step writes artifacts under its own `--out` directory and the next step
reads from there. `analyze` automatically evaluates solver patches with the
SWE-bench harness, so you don't need a separate "eval predictions" step. You
can stop anywhere — `eval`, `run`, and `analyze` use a container runtime
(Docker or Podman).

### 1. Mine candidate bugfix commits

```bash
uv run repogauge mine /path/to/your/repo --out ./out/mine --llm-mode off
```

Scans the default branch for bugfix-shaped commits using deterministic
heuristics (no LLM required), auto-detects the repository's primary language,
and writes `candidates.jsonl`.

### 2. Review candidates

```bash
uv run repogauge review ./out/mine/candidates.jsonl --out ./out/review --llm-mode off
```

Applies accept/reject heuristics and emits `reviewed.jsonl` plus a
browsable `review.html`. Run `--llm-mode local_only` or `allow_remote` to
get advisory triage from a model; otherwise it's pure rules.

### 3. Export the dataset

```bash
uv run repogauge export ./out/review/reviewed.jsonl --out ./out/export --llm-mode off
```

Materializes SWE-bench-compatible instances at
`./out/export/dataset/dataset.jsonl`, writes the gold predictions, and
generates a **repo-specific harness adapter** (`adapter_<repo>.py`) so the
official SWE-bench harness can evaluate patches against your repository
even if it's never seen it before.

### 4. Validate the gold patches (sanity check)

```bash
uv run repogauge eval ./out/export/dataset/dataset.jsonl --gold --out ./out/eval --llm-mode off
```

Runs the official SWE-bench harness against the gold patches to confirm
every task is actually solvable in your container image. Produces
`validation.jsonl`, `instance_results.jsonl`, and the resolved-only slice
at `./out/eval/dataset.resolved.jsonl`. Add `--container-runtime podman` if
Docker isn't available.

### 5. Run a solver matrix

```bash
uv run repogauge run examples/matrix.yaml \
  --dataset ./out/eval/dataset.resolved.jsonl \
  --out ./out/run
```

Executes each solver in your matrix against the dataset. Writes one row per
attempt to `attempts.jsonl` with patch, tokens, cost, duration, and exit
reason. Workspace-backed CLI solvers run inside Docker/Podman as well, using
the repo-specific SWE-bench image by default; set `providers.<id>.image` in
the matrix when you want to override that image.

`examples/matrix.yaml` uses the fully local `mock` provider (no credentials
needed). For a real head-to-head comparison, see
`examples/matrix.codex-cli.yaml` — it runs the Codex CLI and the Claude CLI
side-by-side using each CLI's existing local auth (no API keys to manage)
and needs `--llm-mode allow_remote`.

### 6. Analyze the run (auto-evaluates solver patches)

```bash
uv run repogauge analyze ./out/run/<run_id>
```

`analyze` does everything needed to turn raw attempts into a report:

1. Turns `attempts.jsonl` into a SWE-bench predictions file
   (`predictions.jsonl`).
2. Runs the official harness over those predictions against the same dataset
   the run used, writing `eval/instance_results.jsonl`.
3. Joins attempts against harness verdicts and writes
   `analysis_report.json`, per-solver/per-group summaries, a browsable
   `report.html`, and `router_train.parquet`.

Output includes pass rate, mean cost per solved instance, token
distribution, timeout rate, and the expensive-attempt tail — all the numbers
you need to stop guessing which agent is best for your codebase.

Useful flags:

- `--dataset PATH` — override the dataset (defaults to the one recorded in
  `attempts.jsonl`).
- `--adapter PATH` — override the generated harness adapter.
- `--container-runtime {docker,podman}` — pick the container backend.
- `--skip-eval` — reuse an existing `eval/instance_results.jsonl` without
  re-running the harness (useful for iterating on report shape).

### 7. (Optional) Train a cost-aware router

```bash
uv run repogauge train-router ./out/run/<run_id>
```

Fits a small decision-tree router on `router_train.parquet` so future runs
can pick the cheapest solver likely to succeed on each task.

### 8. Package a source-safe cloud bundle

```bash
uv run repogauge cloud-bundle ./out/run/<run_id>/analyze --out ./out/cloud
```

`cloud-bundle` packages an existing local report directory into a deterministic
`.zip` archive with a top-level `manifest.json` compatible with RepoGauge Cloud
uploads. The source directory must contain `analysis_report.json` and at least
one of `attempts.jsonl` or `instance_results.jsonl`. The command includes only
approved report artifacts, prints warnings for absolute-path-like or
source-snippet-like text, and never mutates the original run directory.

## What you can answer with this

Concrete questions that go from "hand-wave" to "here's the parquet":

- **Which of Sonnet, Opus, GPT-5, and Codex CLI actually solves the most
  bugs in my codebase, and at what cost per solved bug?**
- **Does turning on extended thinking improve my pass rate enough to justify
  the token bill?**
- **Did last week's provider update quietly regress my "easy" tier?** Rerun,
  diff `analysis_report.json`.
- **Can I route 70 % of tasks to the cheap model without dropping pass
  rate?** `train-router` tells you where the boundary is.
- **What fraction of my solver's failures are timeouts vs. bad patches vs.
  infrastructure flakes?** `analyze` breaks this down by `exit_reason`.

## How it works

```
mine → review → export → eval (gold) → run → analyze → train-router
 │       │         │         │           │       │
 │       │         │         │           │       └─ harness on agent patches + cost/quality report
 │       │         │         │           └─ each solver attempts each task
 │       │         │         └─ sanity-check dataset with gold patches
 │       │         └─ SWE-bench dataset + repo-specific adapter
 │       └─ accept/reject heuristics
 └─ deterministic bugfix-commit scan
```

Everything is deterministic, resumable, and hash-keyed on inputs so
reruns skip work that hasn't changed.

## Design principles

- **Local-first.** No hosted service, no database. Artifacts are JSONL and
  parquet on your disk.
- **Your code stays yours.** Repository contents are not sent anywhere by
  default; remote providers are opt-in per command with `--llm-mode`.
- **Deterministic.** Seeds, hashes, and manifest-based resume mean the same
  input produces the same output — so regressions are attributable to the
  provider, not to RepoGauge.
- **Real harness, not a simulation.** Evaluation runs through the official
  SWE-bench harness, same as the public leaderboards.
- **Language-aware dispatch.** The adapter-registry invariants live in
  [ADR-0002](docs/ADRs/0002-language-adapter-registry.md).

## Scope and non-goals

**v1 is:**

- Supported-language task mining and evaluation across Python, Go,
  JavaScript/TypeScript, Java, and Rust.
- SWE-bench-compatible dataset export with auto-generated harness adapters.
- Matrix-driven multi-solver runs with per-attempt cost/token telemetry.
- Cost- and quality-aware analysis and router training.
- `mine` and `eval` work with `--llm-mode off` across the supported languages.

**v1 is not:**

- Multi-commit PR reconstruction.
- Synthetic test generation.
- A hosted service, a leaderboard, or a database-backed orchestrator.
- A way to call remote models by default — that's opt-in, every time.

## Roadmap

Planned follow-on work after the current multi-language v1 includes:

- Broader coding-agent coverage, including adapters for more agent CLIs and
  providers such as Opencode, Gemini, Pi, and similar tools.
- Broader language coverage beyond the current Python, Go,
  JavaScript/TypeScript, Java, and Rust support.
- A managed option for teams that want continuous benchmark runs without
  babysitting local pipelines, so regressions and cost shifts can be tracked
  over time with less operational overhead.

## CLI reference

| Command | What it does |
|---|---|
| `repogauge mine PATH` | Scan commits for bugfix-shaped changes |
| `repogauge review CANDIDATES` | Accept/reject candidates |
| `repogauge export REVIEWED` | Build SWE-bench dataset + harness adapter |
| `repogauge eval DATASET` | Run SWE-bench harness (gold or predictions) |
| `repogauge run MATRIX` | Execute a solver matrix |
| `repogauge analyze RUN` | Produce per-solver cost/quality reports |
| `repogauge train-router RUN` | Fit a cost-aware solver router |
| `repogauge cloud-bundle RUN_ANALYZE_DIR` | Package a source-safe cloud upload bundle |

Global flags on every command:

- `--config` — merge config files over built-in defaults
- `--out` — where artifacts land
- `--resume` — continue from existing outputs
- `--dry-run` — validate without writing artifacts
- `--llm-mode {off,local_only,allow_remote}` — remote-call policy
- `--container-runtime {docker,podman}` — container backend for `eval`, `run`, and `analyze`

## Try it on RepoGauge itself

The repo ships a script that runs the full pipeline against its own source
tree. Good for smoke-testing after a change:

```bash
scripts/gauge_self.sh
```

| Flag | Default | Description |
|---|---|---|
| `--out DIR` | `./out` | Root directory for all artifacts |
| `--enrich-github` | disabled | Fetch GitHub issue/PR metadata |
| `--max-commits N` | `100` | Commits to scan |
| `--github-token TOKEN` | `$GITHUB_TOKEN` | Token for GitHub API calls |
| `--github-enrichment-cache PATH` | `<out>/github_enrichment_cache.json` | Local cache for enrichment |
| `--decisions FILE` | _(none)_ | JSONL of manual accept/reject decisions |

And an end-to-end pytest that validates every artifact at every stage:

```bash
uv run python -m pytest tests/e2e/test_self_gauge.py -v
```

## Sample GitHub Actions

The repo also includes two sample manual workflows under
`.github/workflows/`:

- `sample-harvest-testcases.yml` checks out full history, runs
  `mine -> review -> export -> eval --gold`, and uploads the harvested
  testcase bundle as a GitHub Actions artifact. This is the easiest way to
  produce a reusable `dataset.resolved.jsonl` plus the generated
  `adapter_<repo>.py` in CI.
- `sample-evaluate-matrix.yml` is the matching matrix runner. It can consume
  either a dataset path already present in the repository or the artifact
  emitted by the harvest workflow, then runs `repogauge run` followed by
  `repogauge analyze`.

`sample-evaluate-matrix.yml` is intentionally checked in disabled right now
via `if: ${{ false }}` at the job level, so it serves as a concrete template
without accidentally burning solver budget. Re-enable that job when you are
ready to spend tokens on matrix runs.

## Artifact contract

Every command writes `manifest.json` and `events.jsonl` alongside its
command-specific outputs:

| Command | Notable artifacts |
|---|---|
| `mine` | `repo_profile.json`, `scan.jsonl`, `candidates.jsonl` |
| `review` | `reviewed.jsonl`, `review.md`, `review.html` |
| `export` | `materialized.jsonl`, `dataset/dataset.jsonl`, `dataset/predictions.gold.jsonl`, `adapter_<repo>.py`, `specs.json` |
| `eval` | `dataset.resolved.jsonl`, `predictions.resolved.jsonl`, `validation.jsonl`, `instance_results.jsonl` |
| `run` | `matrix.yaml`, `jobs.jsonl`, `attempts.jsonl`, `attempts.parquet`, `attempt_logs/`, `attempt_workspaces/`, `run_summary.json` |
| `analyze` | `router_train.parquet`, `summary.json`, `analysis_report.json` |
| `cloud-bundle` | `repogauge-bundle.zip`, `repogauge-bundle.manifest.json` |

## Docs and examples

- [Mining a repo → review → export](docs/tutorials/mine-review-export.md)
- [Running a solver matrix](docs/tutorials/run-matrix.md)
- [Troubleshooting guide](docs/tutorials/troubleshooting.md)
- [Example matrix + outputs](examples/matrix.yaml)
- [Codex CLI vs. Claude CLI comparison matrix](examples/matrix.codex-cli.yaml)
- [Architecture: MVP ADR](docs/ADRs/0001-mvp-architecture.md)
- [Full design](DESIGN.md)
- [JUnit parser contract](docs/junit_parser_contract.md)

## Status

Active development. The pipeline end-to-end works and is used to benchmark
RepoGauge itself on every change. Expect sharp edges around new provider
adapters and error messages — and please file issues when you find them.

## Contributing

Bug reports and PRs welcome. A good first contribution is adding your
favorite coding agent as a new solver adapter — each one is ~150 lines in
`repogauge/runner/adapters.py`, modeled on the existing `codex_cli` and
`claude_cli` adapters. If RepoGauge ever spares you the frustration of
wondering whether the new model is really better, paying that forward with
a regression test is the highest-leverage thing you can do.
