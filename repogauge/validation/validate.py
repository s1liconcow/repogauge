"""Local eval pipeline for SWE-bench-style dataset instances (bead j7y).

For each dataset instance the evaluator runs four passes:

  Run A  base commit sanity check
  Run B  base_commit + test_patch                  (establishes which tests fail)
  Run C  base_commit + test_patch + pred_patch      (establishes which tests the fix resolves)
  Run D  reruns for flake detection

From those runs:
  FAIL_TO_PASS  = tests that were fail/error in B and pass in C
  PASS_TO_PASS  = tests that passed in both B and C
  resolved      = all of FAIL_TO_PASS pass in C (or, if FAIL_TO_PASS is empty, any test passes)

The evaluator writes one ValidationRow per instance to validation.jsonl.

This is a local, harness-free implementation.  It uses git worktrees for
isolation and runs tests through the active language adapter.
Python currently uses the current interpreter and workspace-relative imports
so editable installs are not required.
"""

from __future__ import annotations

import json
import os
import shlex
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from repogauge.exec import run_command
from repogauge.lang import LanguageAdapter, find_adapter
from repogauge.utils.git import CommandPatchError, apply_patch_text, create_worktree
from repogauge.validation.junit_parser import (
    JUnitParseError,
    OUTCOME_PASS,
)
from repogauge.validation.evidence import (
    normalize_failure_reason,
    tail,
    write_validation_bundle,
)
from repogauge.validation.testsel import build_targeted_test_plan


_JUNIT_XML_FLAGS = ("--junit-xml", "--junitxml")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvalRunSummary:
    validation_path: str
    total: int
    resolved: int
    not_resolved: int
    error: int
    skipped: int
    resolve_rate: float
    results_path: str | None = None
    instance_results_path: str | None = None
    dataset_path: str | None = None
    predictions_path: str | None = None
    harness_output: str | None = None


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value:
            continue
        rows.append(json.loads(value))
    return rows


def _resolve_dataset(path: Path) -> Tuple[Path, Path]:
    """Given a dataset.jsonl path or its parent directory, return (dataset, gold_predictions)."""
    if path.is_dir():
        dataset = path / "dataset" / "dataset.jsonl"
        predictions = path / "dataset" / "predictions.gold.jsonl"
    elif path.name == "dataset.jsonl":
        dataset = path
        predictions = path.parent / "predictions.gold.jsonl"
    else:
        dataset = path
        predictions = path.parent / "predictions.gold.jsonl"
    return dataset, predictions


def _prediction_key(row: Dict[str, Any]) -> tuple[str, str]:
    instance_id = str(row.get("instance_id", "")).strip()
    solver_id = str(
        row.get("solver_id") or row.get("model_name_or_path") or "unknown"
    ).strip()
    return solver_id, instance_id


def _write_resolved_eval_artifacts(
    *,
    out_root: Path,
    dataset_rows: List[Dict[str, Any]],
    prediction_rows: List[Dict[str, Any]],
    instance_rows: List[Dict[str, Any]],
) -> tuple[Path, Path]:
    dataset_path = out_root / "dataset.resolved.jsonl"
    predictions_path = out_root / "predictions.resolved.jsonl"
    dataset_by_id = {
        str(row.get("instance_id", "")).strip(): row
        for row in dataset_rows
        if str(row.get("instance_id", "")).strip()
    }
    prediction_by_key = {
        _prediction_key(row): row
        for row in prediction_rows
        if str(row.get("instance_id", "")).strip()
    }
    resolved_datasets: List[Dict[str, Any]] = []
    resolved_predictions: List[Dict[str, Any]] = []
    seen_prediction_keys: set[tuple[str, str]] = set()

    for row in instance_rows:
        if not row.get("resolved"):
            continue
        instance_id = str(row.get("instance_id", "")).strip()
        if not instance_id:
            continue
        solver_id = str(
            row.get("solver_id") or row.get("model_name_or_path") or "unknown"
        ).strip()
        key = (solver_id, instance_id)
        if key in seen_prediction_keys:
            continue
        prediction_row = prediction_by_key.get(key)
        dataset_row = dataset_by_id.get(instance_id)
        if prediction_row is None or dataset_row is None:
            continue
        resolved_predictions.append(dict(prediction_row))
        resolved_datasets.append(dict(dataset_row))
        seen_prediction_keys.add(key)

    dataset_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in resolved_datasets),
        encoding="utf-8",
    )
    predictions_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in resolved_predictions),
        encoding="utf-8",
    )
    return dataset_path, predictions_path


def _resolve_adapter(
    adapter: LanguageAdapter | None = None,
    *,
    language: str | None = None,
) -> LanguageAdapter:
    if adapter is not None:
        return adapter

    candidate = "python"
    if isinstance(language, str):
        normalized = language.strip().lower()
        if normalized:
            candidate = normalized

    try:
        return find_adapter(candidate)
    except KeyError:
        return find_adapter("python")


