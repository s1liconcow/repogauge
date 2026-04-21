#!/usr/bin/env bash
# Run repogauge against this repository.
# Usage: scripts/gauge_self.sh [--out DIR] [--max-commits N] [--decisions FILE]
#                               [--jobs N] [--eval-workers N]
#                               [--max-instances N]
#                               [--eval-timeout SECONDS]
#                               [--eval-container-runtime docker|podman]
#                               [--eval-container-host URI]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
source "$SCRIPT_DIR/lib/gauge_common.sh"
export UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/codex-uv-cache}"
OUT_DIR="$REPO_ROOT/out"
# NOTE: --out must be a path inside the repo so that the export step can
# resolve the git root by walking up from the output directory.
MAX_COMMITS=300
DECISIONS_FILE=""
JOBS=4
MAX_INSTANCES=0
EVAL_TIMEOUT=120
EVAL_CONTAINER_RUNTIME="podman"
EVAL_CONTAINER_HOST=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --out) OUT_DIR="$2"; shift 2 ;;
        --max-commits) MAX_COMMITS="$2"; shift 2 ;;
        --decisions) DECISIONS_FILE="$2"; shift 2 ;;
        --jobs|--eval-workers) JOBS="$2"; shift 2 ;;
        --max-instances) MAX_INSTANCES="$2"; shift 2 ;;
        --eval-batch-size|--eval-max-parallel-batches|--eval-workers-per-batch)
            echo "WARNING: $1 is deprecated and ignored; repogauge eval now uses --jobs." >&2
            shift 2
            ;;
        --eval-timeout) EVAL_TIMEOUT="$2"; shift 2 ;;
        --eval-container-runtime) EVAL_CONTAINER_RUNTIME="$2"; shift 2 ;;
        --eval-container-host) EVAL_CONTAINER_HOST="$2"; shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

mkdir -p "$OUT_DIR"
OUT_DIR_ABS="$(realpath "$OUT_DIR")"
REPO_ROOT_ABS="$(realpath "$REPO_ROOT")"
rg_require_out_inside_repo "$OUT_DIR_ABS" "$REPO_ROOT_ABS"

rg_run_mine_review_export "$REPO_ROOT_ABS" "$OUT_DIR_ABS" "$MAX_COMMITS" "$DECISIONS_FILE"

EXPORT_OUT="$OUT_DIR_ABS/export"
EVAL_OUT="$OUT_DIR_ABS/eval"
EVAL_DATASET_ROOT="$EXPORT_OUT"

if [[ "$MAX_INSTANCES" -gt 0 ]]; then
    EVAL_DATASET_ROOT="$OUT_DIR_ABS/eval_dataset"
    mkdir -p "$EVAL_DATASET_ROOT"
    echo "==> eval: selecting first $MAX_INSTANCES exported instance(s)"
    SELECTED_COUNT="$(
        UV_CACHE_DIR="$UV_CACHE_DIR" uv run python - <<'PY' \
            "$EXPORT_OUT/dataset/dataset.jsonl" \
            "$EXPORT_OUT/dataset/predictions.gold.jsonl" \
            "$EVAL_DATASET_ROOT/dataset.jsonl" \
            "$EVAL_DATASET_ROOT/predictions.gold.jsonl" \
            "$MAX_INSTANCES"
import json
import sys
from pathlib import Path

dataset_in = Path(sys.argv[1])
predictions_in = Path(sys.argv[2])
dataset_out = Path(sys.argv[3])
predictions_out = Path(sys.argv[4])
limit = max(1, int(sys.argv[5]))

dataset_rows = []
with dataset_in.open(encoding="utf-8") as stream:
    for line in stream:
        value = line.strip()
        if not value:
            continue
        dataset_rows.append(json.loads(value))
        if len(dataset_rows) >= limit:
            break

selected_ids = {str(row.get("instance_id", "")).strip() for row in dataset_rows}
prediction_rows = []
with predictions_in.open(encoding="utf-8") as stream:
    for line in stream:
        value = line.strip()
        if not value:
            continue
        row = json.loads(value)
        if str(row.get("instance_id", "")).strip() in selected_ids:
            prediction_rows.append(row)

dataset_out.write_text(
    "".join(json.dumps(row, sort_keys=True) + "\n" for row in dataset_rows),
    encoding="utf-8",
)
predictions_out.write_text(
    "".join(json.dumps(row, sort_keys=True) + "\n" for row in prediction_rows),
    encoding="utf-8",
)
print(len(dataset_rows))
PY
    )"
    echo "==> eval: selected $SELECTED_COUNT instance(s)"
fi

echo "==> eval: running gold evaluation"
if rg_run_eval_gold \
    "$EVAL_DATASET_ROOT" \
    "$EVAL_OUT" \
    "$JOBS" \
    "$EVAL_TIMEOUT" \
    "$EVAL_CONTAINER_RUNTIME" \
    "$EVAL_CONTAINER_HOST"; then
    EVAL_OK=1
else
    EVAL_OK=0
fi

echo ""
rg_print_common_artifacts "$OUT_DIR_ABS"
if [[ "$EVAL_OK" -eq 1 ]]; then
    echo "  eval/dataset.resolved.jsonl"
    echo "  eval/predictions.resolved.jsonl"
    echo "  eval/validation.jsonl"
else
    echo ""
    echo "WARNING: eval step failed (swebench not installed — run 'pip install swebench' to enable harness evaluation)"
fi
