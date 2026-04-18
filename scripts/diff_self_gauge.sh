#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
FIXTURE_ROOT="$REPO_ROOT/tests/fixtures/golden_self_gauge_v0_1_0"
MAX_COMMITS="${SELF_GAUGE_MAX_COMMITS:-100}"
WORK_DIR="$REPO_ROOT/.e2e_tmp"
mkdir -p "$WORK_DIR"
OUT_DIR="$(mktemp -d "$WORK_DIR/self-gauge.XXXXXX")"
CURRENT_EXPORT="$OUT_DIR/out/export"
PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
GAUGE_LOG="$OUT_DIR/gauge_self.log"
export UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/codex-uv-cache}"

cleanup() {
    rm -rf "$OUT_DIR"
}
trap cleanup EXIT

if [[ ! -x "$PYTHON_BIN" ]]; then
    PYTHON_BIN="$(command -v python3)"
fi

echo "==> self-gauge: running gauge_self.sh"
if ! "$REPO_ROOT/scripts/gauge_self.sh" --out "$OUT_DIR/out" --max-commits "$MAX_COMMITS" >"$GAUGE_LOG" 2>&1; then
    cat "$GAUGE_LOG" >&2
    exit 1
fi

echo "==> self-gauge: comparing against golden fixture"
"$PYTHON_BIN" - "$FIXTURE_ROOT" "$CURRENT_EXPORT" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

fixture_root = Path(sys.argv[1])
current_root = Path(sys.argv[2])
allowed_new_keys = {"language", "language_version", "runtime_version"}


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def compare_specs() -> None:
    expected = load_json(fixture_root / "specs.json")
    actual = load_json(current_root / "specs.json")
    differences: list[str] = []
    for key in sorted(set(expected) | set(actual)):
        expected_has = key in expected
        actual_has = key in actual
        if key in allowed_new_keys:
            if expected_has and not actual_has:
                differences.append(
                    f"{key}: fixture={expected.get(key)!r} current=<missing>"
                )
            elif expected_has and actual_has and expected.get(key) != actual.get(key):
                differences.append(
                    f"{key}: fixture={expected.get(key)!r} current={actual.get(key)!r}"
                )
            continue
        if expected.get(key) != actual.get(key):
            differences.append(
                f"{key}: fixture={expected.get(key)!r} current={actual.get(key)!r}"
            )
    if differences:
        raise SystemExit(
            "specs.json drifted outside the allowed language fields:\n"
            + "\n".join(f"  - {line}" for line in differences)
        )


def compare_bytes(rel_path: str) -> None:
    expected = (fixture_root / rel_path).read_text(encoding="utf-8")
    actual = (current_root / rel_path).read_text(encoding="utf-8")
    if expected != actual:
        raise SystemExit(f"{rel_path} drifted from the golden fixture")


compare_specs()
compare_bytes("dataset/dataset.jsonl")
compare_bytes("adapter_s1liconcow_repogauge.py")

print("self-gauge diff report")
print("  allowed new JSON keys: language, language_version, runtime_version")
print("  specs.json: matched fixture except for the allowed keys")
print("  dataset/dataset.jsonl: matched fixture")
print("  adapter_s1liconcow_repogauge.py: matched fixture")
PY
