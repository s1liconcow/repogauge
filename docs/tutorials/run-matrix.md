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