def _normalize_junit_output_flag(
    base_cmd: List[str], junit_xml: Path
) -> Tuple[List[str], Path]:
    """Return a command with a concrete junit output path.

    Pytest supports both `--junit-xml` and `--junitxml` variants,
    each in either space- or equals-separated form.
    """
    junit_output = Path(junit_xml)
    junit_output_str = str(junit_output)
    cmd = list(base_cmd)

    for index, part in enumerate(cmd):
        for flag in _JUNIT_XML_FLAGS:
            prefix = f"{flag}="
            if part == flag:
                if index + 1 >= len(cmd):
                    cmd.append(junit_output_str)
                else:
                    cmd[index + 1] = junit_output_str
                return cmd, junit_output
            if part.startswith(prefix):
                cmd[index] = f"{prefix}{junit_output_str}"
                return cmd, junit_output

    cmd.append(f"--junit-xml={junit_output_str}")
    return cmd, junit_output


class TestExecutionError(RuntimeError):
    """Raised when deterministic test attempts fail to produce parseable output."""

    __test__ = False

    def __init__(self, message: str, attempts: List[Dict[str, Any]]) -> None:
        super().__init__(message)
        self.attempts = attempts


PytestExecutionError = TestExecutionError


def _bundle_payload(outcome: Dict[str, Any]) -> Dict[str, Any]:
    """Build a minimal payload for ``write_validation_bundle``."""
    return {
        "log_a": outcome.get("log_a", ""),
        "log_b": outcome.get("log_b", ""),
        "log_c": outcome.get("log_c", ""),
        "log_b_rerun": outcome.get("log_b_rerun", ""),
        "log_c_rerun": outcome.get("log_c_rerun", ""),
        "run_a_attempts": outcome.get("run_a_attempts", []),
        "run_b_attempts": outcome.get("run_b_attempts", []),
        "run_c_attempts": outcome.get("run_c_attempts", []),
        "run_b_rerun_attempts": outcome.get("run_b_rerun_attempts", []),
        "run_c_rerun_attempts": outcome.get("run_c_rerun_attempts", []),
    }


def _test_command_attempts(
    test_cmd_base: str, *, adapter: LanguageAdapter
) -> List[List[str]]:
    """Return deterministic command attempts for the active test runner."""
    attempts = [
        list(attempt) for attempt in adapter.test_command_attempts(test_cmd_base)
    ]
    return attempts or [shlex.split(test_cmd_base)]


def _pytest_command_attempts(test_cmd_base: str) -> List[List[str]]:
    """Back-compat wrapper for the Python adapter's command attempts."""
    adapter = _resolve_adapter(language="python")
    return _test_command_attempts(test_cmd_base, adapter=adapter)


def _test_report_path(temp_root: Path, label: str, adapter: LanguageAdapter) -> Path:
    report_filename = adapter.test_report_filename() or "junit.xml"
    return temp_root / f"{label}_{Path(report_filename).name}"


def _run_test(
    worktree: Path,
    *,
    test_files: List[str],
    test_report_path: Path,
    timeout_seconds: int = 120,
    test_cmd_base: str = "",
    adapter: LanguageAdapter | None = None,
    test_spec: object | None = None,
) -> Tuple[Dict[str, str], str, List[Dict[str, Any]]]:
    """Run tests in *worktree* with deterministic command retries.

    Returns:
        - ``results_dict`` maps test_id -> outcome string
        - ``raw_output`` from the final attempt
        - ``attempts`` persisted attempt metadata

    ``results_dict`` is empty if XML parsing fails for every deterministic attempt.
    ``raw_output`` is the combined stdout+stderr for log purposes.
    ``test_cmd_base`` is taken from the adapter spec when available.
    """
    active_adapter = _resolve_adapter(adapter)
    env = {**os.environ, **active_adapter.env_overrides(worktree)}
    attempts: List[Dict[str, Any]] = []
    raw = ""
    last_parse_error: str | None = None

    for index, base_cmd in enumerate(
        _test_command_attempts(test_cmd_base, adapter=active_adapter)
    ):
        test_cmd = list(base_cmd)
        report_input: object = test_report_path
        if active_adapter.name() == "python":
            test_cmd, report_path_to_read = _normalize_junit_output_flag(
                test_cmd, test_report_path
            )
            report_input = report_path_to_read
        else:
            report_glob_getter = getattr(active_adapter, "test_report_glob", None)
            report_filename_getter = getattr(active_adapter, "test_report_filename", None)
            report_glob = report_glob_getter() if callable(report_glob_getter) else None
            report_filename = (
                report_filename_getter() if callable(report_filename_getter) else None
            )
            if report_glob:
                report_input = worktree / report_glob
            elif report_filename:
                report_input = worktree / report_filename
            else:
                report_input = None

        if isinstance(report_input, Path) and report_input.exists():
            try:
                report_input.unlink()
            except OSError:
                pass

        cmd = list(test_cmd)
        if active_adapter.name() == "python":
            cmd += ["--tb=no", "-q"]
        if test_files:
            cmd += test_files
        result = run_command(
            cmd, cwd=str(worktree), env=env, timeout_seconds=timeout_seconds
        )
        raw = f"[stdout]\n{result.stdout}\n[stderr]\n{result.stderr}"

        attempt_entry: Dict[str, Any] = {
            "attempt": index + 1,
            "command": cmd,
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "elapsed_ms": result.elapsed_ms,
            "status": "unknown",
        }

        try:
            parse_input = report_input if report_input is not None else raw
            outcomes = active_adapter.parse_test_output(parse_input, test_spec)
        except JUnitParseError as exc:
            last_parse_error = str(exc)
            attempt_entry.update(
                {
                    "status": "parse_error",
                    "error": str(exc),
                }
            )
            attempts.append(attempt_entry)
            continue

        attempt_entry["status"] = "success"
        attempt_entry["tests_run"] = len(outcomes)
        attempts.append(attempt_entry)
        return outcomes, raw, attempts

    if last_parse_error:
        raise PytestExecutionError(
            f"failed to obtain valid junit output after {len(attempts)} attempt(s): {last_parse_error}",
            attempts,
        )

    raise PytestExecutionError(
        f"test execution produced no parseable output for {test_files}",
        attempts,
    )


