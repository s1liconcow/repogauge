"""Router training data export and baseline policy evaluation."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
import math
from pathlib import Path
from typing import Any, Mapping

from repogauge.runner.features import build_task_feature_bundle

ROUTER_MODEL_VERSION = "router-model-v1"
ROUTER_FEATURE_SPACE_VERSION = "router-feature-space-v1"

_ROUTER_LABELS = (
    "cheap_is_enough",
    "needs_expensive",
    "likely_unsolved",
)
_ROUTER_NUMERIC_FEATURES = (
    "problem_statement_char_count",
    "problem_statement_line_count",
    "problem_statement_word_count",
    "repo_segment_count",
    "repo_slug_length",
    "base_commit_present",
)
_ROUTER_LENGTH_BUCKETS = ("short", "medium", "long", "very_long")
_ROUTER_SIGNAL_BUCKETS = ("stacktrace", "error", "test", "path", "neutral")
_ROUTER_VERSION_BUCKETS = (
    "unknown",
    "python-tagged",
    "semantic",
    "compound",
    "opaque",
)


_ESCALATION_SIGNAL_TERMS = (
    "invalid_patch",
    "invalid patch",
    "timed_out",
    "timed out",
    "timeout",
    "no_progress",
    "no progress",
    "no-progress",
    "budget_exceeded",
    "budget exceeded",
)


def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
            "resolved",
            "passed",
            "success",
        }
    return False


def _coerce_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _coerce_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _read_row_cost(row: Mapping[str, Any]) -> float:
    cost = row.get("cost", {})
    if isinstance(cost, Mapping):
        for key in ("total_cost", "usd", "value", "amount", "total_usd"):
            if key in cost:
                return _coerce_float(cost.get(key))
    return _coerce_float(row.get("attempt_cost_usd"))


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix == ".parquet":
        try:
            import pyarrow.parquet as pq  # type: ignore

            table = pq.read_table(path)
            return [
                {key: value for key, value in row.items() if key is not None}
                for row in table.to_pylist()
            ]
        except Exception:
            pass
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value:
            continue
        rows.append(json.loads(value))
    return rows


def _write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq  # type: ignore

        table = pa.Table.from_pylist(rows)
        pq.write_table(table, str(path))
        return
    except Exception:
        path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )


def _attempt_sort_key(row: Mapping[str, Any]) -> tuple[int, str, str, str]:
    return (
        _coerce_int(row.get("attempt_index")),
        _coerce_str(row.get("attempt_ended_at")),
        _coerce_str(row.get("attempt_started_at")),
        _coerce_str(row.get("attempt_id")),
    )


def _solver_instance_key(row: Mapping[str, Any]) -> tuple[str, str]:
    return (_coerce_str(row.get("instance_id")), _coerce_str(row.get("solver_id")))


def _instance_value(row: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = _coerce_str(row.get(key))
        if value:
            return value
    metadata = _coerce_mapping(row.get("metadata"))
    for key in keys:
        value = _coerce_str(metadata.get(key))
        if value:
            return value
    return ""


def _compact_attempt_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "attempt_id": _coerce_str(row.get("attempt_id")),
        "attempt_index": _coerce_int(row.get("attempt_index")),
        "attempt_state": _coerce_str(row.get("attempt_state")),
        "duration_ms": _coerce_int(row.get("duration_ms")),
        "cost_usd": _read_row_cost(row),
        "exit_reason": _coerce_str(row.get("exit_reason")),
        "started_at": _coerce_str(row.get("attempt_started_at")),
        "ended_at": _coerce_str(row.get("attempt_ended_at")),
    }


def _aggregate_solver_instance_rows(
    joined_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in joined_rows:
        grouped[_solver_instance_key(row)].append(row)

    aggregates: list[dict[str, Any]] = []
    for (instance_id, solver_id), rows in grouped.items():
        ordered = sorted(rows, key=_attempt_sort_key)
        representative = ordered[0]
        final_attempt = ordered[-1]
        total_duration_ms = sum(_coerce_int(row.get("duration_ms")) for row in ordered)
        total_cost_usd = sum(_read_row_cost(row) for row in ordered)

        aggregate = {
            "instance_id": instance_id,
            "solver_id": solver_id,
            "attempt_count": len(ordered),
            "attempts": [_compact_attempt_row(row) for row in ordered],
            "total_duration_ms": total_duration_ms,
            "total_cost_usd": total_cost_usd,
            "resolved": _coerce_bool(final_attempt.get("resolved")),
            "harness_outcome": _coerce_str(final_attempt.get("harness_outcome")),
            "attempt_state": _coerce_str(
                final_attempt.get("attempt_state") or final_attempt.get("status")
            ),
            "failure_reason": final_attempt.get("failure_reason"),
            "exit_reason": _coerce_str(final_attempt.get("exit_reason")),
            "prompt_policy_hash": _coerce_str(final_attempt.get("prompt_policy_hash")),
            "tool_policy_hash": _coerce_str(final_attempt.get("tool_policy_hash")),
            "solver_config_hash": _coerce_str(final_attempt.get("solver_config_hash")),
            "repo": _instance_value(
                representative,
                "repo",
                "instance_repo",
                "source_repo",
            ),
            "base_commit": _instance_value(
                representative,
                "base_commit",
                "instance_base_commit",
            ),
            "version": _instance_value(
                representative,
                "version",
                "instance_version",
            ),
            "problem_statement": _coerce_str(representative.get("problem_statement")),
            "task_feature_version": _coerce_str(
                representative.get("task_feature_version")
            ),
            "task_feature_hash": _coerce_str(representative.get("task_feature_hash")),
            "task_cluster": _coerce_str(representative.get("task_cluster")),
            "task_features": _coerce_mapping(representative.get("task_features")),
            "metadata": {
                **_coerce_mapping(representative.get("metadata")),
                "attempt_ids": [_coerce_str(row.get("attempt_id")) for row in ordered],
                "attempt_states": [
                    _coerce_str(row.get("attempt_state")) for row in ordered
                ],
                "solver_id": solver_id,
                "instance_id": instance_id,
                "total_cost_usd": total_cost_usd,
                "total_duration_ms": total_duration_ms,
            },
        }

        task_features = build_task_feature_bundle(aggregate)
        aggregate.setdefault("task_feature_version", task_features.feature_version)
        aggregate.setdefault("task_feature_hash", task_features.feature_hash)
        aggregate.setdefault("task_cluster", task_features.cluster_label)
        aggregate.setdefault("task_features", task_features.features)
        metadata = _coerce_mapping(aggregate.get("metadata"))
        metadata.update(task_features.to_metadata())
        aggregate["metadata"] = metadata
        aggregates.append(aggregate)

    aggregates.sort(key=lambda row: (_coerce_str(row["instance_id"]), row["solver_id"]))
    return aggregates


def _rank_solvers(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    solver_totals: dict[str, dict[str, float]] = defaultdict(
        lambda: {"cost": 0.0, "duration": 0.0, "count": 0.0, "resolved": 0.0}
    )
    for row in rows:
        solver = _coerce_str(row.get("solver_id"))
        bucket = solver_totals[solver]
        bucket["cost"] += _coerce_float(row.get("total_cost_usd"))
        bucket["duration"] += _coerce_float(row.get("total_duration_ms"))
        bucket["count"] += 1.0
        if _coerce_bool(row.get("resolved")):
            bucket["resolved"] += 1.0

    ranked = []
    for solver_id, totals in solver_totals.items():
        count = totals["count"] or 1.0
        ranked.append(
            {
                "solver_id": solver_id,
                "average_cost_usd": totals["cost"] / count,
                "average_duration_ms": totals["duration"] / count,
                "resolved_count": int(totals["resolved"]),
                "instance_count": int(totals["count"]),
            }
        )
    ranked.sort(
        key=lambda item: (
            item["average_cost_usd"],
            item["average_duration_ms"],
            item["solver_id"],
        )
    )
    return ranked


def _pick_solver_row(
    rows: list[dict[str, Any]], solver_id: str, *, prefer_last: bool = False
) -> dict[str, Any] | None:
    for row in rows:
        if _coerce_str(row.get("solver_id")) == solver_id:
            return row
    if not rows:
        return None
    ordered = sorted(rows, key=lambda row: (_coerce_str(row["solver_id"]),))
    return ordered[-1] if prefer_last else ordered[0]


def _policy_trigger_on_signal(row: Mapping[str, Any]) -> bool:
    if _coerce_bool(row.get("resolved")):
        return False
    if _coerce_str(row.get("attempt_state")).lower() in {
        "invalid_patch",
        "timed_out",
        "budget_exceeded",
    }:
        return True

    haystack = " ".join(
        [
            _coerce_str(row.get("attempt_state")),
            _coerce_str(row.get("failure_reason")),
            _coerce_str(row.get("exit_reason")),
            _coerce_str(row.get("harness_outcome")),
        ]
    ).lower()
    return any(term in haystack for term in _ESCALATION_SIGNAL_TERMS)


def _policy_result(
    *,
    resolved: bool,
    cost_usd: float,
    latency_ms: int,
    escalated: bool,
    solver_id: str,
) -> dict[str, Any]:
    return {
        "resolved": bool(resolved),
        "cost_usd": float(cost_usd),
        "latency_ms": int(latency_ms),
        "escalated": bool(escalated),
        "solver_id": solver_id,
    }


def _percentile(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1))
    return int(ordered[index])


def _router_feature_vector(
    row: Mapping[str, Any],
) -> tuple[dict[str, float], dict[str, Any]]:
    source_row = row.get("row")
    if isinstance(source_row, Mapping):
        row = source_row
    bundle = build_task_feature_bundle(row)
    features = bundle.features

    encoded: dict[str, float] = {
        "problem_statement_char_count": float(
            features.get("problem_statement_char_count", 0)
        ),
        "problem_statement_line_count": float(
            features.get("problem_statement_line_count", 0)
        ),
        "problem_statement_word_count": float(
            features.get("problem_statement_word_count", 0)
        ),
        "repo_segment_count": float(features.get("repo_segment_count", 0)),
        "repo_slug_length": float(features.get("repo_slug_length", 0)),
        "base_commit_present": 1.0 if features.get("base_commit_present") else 0.0,
    }

    version_bucket = _coerce_str(features.get("version_bucket")) or "unknown"
    length_bucket = (
        _coerce_str(features.get("problem_statement_length_bucket")) or "short"
    )
    signal_bucket = _coerce_str(features.get("problem_statement_signal")) or "neutral"

    for bucket in _ROUTER_VERSION_BUCKETS:
        encoded[f"version_bucket={bucket}"] = 1.0 if version_bucket == bucket else 0.0
    for bucket in _ROUTER_LENGTH_BUCKETS:
        encoded[f"problem_statement_length_bucket={bucket}"] = (
            1.0 if length_bucket == bucket else 0.0
        )
    for bucket in _ROUTER_SIGNAL_BUCKETS:
        encoded[f"problem_statement_signal={bucket}"] = (
            1.0 if signal_bucket == bucket else 0.0
        )

    return encoded, bundle.to_metadata()


def _router_feature_names() -> tuple[str, ...]:
    return (
        *_ROUTER_NUMERIC_FEATURES,
        *(f"version_bucket={bucket}" for bucket in _ROUTER_VERSION_BUCKETS),
        *(
            f"problem_statement_length_bucket={bucket}"
            for bucket in _ROUTER_LENGTH_BUCKETS
        ),
        *(f"problem_statement_signal={bucket}" for bucket in _ROUTER_SIGNAL_BUCKETS),
    )


def _router_row_label(row: Mapping[str, Any]) -> str:
    label = _coerce_str(row.get("route_label")) or _coerce_str(row.get("label"))
    if label not in _ROUTER_LABELS:
        raise RuntimeError(
            f"router training row missing valid route_label: {_coerce_str(row.get('instance_id'))}"
        )
    return label


def _router_split_key(row: Mapping[str, Any], *, seed: int) -> str:
    payload = {
        "seed": seed,
        "instance_id": _coerce_str(row.get("instance_id")),
        "route_label": _router_row_label(row),
        "task_feature_hash": _coerce_str(row.get("task_feature_hash")),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _router_split_rows(
    rows: list[dict[str, Any]],
    *,
    seed: int,
    train_fraction: float,
    validation_fraction: float,
) -> dict[str, list[dict[str, Any]]]:
    if not rows:
        return {"train": [], "validation": [], "test": []}

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[_router_row_label(row)].append(row)

    partitions = {"train": [], "validation": [], "test": []}
    for label in sorted(grouped):
        ordered = sorted(
            grouped[label],
            key=lambda row: (
                _router_split_key(row, seed=seed),
                _coerce_str(row.get("instance_id")),
            ),
        )
        count = len(ordered)
        if count == 1:
            partitions["train"].extend(ordered)
            continue

        train_count = int(round(count * train_fraction))
        train_count = max(1, min(count - 1, train_count))

        remaining = count - train_count
        validation_count = int(round(count * validation_fraction))
        if validation_fraction > 0 and remaining > 1 and validation_count == 0:
            validation_count = 1
        validation_count = min(max(0, validation_count), max(0, remaining - 1))

        test_count = count - train_count - validation_count
        if test_count <= 0:
            if validation_count > 0:
                validation_count -= 1
            elif train_count > 1:
                train_count -= 1
            test_count = count - train_count - validation_count

        partitions["train"].extend(ordered[:train_count])
        partitions["validation"].extend(
            ordered[train_count : train_count + validation_count]
        )
        partitions["test"].extend(ordered[train_count + validation_count :])

    return partitions


def _router_class_counts(rows: list[dict[str, Any]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for row in rows:
        counts[_router_row_label(row)] += 1
    return counts


def _router_majority_label(rows: list[dict[str, Any]]) -> str:
    counts = _router_class_counts(rows)
    if not counts:
        return _ROUTER_LABELS[0]
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def _router_gini(counts: Mapping[str, int]) -> float:
    total = sum(max(0, count) for count in counts.values())
    if total <= 0:
        return 0.0
    impurity = 1.0
    for count in counts.values():
        probability = count / total
        impurity -= probability * probability
    return impurity


def _router_best_split(
    rows: list[dict[str, Any]],
    *,
    feature_names: tuple[str, ...],
) -> tuple[str, float, list[dict[str, Any]], list[dict[str, Any]], float] | None:
    if len(rows) < 2:
        return None

    parent_counts = _router_class_counts(rows)
    parent_gini = _router_gini(parent_counts)
    best: (
        tuple[str, float, list[dict[str, Any]], list[dict[str, Any]], float] | None
    ) = None

    for feature_name in feature_names:
        values = sorted({float(row["features"][feature_name]) for row in rows})
        if len(values) < 2:
            continue
        thresholds = sorted(
            {(left + right) / 2.0 for left, right in zip(values, values[1:])}
        )
        for threshold in thresholds:
            left_rows = [
                row for row in rows if float(row["features"][feature_name]) <= threshold
            ]
            right_rows = [
                row for row in rows if float(row["features"][feature_name]) > threshold
            ]
            if not left_rows or not right_rows:
                continue

            left_counts = _router_class_counts(left_rows)
            right_counts = _router_class_counts(right_rows)
            weighted_gini = len(left_rows) / len(rows) * _router_gini(
                left_counts
            ) + len(right_rows) / len(rows) * _router_gini(right_counts)
            if weighted_gini >= parent_gini:
                continue

            candidate = (feature_name, threshold, left_rows, right_rows, weighted_gini)
            if best is None or candidate[4] < best[4] - 1e-12:
                best = candidate
                continue
            if best is not None and abs(candidate[4] - best[4]) <= 1e-12:
                candidate_balance = abs(len(left_rows) - len(right_rows))
                best_balance = abs(len(best[2]) - len(best[3]))
                if candidate_balance < best_balance:
                    best = candidate
                elif candidate_balance == best_balance and (
                    feature_name,
                    threshold,
                ) < (best[0], best[1]):
                    best = candidate

    return best


def _train_router_tree(
    rows: list[dict[str, Any]],
    *,
    feature_names: tuple[str, ...],
    max_depth: int,
    depth: int = 0,
) -> dict[str, Any]:
    if not rows:
        return {
            "node_type": "leaf",
            "prediction": _ROUTER_LABELS[0],
            "class_counts": {},
            "sample_count": 0,
        }

    class_counts = _router_class_counts(rows)
    prediction = _router_majority_label(rows)
    node: dict[str, Any] = {
        "node_type": "leaf",
        "prediction": prediction,
        "class_counts": dict(sorted(class_counts.items())),
        "sample_count": len(rows),
    }
    if depth >= max_depth or len(class_counts) <= 1:
        return node

    split = _router_best_split(rows, feature_names=feature_names)
    if split is None:
        return node

    feature_name, threshold, left_rows, right_rows, split_score = split
    if split_score >= _router_gini(class_counts) - 1e-12:
        return node

    node.update(
        {
            "node_type": "split",
            "feature": feature_name,
            "threshold": threshold,
            "gini": _router_gini(class_counts),
            "left": _train_router_tree(
                left_rows,
                feature_names=feature_names,
                max_depth=max_depth,
                depth=depth + 1,
            ),
            "right": _train_router_tree(
                right_rows,
                feature_names=feature_names,
                max_depth=max_depth,
                depth=depth + 1,
            ),
        }
    )
    return node


def _predict_router_label(
    tree: Mapping[str, Any], features: Mapping[str, float]
) -> str:
    node: Mapping[str, Any] = tree
    while node.get("node_type") == "split":
        feature_name = _coerce_str(node.get("feature"))
        threshold = float(node.get("threshold", 0.0))
        next_node = node.get("left")
        if float(features.get(feature_name, 0.0)) > threshold:
            next_node = node.get("right")
        if not isinstance(next_node, Mapping):
            break
        node = next_node
    prediction = _coerce_str(node.get("prediction"))
    return prediction if prediction in _ROUTER_LABELS else _ROUTER_LABELS[0]


def _prepare_router_training_rows(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for row in rows:
        label = _router_row_label(row)
        encoded_features, bundle_metadata = _router_feature_vector(row)
        prepared.append(
            {
                **row,
                "row": row,
                "instance_id": _coerce_str(row.get("instance_id")),
                "label": label,
                "route_label": label,
                "features": encoded_features,
                "bundle_metadata": bundle_metadata,
                "task_feature_version": _coerce_str(row.get("task_feature_version"))
                or _coerce_str(bundle_metadata.get("task_feature_version")),
                "task_feature_hash": _coerce_str(row.get("task_feature_hash"))
                or _coerce_str(bundle_metadata.get("task_feature_hash")),
                "task_cluster": _coerce_str(row.get("task_cluster"))
                or _coerce_str(bundle_metadata.get("task_cluster")),
            }
        )
    return prepared


def _build_router_model_payload(
    rows: list[dict[str, Any]],
    *,
    seed: int,
    train_fraction: float,
    validation_fraction: float,
    max_depth: int,
) -> dict[str, Any]:
    if train_fraction <= 0 or train_fraction >= 1:
        raise RuntimeError("train_fraction must be between 0 and 1")
    if validation_fraction < 0 or validation_fraction >= 1:
        raise RuntimeError("validation_fraction must be between 0 and 1")
    if train_fraction + validation_fraction >= 1:
        raise RuntimeError("train_fraction + validation_fraction must be < 1")
    if max_depth < 1:
        raise RuntimeError("max_depth must be >= 1")
    if not rows:
        raise RuntimeError("router training data contained no rows")

    task_feature_versions = {
        _coerce_str(row.get("task_feature_version"))
        for row in rows
        if _coerce_str(row.get("task_feature_version"))
    }
    if len(task_feature_versions) > 1:
        raise RuntimeError(
            "router training rows must share a single task feature version"
        )
    task_feature_version = next(iter(task_feature_versions), "")
    if not task_feature_version:
        task_feature_version = _coerce_str(rows[0].get("task_feature_version"))

    prepared_rows = _prepare_router_training_rows(rows)
    split = _router_split_rows(
        prepared_rows,
        seed=seed,
        train_fraction=train_fraction,
        validation_fraction=validation_fraction,
    )
    train_rows = split["train"]
    validation_rows = split["validation"]
    test_rows = split["test"]
    if not train_rows:
        raise RuntimeError("router training split produced no training rows")

    feature_names = _router_feature_names()
    candidate_depths = tuple(range(1, max(1, max_depth) + 1))
    if validation_rows:
        validation_scores: list[dict[str, Any]] = []
        best_depth = candidate_depths[0]
        best_accuracy = -1.0
        for depth in candidate_depths:
            tree = _train_router_tree(
                train_rows, feature_names=feature_names, max_depth=depth
            )
            accuracy = _router_accuracy(validation_rows, tree)
            validation_scores.append({"depth": depth, "accuracy": accuracy})
            if accuracy > best_accuracy + 1e-12 or (
                abs(accuracy - best_accuracy) <= 1e-12 and depth < best_depth
            ):
                best_depth = depth
                best_accuracy = accuracy
        fit_rows = train_rows + validation_rows
    else:
        validation_scores = []
        best_depth = candidate_depths[0]
        best_accuracy = -1.0
        for depth in candidate_depths:
            tree = _train_router_tree(
                train_rows, feature_names=feature_names, max_depth=depth
            )
            accuracy = _router_accuracy(train_rows, tree)
            validation_scores.append({"depth": depth, "accuracy": accuracy})
            if accuracy > best_accuracy + 1e-12 or (
                abs(accuracy - best_accuracy) <= 1e-12 and depth < best_depth
            ):
                best_depth = depth
                best_accuracy = accuracy
        fit_rows = train_rows

    tree = _train_router_tree(
        fit_rows, feature_names=feature_names, max_depth=best_depth
    )
    dataset_version = _router_dataset_version(rows)

    return {
        "model_version": ROUTER_MODEL_VERSION,
        "feature_space_version": ROUTER_FEATURE_SPACE_VERSION,
        "task_feature_version": task_feature_version,
        "dataset_version": dataset_version,
        "seed": seed,
        "split": {
            "train_fraction": train_fraction,
            "validation_fraction": validation_fraction,
            "test_fraction": max(0.0, 1.0 - train_fraction - validation_fraction),
            "train_count": len(train_rows),
            "validation_count": len(validation_rows),
            "test_count": len(test_rows),
            "train_label_counts": dict(
                sorted(_router_class_counts(train_rows).items())
            ),
            "validation_label_counts": dict(
                sorted(_router_class_counts(validation_rows).items())
            ),
            "test_label_counts": dict(sorted(_router_class_counts(test_rows).items())),
        },
        "feature_names": feature_names,
        "candidate_depths": list(candidate_depths),
        "validation_scores": validation_scores,
        "selected_depth": best_depth,
        "validation_accuracy": best_accuracy,
        "tree": tree,
        "train_count": len(train_rows),
        "validation_count": len(validation_rows),
        "test_count": len(test_rows),
        "row_count": len(rows),
    }


def _router_dataset_version(rows: list[dict[str, Any]]) -> str:
    identity_rows = [
        {
            "instance_id": _coerce_str(row.get("instance_id")),
            "route_label": _coerce_str(row.get("route_label")),
            "task_feature_version": _coerce_str(row.get("task_feature_version")),
            "task_feature_hash": _coerce_str(row.get("task_feature_hash")),
            "cheap_solver_id": _coerce_str(row.get("cheap_solver_id")),
            "expensive_solver_id": _coerce_str(row.get("expensive_solver_id")),
            "oracle_solver_id": _coerce_str(row.get("oracle_solver_id")),
            "task_cluster": _coerce_str(row.get("task_cluster")),
        }
        for row in sorted(
            rows,
            key=lambda item: (
                _coerce_str(item.get("instance_id")),
                _coerce_str(item.get("solver_id")),
            ),
        )
    ]
    return hashlib.sha256(
        json.dumps(identity_rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _router_accuracy(rows: list[dict[str, Any]], tree: Mapping[str, Any]) -> float:
    if not rows:
        return 0.0
    correct = 0
    for row in rows:
        features = _router_feature_vector(row["row"])[0]
        if _predict_router_label(tree, features) == row["label"]:
            correct += 1
    return correct / len(rows)


def _build_learned_router_rows(
    rows: list[dict[str, Any]],
    model: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    learned_rows: list[dict[str, Any]] = []
    prediction_counts: Counter[str] = Counter()
    correct = 0
    for row in rows:
        features = _router_feature_vector(row)[0]
        predicted_label = _predict_router_label(model["tree"], features)
        prediction_counts[predicted_label] += 1
        true_label = _router_row_label(row)
        if predicted_label == true_label:
            correct += 1
        use_expensive = predicted_label == "needs_expensive"
        prefix = "expensive" if use_expensive else "cheap"
        learned_rows.append(
            {
                **row,
                "predicted_route_label": predicted_label,
                "route_label_correct": predicted_label == true_label,
                "policy_learned_router_resolved": _coerce_bool(
                    row.get(f"{prefix}_resolved")
                ),
                "policy_learned_router_cost_usd": _coerce_float(
                    row.get(f"{prefix}_cost_usd")
                ),
                "policy_learned_router_latency_ms": _coerce_int(
                    row.get(f"{prefix}_latency_ms")
                ),
                "policy_learned_router_escalated": use_expensive,
                "policy_learned_router_solver_id": _coerce_str(
                    row.get(f"{prefix}_solver_id")
                ),
            }
        )

    learned_summary = _summarize_policy(learned_rows, "policy_learned_router")
    total = len(rows)
    learned_summary.update(
        {
            "route_label_accuracy": correct / total if total else 0.0,
            "route_label_correct": correct,
            "route_label_total": total,
            "predicted_label_counts": dict(sorted(prediction_counts.items())),
            "model_version": model.get("model_version"),
            "feature_space_version": model.get("feature_space_version"),
            "task_feature_version": model.get("task_feature_version"),
            "dataset_version": model.get("dataset_version"),
            "selected_depth": model.get("selected_depth"),
            "validation_accuracy": model.get("validation_accuracy"),
            "candidate_depths": model.get("candidate_depths", []),
            "validation_scores": model.get("validation_scores", []),
            "split": model.get("split", {}),
            "feature_names": model.get("feature_names", []),
        }
    )
    return learned_rows, learned_summary


def write_router_model(path: Path, model: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(model, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_router_model(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def train_router_model(
    rows: list[dict[str, Any]],
    *,
    seed: int = 0,
    train_fraction: float = 0.8,
    validation_fraction: float = 0.1,
    max_depth: int = 3,
) -> dict[str, Any]:
    return _build_router_model_payload(
        rows,
        seed=seed,
        train_fraction=train_fraction,
        validation_fraction=validation_fraction,
        max_depth=max_depth,
    )


def evaluate_router_model(
    rows: list[dict[str, Any]],
    model: Mapping[str, Any],
) -> dict[str, Any]:
    _, learned_summary = _build_learned_router_rows(rows, model)
    return learned_summary


def build_router_training_rows(
    attempt_rows: list[Mapping[str, Any]],
    instance_results: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Build one router-training row per instance."""
    if not attempt_rows:
        return []

    from repogauge.runner.analyze import join_attempt_rows

    joined_rows = join_attempt_rows(
        [dict(row) for row in attempt_rows], [dict(row) for row in instance_results]
    )
    if not joined_rows:
        return []

    solver_instance_rows = _aggregate_solver_instance_rows(joined_rows)
    ranked_solvers = _rank_solvers(solver_instance_rows)
    if not ranked_solvers:
        return []

    cheap_solver_id = ranked_solvers[0]["solver_id"]
    expensive_solver_id = ranked_solvers[-1]["solver_id"]
    solver_order = [item["solver_id"] for item in ranked_solvers]
    solver_rank_costs = [item["average_cost_usd"] for item in ranked_solvers]
    solver_rank_latencies = [item["average_duration_ms"] for item in ranked_solvers]

    by_instance: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in solver_instance_rows:
        by_instance[_coerce_str(row.get("instance_id"))].append(row)

    router_rows: list[dict[str, Any]] = []
    for instance_id in sorted(by_instance):
        solver_rows = sorted(
            by_instance[instance_id],
            key=lambda row: (_coerce_str(row.get("solver_id")),),
        )
        cheap_row = _pick_solver_row(solver_rows, cheap_solver_id)
        expensive_row = _pick_solver_row(
            solver_rows, expensive_solver_id, prefer_last=True
        )
        if cheap_row is None and expensive_row is None:
            continue
        if cheap_row is None:
            cheap_row = expensive_row
        if expensive_row is None:
            expensive_row = cheap_row
        assert cheap_row is not None
        assert expensive_row is not None

        instance_meta = cheap_row or expensive_row
        task_features = build_task_feature_bundle(instance_meta)
        resolved_rows = [
            row for row in solver_rows if _coerce_bool(row.get("resolved"))
        ]
        oracle_row = (
            min(
                resolved_rows,
                key=lambda row: (
                    _coerce_float(row.get("total_cost_usd")),
                    _coerce_float(row.get("total_duration_ms")),
                    _coerce_str(row.get("solver_id")),
                ),
            )
            if resolved_rows
            else None
        )
        solver_outcomes = [
            {
                "solver_id": row["solver_id"],
                "attempt_count": row["attempt_count"],
                "resolved": row["resolved"],
                "harness_outcome": row["harness_outcome"],
                "attempt_state": row["attempt_state"],
                "cost_usd": row["total_cost_usd"],
                "latency_ms": row["total_duration_ms"],
                "failure_reason": row["failure_reason"],
                "exit_reason": row["exit_reason"],
                "prompt_policy_hash": row["prompt_policy_hash"],
                "tool_policy_hash": row["tool_policy_hash"],
                "solver_config_hash": row["solver_config_hash"],
            }
            for row in solver_rows
        ]
        policy_signal = _policy_trigger_on_signal(cheap_row)
        cheap_resolved = _coerce_bool(cheap_row.get("resolved"))
        expensive_resolved = _coerce_bool(expensive_row.get("resolved"))
        cheap_policy = _policy_result(
            resolved=cheap_resolved,
            cost_usd=_coerce_float(cheap_row.get("total_cost_usd")),
            latency_ms=_coerce_int(cheap_row.get("total_duration_ms")),
            escalated=False,
            solver_id=cheap_solver_id,
        )
        expensive_policy = _policy_result(
            resolved=expensive_resolved,
            cost_usd=_coerce_float(expensive_row.get("total_cost_usd")),
            latency_ms=_coerce_int(expensive_row.get("total_duration_ms")),
            escalated=False,
            solver_id=expensive_solver_id,
        )
        failure_escalated = not cheap_resolved
        failure_policy = _policy_result(
            resolved=cheap_resolved or expensive_resolved,
            cost_usd=_coerce_float(cheap_row.get("total_cost_usd"))
            + (
                _coerce_float(expensive_row.get("total_cost_usd"))
                if failure_escalated
                else 0.0
            ),
            latency_ms=_coerce_int(cheap_row.get("total_duration_ms"))
            + (
                _coerce_int(expensive_row.get("total_duration_ms"))
                if failure_escalated
                else 0
            ),
            escalated=failure_escalated,
            solver_id=expensive_solver_id if failure_escalated else cheap_solver_id,
        )
        signal_escalated = policy_signal
        signal_policy = _policy_result(
            resolved=cheap_resolved or (signal_escalated and expensive_resolved),
            cost_usd=_coerce_float(cheap_row.get("total_cost_usd"))
            + (
                _coerce_float(expensive_row.get("total_cost_usd"))
                if signal_escalated
                else 0.0
            ),
            latency_ms=_coerce_int(cheap_row.get("total_duration_ms"))
            + (
                _coerce_int(expensive_row.get("total_duration_ms"))
                if signal_escalated
                else 0
            ),
            escalated=signal_escalated,
            solver_id=expensive_solver_id if signal_escalated else cheap_solver_id,
        )
        route_label = "cheap_is_enough"
        if not cheap_resolved and expensive_resolved:
            route_label = "needs_expensive"
        elif not cheap_resolved and not expensive_resolved:
            route_label = "likely_unsolved"

        router_rows.append(
            {
                "instance_id": instance_id,
                "repo": _instance_value(instance_meta, "repo", "instance_repo"),
                "base_commit": _instance_value(
                    instance_meta, "base_commit", "instance_base_commit"
                ),
                "version": _instance_value(
                    instance_meta, "version", "instance_version"
                ),
                "problem_statement": _coerce_str(
                    instance_meta.get("problem_statement")
                ),
                "task_feature_version": task_features.feature_version,
                "task_feature_hash": task_features.feature_hash,
                "task_cluster": task_features.cluster_label,
                "task_features": task_features.features,
                "solver_count": len(solver_rows),
                "resolved_solver_count": len(resolved_rows),
                "solver_ranked_ids": solver_order,
                "solver_ranked_average_cost_usd": solver_rank_costs,
                "solver_ranked_average_latency_ms": solver_rank_latencies,
                "cheap_solver_id": cheap_solver_id,
                "cheap_resolved": cheap_policy["resolved"],
                "cheap_cost_usd": cheap_policy["cost_usd"],
                "cheap_latency_ms": cheap_policy["latency_ms"],
                "cheap_attempt_state": _coerce_str(cheap_row.get("attempt_state")),
                "cheap_harness_outcome": _coerce_str(cheap_row.get("harness_outcome")),
                "cheap_failure_reason": cheap_row.get("failure_reason"),
                "cheap_exit_reason": _coerce_str(cheap_row.get("exit_reason")),
                "cheap_prompt_policy_hash": cheap_row.get("prompt_policy_hash"),
                "cheap_tool_policy_hash": cheap_row.get("tool_policy_hash"),
                "cheap_solver_config_hash": cheap_row.get("solver_config_hash"),
                "expensive_solver_id": expensive_solver_id,
                "expensive_resolved": expensive_policy["resolved"],
                "expensive_cost_usd": expensive_policy["cost_usd"],
                "expensive_latency_ms": expensive_policy["latency_ms"],
                "expensive_attempt_state": _coerce_str(
                    expensive_row.get("attempt_state")
                ),
                "expensive_harness_outcome": _coerce_str(
                    expensive_row.get("harness_outcome")
                ),
                "expensive_failure_reason": expensive_row.get("failure_reason"),
                "expensive_exit_reason": _coerce_str(expensive_row.get("exit_reason")),
                "expensive_prompt_policy_hash": expensive_row.get("prompt_policy_hash"),
                "expensive_tool_policy_hash": expensive_row.get("tool_policy_hash"),
                "expensive_solver_config_hash": expensive_row.get("solver_config_hash"),
                "oracle_resolved": _coerce_bool(oracle_row.get("resolved"))
                if oracle_row
                else False,
                "oracle_solver_id": _coerce_str(oracle_row.get("solver_id"))
                if oracle_row
                else None,
                "oracle_cost_usd": _coerce_float(oracle_row.get("total_cost_usd"))
                if oracle_row and _coerce_bool(oracle_row.get("resolved"))
                else None,
                "oracle_latency_ms": _coerce_int(oracle_row.get("total_duration_ms"))
                if oracle_row and _coerce_bool(oracle_row.get("resolved"))
                else None,
                "oracle_harness_outcome": _coerce_str(oracle_row.get("harness_outcome"))
                if oracle_row
                else "",
                "policy_always_cheap_resolved": cheap_policy["resolved"],
                "policy_always_cheap_cost_usd": cheap_policy["cost_usd"],
                "policy_always_cheap_latency_ms": cheap_policy["latency_ms"],
                "policy_always_cheap_escalated": False,
                "policy_always_expensive_resolved": expensive_policy["resolved"],
                "policy_always_expensive_cost_usd": expensive_policy["cost_usd"],
                "policy_always_expensive_latency_ms": expensive_policy["latency_ms"],
                "policy_always_expensive_escalated": False,
                "policy_cheap_then_escalate_on_failure_resolved": failure_policy[
                    "resolved"
                ],
                "policy_cheap_then_escalate_on_failure_cost_usd": failure_policy[
                    "cost_usd"
                ],
                "policy_cheap_then_escalate_on_failure_latency_ms": failure_policy[
                    "latency_ms"
                ],
                "policy_cheap_then_escalate_on_failure_escalated": failure_policy[
                    "escalated"
                ],
                "policy_cheap_then_escalate_on_invalid_patch_timeout_no_progress_resolved": signal_policy[
                    "resolved"
                ],
                "policy_cheap_then_escalate_on_invalid_patch_timeout_no_progress_cost_usd": signal_policy[
                    "cost_usd"
                ],
                "policy_cheap_then_escalate_on_invalid_patch_timeout_no_progress_latency_ms": signal_policy[
                    "latency_ms"
                ],
                "policy_cheap_then_escalate_on_invalid_patch_timeout_no_progress_escalated": signal_policy[
                    "escalated"
                ],
                "policy_signal_triggered": policy_signal,
                "policy_cheap_then_escalate_on_invalid_patch_timeout_no_progress_triggered": policy_signal,
                "route_label": route_label,
                "solver_outcomes": solver_outcomes,
                "metadata": {
                    **_coerce_mapping(instance_meta.get("metadata")),
                    "cheap_solver_id": cheap_solver_id,
                    "expensive_solver_id": expensive_solver_id,
                    "solver_ranked_ids": solver_order,
                    "oracle_solver_id": _coerce_str(oracle_row.get("solver_id"))
                    if oracle_row
                    else None,
                    "policy_assumption": (
                        "cheap and expensive are inferred from historical average solver cost"
                    ),
                },
            }
        )

    return router_rows


