#!/usr/bin/env bash
# Run repogauge against this repository.
# Usage: scripts/gauge_self.sh [--out DIR] [--max-commits N] [--decisions FILE]

set -euo pipefail

REPO_ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
OUT_DIR="$REPO_ROOT/out"
# NOTE: --out must be a path inside the repo so that the export step can
# resolve the git root by walking up from the output directory.
MAX_COMMITS=100
DECISIONS_FILE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --out)        OUT_DIR="$2";       shift 2 ;;
        --max-commits) MAX_COMMITS="$2";  shift 2 ;;
        --decisions)  DECISIONS_FILE="$2"; shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

MINE_OUT="$OUT_DIR/mine"
REVIEW_OUT="$OUT_DIR/review"
EXPORT_OUT="$OUT_DIR/export"

echo "==> mine: scanning up to $MAX_COMMITS commits"
repogauge mine "$REPO_ROOT" \
    --out "$MINE_OUT" \
    --max-commits "$MAX_COMMITS" \
    --llm-mode off

echo "==> review: applying decisions"
REVIEW_ARGS=(review "$MINE_OUT/candidates.jsonl" --out "$REVIEW_OUT" --llm-mode off)
if [[ -n "$DECISIONS_FILE" ]]; then
    REVIEW_ARGS+=(--decisions "$DECISIONS_FILE")
fi
repogauge "${REVIEW_ARGS[@]}"

echo "==> export: materializing dataset"
repogauge export "$REVIEW_OUT/reviewed.jsonl" \
    --out "$EXPORT_OUT" \
    --llm-mode off

echo ""
echo "Artifacts written to $OUT_DIR:"
echo "  mine/repo_profile.json"
echo "  mine/candidates.jsonl"
echo "  review/reviewed.jsonl"
echo "  review/review.html"
echo "  export/dataset/dataset.jsonl"
echo "  export/dataset/predictions.gold.jsonl"
