#!/usr/bin/env bash

rg_require_out_inside_repo() {
    local out_dir_abs="$1"
    local repo_root_abs="$2"

    if [[ "$out_dir_abs" != "$repo_root_abs" && "$out_dir_abs" != "$repo_root_abs/"* ]]; then
        echo "--out must live inside the repository root: $repo_root_abs" >&2
        exit 1
    fi
}


rg_run_mine_review_export() {
    local source_repo_root="$1"
    local out_dir_abs="$2"
    local max_commits="$3"
    local decisions_file="${4:-}"

    local mine_out="$out_dir_abs/mine"
    local review_out="$out_dir_abs/review"
    local export_out="$out_dir_abs/export"

    echo "==> mine: scanning up to $max_commits commits"
    uv run repogauge mine "$source_repo_root" \
        --out "$mine_out" \
        --max-commits "$max_commits" \
        --llm-mode off

    echo "==> review: applying decisions"
    local -a review_args=(review "$mine_out/candidates.jsonl" --out "$review_out" --llm-mode off)
    if [[ -n "$decisions_file" ]]; then
        review_args+=(--decisions "$decisions_file")
    fi
    uv run repogauge "${review_args[@]}"

    echo "==> export: materializing dataset"
    uv run repogauge export "$review_out/reviewed.jsonl" \
        --out "$export_out" \
        --llm-mode off
}


rg_run_eval_gold() {
    local dataset_root="$1"
    local eval_out="$2"
    local jobs="$3"
    local eval_timeout="$4"
    local eval_container_runtime="$5"
    local eval_container_host="${6:-}"

    local -a eval_args=(
        eval "$dataset_root"
        --gold
        --out "$eval_out"
        --jobs "$jobs"
        --timeout "$eval_timeout"
        --container-runtime "$eval_container_runtime"
        --llm-mode off
    )
    if [[ -n "$eval_container_host" ]]; then
        eval_args+=(--container-host "$eval_container_host")
    fi
    uv run repogauge "${eval_args[@]}"
}


rg_print_common_artifacts() {
    local out_dir_abs="$1"

    echo "Artifacts written to $out_dir_abs:"
    echo "  mine/repo_profile.json"
    echo "  mine/candidates.jsonl"
    echo "  review/reviewed.jsonl"
    echo "  review/review.html"
    echo "  export/dataset/dataset.jsonl"
    echo "  export/dataset/predictions.gold.jsonl"
}