def write_router_training_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    _write_rows(path, rows)


def load_router_training_rows(path: Path) -> list[dict[str, Any]]:
    return _read_rows(path)


def _summarize_policy(rows: list[dict[str, Any]], prefix: str) -> dict[str, Any]:
    resolved_count = 0
    total_cost = 0.0
    total_latency = 0
    escalated_count = 0
    latencies: list[int] = []
    oracle_cost_regret_total = 0.0
    oracle_cost_regret_count = 0
    oracle_latency_regret_total = 0.0
    oracle_latency_regret_count = 0
    for row in rows:
        if _coerce_bool(row.get(f"{prefix}_resolved")):
            resolved_count += 1
        cost_usd = _coerce_float(row.get(f"{prefix}_cost_usd"))
        latency_ms = _coerce_int(row.get(f"{prefix}_latency_ms"))
        total_cost += cost_usd
        total_latency += latency_ms
        latencies.append(latency_ms)
        if _coerce_bool(row.get(f"{prefix}_escalated")):
            escalated_count += 1
        oracle_cost = row.get("oracle_cost_usd")
        oracle_latency = row.get("oracle_latency_ms")
        if oracle_cost is not None:
            try:
                oracle_cost_value = float(oracle_cost)
            except (TypeError, ValueError):
                oracle_cost_value = None
            else:
                if cost_usd >= 0.0:
                    oracle_cost_regret_total += max(0.0, cost_usd - oracle_cost_value)
                    oracle_cost_regret_count += 1
        if oracle_latency is not None:
            try:
                oracle_latency_value = float(oracle_latency)
            except (TypeError, ValueError):
                oracle_latency_value = None
            else:
                oracle_latency_regret_total += max(
                    0.0, float(latency_ms) - oracle_latency_value
                )
                oracle_latency_regret_count += 1

    total_instances = len(rows)
    oracle_resolved_count = sum(
        1 for row in rows if _coerce_bool(row.get("oracle_resolved"))
    )
    oracle_resolve_rate = (
        oracle_resolved_count / total_instances if total_instances else 0.0
    )
    resolve_rate = resolved_count / total_instances if total_instances else 0.0

    return {
        "policy": prefix.removeprefix("policy_"),
        "instance_count": total_instances,
        "resolved_count": resolved_count,
        "resolve_rate": resolve_rate,
        "total_cost_usd": total_cost,
        "average_cost_usd": total_cost / total_instances if total_instances else 0.0,
        "total_latency_ms": total_latency,
        "average_latency_ms": total_latency / total_instances
        if total_instances
        else 0.0,
        "p95_latency_ms": _percentile(latencies, 0.95),
        "escalated_count": escalated_count,
        "escalation_rate": escalated_count / total_instances
        if total_instances
        else 0.0,
        "oracle_resolved_count": oracle_resolved_count,
        "oracle_resolve_rate": oracle_resolve_rate,
        "resolve_gap_vs_oracle": oracle_resolve_rate - resolve_rate,
        "resolved_gap_vs_oracle": oracle_resolved_count - resolved_count,
        "average_regret_vs_oracle_cost_usd": (
            oracle_cost_regret_total / oracle_cost_regret_count
            if oracle_cost_regret_count
            else 0.0
        ),
        "oracle_cost_regret_count": oracle_cost_regret_count,
        "average_regret_vs_oracle_latency_ms": (
            oracle_latency_regret_total / oracle_latency_regret_count
            if oracle_latency_regret_count
            else 0.0
        ),
        "oracle_latency_regret_count": oracle_latency_regret_count,
    }


