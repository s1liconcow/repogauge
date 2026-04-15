"""SWE-bench-style dataset materialization utilities.

This module converts ``run_materialization`` output rows into
``DatasetInstance`` rows plus matching gold ``PredictionRow`` rows.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

from repogauge.artifacts import ArtifactLayout
from repogauge.config import DatasetInstance, PredictionRow


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value:
            continue
        rows.append(json.loads(value))
    return rows


def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text


def _coerce_repo_slug(repo: Any) -> str:
    text = _coerce_str(repo)
    if text:
        return text.replace("/", "__")
    return "repo"


def _coerce_short_commit(commit: Any, fallback: str = "") -> str:
    text = _coerce_str(commit)
    if not text:
        return fallback
    return text[:12]


def _coerce_instance_id(row: Dict[str, Any]) -> str:
    candidate_id = _coerce_str(row.get("candidate_id") or row.get("id"))
    if candidate_id:
        return candidate_id
    repo = _coerce_repo_slug(row.get("repo"))
    short_commit = _coerce_short_commit(row.get("commit"), fallback="unknown")
    return f"{repo}-rg-{short_commit}"


def _coerce_test_ids(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items = [str(item).strip() for item in value if str(item).strip()]
        return sorted(set(items), key=lambda item: items.index(item))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        return [segment.strip() for segment in text.splitlines() if segment.strip()]
    return []


def _coerce_patch(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _coerce_version(row: Dict[str, Any]) -> str:
    direct = _coerce_str(row.get("version"))
    if direct:
        return direct

    metadata = row.get("metadata")
    if isinstance(metadata, dict):
        env = metadata.get("environment_signature")
        if isinstance(env, dict):
            value = _coerce_str(env.get("version"))
            if value:
                return value

    return _coerce_str(row.get("repo_version")) or "0.0.0"


def _coerce_str_or_none(value: Any) -> str | None:
    text = _coerce_str(value)
    return text if text else None


def _to_dataset_rows(materialized_rows: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    datasets: list[dict[str, Any]] = []
    predictions: list[dict[str, Any]] = []

    for row in materialized_rows:
        repo = _coerce_str(row.get("repo"))
        commit = _coerce_str(row.get("commit"))
        base_commit = _coerce_str(row.get("base_commit"))
        patch = _coerce_patch(row.get("patch"))
        prod_patch = _coerce_patch(row.get("prod_patch")) or patch
        test_patch = _coerce_patch(row.get("test_patch"))
        problem_statement = _coerce_str(row.get("problem_statement"))
        candidate_id = _coerce_instance_id(row)
        metadata = row.get("metadata")
        metadata_value: dict[str, Any] = metadata if isinstance(metadata, dict) else {}

        fail_to_pass = _coerce_test_ids(metadata_value.get("FAIL_TO_PASS"))
        pass_to_pass = _coerce_test_ids(metadata_value.get("PASS_TO_PASS"))
        if "problem_statement_source" in metadata_value:
            metadata_value = dict(metadata_value)
            metadata_value["problem_statement_source"] = metadata_value["problem_statement_source"]
        dataset_row = DatasetInstance(
            instance_id=candidate_id,
            repo=repo,
            base_commit=base_commit,
            problem_statement=problem_statement,
            version=_coerce_version(row),
            patch=prod_patch,
            test_patch=test_patch,
            FAIL_TO_PASS=fail_to_pass,
            PASS_TO_PASS=pass_to_pass,
            metadata={
                "source_commit": commit,
                "source_repo": repo,
                "source_candidate_id": candidate_id,
                "source_short_commit": _coerce_short_commit(commit, fallback="unknown"),
                **metadata_value,
            },
        )
        datasets.append(dataset_row.to_dict())

        prediction_row = PredictionRow(
            instance_id=candidate_id,
            model_name_or_path="gold",
            model_patch=prod_patch,
            prompt_hash=None,
            solver_id=_coerce_str_or_none(metadata_value.get("solver_id")),
            metadata={
                "source_candidate_id": candidate_id,
                "source_commit": commit,
                "source_base_commit": base_commit,
            },
        )
        predictions.append(prediction_row.to_dict())

    return datasets, predictions


def run_export(materialized_path: str | Path, out_root: str | Path) -> Dict[str, Any]:
    """Export materialized rows into dataset and prediction artifacts."""

    source = Path(materialized_path)
    out_root_path = Path(out_root)
    out_root_path.mkdir(parents=True, exist_ok=True)
    layout = ArtifactLayout(out_root_path)

    materialized_rows = _read_jsonl(source)
    dataset_rows, prediction_rows = _to_dataset_rows(materialized_rows)

    dataset_path = layout.dataset_file
    predictions_path = layout.predictions_file
    dataset_path.parent.mkdir(parents=True, exist_ok=True)

    dataset_path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in dataset_rows), encoding="utf-8")
    predictions_path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in prediction_rows), encoding="utf-8")

    return {
        "dataset_path": str(dataset_path),
        "predictions_path": str(predictions_path),
        "dataset_count": len(dataset_rows),
        "prediction_count": len(prediction_rows),
        "materialized_path": str(source),
    }