def _run_pytest(
    worktree: Path,
    *,
    test_files: List[str],
    junit_xml: Path,
    timeout_seconds: int = 120,
    test_cmd_base: str = "python -m pytest",
) -> Tuple[Dict[str, str], str, List[Dict[str, Any]]]:
    """Back-compat wrapper around :func:`_run_test`."""
    return _run_test(
        worktree,
        test_files=test_files,
        test_report_path=junit_xml,
        timeout_seconds=timeout_seconds,
        test_cmd_base=test_cmd_base,
        adapter=_resolve_adapter(language="python"),
    )


def _derive_test_lists(
    run_b: Dict[str, str],
    run_c: Dict[str, str],
    declared_ftp: List[str],
    declared_ptp: List[str],
) -> Tuple[List[str], List[str]]:
    """Compute FAIL_TO_PASS and PASS_TO_PASS from two run outcomes.

    If declared lists are non-empty, filter to only tests that appear in them;
    otherwise derive them purely from the two runs.
    """
    all_ids = set(run_b) | set(run_c)

    ftp = sorted(
        tid
        for tid in all_ids
        # "absent" (None) covers collection errors and newly-added tests that couldn't
        # be collected at base_commit because the implementation didn't exist yet.
        if run_b.get(tid) in {"fail", "error", None} and run_c.get(tid) == OUTCOME_PASS
    )
    ptp = sorted(
        tid
        for tid in all_ids
        if run_b.get(tid) == OUTCOME_PASS and run_c.get(tid) == OUTCOME_PASS
    )

    # If the dataset already has declared lists, restrict to those subsets.
    if declared_ftp:
        ftp = [t for t in declared_ftp if run_c.get(t) == OUTCOME_PASS]
    if declared_ptp:
        ptp = [t for t in declared_ptp if run_c.get(t) == OUTCOME_PASS]

    return ftp, ptp


def _is_resolved(
    ftp: List[str], run_c: Dict[str, str], declared_ftp: List[str]
) -> bool:
    """Return True when the prediction resolves the instance."""
    if declared_ftp:
        return all(run_c.get(t) == OUTCOME_PASS for t in declared_ftp)
    # No declared list — resolved if we derived at least one FAIL_TO_PASS test.
    return bool(ftp)