def evaluate_router_baselines(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize baseline routing policies from router training rows."""
    if not rows:
        return {
            "instance_count": 0,
            "policies": [],
            "cheap_solver_id": None,
            "expensive_solver_id": None,
            "solver_ranked_ids": [],
            "solver_ranked_average_cost_usd": [],
            "solver_ranked_average_latency_ms": [],
            "notes": [
                "router training file contained no rows",
            ],
        }

    ordered_rows = sorted(rows, key=lambda row: _coerce_str(row.get("instance_id")))
    first = ordered_rows[0]
    policies = [
        _summarize_policy(ordered_rows, "policy_always_cheap"),
        _summarize_policy(ordered_rows, "policy_always_expensive"),
        _summarize_policy(ordered_rows, "policy_cheap_then_escalate_on_failure"),
        _summarize_policy(
            ordered_rows,
            "policy_cheap_then_escalate_on_invalid_patch_timeout_no_progress",
        ),
    ]

    return {
        "instance_count": len(ordered_rows),
        "cheap_solver_id": first.get("cheap_solver_id"),
        "expensive_solver_id": first.get("expensive_solver_id"),
        "solver_ranked_ids": first.get("solver_ranked_ids", []),
        "solver_ranked_average_cost_usd": first.get(
            "solver_ranked_average_cost_usd", []
        ),
        "solver_ranked_average_latency_ms": first.get(
            "solver_ranked_average_latency_ms", []
        ),
        "task_feature_version": first.get("task_feature_version"),
        "task_feature_hashes": sorted(
            {
                _coerce_str(row.get("task_feature_hash"))
                for row in ordered_rows
                if _coerce_str(row.get("task_feature_hash"))
            }
        ),
        "task_clusters": sorted(
            {
                _coerce_str(row.get("task_cluster"))
                for row in ordered_rows
                if _coerce_str(row.get("task_cluster"))
            }
        ),
        "policies": policies,
        "notes": [
            "cheap and expensive solver roles are inferred from historical average cost within the run",
            "always-expensive and escalation policies are evaluated offline from the exported router table",
        ],
    }


def write_router_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def run_router_training(
    router_train_path: str | Path,
    *,
    out_root: str | Path | None = None,
    seed: int = 0,
    train_fraction: float = 0.8,
    validation_fraction: float = 0.1,
    max_depth: int = 3,
) -> dict[str, Any]:
    """Read router training rows, evaluate baselines, and persist a report."""
    source = Path(router_train_path).resolve()
    if not source.exists():
        raise FileNotFoundError(f"router training file not found: {source}")

    out_root_path = Path(out_root).resolve() if out_root else source.parent
    out_root_path.mkdir(parents=True, exist_ok=True)

    rows = load_router_training_rows(source)
    model = _build_router_model_payload(
        rows,
        seed=seed,
        train_fraction=train_fraction,
        validation_fraction=validation_fraction,
        max_depth=max_depth,
    )
    model_path = out_root_path / "router_model.json"
    write_router_model(model_path, model)

    split = model.get("split", {})
    eval_rows = (
        model.get("test_count", 0)
        and _router_split_rows(
            _prepare_router_training_rows(rows),
            seed=seed,
            train_fraction=train_fraction,
            validation_fraction=validation_fraction,
        )["test"]
        or []
    )
    if not eval_rows:
        split_rows = _router_split_rows(
            _prepare_router_training_rows(rows),
            seed=seed,
            train_fraction=train_fraction,
            validation_fraction=validation_fraction,
        )
        eval_rows = split_rows["validation"] or split_rows["train"]

    baseline_report = evaluate_router_baselines(eval_rows)
    learned_rows, learned_summary = _build_learned_router_rows(eval_rows, model)
    report = {
        **baseline_report,
        "dataset_instance_count": len(rows),
        "split": split,
        "model": {
            "model_version": model.get("model_version"),
            "feature_space_version": model.get("feature_space_version"),
            "task_feature_version": model.get("task_feature_version"),
            "dataset_version": model.get("dataset_version"),
            "seed": seed,
            "selected_depth": model.get("selected_depth"),
            "validation_accuracy": model.get("validation_accuracy"),
            "candidate_depths": model.get("candidate_depths", []),
            "validation_scores": model.get("validation_scores", []),
            "feature_names": model.get("feature_names", []),
            "row_count": model.get("row_count", len(rows)),
        },
        "learned_router": learned_summary,
        "learned_router_sample": [
            {
                "instance_id": _coerce_str(row.get("instance_id")),
                "route_label": _coerce_str(row.get("route_label")),
                "predicted_route_label": _coerce_str(row.get("predicted_route_label")),
                "policy_learned_router_resolved": _coerce_bool(
                    row.get("policy_learned_router_resolved")
                ),
                "policy_learned_router_cost_usd": _coerce_float(
                    row.get("policy_learned_router_cost_usd")
                ),
                "policy_learned_router_latency_ms": _coerce_int(
                    row.get("policy_learned_router_latency_ms")
                ),
                "policy_learned_router_escalated": _coerce_bool(
                    row.get("policy_learned_router_escalated")
                ),
            }
            for row in learned_rows[:5]
        ],
    }
    report_path = out_root_path / "router_report.json"
    write_router_report(report_path, report)

    return {
        "router_train_path": str(source),
        "router_model_path": str(model_path),
        "router_report_path": str(report_path),
        "instance_count": len(rows),
        "model": model,
        "report": report,
    }


__all__ = [
    "build_router_training_rows",
    "evaluate_router_baselines",
    "evaluate_router_model",
    "load_router_training_rows",
    "load_router_model",
    "run_router_training",
    "ROUTER_MODEL_VERSION",
    "train_router_model",
    "write_router_model",
    "write_router_report",
    "write_router_training_rows",
]
