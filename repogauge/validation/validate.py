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

The evaluator writes one ValidationRow per evaluated prediction to
validation.jsonl.

This is a local, harness-free implementation.  It uses git worktrees for
isolation and runs tests through the active language adapter.
Python currently uses the current interpreter and workspace-relative imports
so editable installs are not required.
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import nullcontext
import json
import os
import shlex
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, TextIO, Tuple

from repogauge.exec import CommandResult, run_command
from repogauge.lang import LanguageAdapter, find_adapter
from repogauge.runner.container_exec import (
    WorkspaceContainerSession,
    prepare_local_eval_image,
    run_workspace_command_in_container,
    workspace_container_session,
)
from repogauge.runner.normalize_patch import exclude_patch_paths
from repogauge.runner.progress import CountedProgressReporter
from repogauge.utils.git import CommandPatchError, apply_patch_text, create_checkout
from repogauge.validation.junit_parser import (
    JUnitParseError,
    OUTCOME_PASS,
)
from repogauge.validation.evidence import (
    normalize_failure_reason,
    tail,
    write_validation_bundle,
)
from repogauge.validation.testsel import build_targeted_test_plan, extract_patch_paths
from swebench.harness.constants import DOCKER_WORKDIR


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


def _shared_container_env(adapter_env: Mapping[str, str]) -> Dict[str, str]:
    shared_cache_env = {
        key: value
        for key in ("UV_CACHE_DIR", "PIP_CACHE_DIR")
        if (value := os.environ.get(key))
    }
    if "UV_CACHE_DIR" in shared_cache_env and "PIP_CACHE_DIR" not in shared_cache_env:
        shared_cache_env["PIP_CACHE_DIR"] = str(
            Path(shared_cache_env["UV_CACHE_DIR"]).with_name("codex-pip-cache")
        )
    return {**shared_cache_env, **adapter_env}


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


def _emit_eval_progress(
    progress_stream: TextIO | None,
    message: str,
) -> None:
    if progress_stream is None:
        return
    print(f"repogauge eval: {message}", file=progress_stream, flush=True)


def _immutable_paths(ds: Mapping[str, Any], test_patch: str) -> List[str]:
    raw = ds.get("immutable_paths")
    if isinstance(raw, list):
        return [str(value).strip() for value in raw if str(value).strip()]
    return extract_patch_paths(test_patch)


def _result_row_from_outcome(
    *,
    ds: Mapping[str, Any],
    pred: Mapping[str, Any] | None,
    outcome: Mapping[str, Any] | None,
    environment_strategy: str,
) -> Dict[str, Any]:
    iid = str(ds["instance_id"])
    if pred is None:
        return {
            "instance_id": iid,
            "solver_id": "",
            "status": "skipped",
            "harness_outcome": "skipped",
            "error": "no matching prediction",
            "reason": "missing_prediction",
            "failure_reason": "missing_prediction",
            "resolved": False,
            "targeted_test_cmd": "",
            "targeted_test_inputs": [],
            "environment_strategy": environment_strategy,
            "test_strategy": "full_command",
            "FAIL_TO_PASS": ds.get("FAIL_TO_PASS", []),
            "PASS_TO_PASS": ds.get("PASS_TO_PASS", []),
            "metadata": {},
        }

    assert outcome is not None
    return {
        "instance_id": iid,
        "solver_id": str(
            pred.get("solver_id") or pred.get("model_name_or_path") or ""
        ).strip(),
        "status": outcome["status"],
        "harness_outcome": outcome["status"],
        "reason": outcome["reason"],
        "failure_reason": outcome["reason"],
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
            "withheld_test_paths": outcome.get("withheld_test_paths", []),
            "withheld_test_paths_touched": outcome.get(
                "withheld_test_paths_touched", []
            ),
            "withheld_test_patch_sanitized": outcome.get(
                "withheld_test_patch_sanitized", False
            ),
        },
    }


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


def _pytest_file_fallback_inputs(test_inputs: List[str]) -> List[str]:
    fallback_inputs: List[str] = []
    for value in test_inputs:
        candidate = str(value).strip()
        if not candidate:
            continue
        file_path = candidate.split("::", 1)[0]
        if file_path and file_path not in fallback_inputs:
            fallback_inputs.append(file_path)
    return fallback_inputs


