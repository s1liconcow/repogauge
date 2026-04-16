"""Judge queue orchestration for SWE-bench style evaluation.

The current execution model keeps solver output normalization and official harness
invocation separate from local evaluation logic. For now, this module implements the
harness wrapper path used by ``repogauge eval``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    results_path: str | None = None
    instance_results_path: str | None = None


@dataclass(frozen=True)
class JudgeSchedulerConfig:
    """Configuration for batched judge execution."""

    batch_size: int = 32
    max_parallel_batches: int = 1
    workers_per_batch: int = 1


@dataclass(frozen=True)
class JudgeBatchResult:
    """Normalized results emitted for a single harness batch."""

    instance_rows: list[dict[str, Any]]
    metadata: dict[str, Any]
    batch_key: str


def _iter_chunks(values: list[Any], size: int) -> Iterable[list[Any]]:
    if size <= 0:
        size = 1
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _write_jsonl_rows(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _batch_key_for_prediction(
    dataset_row: Mapping[str, Any],
    prediction_row: Mapping[str, Any],
) -> str:
    solver_id = _coerce_text(prediction_row.get("model_name_or_path"))
    repo_id = _coerce_text(dataset_row.get("repo"))
    version_id = _coerce_text(dataset_row.get("version"))
    return "|".join((solver_id, repo_id, version_id))


def _coerce_judge_config(
    config: JudgeSchedulerConfig | None,
) -> JudgeSchedulerConfig:
    config = config or JudgeSchedulerConfig()
    if config.batch_size < 1:
        raise HarnessEvaluationError("batch_size must be >= 1")
    if config.max_parallel_batches < 1:
        raise HarnessEvaluationError("max_parallel_batches must be >= 1")
    if config.workers_per_batch < 1:
        raise HarnessEvaluationError("workers_per_batch must be >= 1")
    return config


def _safe_batch_key(value: str) -> str:
    if not value:
        return "default"
    safe = []
    for ch in value:
        if ch.isalnum() or ch in "-._":
            safe.append(ch)
        else:
            safe.append("_")
    return "".join(safe) or "default"


def _prepare_prediction_index(
    predictions_rows: list[Dict[str, Any]],
) -> dict[str, Dict[str, Any]]:
    by_id: dict[str, Dict[str, Any]] = {}
    for prediction in predictions_rows:
        instance_id = _coerce_text(prediction.get("instance_id"))
        if not instance_id:
            continue
        by_id[instance_id] = dict(prediction)
    return by_id


BatchRows = list[tuple[Dict[str, Any], Dict[str, Any]]]
PreparedBatches = tuple[list[tuple[str, BatchRows]], list[dict[str, Any]]]


def _prepare_batches(
    *,
    dataset_rows: list[Dict[str, Any]],
    predictions_rows: list[Dict[str, Any]],
    batch_size: int,
) -> PreparedBatches:
    prediction_by_id = _prepare_prediction_index(predictions_rows)
    grouped: dict[str, list[tuple[Dict[str, Any], Dict[str, Any]]]] = {}
    missing_prediction_rows: list[dict[str, Any]] = []

    for dataset_row in dataset_rows:
        instance_id = _coerce_text(dataset_row.get("instance_id"))
        if not instance_id:
            continue
        prediction = prediction_by_id.get(instance_id)
        if prediction is None:
            missing_prediction_rows.append(
                _result_row_from_instance(
                    dataset_row=dataset_row,
                    status="skipped",
                    reason="missing_prediction",
                )
            )
            continue
        key = _batch_key_for_prediction(
            dataset_row=dataset_row, prediction_row=prediction
        )
        grouped.setdefault(key, []).append((dataset_row, prediction))

    batches: list[tuple[str, BatchRows]] = []
    for key, pairs in grouped.items():
        for chunk in _iter_chunks(pairs, batch_size):
            batches.append((key, chunk))

    return batches, missing_prediction_rows


def _run_batch(
    *,
    batch_index: int,
    batch_key: str,
    rows: list[tuple[Dict[str, Any], Dict[str, Any]]],
    out_root: Path,
    workers: int,
    timeout_seconds: int,
) -> JudgeBatchResult:
    batch_root = (
        out_root
        / "judge_batches"
        / f"batch_{batch_index:04d}_{_safe_batch_key(batch_key)}"
    )
    dataset_path = batch_root / "dataset.jsonl"
    predictions_path = batch_root / "predictions.jsonl"
    dataset_rows = [dataset_row for dataset_row, _ in rows]
    prediction_rows = [prediction_row for _, prediction_row in rows]

    _write_jsonl_rows(
        dataset_path,
        (
            dict(dataset_row)
            for dataset_row in dataset_rows
            if _coerce_text(dataset_row.get("instance_id"))
        ),
    )
    _write_jsonl_rows(predictions_path, prediction_rows)

    harness_result = _invoke_swebench_harness(
        dataset_path=dataset_path,
        predictions_path=predictions_path,
        out_root=batch_root,
        workers=workers,
        timeout_seconds=timeout_seconds,
    )
    instance_rows, metadata = _parse_harness_results(harness_result, dataset_rows)

    if not instance_rows:
        instance_rows = [
            _result_row_from_instance(
                dataset_row=dataset_row,
                status="error",
                reason="missing harness per-instance results",
            )
            for dataset_row in dataset_rows
        ]

    for row in instance_rows:
        row["metadata"] = dict(row.get("metadata", {}), **{"harness_output": metadata})

    return JudgeBatchResult(
        instance_rows=instance_rows,
        metadata=metadata,
        batch_key=batch_key,
    )


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
        "solver_id": _coerce_text(
            dataset_row.get("solver_id") or dataset_row.get("model_name_or_path")
        ),
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

        # swebench 4.x format: separate resolved/unresolved/error id lists
        if "resolved_ids" in candidate or "unresolved_ids" in candidate:
            for iid in candidate.get("resolved_ids", []):
                iid = _coerce_text(iid)
                if iid:
                    by_instance[iid] = {"status": "resolved", "resolved": True, "reason": None, "error": None, "metadata": {}}
            for iid in candidate.get("unresolved_ids", []):
                iid = _coerce_text(iid)
                if iid:
                    by_instance[iid] = {"status": "not_resolved", "resolved": False, "reason": None, "error": None, "metadata": {}}
            for iid in candidate.get("error_ids", []):
                iid = _coerce_text(iid)
                if iid and iid not in by_instance:
                    by_instance[iid] = {"status": "error", "resolved": False, "reason": "harness error", "error": "harness error", "metadata": {}}
            for iid in candidate.get("incomplete_ids", []):
                iid = _coerce_text(iid)
                if iid and iid not in by_instance:
                    by_instance[iid] = {"status": "error", "resolved": False, "reason": "incomplete", "error": "incomplete", "metadata": {}}
        else:
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
    """Call the swebench 4.x ``run_instances`` API."""
    import os

    import docker as docker_module  # type: ignore[import]
    import swebench.harness.run_evaluation as run_module  # type: ignore[import]

    dataset_rows = _read_jsonl(dataset_path)
    pred_rows = _read_jsonl(predictions_path)
    predictions = {
        row["instance_id"]: row
        for row in pred_rows
        if row.get("instance_id")
    }
    instances = [
        row for row in dataset_rows
        if row.get("instance_id") in predictions
    ]
    if not instances:
        return {}

    run_id = f"repogauge-{out_root.parent.name}-{out_root.name}"
    client = docker_module.from_env()

    print("repogauge eval: building environment images", file=sys.stderr)
    run_module.build_env_images(
        client,
        instances,
        force_rebuild=False,
        max_workers=workers,
        namespace="swebench",
        instance_image_tag="latest",
        env_image_tag="latest",
    )

    out_root.mkdir(parents=True, exist_ok=True)
    orig_dir = os.getcwd()
    os.chdir(out_root)
    try:
        print(
            "repogauge eval: dispatching to official SWE-bench harness",
            file=sys.stderr,
        )
        run_module.run_instances(
            predictions=predictions,
            instances=instances,
            cache_level="instance",
            clean=False,
            force_rebuild=False,
            max_workers=workers,
            run_id=run_id,
            timeout=timeout_seconds,
            namespace="swebench",
        )
        report_path = run_module.make_run_report(
            predictions,
            instances,
            run_id,
            namespace="swebench",
        )
        return json.loads(report_path.read_text())
    finally:
        os.chdir(orig_dir)


def run_harness_evaluation(
    *,
    dataset_path: Path,
    predictions_path: Optional[Path],
    out_root: Path,
    adapter_path: Optional[Path],
    workers: int = 1,
    timeout_seconds: int = 120,
    gold_if_missing: bool = False,
    judge_config: JudgeSchedulerConfig | None = None,
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
    results_path = out_root / "results.json"
    instance_results_path = out_root / "instance_results.jsonl"

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
            results_path=str(results_path),
            instance_results_path=str(instance_results_path),
            harness_output="dataset empty",
        )
        validation_path.write_text("", encoding="utf-8")
        results_path.write_text(
            json.dumps({"batches": []}, sort_keys=True),
            encoding="utf-8",
        )
        instance_results_path.write_text("", encoding="utf-8")
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

    predictions_rows = _read_jsonl(predictions_path)
    config = _coerce_judge_config(judge_config)
    batches, missing_rows = _prepare_batches(
        dataset_rows=dataset_rows,
        predictions_rows=predictions_rows,
        batch_size=config.batch_size,
    )

    batch_results: list[JudgeBatchResult] = []
    if batches:
        try:
            with ThreadPoolExecutor(max_workers=config.max_parallel_batches) as pool:
                futures = {
                    pool.submit(
                        _run_batch,
                        batch_index=index,
                        batch_key=batch_key,
                        rows=rows,
                        out_root=out_root,
                        workers=workers * config.workers_per_batch,
                        timeout_seconds=timeout_seconds,
                    ): batch_key
                    for index, (batch_key, rows) in enumerate(batches)
                }
                for future in as_completed(futures):
                    try:
                        batch_results.append(future.result())
                    except Exception as exc:
                        raise HarnessEvaluationError(
                            f"official harness batch execution failed: {exc}"
                        ) from exc
        except HarnessEvaluationError:
            raise
        except (
            Exception
        ) as exc:  # pragma: no cover - defensive for unexpected pool errors
            raise HarnessEvaluationError(
                f"official harness batch execution failed: {exc}"
            ) from exc

    by_instance_id: dict[str, dict[str, Any]] = {}
    for row in missing_rows:
        iid = _coerce_text(row.get("instance_id"))
        if iid:
            by_instance_id[iid] = row

    for batch in batch_results:
        for row in batch.instance_rows:
            iid = _coerce_text(row.get("instance_id"))
            if not iid:
                continue
            row["metadata"] = dict(
                row.get("metadata", {}), **{"batch_key": batch.batch_key}
            )
            by_instance_id[iid] = row

    instance_rows = []
    for dataset_row in dataset_rows:
        iid = _coerce_text(dataset_row.get("instance_id"))
        if not iid:
            continue
        row = by_instance_id.get(iid)
        if row is None:
            instance_rows.append(
                _result_row_from_instance(
                    dataset_row=dataset_row,
                    status="error",
                    reason="no harness result for instance",
                )
            )
            continue
        row["environment_strategy"] = (
            row.get("environment_strategy")
            or _coerce_text(dataset_row.get("version"))
            or "default"
        )
        instance_rows.append(row)

    if not instance_rows:
        instance_rows = [
            _result_row_from_instance(
                dataset_row=row,
                status="error",
                reason="no harness instance results collected",
            )
            for row in dataset_rows
        ]

    for row in instance_rows:
        existing_metadata = row.get("metadata")
        if isinstance(existing_metadata, Mapping):
            row["metadata"] = dict(existing_metadata)

    results_payload = {
        "batch_count": len(batch_results),
        "batch_size": config.batch_size,
        "max_parallel_batches": config.max_parallel_batches,
        "workers_per_batch": config.workers_per_batch,
        "batches": [
            {
                "batch_key": item.batch_key,
                "instance_count": len(item.instance_rows),
                "metadata": item.metadata,
            }
            for item in batch_results
        ],
    }

    results_path.write_text(
        json.dumps(results_payload, sort_keys=True),
        encoding="utf-8",
    )
    instance_results_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in instance_rows),
        encoding="utf-8",
    )

    # Preserve fallback path for compatibility with existing callers that parse
    # validation output as the canonical instance artifact.
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
        results_path=str(results_path),
        instance_results_path=str(instance_results_path),
    )
    return summary
