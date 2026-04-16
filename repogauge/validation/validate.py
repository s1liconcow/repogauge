"""Local eval pipeline for SWE-bench-style dataset instances (bead j7y).

For each dataset instance the evaluator runs two passes:

  Pass B  base_commit + test_patch                  (establishes which tests fail)
  Pass C  base_commit + test_patch + pred_patch      (establishes which tests the fix resolves)

From those two runs:
  FAIL_TO_PASS  = tests that were fail/error in B and pass in C
  PASS_TO_PASS  = tests that passed in both B and C
  resolved      = all of FAIL_TO_PASS pass in C (or, if FAIL_TO_PASS is empty, any test passes)

The evaluator writes one ValidationRow per instance to validation.jsonl.

This is a local, harness-free implementation.  It uses git worktrees for
isolation and runs pytest directly via the current Python interpreter.
``PYTHONPATH`` is set to the worktree root so editable installs are not required.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from repogauge.exec import run_command
from repogauge.utils.git import apply_patch_text, create_worktree
from repogauge.validation.junit_parser import (
    JUnitParseError,
    OUTCOME_PASS,
    parse_junit_xml,
)
from repogauge.validation.testsel import build_targeted_test_plan


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _resolve_test_cmd(test_cmd_base: str) -> List[str]:
    """Split *test_cmd_base* into argv, replacing bare 'python' tokens with sys.executable."""
    parts = (
        shlex.split(test_cmd_base)
        if test_cmd_base.strip()
        else ["python", "-m", "pytest"]
    )
    if parts and re.match(r"^python3?(\.\d+)?$", parts[0]):
        parts[0] = sys.executable
    return parts


class PytestExecutionError(RuntimeError):
    """Raised when deterministic pytest attempts fail to produce parseable output."""

    def __init__(self, message: str, attempts: List[Dict[str, Any]]) -> None:
        super().__init__(message)
        self.attempts = attempts


def _pytest_command_attempts(test_cmd_base: str) -> List[List[str]]:
    """Return deterministic command attempts for a pytest-style base command."""
    primary = _resolve_test_cmd(test_cmd_base)
    attempts = [primary]

    if not primary:
        return attempts

    # A common recovery path when `pytest` is not on PATH: call it through
    # the current interpreter as ``python -m pytest``.
    if primary[0] == "pytest":
        corrected = [sys.executable, "-m", "pytest"] + primary[1:]
        if corrected not in attempts:
            attempts.append(corrected)

    return attempts


def _run_pytest(
    worktree: Path,
    *,
    test_files: List[str],
    junit_xml: Path,
    timeout_seconds: int = 120,
    test_cmd_base: str = "python -m pytest",
) -> Tuple[Dict[str, str], str, List[Dict[str, Any]]]:
    """Run pytest in *worktree* with deterministic command retries.

    Returns:
        - ``results_dict`` maps test_id -> outcome string
        - ``raw_output`` from the final attempt
        - ``attempts`` persisted attempt metadata

    ``results_dict`` is empty if XML parsing fails for every deterministic attempt.
    ``raw_output`` is the combined stdout+stderr for log purposes.
    ``test_cmd_base`` is taken from the adapter spec when available.
    """
    env = {**os.environ, "PYTHONPATH": str(worktree)}
    attempts: List[Dict[str, Any]] = []
    junit_flag = f"--junit-xml={junit_xml}"
    raw = ""
    last_parse_error: str | None = None

    for index, base_cmd in enumerate(_pytest_command_attempts(test_cmd_base)):
        if junit_xml.exists():
            try:
                junit_xml.unlink()
            except OSError:
                pass

        cmd = (
            base_cmd
            + ["--tb=no", "-q", junit_flag]
            + (test_files if test_files else [])
        )
        result = run_command(
            cmd, cwd=str(worktree), env=env, timeout_seconds=timeout_seconds
        )
        raw = f"[stdout]\n{result.stdout}\n[stderr]\n{result.stderr}"

        attempt_entry: Dict[str, Any] = {
            "attempt": index + 1,
            "command": cmd,
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "status": "unknown",
        }

        try:
            outcomes = parse_junit_xml(junit_xml)
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
        f"pytest execution produced no parseable output for {test_files}",
        attempts,
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
    test_cmd_base: str = "python -m pytest",
) -> Dict[str, Any]:
    """Run two-pass evaluation for one instance.  Returns a result dict."""
    targeted_test_cmd, targeted_test_inputs = build_targeted_test_plan(
        test_cmd_base, test_patch
    )
    test_inputs = targeted_test_inputs

    with tempfile.TemporaryDirectory(prefix="repogauge-eval-") as tmpdir:
        tmp = Path(tmpdir)

        # --- Pass B: base + test_patch ----------------------------------------
        wt_b = None
        run_b: Dict[str, str] = {}
        log_b = ""
        attempts_b: List[Dict[str, Any]] = []
        try:
            wt_b = create_worktree(repo_root, ref=base_commit)
            if test_patch.strip():
                apply_patch_text(wt_b.path, test_patch)
            xml_b = tmp / "junit_b.xml"
            run_b, log_b, attempts_b = _run_pytest(
                wt_b.path,
                test_files=test_inputs,
                junit_xml=xml_b,
                timeout_seconds=timeout_seconds,
                test_cmd_base=test_cmd_base,
            )
        except Exception as exc:
            pass_b_attempts = (
                exc.attempts if isinstance(exc, PytestExecutionError) else []
            )
            return {
                "status": "error",
                "error": f"pass_b failed: {exc}",
                "targeted_test_cmd": targeted_test_cmd,
                "targeted_test_inputs": test_inputs,
                "log_b": log_b,
                "log_c": "",
                "run_b": {},
                "run_c": {},
                "run_b_attempts": pass_b_attempts,
                "run_c_attempts": [],
                "FAIL_TO_PASS": [],
                "PASS_TO_PASS": [],
                "resolved": False,
            }
        finally:
            if wt_b is not None:
                try:
                    wt_b.remove()
                except Exception:
                    pass

        # --- Pass C: base + test_patch + pred_patch ----------------------------
        wt_c = None
        run_c: Dict[str, str] = {}
        log_c = ""
        attempts_c: List[Dict[str, Any]] = []
        try:
            wt_c = create_worktree(repo_root, ref=base_commit)
            if test_patch.strip():
                apply_patch_text(wt_c.path, test_patch)
            if pred_patch.strip():
                apply_patch_text(wt_c.path, pred_patch)
            xml_c = tmp / "junit_c.xml"
            run_c, log_c, attempts_c = _run_pytest(
                wt_c.path,
                test_files=test_inputs,
                junit_xml=xml_c,
                timeout_seconds=timeout_seconds,
                test_cmd_base=test_cmd_base,
            )
        except Exception as exc:
            pass_c_attempts = (
                exc.attempts if isinstance(exc, PytestExecutionError) else []
            )
            return {
                "status": "error",
                "error": f"pass_c failed: {exc}",
                "targeted_test_cmd": targeted_test_cmd,
                "targeted_test_inputs": test_inputs,
                "log_b": log_b,
                "log_c": log_c,
                "run_b": run_b,
                "run_c": {},
                "run_b_attempts": attempts_b,
                "run_c_attempts": pass_c_attempts,
                "FAIL_TO_PASS": [],
                "PASS_TO_PASS": [],
                "resolved": False,
            }
        finally:
            if wt_c is not None:
                try:
                    wt_c.remove()
                except Exception:
                    pass

    ftp, ptp = _derive_test_lists(run_b, run_c, declared_ftp, declared_ptp)
    resolved = _is_resolved(ftp, run_c, declared_ftp)

    return {
        "status": "resolved" if resolved else "not_resolved",
        "error": None,
        "log_b": log_b,
        "log_c": log_c,
        "targeted_test_cmd": targeted_test_cmd,
        "targeted_test_inputs": test_inputs,
        "run_b": run_b,
        "run_c": run_c,
        "run_b_attempts": attempts_b,
        "run_c_attempts": attempts_c,
        "FAIL_TO_PASS": ftp,
        "PASS_TO_PASS": ptp,
        "resolved": resolved,
    }


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
) -> Dict[str, Any]:
    """Evaluate predictions against a dataset and write ``validation.jsonl``.

    Args:
        dataset_path:      Path to ``dataset.jsonl``.
        predictions_path:  Path to ``predictions.gold.jsonl`` or custom predictions.
        out_root:          Directory where ``validation.jsonl`` is written.
        repo_root:         Git repo root; inferred from dataset_path if omitted.
        timeout_seconds:   Per-instance pytest timeout.
        adapter_spec:      Adapter spec dict from ``generate_adapter``; provides
                           ``test_cmd_base`` and other harness settings.

    Returns a summary dict with counts.
    """
    if repo_root is None:
        from repogauge.export.materialize import _normalize_repo_root

        repo_root = _normalize_repo_root(dataset_path)

    dataset_rows = _read_jsonl(dataset_path)
    pred_rows = _read_jsonl(predictions_path)
    pred_by_id = {r["instance_id"]: r for r in pred_rows}

    out_root.mkdir(parents=True, exist_ok=True)
    validation_path = out_root / "validation.jsonl"

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
                    "status": "skipped",
                    "error": "no matching prediction",
                    "resolved": False,
                    "targeted_test_cmd": "",
                    "targeted_test_inputs": [],
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
            test_cmd_base=(adapter_spec or {}).get("test_cmd_base", "python -m pytest"),
        )

        if outcome["status"] == "error":
            error_count += 1
        elif outcome["resolved"]:
            resolved_count += 1

        results.append(
            {
                "instance_id": iid,
                "status": outcome["status"],
                "error": outcome["error"],
                "resolved": outcome["resolved"],
                "targeted_test_cmd": outcome["targeted_test_cmd"],
                "targeted_test_inputs": outcome["targeted_test_inputs"],
                "FAIL_TO_PASS": outcome["FAIL_TO_PASS"],
                "PASS_TO_PASS": outcome["PASS_TO_PASS"],
                "metadata": {
                    "base_commit": ds["base_commit"],
                    "run_b_count": len(outcome["run_b"]),
                    "run_c_count": len(outcome["run_c"]),
                    "run_b_attempts": outcome["run_b_attempts"],
                    "run_c_attempts": outcome["run_c_attempts"],
                    "log_b": outcome["log_b"][-2000:],
                    "log_c": outcome["log_c"][-2000:],
                },
            }
        )

    validation_path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in results),
        encoding="utf-8",
    )

    total = len(dataset_rows)
    summary = {
        "validation_path": str(validation_path),
        "total": total,
        "resolved": resolved_count,
        "not_resolved": total - resolved_count - error_count - skipped_count,
        "error": error_count,
        "skipped": skipped_count,
        "resolve_rate": round(resolved_count / total, 3) if total else 0.0,
    }
    return summary