def _should_retry_pytest_with_file_inputs(
    *,
    adapter: LanguageAdapter,
    test_inputs: List[str],
    result: CommandResult,
) -> bool:
    if adapter.name() != "python":
        return False
    if not test_inputs:
        return False
    if not any("::" in str(value) for value in test_inputs):
        return False
    if result.returncode != 4:
        return False
    stderr = str(result.stderr or "")
    stderr_lower = stderr.lower()
    return (
        "found no collectors for" in stderr_lower
        or "not found:" in stderr_lower
        or "no match in any of" in stderr_lower
    )


def _strip_command_flags(parts: List[str], flags: tuple[str, ...]) -> List[str]:
    stripped: List[str] = []
    skip_next = False
    for part in parts:
        if skip_next:
            skip_next = False
            continue
        matched = False
        for flag in flags:
            if part == flag:
                skip_next = True
                matched = True
                break
            if part.startswith(f"{flag}="):
                matched = True
                break
        if matched:
            continue
        stripped.append(part)
    return stripped


def _pytest_file_inputs(test_inputs: List[str]) -> List[str]:
    file_inputs: List[str] = []
    for value in test_inputs:
        candidate = str(value).strip()
        if not candidate:
            continue
        if "::" in candidate:
            candidate = candidate.split("::", 1)[0]
        if candidate not in file_inputs:
            file_inputs.append(candidate)
    return file_inputs


def _parse_pytest_collected_nodes(output: str) -> List[str]:
    nodes: List[str] = []
    for raw_line in output.splitlines():
        candidate = raw_line.strip()
        if not candidate:
            continue
        if candidate.startswith(("=", "ERROR:", "no tests collected")):
            continue
        if "::" not in candidate:
            continue
        if candidate not in nodes:
            nodes.append(candidate)
    return nodes


def _run_pytest_collection(
    worktree: Path,
    *,
    test_inputs: List[str],
    timeout_seconds: int,
    test_cmd_base: str,
    adapter: LanguageAdapter,
    attempt_id_prefix: str,
    container_host: str | None,
    artifacts_root: Path,
    adapter_spec: Mapping[str, Any] | None,
    instance_row: Mapping[str, Any] | None,
    environment_strategy: str,
    workspace_session: WorkspaceContainerSession | None = None,
) -> Tuple[List[str], Dict[str, Any], str]:
    adapter_env = adapter.env_overrides(worktree)
    env = (
        _shared_container_env(adapter_env)
        if container_host is not None
        else {**os.environ, **adapter_env}
    )
    command_attempts = _test_command_attempts(test_cmd_base, adapter=adapter)
    if container_host is not None:
        command_attempts = _container_safe_command_attempts(
            command_attempts, adapter=adapter
        )
    base_cmd = (
        list(command_attempts[0]) if command_attempts else shlex.split(test_cmd_base)
    )
    collect_cmd = _strip_command_flags(base_cmd, _JUNIT_XML_FLAGS)
    if "--collect-only" not in collect_cmd:
        collect_cmd.append("--collect-only")
    if "-q" not in collect_cmd:
        collect_cmd.append("-q")
    if "--tb=no" not in collect_cmd:
        collect_cmd.append("--tb=no")
    collect_cmd.extend(test_inputs)

    effective_adapter_spec = (
        dict(adapter_spec)
        if isinstance(adapter_spec, Mapping)
        else _default_container_adapter_spec(
            dataset_row=instance_row,
            adapter=adapter,
            test_cmd_base=test_cmd_base,
            environment_strategy=environment_strategy,
        )
    )

    if workspace_session is not None:
        result = workspace_session.run(
            command=collect_cmd,
            timeout_seconds=timeout_seconds,
            artifacts_root=artifacts_root,
        )
    elif container_host is not None:
        result = run_workspace_command_in_container(
            attempt_id=f"{attempt_id_prefix}-collect",
            workspace_path=worktree,
            command=collect_cmd,
            timeout_seconds=timeout_seconds,
            container_host=container_host,
            artifacts_root=artifacts_root,
            environment=env,
            adapter_spec=effective_adapter_spec,
            instance_row=instance_row,
        )
    else:
        result = run_command(
            collect_cmd,
            cwd=str(worktree),
            env=env,
            timeout_seconds=timeout_seconds,
        )

    raw = f"[stdout]\n{result.stdout}\n[stderr]\n{result.stderr}"
    collected_nodes = _parse_pytest_collected_nodes(result.stdout)
    status = "collection_empty"
    if collected_nodes:
        status = "collection_resolved"
    elif result.timed_out:
        status = "collection_timeout"
    elif result.returncode != 0:
        status = "collection_fallback"

    attempt_entry: Dict[str, Any] = {
        "attempt": 0,
        "command": collect_cmd,
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "elapsed_ms": result.elapsed_ms,
        "status": status,
        "tests_run": len(collected_nodes),
    }
    if collected_nodes:
        attempt_entry["resolved_test_inputs"] = collected_nodes
        return collected_nodes, attempt_entry, raw

    fallback_inputs = [
        value
        for value in _pytest_file_inputs(test_inputs)
        if (worktree / value).exists()
    ]
    if fallback_inputs:
        attempt_entry["fallback_test_inputs"] = fallback_inputs
        return fallback_inputs, attempt_entry, raw

    return [], attempt_entry, raw


