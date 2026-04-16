# Tutorial: Mine, review, and export a repo

This guide runs the first three production steps and produces a SWE-bench-compatible
dataset plus a generated repo adapter.

## 1) Mine candidates

```bash
uv run repogauge mine /path/to/repo \
  --out out/mine \
  --max-commits 200 \
  --llm-mode off
```

Artifacts are written to:

- `out/mine/scan.jsonl`
- `out/mine/candidates.jsonl`
- `out/mine/repo_profile.json`
- `out/mine/manifest.json`

## 2) Review / accept candidate patches

Use a manual review CSV/JSONL if desired, or let this scaffold auto-review.

```bash
uv run repogauge review out/mine/candidates.jsonl \
  --out out/review \
  --llm-mode off
```

Output:

- `out/review/reviewed.jsonl`
- `out/review/review.md`
- `out/review/review.html`

## 3) Export dataset artifacts

```bash
uv run repogauge export out/review/reviewed.jsonl \
  --out out/export \
  --llm-mode off
```

Output:

- `out/export/dataset/dataset.jsonl`
- `out/export/dataset/predictions.gold.jsonl`
- `out/export/adapter_<repo>.py`
- `out/export/specs.json`

## 4) Optional: validate exported gold patches

```bash
uv run repogauge eval out/export/dataset/dataset.jsonl \
  --gold \
  --llm-mode off
```

This command resolves the generated `adapter_<repo>.py` when present and runs the
official-style checker path in this scaffold. A clean run should show a succeeded
run manifest and a populated `validation.jsonl`.
