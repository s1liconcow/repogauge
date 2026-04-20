#!/usr/bin/env bash
# Run the full RepoGauge e2e pipeline against a sample repository.
# Usage: scripts/gauge_sample.sh SAMPLE_PATH [--out DIR] [--max-commits N]
#                                 [--decisions FILE] [--matrix PATH]
#                                 [--run-llm-mode off|local_only|allow_remote]
#                                 [--jobs N]
#                                 [--eval-timeout SECONDS]
#                                 [--eval-container-runtime docker|podman]
#                                 [--eval-container-host URI]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPOGAUGE_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
CALLER_PWD="$PWD"
export UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/codex-uv-cache}"

usage() {
    cat <<'EOF'
Usage:
  scripts/gauge_sample.sh SAMPLE_PATH [options]

Options:
  --out DIR                          Output root. Defaults under SAMPLE_PATH's git root.
  --max-commits N                    Max commits to scan during mine. Default: 100
  --decisions FILE                   Optional review decisions JSON/JSONL
  --matrix PATH                      Solver matrix for the run step.
                                     Default: examples/matrix.compare_cli.yaml
  --run-llm-mode MODE                off|local_only|allow_remote for run/analyze.
                                     Default: allow_remote
  --jobs N                           Parallel jobs for eval/run/analyze. Default: 4
  --eval-timeout SECONDS             Eval timeout per instance. Default: 120
  --eval-container-runtime RUNTIME   docker|podman. Default: podman
  --eval-container-host URI          Optional container host URI
EOF
}

if [[ $# -lt 1 ]]; then
    usage >&2
    exit 1
fi

SAMPLE_PATH=""
OUT_DIR=""
MAX_COMMITS=100
DECISIONS_FILE=""
MATRIX_PATH="$REPOGAUGE_ROOT/examples/matrix.compare_cli.yaml"
RUN_LLM_MODE="allow_remote"
JOBS=4
EVAL_TIMEOUT=120
EVAL_CONTAINER_RUNTIME="podman"
EVAL_CONTAINER_HOST=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --out) OUT_DIR="$2"; shift 2 ;;
        --max-commits) MAX_COMMITS="$2"; shift 2 ;;
        --decisions) DECISIONS_FILE="$2"; shift 2 ;;
        --matrix) MATRIX_PATH="$2"; shift 2 ;;
        --run-llm-mode) RUN_LLM_MODE="$2"; shift 2 ;;
        --jobs) JOBS="$2"; shift 2 ;;
        --eval-timeout) EVAL_TIMEOUT="$2"; shift 2 ;;
        --eval-container-runtime) EVAL_CONTAINER_RUNTIME="$2"; shift 2 ;;
        --eval-container-host) EVAL_CONTAINER_HOST="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        --*)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
        *)
            if [[ -n "$SAMPLE_PATH" ]]; then
                echo "Unexpected positional argument: $1" >&2
                exit 1
            fi
            SAMPLE_PATH="$1"
            shift
            ;;
    esac
done

if [[ -z "$SAMPLE_PATH" ]]; then
    echo "SAMPLE_PATH is required." >&2
    usage >&2
    exit 1
fi

if [[ -n "$DECISIONS_FILE" ]]; then
    DECISIONS_FILE="$(realpath "$CALLER_PWD/$DECISIONS_FILE")"