def _container_safe_command_attempts(
    attempts: List[List[str]], *, adapter: LanguageAdapter
) -> List[List[str]]:
    if adapter.name() != "python":
        return attempts

    normalized: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for attempt in attempts:
        if not attempt:
            continue
        command = list(attempt)
        executable = Path(command[0]).name.lower()
        if executable in {"pytest", "pytest.exe"}:
            command[0] = "pytest"
        elif executable.startswith("python"):
            command[0] = "python"
        key = tuple(command)
        if key in seen:
            continue
        normalized.append(command)
        seen.add(key)
    return normalized or attempts


def _container_visible_report_path(
    worktree: Path, report_path: Path
) -> tuple[Path, Path]:
    host_path = worktree / report_path.name
    container_path = Path(DOCKER_WORKDIR) / host_path.relative_to(worktree)
    return host_path, container_path


def _default_container_adapter_spec(
    *,
    dataset_row: Mapping[str, Any] | None,
    adapter: LanguageAdapter,
    test_cmd_base: str,
    environment_strategy: str,
) -> Dict[str, Any]:
    row = dict(dataset_row or {})
    language = adapter.name()
    return {
        "repo": str(row.get("repo", "")).strip(),
        "version": str(row.get("version", "0.0.0")).strip() or "0.0.0",
        "language": language,
        "runtime_version": "",
        "python_version": "3.11" if language == "python" else "",
        "pre_install": [],
        "install": [],
        "build": [],
        "test_cmd_base": test_cmd_base,
        "strategy_name": environment_strategy,
        "docker_specs": {},
    }


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
    attempt_id_prefix: str = "eval",
    container_host: str | None = None,
    adapter_spec: Mapping[str, Any] | None = None,
    instance_row: Mapping[str, Any] | None = None,
    environment_strategy: str = "default",
    workspace_session: WorkspaceContainerSession | None = None,
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
    adapter_env = active_adapter.env_overrides(worktree)
    env = (
        _shared_container_env(adapter_env)
        if container_host is not None
        else {**os.environ, **adapter_env}
    )
    attempts: List[Dict[str, Any]] = []
    raw = ""
    last_parse_error: str | None = None
    containerized = container_host is not None
    command_attempts = _test_command_attempts(test_cmd_base, adapter=active_adapter)
    if containerized:
        command_attempts = _container_safe_command_attempts(
            command_attempts, adapter=active_adapter
        )
    effective_adapter_spec = (
        dict(adapter_spec)
        if isinstance(adapter_spec, Mapping)
        else _default_container_adapter_spec(
            dataset_row=instance_row,
            adapter=active_adapter,
            test_cmd_base=test_cmd_base,
            environment_strategy=environment_strategy,
        )
    )

    attempt_number = 0
    for base_cmd in command_attempts:
        pending_test_inputs: List[List[str]] = [list(test_files)]
        while pending_test_inputs:
            current_test_inputs = pending_test_inputs.pop(0)
            attempt_number += 1
            test_cmd = list(base_cmd)
            report_input: object = test_report_path
            if active_adapter.name() == "python":
                if containerized:
                    host_report_path, container_report_path = (
                        _container_visible_report_path(worktree, test_report_path)
                    )
                    test_cmd, _ = _normalize_junit_output_flag(
                        test_cmd, container_report_path
                    )
                    report_input = host_report_path
                else:
                    test_cmd, report_path_to_read = _normalize_junit_output_flag(
                        test_cmd, test_report_path
                    )
                    report_input = report_path_to_read
            else:
                report_glob_getter = getattr(active_adapter, "test_report_glob", None)
                report_filename_getter = getattr(
                    active_adapter, "test_report_filename", None
                )
                report_glob = (
                    report_glob_getter() if callable(report_glob_getter) else None
                )
                report_filename = (
                    report_filename_getter()
                    if callable(report_filename_getter)
                    else None
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
            if current_test_inputs:
                cmd += current_test_inputs
            if workspace_session is not None:
                result = workspace_session.run(
                    command=cmd,
                    timeout_seconds=timeout_seconds,
                    artifacts_root=test_report_path.parent,
                )
            elif containerized:
                result = run_workspace_command_in_container(
                    attempt_id=f"{attempt_id_prefix}-attempt-{attempt_number}",
                    workspace_path=worktree,
                    command=cmd,
                    timeout_seconds=timeout_seconds,
                    container_host=container_host,
                    artifacts_root=test_report_path.parent,
                    environment=env,
                    adapter_spec=effective_adapter_spec,
                    instance_row=instance_row,
                )
            else:
                result = run_command(
                    cmd, cwd=str(worktree), env=env, timeout_seconds=timeout_seconds
                )
            raw = f"[stdout]\n{result.stdout}\n[stderr]\n{result.stderr}"

            attempt_entry: Dict[str, Any] = {
                "attempt": attempt_number,
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

            if _should_retry_pytest_with_file_inputs(
                adapter=active_adapter,
                test_inputs=current_test_inputs,
                result=result,
            ):
                fallback_inputs = _pytest_file_fallback_inputs(current_test_inputs)
                if fallback_inputs and fallback_inputs != current_test_inputs:
                    attempt_entry["status"] = "collector_fallback"
                    attempt_entry["tests_run"] = len(outcomes)
                    attempt_entry["fallback_test_inputs"] = fallback_inputs
                    attempts.append(attempt_entry)
                    pending_test_inputs.append(fallback_inputs)
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


def _path_exists_at_ref(worktree: Path, ref: str, rel_path: str) -> bool:
    result = run_command(
        ["git", "-C", str(worktree), "cat-file", "-e", f"{ref}:{rel_path}"]
    )
    return result.success


def _reset_checkout_for_pass(
    *,
    worktree: Path,
    base_commit: str,
    cleanup_paths: List[str],
) -> None:
    reset_result = run_command(
        ["git", "-C", str(worktree), "reset", "--hard", base_commit]
    )
    if not reset_result.success:
        raise RuntimeError(
            f"failed to reset checkout to {base_commit}: "
            f"{reset_result.stderr.strip() or reset_result.stdout}"
        )

    clean_result = run_command(["git", "-C", str(worktree), "clean", "-fdx"])
    if not clean_result.success:
        raise RuntimeError(
            f"failed to clean checkout at {base_commit}: "
            f"{clean_result.stderr.strip() or clean_result.stdout}"
        )

    for rel_path in cleanup_paths:
        if _path_exists_at_ref(worktree, base_commit, rel_path):
            continue
        target = worktree / rel_path
        if target.is_dir():
            try:
                target.rmdir()
            except OSError:
                pass
            continue
        if target.exists() or target.is_symlink():
            try:
                target.unlink()
            except OSError:
                pass


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
    instance_row: Mapping[str, Any] | None = None,
    adapter_spec: Mapping[str, Any] | None = None,
    container_host: str | None = None,
    environment_strategy: str = "default",
    apply_test_patch: bool = False,
    apply_pred_patch: bool = False,
    checkout_path: Path | None = None,
    workspace_session: WorkspaceContainerSession | None = None,
    cleanup_paths: List[str] | None = None,
) -> Dict[str, Any]:
    """Execute one isolated validation run and return outcomes + telemetry."""
    wt = None
    worktree = checkout_path
    outcomes: Dict[str, str] = {}
    log = ""
    attempts: List[Dict[str, Any]] = []
    collector_log = ""
    failure_code: str | None = None
    active_adapter = _resolve_adapter(adapter)

    try:
        if worktree is None:
            wt = create_checkout(repo_root, ref=base_commit)
            worktree = wt.path
        else:
            _reset_checkout_for_pass(
                worktree=worktree,
                base_commit=base_commit,
                cleanup_paths=list(cleanup_paths or []),
            )
        if test_patch.strip():
            apply_patch_text(worktree, test_patch)
        if pred_patch.strip():
            apply_patch_text(worktree, pred_patch)

        effective_test_inputs = list(test_files)
        target_cmd_tokens = test_cmd_base.split()
        is_pytest = active_adapter.name() == "python" and (
            "pytest" in target_cmd_tokens
            or (
                target_cmd_tokens[:2] == ["python", "-m"]
                and len(target_cmd_tokens) > 2
                and target_cmd_tokens[2] == "pytest"
            )
        )
        if is_pytest:
            collection_inputs = _pytest_file_inputs(effective_test_inputs)
            collect_from_files = bool(collection_inputs) and all(
                value.endswith(".py") for value in collection_inputs
            )
            if collect_from_files:
                existing_collection_inputs = [
                    value for value in collection_inputs if (worktree / value).exists()
                ]
            else:
                existing_collection_inputs = []
            if collect_from_files and not existing_collection_inputs:
                collector_log = (
                    "[stdout]\n\n[stderr]\n"
                    "pytest collection skipped: targeted files are absent in this checkout\n"
                )
                attempts.append(
                    {
                        "attempt": 0,
                        "command": [],
                        "returncode": 0,
                        "timed_out": False,
                        "elapsed_ms": 0,
                        "status": "collection_missing",
                        "fallback_test_inputs": collection_inputs,
                        "tests_run": 0,
                    }
                )
                return {
                    "status": "passed",
                    "error": None,
                    "outcomes": {},
                    "log": collector_log,
                    "attempts": attempts,
                }
            if existing_collection_inputs:
                (
                    effective_test_inputs,
                    collect_attempt,
                    collector_log,
                ) = _run_pytest_collection(
                    worktree,
                    test_inputs=existing_collection_inputs,
                    timeout_seconds=timeout_seconds,
                    test_cmd_base=test_cmd_base,
                    adapter=active_adapter,
                    attempt_id_prefix=(
                        f"eval-{str((instance_row or {}).get('instance_id', 'instance')).strip() or 'instance'}-{label}"
                    ),
                    container_host=container_host,
                    artifacts_root=temp_root,
                    adapter_spec=adapter_spec,
                    instance_row=instance_row,
                    environment_strategy=environment_strategy,
                    workspace_session=workspace_session,
                )
                attempts.append(collect_attempt)
                if not effective_test_inputs:
                    return {
                        "status": "passed",
                        "error": None,
                        "outcomes": {},
                        "log": collector_log,
                        "attempts": attempts,
                    }

        test_report_path = _test_report_path(temp_root, label, active_adapter)
        outcomes, test_log, test_attempts = _run_test(
            worktree,
            test_files=effective_test_inputs,
            test_report_path=test_report_path,
            timeout_seconds=timeout_seconds,
            test_cmd_base=test_cmd_base,
            adapter=active_adapter,
            test_spec=effective_test_inputs or None,
            attempt_id_prefix=(
                f"eval-{str((instance_row or {}).get('instance_id', 'instance')).strip() or 'instance'}-{label}"
            ),
            container_host=container_host,
            adapter_spec=adapter_spec,
            instance_row=instance_row,
            environment_strategy=environment_strategy,
            workspace_session=workspace_session,
        )
        attempts.extend(test_attempts)
        log = test_log if not collector_log else f"{collector_log}\n{test_log}"
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
            attempts.extend(exc.attempts)
        else:
            failure_code = "unknown_validator_failure"
        return {
            "status": "failed",
            "error": f"{label} failed: {exc}",
            "outcomes": {},
            "log": log if not collector_log else f"{collector_log}\n{log}",
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
    withheld_test_paths: List[str] | None = None,
    withheld_test_paths_touched: List[str] | None = None,
    withheld_test_patch_sanitized: bool = False,
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
        "withheld_test_paths": list(withheld_test_paths or []),
        "withheld_test_paths_touched": list(withheld_test_paths_touched or []),
        "withheld_test_patch_sanitized": withheld_test_patch_sanitized,
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
    instance_row: Mapping[str, Any] | None = None,
    adapter_spec: Mapping[str, Any] | None = None,
    container_host: str | None = None,
    out_root: Path | None = None,
    instance_id: str | None = None,
    environment_strategy: str = "default",
) -> Dict[str, Any]:
    """Run validation passes for one instance. Returns a result dict."""
    active_adapter = _resolve_adapter(adapter)
    targeted_test_cmd, targeted_test_inputs = build_targeted_test_plan(
        test_cmd_base, test_patch
    )
    row_for_paths = instance_row if isinstance(instance_row, Mapping) else {}
    withheld_test_paths = _immutable_paths(row_for_paths, test_patch)
    sanitized_pred_patch, _excluded_patch, withheld_test_paths_touched = (
        exclude_patch_paths(pred_patch, withheld_test_paths)
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
        checkout_handle = create_checkout(
            repo_root,
            ref=base_commit,
            checkout_path=tmp / "checkout",
        )
        checkout_path = checkout_handle.path
        cleanup_paths = sorted(
            set(extract_patch_paths(test_patch))
            | set(extract_patch_paths(sanitized_pred_patch))
        )
        effective_adapter_spec = (
            dict(adapter_spec)
            if isinstance(adapter_spec, Mapping)
            else _default_container_adapter_spec(
                dataset_row=instance_row,
                adapter=active_adapter,
                test_cmd_base=targeted_test_cmd,
                environment_strategy=environment_strategy,
            )
        )
        session_environment = _shared_container_env(
            active_adapter.env_overrides(checkout_path)
        )
        instance_key = (
            str(instance_id).strip()
            or str((instance_row or {}).get("instance_id", "")).strip()
            or "instance"
        )
        session_cm = (
            workspace_container_session(
                attempt_id=f"eval-{instance_key}-session",
                workspace_path=checkout_path,
                timeout_seconds=timeout_seconds,
                container_host=container_host,
                artifacts_root=tmp / "_workspace_session",
                environment=session_environment,
                adapter_spec=effective_adapter_spec,
                instance_row=instance_row,
            )
            if container_host is not None
            else nullcontext(None)
        )
        try:
            with session_cm as workspace_session:
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
                    instance_row=instance_row,
                    adapter_spec=adapter_spec,
                    container_host=container_host,
                    environment_strategy=environment_strategy,
                    apply_test_patch=False,
                    apply_pred_patch=False,
                    checkout_path=checkout_path,
                    workspace_session=workspace_session,
                    cleanup_paths=cleanup_paths,
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
                            withheld_test_paths=withheld_test_paths,
                            withheld_test_paths_touched=list(
                                withheld_test_paths_touched
                            ),
                            withheld_test_patch_sanitized=bool(
                                withheld_test_paths_touched
                            ),
                        ),
                        out_root=out_root,
                        instance_id=instance_id,
                    )

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
                    instance_row=instance_row,
                    adapter_spec=adapter_spec,
                    container_host=container_host,
                    environment_strategy=environment_strategy,
                    apply_test_patch=True,
                    apply_pred_patch=False,
                    checkout_path=checkout_path,
                    workspace_session=workspace_session,
                    cleanup_paths=cleanup_paths,
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
                            withheld_test_paths=withheld_test_paths,
                            withheld_test_paths_touched=list(
                                withheld_test_paths_touched
                            ),
                            withheld_test_patch_sanitized=bool(
                                withheld_test_paths_touched
                            ),
                        ),
                        out_root=out_root,
                        instance_id=instance_id,
                    )

                run_c = _run_validation_pass(
                    label="c",
                    temp_root=tmp,
                    repo_root=repo_root,
                    base_commit=base_commit,
                    test_patch=test_patch,
                    pred_patch=sanitized_pred_patch,
                    test_files=test_inputs,
                    timeout_seconds=timeout_seconds,
                    test_cmd_base=targeted_test_cmd,
                    adapter=active_adapter,
                    instance_row=instance_row,
                    adapter_spec=adapter_spec,
                    container_host=container_host,
                    environment_strategy=environment_strategy,
                    apply_test_patch=True,
                    apply_pred_patch=True,
                    checkout_path=checkout_path,
                    workspace_session=workspace_session,
                    cleanup_paths=cleanup_paths,
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
                            withheld_test_paths=withheld_test_paths,
                            withheld_test_paths_touched=list(
                                withheld_test_paths_touched
                            ),
                            withheld_test_patch_sanitized=bool(
                                withheld_test_paths_touched
                            ),
                        ),
                        out_root=out_root,
                        instance_id=instance_id,
                    )

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
                    instance_row=instance_row,
                    adapter_spec=adapter_spec,
                    container_host=container_host,
                    environment_strategy=environment_strategy,
                    apply_test_patch=True,
                    apply_pred_patch=False,
                    checkout_path=checkout_path,
                    workspace_session=workspace_session,
                    cleanup_paths=cleanup_paths,
                )
                run_c_rerun = _run_validation_pass(
                    label="c_rerun",
                    temp_root=tmp,
                    repo_root=repo_root,
                    base_commit=base_commit,
                    test_patch=test_patch,
                    pred_patch=sanitized_pred_patch,
                    test_files=test_inputs,
                    timeout_seconds=timeout_seconds,
                    test_cmd_base=targeted_test_cmd,
                    adapter=active_adapter,
                    instance_row=instance_row,
                    adapter_spec=adapter_spec,
                    container_host=container_host,
                    environment_strategy=environment_strategy,
                    apply_test_patch=True,
                    apply_pred_patch=True,
                    checkout_path=checkout_path,
                    workspace_session=workspace_session,
                    cleanup_paths=cleanup_paths,
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
                            withheld_test_paths=withheld_test_paths,
                            withheld_test_paths_touched=list(
                                withheld_test_paths_touched
                            ),
                            withheld_test_patch_sanitized=bool(
                                withheld_test_paths_touched
                            ),
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
                            withheld_test_paths=withheld_test_paths,
                            withheld_test_paths_touched=list(
                                withheld_test_paths_touched
                            ),
                            withheld_test_patch_sanitized=bool(
                                withheld_test_paths_touched
                            ),
                        ),
                        out_root=out_root,
                        instance_id=instance_id,
                    )

                ftp, ptp = _derive_test_lists(
                    run_b["outcomes"],
                    run_c["outcomes"],
                    declared_ftp,
                    declared_ptp,
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
        finally:
            checkout_handle.remove()

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
            withheld_test_paths=withheld_test_paths,
            withheld_test_paths_touched=list(withheld_test_paths_touched),
            withheld_test_patch_sanitized=bool(withheld_test_paths_touched),
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
    container_host: str | None = None,
    progress_stream: TextIO | None = None,
    jobs: int = 4,
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
        language=(adapter_spec or {}).get("language")
        if isinstance(adapter_spec, dict)
        else None
    )

    dataset_rows = _read_jsonl(dataset_path)
    pred_rows = _read_jsonl(predictions_path)
    dataset_by_id = {
        str(row.get("instance_id", "")).strip(): row
        for row in dataset_rows
        if str(row.get("instance_id", "")).strip()
    }

    eval_items: List[tuple[Dict[str, Any], Dict[str, Any] | None]] = []
    covered_instance_ids: set[str] = set()
    for pred in pred_rows:
        instance_id = str(pred.get("instance_id", "")).strip()
        dataset_row = dataset_by_id.get(instance_id)
        if dataset_row is None:
            continue
        eval_items.append((dataset_row, pred))
        covered_instance_ids.add(instance_id)
    for dataset_row in dataset_rows:
        instance_id = str(dataset_row.get("instance_id", "")).strip()
        if instance_id in covered_instance_ids:
            continue
        eval_items.append((dataset_row, None))

    total = len(eval_items)

    out_root.mkdir(parents=True, exist_ok=True)
    validation_path = out_root / "validation.jsonl"
    instance_results_path = out_root / "instance_results.jsonl"

    results_by_index: Dict[int, Dict[str, Any]] = {}
    resolved_count = error_count = skipped_count = 0
    started_at = time.monotonic()
    execution_mode = (
        "via containers" if container_host is not None else "on host worktrees"
    )
    progress = CountedProgressReporter(
        prefix="repogauge eval",
        total=total,
        noun="evaluating instances",
        stream=progress_stream,
    )
    try:
        progress.start(f"evaluating {total} instances locally {execution_mode}")
        if container_host is not None and adapter_spec is not None and dataset_rows:
            progress.start("preparing reusable container image layers")
            prepare_local_eval_image(
                attempt_id="eval-image-prewarm",
                attempt_root=out_root / "_image_prep",
                instance_row=dataset_rows[0],
                adapter_spec=adapter_spec,
                container_host=container_host,
            )
            progress.start("reused or built local evaluation image layers")

        def _evaluate(ds: Dict[str, Any], pred: Dict[str, Any]) -> Dict[str, Any]:
            return _eval_instance(
                repo_root=repo_root,
                base_commit=ds["base_commit"],
                pred_patch=pred.get("model_patch", ""),
                test_patch=ds.get("test_patch", ""),
                declared_ftp=ds.get("FAIL_TO_PASS") or [],
                declared_ptp=ds.get("PASS_TO_PASS") or [],
                timeout_seconds=timeout_seconds,
                test_cmd_base=test_cmd_base,
                adapter=active_adapter,
                instance_row=ds,
                adapter_spec=adapter_spec,
                container_host=container_host,
                out_root=out_root,
                instance_id=str(ds["instance_id"]),
                environment_strategy=environment_strategy,
            )

        future_map: Dict[Any, tuple[int, Dict[str, Any], Dict[str, Any]]] = {}
        max_workers = max(1, jobs)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            pending_items = iter(enumerate(eval_items, start=1))

            def _submit_next() -> bool:
                nonlocal skipped_count
                for index, (ds, pred) in pending_items:
                    iid = str(ds["instance_id"])
                    if pred is None:
                        skipped_count += 1
                        results_by_index[index] = _result_row_from_outcome(
                            ds=ds,
                            pred=None,
                            outcome=None,
                            environment_strategy=environment_strategy,
                        )
                        elapsed_s = time.monotonic() - started_at
                        progress.advance(
                            status="skipped",
                            message=(
                                f"[{index}/{total}] {iid} skipped (missing prediction) "
                                f"| resolved={resolved_count} errors={error_count} "
                                f"skipped={skipped_count} elapsed={elapsed_s:.1f}s"
                            ),
                        )
                        continue
                    progress.start(f"starting [{index}/{total}] {iid}")
                    future = pool.submit(_evaluate, ds, pred)
                    future_map[future] = (index, ds, pred)
                    return True
                return False

            while len(future_map) < max_workers and _submit_next():
                pass

            while future_map:
                done, _pending = wait(
                    set(future_map),
                    return_when=FIRST_COMPLETED,
                )
                for future in done:
                    index, ds, pred = future_map.pop(future)
                    iid = str(ds["instance_id"])
                    outcome = future.result()
                    if outcome["status"] == "error":
                        error_count += 1
                    elif outcome["resolved"]:
                        resolved_count += 1
                    results_by_index[index] = _result_row_from_outcome(
                        ds=ds,
                        pred=pred,
                        outcome=outcome,
                        environment_strategy=environment_strategy,
                    )
                    elapsed_s = time.monotonic() - started_at
                    progress.advance(
                        status=str(outcome["status"]),
                        message=(
                            f"[{index}/{total}] {iid} {outcome['status']} "
                            f"| resolved={resolved_count} errors={error_count} "
                            f"skipped={skipped_count} elapsed={elapsed_s:.1f}s"
                        ),
                    )

                while len(future_map) < max_workers and _submit_next():
                    pass
    finally:
        progress.close(
            summary=(
                f"finished local eval: resolved={resolved_count} "
                f"errors={error_count} skipped={skipped_count} total={total}"
            )
        )

    results = [results_by_index[index] for index in sorted(results_by_index)]

    payload = "".join(json.dumps(r, sort_keys=True) + "\n" for r in results)
    validation_path.write_text(payload, encoding="utf-8")
    instance_results_path.write_text(payload, encoding="utf-8")
    resolved_dataset_path, resolved_predictions_path = _write_resolved_eval_artifacts(
        out_root=out_root,
        dataset_rows=dataset_rows,
        prediction_rows=pred_rows,
        instance_rows=results,
    )

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
