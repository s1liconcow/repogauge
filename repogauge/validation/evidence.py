"""Validation evidence persistence and failure taxonomy."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping

FAILURE_TAXONOMY: set[str] = {
    "base_repo_unrunnable",
    "env_install_failed",
    "test_targeting_failed",
    "test_patch_apply_failed",
    "patch_apply_failed",
    "no_fail_to_pass",
    "pass_to_pass_regression",
    "flaky_outcomes",
    "missing_junit",
    "unknown_validator_failure",
}


def _coerce_log_path(path: Path) -> str:
    return str(path)


def normalize_failure_reason(
    status: str,
    reason: str | None,
    failure_code: str | None,
) -> str:
    """Normalize failure semantics to a stable taxonomy value."""
    if failure_code in FAILURE_TAXONOMY:
        return failure_code

    if reason in FAILURE_TAXONOMY:
        return reason

    if status == "error":
        if reason == "run_a_failed":
            return "base_repo_unrunnable"
        if reason == "run_b_rerun_failed":
            return "flaky_outcomes"
        if reason == "run_c_rerun_failed":
            return "flaky_outcomes"
        if reason in {"run_b_failed", "run_c_failed"}:
            return "unknown_validator_failure"

    if status == "flaky":
        return "flaky_outcomes"

    if status == "not_resolved":
        if reason in {
            "no_fail_to_pass",
            "pass_to_pass_regression",
            "declared_ftp_not_resolved",
        }:
            return reason
        return "unknown_validator_failure"

    return "unknown_validator_failure"


def write_validation_bundle(
    *,
    out_root: Path,
    instance_id: str,
    outcome: Mapping[str, Any],
) -> Dict[str, Dict[str, str]]:
    """Persist full validation outputs for one instance.

    Returns a mapping keyed by run phase (run_a/run_b/run_c/...) with artifact paths.
    """
    bundle_root = out_root / "logs" / "validation" / instance_id
    bundle_root.mkdir(parents=True, exist_ok=True)

    artifact_paths: Dict[str, Dict[str, str]] = {
        "bundle_root": _coerce_log_path(bundle_root)
    }

    run_map = {
        "run_a": ("log_a", "run_a_attempts"),
        "run_b": ("log_b", "run_b_attempts"),
        "run_c": ("log_c", "run_c_attempts"),
        "run_b_rerun": ("log_b_rerun", "run_b_rerun_attempts"),
        "run_c_rerun": ("log_c_rerun", "run_c_rerun_attempts"),
    }

    for run_name, (log_key, attempts_key) in run_map.items():
        log_path = bundle_root / f"{run_name}.log"
        attempts_path = bundle_root / f"{run_name}_attempts.jsonl"

        log_payload = outcome.get(log_key, "")
        log_path.write_text(str(log_payload), encoding="utf-8")

        attempts_payload = outcome.get(attempts_key, [])
        attempts_payload = (
            attempts_payload if isinstance(attempts_payload, list) else []
        )
        attempts_path.write_text(
            "".join(json.dumps(r, sort_keys=True) + "\n" for r in attempts_payload),
            encoding="utf-8",
        )

        artifact_paths[run_name] = {
            "log": _coerce_log_path(log_path),
            "attempts": _coerce_log_path(attempts_path),
        }

    return artifact_paths


def tail(text: str, max_chars: int = 2000) -> str:
    """Return tail of text for compact artifact rows."""
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]