fi
if [[ "$MATRIX_PATH" != /* ]]; then
    MATRIX_PATH="$(realpath "$CALLER_PWD/$MATRIX_PATH")"
fi

case "$(basename "$MATRIX_PATH")" in
    matrix.compare_cli.yaml|matrix.opencode-cli.yaml)
        if [[ "$RUN_LLM_MODE" == "off" ]]; then
            echo "Matrix $MATRIX_PATH requires remote-capable solver execution." >&2
            echo "Re-run with --run-llm-mode allow_remote, or use --matrix examples/matrix.yaml." >&2
            exit 1
        fi
        ;;
esac

SAMPLE_REPO_ROOT="$(git -C "$SAMPLE_PATH" rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$SAMPLE_REPO_ROOT" ]]; then
    echo "Sample path must be inside a git repository: $SAMPLE_PATH" >&2
    exit 1
fi

if [[ -z "$OUT_DIR" ]]; then
    SAMPLE_BASENAME="$(basename "$SAMPLE_REPO_ROOT")"
    TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
    OUT_DIR="$SAMPLE_REPO_ROOT/.repogauge_e2e/${SAMPLE_BASENAME}-${TIMESTAMP}"
fi

mkdir -p "$OUT_DIR"
OUT_DIR_ABS="$(realpath "$OUT_DIR")"
SAMPLE_REPO_ROOT_ABS="$(realpath "$SAMPLE_REPO_ROOT")"
if [[ "$OUT_DIR_ABS" != "$SAMPLE_REPO_ROOT_ABS" && "$OUT_DIR_ABS" != "$SAMPLE_REPO_ROOT_ABS/"* ]]; then
    echo "--out must live inside the sample repository root: $SAMPLE_REPO_ROOT_ABS" >&2
    exit 1
fi

MINE_OUT="$OUT_DIR_ABS/mine"
REVIEW_OUT="$OUT_DIR_ABS/review"
EXPORT_OUT="$OUT_DIR_ABS/export"
EVAL_OUT="$OUT_DIR_ABS/eval"
RUN_OUT="$OUT_DIR_ABS/run"
ANALYZE_OUT="$RUN_OUT"

cd "$REPOGAUGE_ROOT"

echo "==> sample repo: $SAMPLE_REPO_ROOT_ABS"
echo "==> out: $OUT_DIR_ABS"
echo "==> matrix: $MATRIX_PATH"

echo "==> mine: scanning up to $MAX_COMMITS commits"
uv run repogauge mine "$SAMPLE_REPO_ROOT_ABS" \
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
    --llm-mode off
)
if [[ -n "$EVAL_CONTAINER_HOST" ]]; then
    EVAL_ARGS+=(--container-host "$EVAL_CONTAINER_HOST")
fi
uv run repogauge "${EVAL_ARGS[@]}"

echo "==> run: executing solver matrix"
RUN_ARGS=(
    run "$MATRIX_PATH"
    --dataset "$EVAL_OUT/dataset.resolved.jsonl"
    --out "$RUN_OUT"
    --jobs "$JOBS"
    --llm-mode "$RUN_LLM_MODE"
    --container-runtime "$EVAL_CONTAINER_RUNTIME"
)
if [[ -n "$EVAL_CONTAINER_HOST" ]]; then
    RUN_ARGS+=(--container-host "$EVAL_CONTAINER_HOST")
fi
uv run repogauge "${RUN_ARGS[@]}"

RUN_ROOT="$(UV_CACHE_DIR="$UV_CACHE_DIR" uv run python - <<'PY' "$RUN_OUT"
import json
import sys
from pathlib import Path

manifest_path = Path(sys.argv[1]) / "manifest.json"
payload = json.loads(manifest_path.read_text(encoding="utf-8"))
run_root = payload.get("artifact_paths", {}).get("run_root", "")
if not run_root:
    raise SystemExit("run manifest missing artifact_paths.run_root")
print(run_root)
PY
)"
ATTEMPTS_JSONL="$RUN_ROOT/attempts.jsonl"
ATTEMPTS_PARQUET="$RUN_ROOT/attempts.parquet"

if [[ -f "$ATTEMPTS_JSONL" || -f "$ATTEMPTS_PARQUET" ]]; then
    echo "==> analyze: evaluating solver patches and building reports"
    ANALYZE_ARGS=(
        analyze "$RUN_ROOT"
        --jobs "$JOBS"
        --llm-mode "$RUN_LLM_MODE"
        --container-runtime "$EVAL_CONTAINER_RUNTIME"
    )
    if [[ -n "$EVAL_CONTAINER_HOST" ]]; then
        ANALYZE_ARGS+=(--container-host "$EVAL_CONTAINER_HOST")
    fi
    uv run repogauge "${ANALYZE_ARGS[@]}"
else
    echo "==> analyze: skipping (no attempt artifacts under $RUN_ROOT)"
fi

echo ""
echo "Artifacts written to $OUT_DIR_ABS:"
echo "  mine/repo_profile.json"
echo "  mine/candidates.jsonl"
echo "  review/reviewed.jsonl"
echo "  review/review.html"
echo "  export/dataset/dataset.jsonl"
echo "  export/dataset/predictions.gold.jsonl"
echo "  eval/dataset.resolved.jsonl"
echo "  eval/validation.jsonl"
if [[ -f "$ATTEMPTS_JSONL" ]]; then
    echo "  ${ATTEMPTS_JSONL#$OUT_DIR_ABS/}"
elif [[ -f "$ATTEMPTS_PARQUET" ]]; then
    echo "  ${ATTEMPTS_PARQUET#$OUT_DIR_ABS/}"
else
    echo "  analyze skipped: no attempt artifacts"
fi
if [[ -f "$RUN_ROOT/analysis_report.json" ]]; then
    echo "  ${RUN_ROOT#$OUT_DIR_ABS/}/analysis_report.json"
fi
