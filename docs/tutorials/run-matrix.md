# Tutorial: Run a matrix and inspect outputs

This workflow uses a solver matrix to execute the scaffolded solve/eval path.
For local smoke tests, the included `mock` provider is fully runnable with no
external credentials.

## 1) Prerequisite dataset

Ensure you have a dataset at `dataset/dataset.jsonl` from `repogauge export`.

## 2) Start from the sample matrix

```bash
uv run repogauge run examples/matrix.yaml \
  --dataset /path/to/dataset/dataset.jsonl \
  --out out/run \
  --llm-mode off
```

If you pass `--out`, the command writes attempt/job artifacts under that directory.

## 3) Where results land

- `out/run/run.json` (run manifest)
- `out/run/jobs.jsonl` (planned jobs and hashes)
- `out/run/attempts.jsonl` (per-attempt outcomes in this scaffold)
- `out/run/predictions/` (solver-specific prediction rows)
- `out/run/attempts.parquet` if parquet persistence is enabled in the matrix

## 4) Use a stricter profile for local/offline smoke

The provided `examples/matrix.yaml` intentionally stays simple:

- provider `mock` with no external network calls
- deterministic seed and repeat configuration
- one solver for cheap baseline behavior

If you add real providers, replace the `mock` section with the provider you
actually want and keep `--llm-mode` aligned with your environment policy.

## 5) Codex CLI vs. Claude CLI comparison example

The repo includes a side-by-side CLI comparison matrix at
`examples/matrix.codex-cli.yaml`. It runs both solvers against the same dataset
so their outputs, cost, and usage can be compared:

```bash
uv run repogauge run examples/matrix.codex-cli.yaml \
  --dataset /path/to/dataset/dataset.jsonl \
  --out out/run \
  --llm-mode allow_remote
```

That matrix expects both a `codex` and a `claude` executable on `PATH` and uses
each CLI's existing local auth (no `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`
required). It configures:

- provider kind `codex_cli` (command `codex`) with solver adapter `codex_cli`,
  model `gpt-5.4-mini`
- provider kind `claude_cli` (command `claude`) with solver adapter
  `claude_cli`, model `claude-sonnet-4-6`

Because it makes real network calls via each CLI, use
`--llm-mode allow_remote`. For a parse-only dry run, keep `--llm-mode off` and
the matrix still loads and plans without invoking either CLI.
