"""Judge queue orchestration for SWE-bench style evaluation.

The current execution model keeps solver output normalization and official harness
invocation separate from local evaluation logic. For now, this module implements the
harness wrapper path used by ``repogauge eval``.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional


class HarnessEvaluationError(RuntimeError):
    """Raised when harness wrapper execution cannot complete."""


class AdapterLoadError(HarnessEvaluationError):
    """Raised when the generated adapter cannot be imported or is malformed."""


@dataclass(frozen=True)
class HarnessRunSummary:
    """Minimal per-run summary exposed to CLI and callers."""

    validation_path: str
    total: int
    resolved: int
    not_resolved: int
    error: int
    skipped: int
    resolve_rate: float
    harness_output: str | None = None


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value:
            continue
        rows.append(json.loads(value))
    return rows


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _build_gold_predictions(
    dataset_rows: List[Dict[str, Any]], predictions_path: Path
) -> Path:
    """Create gold predictions from dataset rows and return the output path."""
    rows = []
    for row in dataset_rows:
        rows.append(
            {
                "instance_id": _coerce_text(row.get("instance_id")),
                "model_name_or_path": "gold",
                "model_patch": _coerce_text(row.get("patch")),
            }
        )
    predictions_path = predictions_path
    predictions_path.parent.mkdir(parents=True, exist_ok=True)
    predictions_path.write_text(
        "".join(
            json.dumps(r, sort_keys=True) + "\n"
            for r in rows
            if _coerce_text(r["instance_id"])
        ),
        encoding="utf-8",
    )
    return predictions_path


def _load_adapter(adapter_path: Path) -> tuple[Dict[str, Any], object]:
    if not adapter_path.exists():
        raise AdapterLoadError(f"adapter not found: {adapter_path}")

    module_name = f"repogauge_adapter_{uuid.uuid4().hex[:12]}"
    spec = importlib.util.spec_from_file_location(module_name, adapter_path)
    if spec is None or spec.loader is None:
        raise AdapterLoadError(f"cannot import adapter module from {adapter_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[arg-type]

    if not hasattr(module, "get_spec"):
        raise AdapterLoadError(
            f"adapter module {adapter_path} does not define get_spec()"
        )

    adapter_spec = module.get_spec()
    if not isinstance(adapter_spec, Mapping):
        raise AdapterLoadError(
            f"adapter.get_spec() from {adapter_path} returned {type(adapter_spec).__name__}, "
            "expected mapping"
        )

    return dict(adapter_spec), module


def _adapter_context(module: object) -> Dict[str, Any]:
    """Return registration context from a generated adapter module."""
    if hasattr(module, "registration_context"):
        context = module.registration_context()
        if isinstance(context, Mapping):
            return dict(context)

    # Fallback for partially generated adapters.
    repo = _coerce_text(getattr(module, "REPO", ""))
    version = _coerce_text(getattr(module, "VERSION", "0.0.0"))
    if not repo:
        return {}
    return {
        "repo": repo,
        "version": version,
        "maps": {
            "repo_to_ext": {repo: "py"},
            "repo_version_to_specs": {
                repo: {
                    version: {
                        "docker_specs": {
                            "python_version": _coerce_text(
                                getattr(module, "PYTHON_VERSION", "3.11")
                            )
                        },
                        "pre_install": list(getattr(module, "PRE_INSTALL", [])),
                        "install": list(getattr(module, "INSTALL", [])),
                        "build": list(getattr(module, "BUILD", [])),
                        "test_cmd_base": _coerce_text(
                            getattr(module, "TEST_CMD_BASE", "python -m pytest")
                        ),
                        "parser": _coerce_text(getattr(module, "PARSER", "junit")),
                        "strategy_name": _coerce_text(
                            getattr(module, "STRATEGY_NAME", "")
                        ),
                    }
                }
            },
            "repo_to_parser": {
                repo: getattr(module, "PARSER", "junit"),
            },
        },
    }


def _patch_module_map(
    target: Mapping[str, Any], field: str, updates: Mapping[str, Any]
) -> None:
    mapping = target.get(field)
    if not isinstance(mapping, MutableMapping):
        return
    mapping.update(updates)


def _register_adapter_maps(adapter_context: Mapping[str, Any]) -> Dict[str, Any]:
    """Patch any known harness map modules with generated adapter entries."""
    context_maps = adapter_context.get("maps", {})
    if not isinstance(context_maps, Mapping):
        return {}

    candidates = (
        "swebench.harness.test_spec",
        "swebench.harness.test_spec.test_spec",
    )

    patched: Dict[str, Any] = {}
    for candidate in candidates:
        try:
            module = __import__(candidate, fromlist=["*"])
        except ModuleNotFoundError:
            continue

        for target, source in (
            ("MAP_REPO_TO_EXT", "repo_to_ext"),
            ("MAP_REPO_VERSION_TO_SPECS", "repo_version_to_specs"),
            ("MAP_REPO_TO_PARSER", "repo_to_parser"),
        ):
            value = context_maps.get(source)
            if not isinstance(value, Mapping):
                continue
            try:
                payload = getattr(module, target)
            except AttributeError:
                continue
            if isinstance(payload, dict):
                payload.update(value)
                patched[target] = value

    return patched


def _normalize_status(status: str) -> tuple[str, bool, bool]:
    value = status.lower().strip()
    if value in {"resolved", "passed", "pass"}:
        return "resolved", True, False
    if value in {"not_resolved", "unresolved", "failed"}:
        return "not_resolved", False, False
    if value in {"skipped", "missing_prediction"}:
        return "skipped", False, False
    if value in {"error", "errored", "crash", "failed_with_exception"}:
        return "error", False, True
    return "not_resolved", False, False


def _result_row_from_instance(
    *,
    dataset_row: Mapping[str, Any],
    resolved: bool | None = None,
    reason: str | None = None,
    error: str | None = None,
    status: str = "not_resolved",
    metadata: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    normalized_status, derived_resolved, is_error = _normalize_status(status)
    final_resolved = (
        bool(resolved) if resolved is not None else derived_resolved and not is_error
    )
    final_status = normalized_status
    final_error = error
    if is_error and not final_error:
        final_error = reason

    return {
        "instance_id": _coerce_text(dataset_row.get("instance_id")),
        "status": final_status,
        "reason": reason,
        "failure_code": None,
        "error": final_error,
        "resolved": final_resolved,
        "environment_strategy": _coerce_text(dataset_row.get("version")) or "default",
        "test_strategy": "official_harness",
        "targeted_test_cmd": "",
        "targeted_test_inputs": [],
        "FAIL_TO_PASS": dataset_row.get("FAIL_TO_PASS", []),
        "PASS_TO_PASS": dataset_row.get("PASS_TO_PASS", []),
        "metadata": {
            "base_commit": _coerce_text(dataset_row.get("base_commit")),
            "patch_length": len(_coerce_text(dataset_row.get("patch"))),
            "test_patch_length": len(_coerce_text(dataset_row.get("test_patch"))),
            "adapter_repo": _coerce_text(dataset_row.get("repo")),
            "harness_metadata": dict(metadata or {}),
        },
    }


def _coerce_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_harness_results(
    harness_result: Any,
    dataset_rows: List[Dict[str, Any]],
) -> tuple[list[Dict[str, Any]], Dict[str, Any]]:
    """Attempt to parse harness result output into canonical per-instance rows."""
    by_instance: Dict[str, Dict[str, Any]] = {}
    metadata: Dict[str, Any] = {}

    if isinstance(harness_result, Mapping):
        metadata = dict(harness_result)
        candidate = harness_result
        rows = candidate.get("results") or candidate.get("rows")
        if isinstance(rows, list):
            for raw in rows:
                if not isinstance(raw, Mapping):
                    continue
                iid = _coerce_text(raw.get("instance_id"))
                if not iid:
                    continue
                by_instance[iid] = {
                    "status": _coerce_text(raw.get("status")) or "not_resolved",
                    "resolved": raw.get("resolved"),
                    "reason": _coerce_text(
                        raw.get("reason") or raw.get("failure_reason")
                    ),
                    "error": raw.get("error"),
                    "metadata": raw.get("metadata", {}),
                }

    if isinstance(harness_result, list):
        for raw in harness_result:
            if not isinstance(raw, Mapping):
                continue
            iid = _coerce_text(raw.get("instance_id"))
            if not iid:
                continue
            by_instance[iid] = {
                "status": _coerce_text(raw.get("status")) or "not_resolved",
                "resolved": raw.get("resolved"),
                "reason": _coerce_text(raw.get("reason") or raw.get("failure_reason")),
                "error": raw.get("error"),
                "metadata": raw.get("metadata", {}),
            }

    if not by_instance:
        return [], metadata

    normalized: list[Dict[str, Any]] = []
    for dataset_row in dataset_rows:
        iid = _coerce_text(dataset_row.get("instance_id"))
        raw = by_instance.get(iid)
        if raw is None:
            normalized.append(
                _result_row_from_instance(
                    dataset_row=dataset_row,
                    status="missing",
                    reason="no harness result for instance",
                )
            )
            continue

        normalized.append(
            _result_row_from_instance(
                dataset_row=dataset_row,
                resolved=_coerce_int(raw.get("resolved", "")) != 0
                if isinstance(raw.get("resolved"), (int, bool, float))
                else None,
                reason=raw.get("reason"),
                error=raw.get("error") and _coerce_text(raw.get("error")),
                status=_coerce_text(raw.get("status")) or "not_resolved",
                metadata=raw.get("metadata", {}),
            )
        )

    return normalized, metadata


def _discover_harness_output(output_dir: Path) -> Any:
    """Load results from likely output files created by SWE-bench harness."""
    if not output_dir.exists():
        return None

    candidates = [
        output_dir / "report.json",
        output_dir / "evaluation_result.json",
        output_dir / "results.json",
        output_dir / "results.jsonl",
    ]
    for candidate in candidates:
        if not candidate.exists():
            continue
        payload = candidate.read_text(encoding="utf-8").strip()
        if not payload:
            continue
        if candidate.suffix == ".jsonl":
            rows = []
            for line in payload.splitlines():
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
            return rows
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            continue

    # Some harness versions may emit one report per instance.
    rows: list[dict[str, Any]] = []
    for file in output_dir.glob("*.json"):
        try:
            payload = json.loads(file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(payload, list):
            rows.extend(payload)
        elif isinstance(payload, dict):
            rows.append(payload)
    return rows if rows else None


def _invoke_swebench_harness(
    *,
    dataset_path: Path,
    predictions_path: Path,
    out_root: Path,
    workers: int,
    timeout_seconds: int,
) -> Any:
    """Call ``swebench.harness.run_evaluation`` with defensive kwargs."""
    out_dir = out_root / "harness"
    out_dir.mkdir(parents=True, exist_ok=True)

    import swebench.harness.run_evaluation as run_module  # type: ignore[import]

    run_fn = getattr(run_module, "run_evaluation", None)
    if run_fn is None:
        raise HarnessEvaluationError(
            "swebench.harness.run_evaluation module missing run_evaluation entrypoint"
        )

    signature = inspect.signature(run_fn)
    params = signature.parameters

    kwargs: dict[str, Any] = {}
    if "dataset_path" in params:
        kwargs["dataset_path"] = str(dataset_path)
    elif "dataset" in params:
        kwargs["dataset"] = str(dataset_path)

    if "predictions_path" in params:
        kwargs["predictions_path"] = str(predictions_path)
    elif "predictions" in params:
        kwargs["predictions"] = str(predictions_path)

    if "output_dir" in params:
        kwargs["output_dir"] = str(out_dir)
    elif "output" in params:
        kwargs["output"] = str(out_dir)

    if "max_workers" in params:
        kwargs["max_workers"] = workers
    elif "num_workers" in params:
        kwargs["num_workers"] = workers

    if "timeout" in params:
        kwargs["timeout"] = timeout_seconds

    if "report_path" in params:
        kwargs["report_path"] = str(out_dir / "report.json")

    return run_fn(**kwargs)


def run_harness_evaluation(
    *,
    dataset_path: Path,
    predictions_path: Optional[Path],
    out_root: Path,
    adapter_path: Optional[Path],
    workers: int = 1,
    timeout_seconds: int = 120,
    gold_if_missing: bool = False,
) -> HarnessRunSummary:
    """Run official SWE-bench harness and normalize outputs.

    Args:
        dataset_path:    Path to dataset.jsonl.
        predictions_path: Explicit predictions file. If ``None`` and
            ``gold_if_missing`` is true, gold predictions are generated.
        out_root:        Output directory for ``validation.jsonl`` and harness files.
        adapter_path:    Path to generated adapter module.
        workers:         Optional max parallel workers passed through when supported.
        timeout_seconds: Optional per-evaluation timeout.
        gold_if_missing: Generate ``predictions`` from dataset when no path exists.

    Returns:
        A normalized summary with count metrics and validation artifact path.
    """
    out_root.mkdir(parents=True, exist_ok=True)
    validation_path = out_root / "validation.jsonl"

    dataset_rows = _read_jsonl(dataset_path)
    if not dataset_rows:
        summary = HarnessRunSummary(
            validation_path=str(validation_path),
            total=0,
            resolved=0,
            not_resolved=0,
            error=0,
            skipped=0,
            resolve_rate=0.0,
            harness_output="dataset empty",
        )
        validation_path.write_text("", encoding="utf-8")
        return summary

    adapter_module: object | None = None
    if adapter_path is not None:
        _, adapter_module = _load_adapter(adapter_path)
        if adapter_module is not None:
            context = _adapter_context(adapter_module)
            _register_adapter_maps(context)

    if predictions_path is None:
        if not gold_if_missing:
            raise HarnessEvaluationError(
                "predictions_path is required unless --gold is set"
            )
        predictions_path = _build_gold_predictions(
            dataset_rows,
            out_root / "predictions.gold.jsonl",
        )

    if not predictions_path.exists():
        if gold_if_missing:
            predictions_path = _build_gold_predictions(
                dataset_rows,
                out_root / "predictions.gold.jsonl",
            )
        else:
            raise HarnessEvaluationError(
                f"predictions file not found: {predictions_path}"
            )

    try:
        harness_output = _invoke_swebench_harness(
            dataset_path=dataset_path,
            predictions_path=predictions_path,
            out_root=out_root,
            workers=workers,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:
        raise HarnessEvaluationError(
            f"official harness execution failed: {exc}"
        ) from exc

    output_payload = _discover_harness_output(out_root / "harness")
    instance_rows, parsed_metadata = _parse_harness_results(
        harness_output, dataset_rows
    )
    if not instance_rows:
        if isinstance(output_payload, Iterable) and not isinstance(
            output_payload, (str, bytes, dict)
        ):
            instance_rows, parsed_metadata = _parse_harness_results(
                output_payload, dataset_rows
            )

    if not instance_rows:
        # Fallback: surface one row per dataset with an explicit error status.
        instance_rows = [
            _result_row_from_instance(
                dataset_row=row,
                status="error",
                reason="missing harness per-instance results",
            )
            for row in dataset_rows
        ]

    for row in instance_rows:
        row["metadata"] = dict(
            row.get("metadata", {}), **{"harness_output": parsed_metadata}
        )

    validation_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in instance_rows),
        encoding="utf-8",
    )

    total = len(instance_rows)
    resolved = 0
    error_count = 0
    skipped_count = 0
    for row in instance_rows:
        if row["status"] == "resolved":
            resolved += 1
        elif row["status"] == "error":
            error_count += 1
        elif row["status"] == "skipped":
            skipped_count += 1

    not_resolved = total - resolved - error_count - skipped_count
    summary = HarnessRunSummary(
        validation_path=str(validation_path),
        total=total,
        resolved=resolved,
        not_resolved=not_resolved,
        error=error_count,
        skipped=skipped_count,
        resolve_rate=round(resolved / total, 3) if total else 0.0,
        harness_output="official_swebench",
    )
    return summary
