#!/usr/bin/env bash
# Run repogauge against this repository.
# Usage: scripts/gauge_self.sh [--out DIR] [--max-commits N] [--decisions FILE]
#                               [--jobs N] [--eval-workers N]
#                               [--eval-timeout SECONDS]
#                               [--eval-container-runtime docker|podman]
#                               [--eval-container-host URI]

set -euo pipefail

REPO_ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
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

MINE_OUT="$OUT_DIR/mine"
REVIEW_OUT="$OUT_DIR/review"
EXPORT_OUT="$OUT_DIR/export"
EVAL_OUT="$OUT_DIR/eval"

echo "==> mine: scanning up to $MAX_COMMITS commits"
uv run repogauge mine "$REPO_ROOT" \
    --out "$MINE_OUT" \
    --max-commits "$MAX_COMMITS" \
    --llm-mode off

echo "==> review: applying decisions"
REVIEW_ARGS=(review "$MINE_OUT/candidates.jsonl" --out "$REVIEW_OUT" --llm-mode off)
if [[ -n "$DECISIONS_FILE" ]]; then
    REVIEW_ARGS+=(--decisions "$DECISIONS_FILE")
fi
uv run repogauge "${REVIEW_ARGS[@]}"

echo "==> export: materializing dataset"
uv run repogauge export "$REVIEW_OUT/reviewed.jsonl" \
    --out "$EXPORT_OUT" \
    --llm-mode off

echo "==> eval: running gold evaluation"
EVAL_ARGS=(
    eval "$EXPORT_OUT"
    --gold
    --out "$EVAL_OUT"
    --jobs "$JOBS"
    --timeout "$EVAL_TIMEOUT"
    --container-runtime "$EVAL_CONTAINER_RUNTIME"
)
if [[ -n "$EVAL_CONTAINER_HOST" ]]; then
    EVAL_ARGS+=(--container-host "$EVAL_CONTAINER_HOST")
fi
if uv run repogauge "${EVAL_ARGS[@]}"; then
    EVAL_OK=1
else
    EVAL_OK=0
fi

echo ""
if [[ "$EVAL_OK" -eq 1 ]]; then
    echo "Artifacts written to $OUT_DIR:"
    echo "  mine/repo_profile.json"
    echo "  mine/candidates.jsonl"
    echo "  review/reviewed.jsonl"
    echo "  review/review.html"
    echo "  export/dataset/dataset.jsonl"
    echo "  export/dataset/predictions.gold.jsonl"
    echo "  eval/dataset.resolved.jsonl"
    echo "  eval/predictions.resolved.jsonl"
    echo "  eval/validation.jsonl"
else
    echo "Artifacts written to $OUT_DIR:"
    echo "  mine/repo_profile.json"
    echo "  mine/candidates.jsonl"
    echo "  review/reviewed.jsonl"
    echo "  review/review.html"
    echo "  export/dataset/dataset.jsonl"
    echo "  export/dataset/predictions.gold.jsonl"
    echo ""
    echo "WARNING: eval step failed (swebench not installed — run 'pip install swebench' to enable harness evaluation)"
fi