def _run_validation_pass(
    *,
    label: str,
    temp_root: Path,
    repo_root: Path,
    base_commit: str,
    test_patch: str,
    pred_patch: str,
    test_files: List[str],
    timeout_seconds: int,
    test_cmd_base: str = "",
    adapter: LanguageAdapter | None = None,
    apply_test_patch: bool = False,
    apply_pred_patch: bool = False,
) -> Dict[str, Any]:
    """Execute one isolated validation run and return outcomes + telemetry."""
    wt = None
    outcomes: Dict[str, str] = {}
    log = ""
    attempts: List[Dict[str, Any]] = []
    failure_code: str | None = None
    active_adapter = _resolve_adapter(adapter)

    try:
        wt = create_worktree(repo_root, ref=base_commit)
        if test_patch.strip():
            apply_patch_text(wt.path, test_patch)
        if pred_patch.strip():
            apply_patch_text(wt.path, pred_patch)

        test_report_path = _test_report_path(temp_root, label, active_adapter)
        outcomes, log, attempts = _run_test(
            wt.path,
            test_files=test_files,
            test_report_path=test_report_path,
            timeout_seconds=timeout_seconds,
            test_cmd_base=test_cmd_base,
            adapter=active_adapter,
            test_spec=test_files or None,
        )
    except Exception as exc:
        if isinstance(exc, CommandPatchError):
            if apply_pred_patch:
                failure_code = "patch_apply_failed"
            elif apply_test_patch:
                failure_code = "test_patch_apply_failed"
            else:
                failure_code = "unknown_validator_failure"
        elif isinstance(exc, PytestExecutionError):
            failure_code = "missing_junit"
            attempts = exc.attempts
        else:
            failure_code = "unknown_validator_failure"
        return {
            "status": "failed",
            "error": f"{label} failed: {exc}",
            "outcomes": {},
            "log": log,
            "attempts": attempts,
            "failure_code": failure_code,
        }
    finally:
        if wt is not None:
            try:
                wt.remove()
            except Exception:
                pass

    return {
        "status": "passed",
        "error": None,
        "outcomes": outcomes,
        "log": log,
        "attempts": attempts,
    }


def _is_flaky(
    run_b: Dict[str, str],
    run_b_rerun: Dict[str, str],
    run_c: Dict[str, str],
    run_c_rerun: Dict[str, str],
) -> bool:
    """Return True when reruns are not stable."""
    return run_b != run_b_rerun or run_c != run_c_rerun


def _has_pass_to_pass_regression(
    run_b: Dict[str, str],
    run_c: Dict[str, str],
    declared_ptp: List[str],
) -> bool:
    """Return True when any declared PASS_TO_PASS test regresses in Run 3."""
    if not declared_ptp:
        return False
    return any(
        run_b.get(test_id) == OUTCOME_PASS and run_c.get(test_id) != OUTCOME_PASS
        for test_id in declared_ptp
    )


def _declared_ftp_not_resolved(
    run_c: Dict[str, str],
    declared_ftp: List[str],
) -> bool:
    """Return True when any declared FAIL_TO_PASS test still fails in Run 3."""
    if not declared_ftp:
        return False
    return any(run_c.get(test_id) != OUTCOME_PASS for test_id in declared_ftp)


def _build_eval_result(
    *,
    status: str,
    error: str | None,
    reason: str | None,
    targeted_test_cmd: str,
    test_inputs: List[str],
    log_a: str,
    log_b: str = "",
    log_c: str = "",
    log_b_rerun: str = "",
    log_c_rerun: str = "",
    run_a: Dict[str, str],
    run_b: Dict[str, str],
    run_c: Dict[str, str],
    run_b_rerun: Dict[str, str],
    run_c_rerun: Dict[str, str],
    run_a_attempts: List[Dict[str, Any]],
    run_b_attempts: List[Dict[str, Any]],
    run_c_attempts: List[Dict[str, Any]],
    run_b_rerun_attempts: List[Dict[str, Any]],
    run_c_rerun_attempts: List[Dict[str, Any]],
    flake_runs: int,
    FAIL_TO_PASS: List[str],
    PASS_TO_PASS: List[str],
    resolved: bool,
    failure_code: str | None = None,
    test_strategy: str | None = None,
    environment_strategy: str = "default",
) -> Dict[str, Any]:
    normalized_reason = reason
    if status != "resolved" and reason is not None:
        normalized_reason = normalize_failure_reason(
            status=status, reason=reason, failure_code=failure_code
        )

    return {
        "status": status,
        "error": error,
        "reason": normalized_reason,
        "failure_code": failure_code,
        "environment_strategy": environment_strategy,
        "test_strategy": test_strategy or "full",
        "targeted_test_cmd": targeted_test_cmd,
        "targeted_test_inputs": test_inputs,
        "log_a": log_a,
        "log_b": log_b,
        "log_c": log_c,
        "log_b_rerun": log_b_rerun,
        "log_c_rerun": log_c_rerun,
        "run_a": run_a,
        "run_b": run_b,
        "run_c": run_c,
        "run_b_rerun": run_b_rerun,
        "run_c_rerun": run_c_rerun,
        "run_a_attempts": run_a_attempts,
        "run_b_attempts": run_b_attempts,
        "run_c_attempts": run_c_attempts,
        "run_b_rerun_attempts": run_b_rerun_attempts,
        "run_c_rerun_attempts": run_c_rerun_attempts,
        "flake_runs": flake_runs,
        "FAIL_TO_PASS": FAIL_TO_PASS,
        "PASS_TO_PASS": PASS_TO_PASS,
        "resolved": resolved,
    }


