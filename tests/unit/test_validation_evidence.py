"""Tests for validation evidence persistence and failure taxonomy."""

from pathlib import Path

from repogauge.validation.evidence import (
    FAILURE_TAXONOMY,
    normalize_failure_reason,
    write_validation_bundle,
)


def test_normalize_failure_reason_maps_known_cases() -> None:
    assert (
        normalize_failure_reason(
            status="error",
            reason="run_a_failed",
            failure_code=None,
        )
        == "base_repo_unrunnable"
    )
    assert (
        normalize_failure_reason(
            status="error",
            reason="run_b_failed",
            failure_code="patch_apply_failed",
        )
        == "patch_apply_failed"
    )
    assert (
        normalize_failure_reason(
            status="error",
            reason="missing_thing",
            failure_code="missing_junit",
        )
        == "missing_junit"
    )
    assert (
        normalize_failure_reason(
            status="flaky",
            reason="run_c_rerun_mismatch",
            failure_code=None,
        )
        == "flaky_outcomes"
    )
    assert (
        normalize_failure_reason(
            status="not_resolved",
            reason="no_fail_to_pass",
            failure_code=None,
        )
        == "no_fail_to_pass"
    )


def test_normalize_failure_reason_falls_back_to_unknown_for_unknown_codes() -> None:
    assert (
        normalize_failure_reason(
            status="error",
            reason="weird_reason",
            failure_code="weird_code",
        )
        == "unknown_validator_failure"
    )


def test_normalize_failure_reason_has_stable_taxonomy_membership() -> None:
    unknown = normalize_failure_reason("error", "nope", "nope")
    assert unknown in FAILURE_TAXONOMY


def test_write_validation_bundle_creates_expected_artifacts(tmp_path: Path) -> None:
    outcome = {
        "log_a": "alpha",
        "log_b": "beta",
        "log_c": "charlie",
        "log_b_rerun": "bravo",
        "log_c_rerun": "delta",
        "run_a_attempts": [{"attempt": 1}],
        "run_b_attempts": [{"attempt": 1}],
        "run_c_attempts": [{"attempt": 1}],
        "run_b_rerun_attempts": [{"attempt": 1}],
        "run_c_rerun_attempts": [{"attempt": 1}],
    }

    paths = write_validation_bundle(
        out_root=tmp_path,
        instance_id="inst-1",
        outcome=outcome,
    )

    assert set(paths) == {
        "bundle_root",
        "run_a",
        "run_b",
        "run_c",
        "run_b_rerun",
        "run_c_rerun",
    }
    assert (tmp_path / "logs" / "validation" / "inst-1" / "run_a.log").exists()
    assert (
        tmp_path / "logs" / "validation" / "inst-1" / "run_b_attempts.jsonl"
    ).exists()
    assert (
        tmp_path / "logs" / "validation" / "inst-1" / "run_c_rerun_attempts.jsonl"
    ).exists()
