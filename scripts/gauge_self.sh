#!/usr/bin/env bash
# Run repogauge against this repository.
# Usage: scripts/gauge_self.sh [--out DIR] [--max-commits N] [--decisions FILE]
#                               [--jobs N] [--eval-workers N]
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
EVAL_TIMEOUT=120
EVAL_CONTAINER_RUNTIME="podman"
EVAL_CONTAINER_HOST=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --out) OUT_DIR="$2"; shift 2 ;;
        --max-commits) MAX_COMMITS="$2"; shift 2 ;;
        --decisions) DECISIONS_FILE="$2"; shift 2 ;;
        --jobs|--eval-workers) JOBS="$2"; shift 2 ;;
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

echo "==> eval: running gold evaluation"
if rg_run_eval_gold \
    "$EXPORT_OUT" \
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