def _finalize_eval_result(
    *,
    outcome: Dict[str, Any],
    out_root: Path | None,
    instance_id: str | None,
) -> Dict[str, Any]:
    if out_root is not None and instance_id:
        outcome["validation_bundle"] = write_validation_bundle(
            out_root=out_root,
            instance_id=instance_id,
            outcome=_bundle_payload(outcome),
        )
    return outcome


# ---------------------------------------------------------------------------
# Per-instance evaluation
# ---------------------------------------------------------------------------


def _eval_instance(
    *,
    repo_root: Path,
    base_commit: str,
    pred_patch: str,
    test_patch: str,
    declared_ftp: List[str],
    declared_ptp: List[str],
    timeout_seconds: int,
    test_cmd_base: str = "",
    adapter: LanguageAdapter | None = None,
    out_root: Path | None = None,
    instance_id: str | None = None,
    environment_strategy: str = "default",
) -> Dict[str, Any]:
    """Run validation passes for one instance. Returns a result dict."""
    active_adapter = _resolve_adapter(adapter)
    targeted_test_cmd, targeted_test_inputs = build_targeted_test_plan(
        test_cmd_base, test_patch
    )
    test_inputs = targeted_test_inputs
    target_cmd_tokens = targeted_test_cmd.split()
    is_python_adapter = active_adapter.name() == "python"
    is_pytest = is_python_adapter and (
        "pytest" in target_cmd_tokens
        or (
            target_cmd_tokens[:2] == ["python", "-m"]
            and len(target_cmd_tokens) > 2
            and target_cmd_tokens[2] == "pytest"
        )
    )
    if is_pytest:
        test_strategy = "targeted_pytest" if test_inputs else "full_pytest"
    else:
        test_strategy = "targeted_command" if test_inputs else "full_command"

    with tempfile.TemporaryDirectory(prefix="repogauge-eval-") as tmpdir:
        tmp = Path(tmpdir)

        run_a = _run_validation_pass(
            label="a",
            temp_root=tmp,
            repo_root=repo_root,
            base_commit=base_commit,
            test_patch="",
            pred_patch="",
            test_files=test_inputs,
            timeout_seconds=timeout_seconds,
            test_cmd_base=targeted_test_cmd,
            adapter=active_adapter,
            apply_test_patch=False,
            apply_pred_patch=False,
        )
        if run_a["status"] == "failed":
            return _finalize_eval_result(
                outcome=_build_eval_result(
                    status="error",
                    error=run_a["error"],
                    failure_code=run_a.get("failure_code"),
                    reason="run_a_failed",
                    targeted_test_cmd=targeted_test_cmd,
                    test_inputs=test_inputs,
                    log_a=run_a["log"],
                    run_a=run_a["outcomes"],
                    run_b={},
                    run_c={},
                    run_b_rerun={},
                    run_c_rerun={},
                    run_a_attempts=run_a["attempts"],
                    run_b_attempts=[],
                    run_c_attempts=[],
                    run_b_rerun_attempts=[],
                    run_c_rerun_attempts=[],
                    flake_runs=0,
                    FAIL_TO_PASS=[],
                    PASS_TO_PASS=[],
                    resolved=False,
                    test_strategy=test_strategy,
                    environment_strategy=environment_strategy,
                ),
                out_root=out_root,
                instance_id=instance_id,
            )

        # --- Pass B: base + test_patch --------------------------------------
        run_b = _run_validation_pass(
            label="b",
            temp_root=tmp,
            repo_root=repo_root,
            base_commit=base_commit,
            test_patch=test_patch,
            pred_patch="",
            test_files=test_inputs,
            timeout_seconds=timeout_seconds,
            test_cmd_base=targeted_test_cmd,
            adapter=active_adapter,
            apply_test_patch=True,
            apply_pred_patch=False,
        )
        if run_b["status"] == "failed":
            return _finalize_eval_result(
                outcome=_build_eval_result(
                    status="error",
                    error=run_b["error"],
                    failure_code=run_b.get("failure_code"),
                    reason="run_b_failed",
                    targeted_test_cmd=targeted_test_cmd,
                    test_inputs=test_inputs,
                    log_a=run_a["log"],
                    log_b=run_b["log"],
                    run_a=run_a["outcomes"],
                    run_b=run_b["outcomes"],
                    run_c={},
                    run_b_rerun={},
                    run_c_rerun={},
                    run_a_attempts=run_a["attempts"],
                    run_b_attempts=run_b["attempts"],
                    run_c_attempts=[],
                    run_b_rerun_attempts=[],
                    run_c_rerun_attempts=[],
                    flake_runs=0,
                    FAIL_TO_PASS=[],
                    PASS_TO_PASS=[],
                    resolved=False,
                    test_strategy=test_strategy,
                    environment_strategy=environment_strategy,
                ),
                out_root=out_root,
                instance_id=instance_id,
            )

        # --- Pass C: base + test_patch + pred_patch --------------------------
        run_c = _run_validation_pass(
            label="c",
            temp_root=tmp,
            repo_root=repo_root,
            base_commit=base_commit,
            test_patch=test_patch,
            pred_patch=pred_patch,
            test_files=test_inputs,
            timeout_seconds=timeout_seconds,
            test_cmd_base=targeted_test_cmd,
            adapter=active_adapter,
            apply_test_patch=True,
            apply_pred_patch=True,
        )
        if run_c["status"] == "failed":
            return _finalize_eval_result(
                outcome=_build_eval_result(
                    status="error",
                    error=run_c["error"],
                    failure_code=run_c.get("failure_code"),
                    reason="run_c_failed",
                    targeted_test_cmd=targeted_test_cmd,
                    test_inputs=test_inputs,
                    log_a=run_a["log"],
                    log_b=run_b["log"],
                    log_c=run_c["log"],
                    run_a=run_a["outcomes"],
                    run_b=run_b["outcomes"],
                    run_c={},
                    run_b_rerun={},
                    run_c_rerun={},
                    run_a_attempts=run_a["attempts"],
                    run_b_attempts=run_b["attempts"],
                    run_c_attempts=run_c["attempts"],
                    run_b_rerun_attempts=[],
                    run_c_rerun_attempts=[],
                    flake_runs=0,
                    FAIL_TO_PASS=[],
                    PASS_TO_PASS=[],
                    resolved=False,
                    test_strategy=test_strategy,
                    environment_strategy=environment_strategy,
                ),
                out_root=out_root,
                instance_id=instance_id,
            )

        # --- Pass D: reruns for flake detection ------------------------------
        run_b_rerun = _run_validation_pass(
            label="b_rerun",
            temp_root=tmp,
            repo_root=repo_root,
            base_commit=base_commit,
            test_patch=test_patch,
            pred_patch="",
            test_files=test_inputs,
            timeout_seconds=timeout_seconds,
            test_cmd_base=targeted_test_cmd,
            adapter=active_adapter,
            apply_test_patch=True,
            apply_pred_patch=False,
        )
        run_c_rerun = _run_validation_pass(
            label="c_rerun",
            temp_root=tmp,
            repo_root=repo_root,
            base_commit=base_commit,
            test_patch=test_patch,
            pred_patch=pred_patch,
            test_files=test_inputs,
            timeout_seconds=timeout_seconds,
            test_cmd_base=targeted_test_cmd,
            adapter=active_adapter,
            apply_test_patch=True,
            apply_pred_patch=True,
        )

        if run_b_rerun["status"] == "failed" or run_c_rerun["status"] == "failed":
            rerun_error = (
                run_b_rerun["error"]
                if run_b_rerun["status"] == "failed"
                else run_c_rerun["error"]
            )
            reason = (
                "run_b_rerun_failed"
                if run_b_rerun["status"] == "failed"
                else "run_c_rerun_failed"
            )
            return _finalize_eval_result(
                outcome=_build_eval_result(
                    status="error",
                    error=rerun_error,
                    failure_code=(
                        run_b_rerun.get("failure_code")
                        if run_b_rerun["status"] == "failed"
                        else run_c_rerun.get("failure_code")
                    ),
                    reason=reason,
                    targeted_test_cmd=targeted_test_cmd,
                    test_inputs=test_inputs,
                    log_a=run_a["log"],
                    log_b=run_b["log"],
                    log_c=run_c["log"],
                    log_b_rerun=run_b_rerun["log"],
                    log_c_rerun=run_c_rerun["log"],
                    run_a=run_a["outcomes"],
                    run_b=run_b["outcomes"],
                    run_c=run_c["outcomes"],
                    run_b_rerun=run_b_rerun["outcomes"],
                    run_c_rerun=run_c_rerun["outcomes"],
                    run_a_attempts=run_a["attempts"],
                    run_b_attempts=run_b["attempts"],
                    run_c_attempts=run_c["attempts"],
                    run_b_rerun_attempts=run_b_rerun["attempts"],
                    run_c_rerun_attempts=run_c_rerun["attempts"],
                    flake_runs=2,
                    FAIL_TO_PASS=[],
                    PASS_TO_PASS=[],
                    resolved=False,
                    test_strategy=test_strategy,
                    environment_strategy=environment_strategy,
                ),
                out_root=out_root,
                instance_id=instance_id,
            )

        if _is_flaky(
            run_b["outcomes"],
            run_b_rerun["outcomes"],
            run_c["outcomes"],
            run_c_rerun["outcomes"],
        ):
            if run_b["outcomes"] != run_b_rerun["outcomes"]:
                reason = "run_b_rerun_mismatch"
            elif run_c["outcomes"] != run_c_rerun["outcomes"]:
                reason = "run_c_rerun_mismatch"
            else:
                reason = "unstable_reruns"
            return _finalize_eval_result(
                outcome=_build_eval_result(
                    status="flaky",
                    error="rerun outcomes changed",
                    failure_code="flaky_outcomes",
                    reason=reason,
                    targeted_test_cmd=targeted_test_cmd,
                    test_inputs=test_inputs,
                    log_a=run_a["log"],
                    log_b=run_b["log"],
                    log_c=run_c["log"],
                    log_b_rerun=run_b_rerun["log"],
                    log_c_rerun=run_c_rerun["log"],
                    run_a=run_a["outcomes"],
                    run_b=run_b["outcomes"],
                    run_c=run_c["outcomes"],
                    run_b_rerun=run_b_rerun["outcomes"],
                    run_c_rerun=run_c_rerun["outcomes"],
                    run_a_attempts=run_a["attempts"],
                    run_b_attempts=run_b["attempts"],
                    run_c_attempts=run_c["attempts"],
                    run_b_rerun_attempts=run_b_rerun["attempts"],
                    run_c_rerun_attempts=run_c_rerun["attempts"],
                    flake_runs=2,
                    FAIL_TO_PASS=[],
                    PASS_TO_PASS=[],
                    resolved=False,
                    test_strategy=test_strategy,
                    environment_strategy=environment_strategy,
                ),
                out_root=out_root,
                instance_id=instance_id,
            )

        ftp, ptp = _derive_test_lists(
            run_b["outcomes"], run_c["outcomes"], declared_ftp, declared_ptp
        )
        resolved = _is_resolved(ftp, run_c["outcomes"], declared_ftp)
        if _has_pass_to_pass_regression(
            run_b["outcomes"], run_c["outcomes"], declared_ptp
        ):
            resolved = False
            rejection_reason = "pass_to_pass_regression"
        elif _declared_ftp_not_resolved(run_c["outcomes"], declared_ftp):
            resolved = False
            rejection_reason = "declared_ftp_not_resolved"
        elif not ftp:
            rejection_reason = "no_fail_to_pass"
        else:
            rejection_reason = None
        status = "resolved" if resolved else "not_resolved"

    return _finalize_eval_result(
        outcome=_build_eval_result(
            status=status,
            error=None,
            failure_code=None,
            reason=rejection_reason,
            targeted_test_cmd=targeted_test_cmd,
            test_inputs=test_inputs,
            log_a=run_a["log"],
            log_b=run_b["log"],
            log_c=run_c["log"],
            log_b_rerun=run_b_rerun["log"],
            log_c_rerun=run_c_rerun["log"],
            run_a=run_a["outcomes"],
            run_b=run_b["outcomes"],
            run_c=run_c["outcomes"],
            run_b_rerun=run_b_rerun["outcomes"],
            run_c_rerun=run_c_rerun["outcomes"],
            run_a_attempts=run_a["attempts"],
            run_b_attempts=run_b["attempts"],
            run_c_attempts=run_c["attempts"],
            run_b_rerun_attempts=run_b_rerun["attempts"],
            run_c_rerun_attempts=run_c_rerun["attempts"],
            flake_runs=2,
            FAIL_TO_PASS=ftp,
            PASS_TO_PASS=ptp,
            resolved=resolved,
            test_strategy=test_strategy,
            environment_strategy=environment_strategy,
        ),
        out_root=out_root,
        instance_id=instance_id,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_eval(
    *,
    dataset_path: Path,
    predictions_path: Path,
    out_root: Path,
    repo_root: Optional[Path] = None,
    timeout_seconds: int = 120,
    adapter_spec: Optional[Dict[str, Any]] = None,
) -> EvalRunSummary:
    """Evaluate predictions against a dataset and write ``validation.jsonl``.

    Args:
        dataset_path:      Path to ``dataset.jsonl``.
        predictions_path:  Path to ``predictions.gold.jsonl`` or custom predictions.
        out_root:          Directory where ``validation.jsonl`` is written.
        repo_root:         Git repo root; inferred from dataset_path if omitted.
        timeout_seconds:   Per-instance test timeout.
        adapter_spec:      Adapter spec dict from ``generate_adapter``; provides
                           ``test_cmd_base`` and other harness settings.

    Returns a summary dict with counts.
    """
    if repo_root is None:
        from repogauge.export.materialize import _normalize_repo_root

        repo_root = _normalize_repo_root(dataset_path)

    environment_strategy = (adapter_spec or {}).get("strategy_name", "default")
    test_cmd_base = (adapter_spec or {}).get("test_cmd_base", "")
    active_adapter = _resolve_adapter(
        language=(
            adapter_spec or {}
        ).get("language")
        if isinstance(adapter_spec, dict)
        else None
    )

    dataset_rows = _read_jsonl(dataset_path)
    pred_rows = _read_jsonl(predictions_path)
    pred_by_id = {r["instance_id"]: r for r in pred_rows}

    out_root.mkdir(parents=True, exist_ok=True)
    validation_path = out_root / "validation.jsonl"
    instance_results_path = out_root / "instance_results.jsonl"

    results: List[Dict[str, Any]] = []
    resolved_count = error_count = skipped_count = 0

    for ds in dataset_rows:
        iid = ds["instance_id"]
        pred = pred_by_id.get(iid)
        if pred is None:
            skipped_count += 1
            results.append(
                {
                    "instance_id": iid,
                    "solver_id": "",
                    "status": "skipped",
                    "error": "no matching prediction",
                    "reason": "missing_prediction",
                    "resolved": False,
                    "targeted_test_cmd": "",
                    "targeted_test_inputs": [],
                    "environment_strategy": environment_strategy,
                    "test_strategy": "full_command",
                    "FAIL_TO_PASS": ds.get("FAIL_TO_PASS", []),
                    "PASS_TO_PASS": ds.get("PASS_TO_PASS", []),
                    "metadata": {},
                }
            )
            continue

        outcome = _eval_instance(
            repo_root=repo_root,
            base_commit=ds["base_commit"],
            pred_patch=pred.get("model_patch", ""),
            test_patch=ds.get("test_patch", ""),
            declared_ftp=ds.get("FAIL_TO_PASS") or [],
            declared_ptp=ds.get("PASS_TO_PASS") or [],
            timeout_seconds=timeout_seconds,
            test_cmd_base=test_cmd_base,
            adapter=active_adapter,
            out_root=out_root,
            instance_id=iid,
            environment_strategy=environment_strategy,
        )

        if outcome["status"] == "error":
            error_count += 1
        elif outcome["resolved"]:
            resolved_count += 1

        results.append(
            {
                "instance_id": iid,
                "solver_id": str(
                    pred.get("solver_id") or pred.get("model_name_or_path") or ""
                ).strip(),
                "status": outcome["status"],
                "reason": outcome["reason"],
                "failure_code": outcome["failure_code"],
                "error": outcome["error"],
                "resolved": outcome["resolved"],
                "environment_strategy": outcome["environment_strategy"],
                "test_strategy": outcome["test_strategy"],
                "targeted_test_cmd": outcome["targeted_test_cmd"],
                "targeted_test_inputs": outcome["targeted_test_inputs"],
                "FAIL_TO_PASS": outcome["FAIL_TO_PASS"],
                "PASS_TO_PASS": outcome["PASS_TO_PASS"],
                "metadata": {
                    "base_commit": ds["base_commit"],
                    "run_a": outcome["run_a"],
                    "run_b": outcome["run_b"],
                    "run_c": outcome["run_c"],
                    "run_b_rerun": outcome["run_b_rerun"],
                    "run_c_rerun": outcome["run_c_rerun"],
                    "run_a_count": len(outcome["run_a"]),
                    "run_b_count": len(outcome["run_b"]),
                    "run_c_count": len(outcome["run_c"]),
                    "run_b_rerun_count": len(outcome["run_b_rerun"]),
                    "run_c_rerun_count": len(outcome["run_c_rerun"]),
                    "flake_runs": outcome["flake_runs"],
                    "run_b_attempts": outcome["run_b_attempts"],
                    "run_c_attempts": outcome["run_c_attempts"],
                    "run_a_attempts": outcome["run_a_attempts"],
                    "run_b_rerun_attempts": outcome["run_b_rerun_attempts"],
                    "run_c_rerun_attempts": outcome["run_c_rerun_attempts"],
                    "log_b": tail(outcome["log_b"]),
                    "log_c": tail(outcome["log_c"]),
                    "log_b_rerun": tail(outcome["log_b_rerun"]),
                    "log_c_rerun": tail(outcome["log_c_rerun"]),
                    "validation_bundle": outcome.get("validation_bundle", {}),
                },
            }
        )

    payload = "".join(json.dumps(r, sort_keys=True) + "\n" for r in results)
    validation_path.write_text(payload, encoding="utf-8")
    instance_results_path.write_text(payload, encoding="utf-8")
    resolved_dataset_path, resolved_predictions_path = _write_resolved_eval_artifacts(
        out_root=out_root,
        dataset_rows=dataset_rows,
        prediction_rows=pred_rows,
        instance_rows=results,
    )

    total = len(dataset_rows)
    return EvalRunSummary(
        validation_path=str(validation_path),
        total=total,
        resolved=resolved_count,
        not_resolved=total - resolved_count - error_count - skipped_count,
        error=error_count,
        skipped=skipped_count,
        resolve_rate=round(resolved_count / total, 3) if total else 0.0,
        instance_results_path=str(instance_results_path),
        dataset_path=str(resolved_dataset_path),
        predictions_path=str(resolved_predictions_path),
    )
