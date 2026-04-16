"""Deterministic analysis helpers for solver attempts and judge outcomes."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .features import build_task_feature_bundle


@dataclass(frozen=True)
class ResolutionMetrics:
    """Compact summary for a grouped set of joined attempt/eval rows."""

    group: tuple[tuple[str, str], ...]
    attempt_count: int
    unique_instance_count: int
    resolved_instance_count: int
    raw_resolution_rate: float
    total_duration_ms: int
    resolved_duration_ms: int
    total_cost_usd: float
    resolved_cost_usd: float
    cost_per_resolved_issue: float | None
    latency_ms_per_resolved_issue: float | None
    expensive_coverage: float
    exclusive_expensive_win_rate: float
    marginal_cost_per_extra_resolve: float | None


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y", "passed", "resolved"}
    if isinstance(value, (int, float)):
        return value != 0
    return False


def _coerce_str(value: Any) -> str:
    return "" if value is None else str(value)


def _coerce_non_negative_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _coerce_non_negative_float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if parsed > 0 else 0.0


def _read_row_cost(row: Mapping[str, Any]) -> float | None:
    cost = row.get("cost", {})
    if not isinstance(cost, Mapping):
        return None
    for key in ("total_cost", "usd", "value", "amount", "total_usd"):
        if key in cost:
            cost_value = cost.get(key)
            if cost_value is None:
                continue
            try:
                return float(cost_value)
            except (TypeError, ValueError):
                continue
    return None


def _read_group_value(row: Mapping[str, Any], field: str) -> str:
    if field in row:
        return _coerce_str(row.get(field))
    metadata = row.get("metadata")
    if isinstance(metadata, Mapping) and field in metadata:
        return _coerce_str(metadata.get(field))
    return "unknown"


def _safe_rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def _safe_cost(values: list[float]) -> float:
    total = 0.0
    for value in values:
        total += max(0.0, float(value))
    return total


def load_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    """Load newline-delimited JSON rows from a file.

    The parser keeps rows deterministic and raises only when input is malformed.
    """
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value:
            continue
        rows.append(json.loads(value))
    return rows


def load_attempt_rows(path: Path) -> list[dict[str, Any]]:
    """Load solver attempt telemetry rows from JSONL or Parquet."""
    if path.suffix == ".parquet":
        try:
            import pyarrow.parquet as pq  # type: ignore

            table = pq.read_table(path)
            return [
                {key: value for key, value in row.items() if key is not None}
                for row in table.to_pylist()
            ]
        except Exception:
            # Scheduler writes JSONL fallback when parquet serialization is not
            # available, so keep this tolerant for both code paths.
            return load_jsonl_rows(path)
    return load_jsonl_rows(path)


def load_instance_result_rows(path: Path) -> list[dict[str, Any]]:
    """Load judge instance results from JSONL."""
    return load_jsonl_rows(path)


def _resolution_summary_to_dict(
    summary: ResolutionMetrics,
) -> dict[str, Any]:
    payload = asdict(summary)
    payload["group"] = [
        {"dimension": dimension, "value": value} for dimension, value in summary.group
    ]
    payload["group_by"] = [dimension for dimension, _ in summary.group]
    payload["group_values"] = {dimension: value for dimension, value in summary.group}
    payload["group_json"] = json.dumps(payload["group"])
    return payload


def _build_group_csv_row(
    summary: ResolutionMetrics,
    group_by: tuple[str, ...],
    payload: dict[str, Any],
) -> dict[str, Any]:
    row = {**payload["group_values"]}
    for column in group_by:
        row.setdefault(column, "")
    row.update(
        {
            "group_json": payload["group_json"],
            "attempt_count": summary.attempt_count,
            "unique_instance_count": summary.unique_instance_count,
            "resolved_instance_count": summary.resolved_instance_count,
            "raw_resolution_rate": summary.raw_resolution_rate,
            "total_duration_ms": summary.total_duration_ms,
            "resolved_duration_ms": summary.resolved_duration_ms,
            "total_cost_usd": summary.total_cost_usd,
            "resolved_cost_usd": summary.resolved_cost_usd,
            "cost_per_resolved_issue": summary.cost_per_resolved_issue,
            "latency_ms_per_resolved_issue": summary.latency_ms_per_resolved_issue,
            "expensive_coverage": summary.expensive_coverage,
            "exclusive_expensive_win_rate": summary.exclusive_expensive_win_rate,
            "marginal_cost_per_extra_resolve": summary.marginal_cost_per_extra_resolve,
        }
    )
    return row


def write_summary_json(
    path: Path,
    summaries: list[ResolutionMetrics],
    metadata: dict[str, Any] | None = None,
) -> None:
    """Serialize summary records for API/automation consumption."""
    payload: dict[str, Any] = {
        "summary": [_resolution_summary_to_dict(summary) for summary in summaries],
        "metadata": metadata or {},
    }
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def write_summary_csv(
    path: Path,
    summaries: list[ResolutionMetrics],
    group_by: tuple[str, ...],
) -> None:
    """Serialize summary as deterministic CSV."""
    metric_fields = [
        "attempt_count",
        "unique_instance_count",
        "resolved_instance_count",
        "raw_resolution_rate",
        "total_duration_ms",
        "resolved_duration_ms",
        "total_cost_usd",
        "resolved_cost_usd",
        "cost_per_resolved_issue",
        "latency_ms_per_resolved_issue",
        "expensive_coverage",
        "exclusive_expensive_win_rate",
        "marginal_cost_per_extra_resolve",
        "group_json",
    ]

    fieldnames = tuple(group_by) + tuple(metric_fields)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for summary in summaries:
            payload = _resolution_summary_to_dict(summary)
            row = _build_group_csv_row(summary, group_by, payload)
            writer.writerow(row)


def write_summary_parquet(
    path: Path,
    summaries: list[ResolutionMetrics],
    group_by: tuple[str, ...],
) -> None:
    """Serialize summary as Parquet when available, JSONL fallback otherwise."""
    payload = [_resolution_summary_to_dict(summary) for summary in summaries]
    rows = [
        _build_group_csv_row(summary, group_by, _resolution_summary_to_dict(summary))
        for summary in summaries
    ]

    try:
        import pyarrow as pa
        import pyarrow.parquet as pq  # type: ignore

        table = pa.Table.from_pylist(payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, str(path))
        return
    except Exception:
        # Deterministic fallback that still creates the requested artifact in
        # environments where optional parquet deps are unavailable.
        path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )


def write_summary_html(
    path: Path,
    summaries: list[ResolutionMetrics],
    group_by: tuple[str, ...],
    metadata: dict[str, Any] | None = None,
) -> None:
    """Generate a tiny static HTML report."""
    metric_headers = [
        "attempt_count",
        "unique_instance_count",
        "resolved_instance_count",
        "raw_resolution_rate",
        "total_duration_ms",
        "resolved_duration_ms",
        "total_cost_usd",
        "resolved_cost_usd",
        "cost_per_resolved_issue",
        "latency_ms_per_resolved_issue",
        "expensive_coverage",
        "exclusive_expensive_win_rate",
        "marginal_cost_per_extra_resolve",
    ]

    rows = [_resolution_summary_to_dict(summary) for summary in summaries]
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        '<html><head><meta charset="utf-8"/>',
        "<title>RepoGauge Analysis</title>",
        "<style>body{font-family:Inter,Arial,sans-serif;max-width:1000px;padding:24px;line-height:1.4}"
        "table{border-collapse:collapse;width:100%;margin-top:12px}"
        "th,td{border:1px solid #ccc;padding:6px;font-size:12px;text-align:left}"
        "th{background:#f5f5f5}",
        "</style>",
        "</head><body>",
    ]
    lines.append("<h1>RepoGauge Analysis Report</h1>")
    if metadata:
        lines.append(
            "<pre>{}</pre>".format(json.dumps(metadata, sort_keys=True, indent=2))
        )
    lines.append("<table>\n")
    headers = list(group_by) + metric_headers
    lines.append("<tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr>")

    for row in rows:
        group_values = {item["dimension"]: item["value"] for item in row["group"]}
        lines.append("<tr>")
        for column in group_by:
            lines.append(f"<td>{group_values.get(column, '')}</td>")
        for header in metric_headers:
            lines.append(f"<td>{row.get(header)}</td>")
        lines.append("</tr>")

    lines.append("</table>")
    lines.append("</body></html>")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def join_attempt_rows(
    attempt_rows: list[Mapping[str, Any]],
    eval_rows: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Join solver attempts to judge rows by (solver_id, instance_id)."""
    eval_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for eval_row in eval_rows:
        key = (
            _coerce_str(eval_row.get("solver_id")),
            _coerce_str(eval_row.get("instance_id")),
        )
        eval_by_key[key] = dict(eval_row)

    joined: list[dict[str, Any]] = []
    for attempt in attempt_rows:
        attempt_solver = _coerce_str(attempt.get("solver_id"))
        attempt_instance = _coerce_str(attempt.get("instance_id"))
        key = (attempt_solver, attempt_instance)
        eval_row = eval_by_key.get(key, {})
        outcome = _coerce_str(eval_row.get("harness_outcome", "unknown"))
        resolved = _coerce_bool(
            eval_row.get("resolved", outcome.lower() in {"passed", "resolved"})
        )

        item = dict(attempt)
        item.update(
            {
                "resolved": resolved,
                "harness_outcome": outcome,
                "failure_reason": eval_row.get("failure_reason"),
                "eval_metadata": eval_row.get("metadata", {}),
            }
        )
        item["attempt_cost_usd"] = _read_row_cost(item)
        item["instance_id"] = attempt_instance

        task_features = build_task_feature_bundle(item)
        item.setdefault("task_feature_version", task_features.feature_version)
        item.setdefault("task_feature_hash", task_features.feature_hash)
        item.setdefault("task_cluster", task_features.cluster_label)
        item.setdefault("task_features", task_features.features)

        existing_metadata = item.get("metadata", {})
        metadata = (
            dict(existing_metadata) if isinstance(existing_metadata, Mapping) else {}
        )
        metadata.update(task_features.to_metadata())
        item["metadata"] = metadata
        joined.append(item)
    return joined


def _group_rows(
    rows: list[dict[str, Any]], dimensions: tuple[str, ...]
) -> dict[tuple[tuple[str, str], ...], list[dict[str, Any]]]:
    grouped: dict[tuple[tuple[str, str], ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = tuple(
            (dimension, _read_group_value(row, dimension)) for dimension in dimensions
        )
        grouped[key].append(row)
    return grouped


def summarize_attempt_metrics(
    *,
    attempts: list[dict[str, Any]],
    instance_results: list[dict[str, Any]],
    group_by: tuple[str, ...] = ("solver_id",),
    expensive_cost_threshold: float = 1.0,
) -> list[ResolutionMetrics]:
    """Return deterministic summary rows grouped by `group_by` dimensions."""
    joined = join_attempt_rows(attempts, instance_results)
    if not joined:
        return []

    grouped = _group_rows(joined, dimensions=group_by)
    summaries: list[ResolutionMetrics] = []

    for group_key, rows in grouped.items():
        instances = {}
        resolved_instances: set[str] = set()
        resolved_duration_ms = 0
        duration_ms_sum = 0
        expensive_resolved_instances = set[str]()
        instance_costs: dict[str, list[float]] = defaultdict(list)

        for row in rows:
            instance_id = _coerce_str(row.get("instance_id"))
            if not instance_id:
                continue
            instances[instance_id] = True
            duration = _coerce_non_negative_int(row.get("duration_ms"))
            duration_ms_sum += duration
            cost = row.get("attempt_cost_usd")
            if isinstance(cost, (int, float)) and cost > 0:
                instance_costs[instance_id].append(float(cost))
            if row.get("resolved"):
                resolved_instances.add(instance_id)
                resolved_duration_ms += duration
                if cost is not None:
                    if float(cost) >= expensive_cost_threshold:
                        expensive_resolved_instances.add(instance_id)

        resolved_instance_count = len(resolved_instances)
        attempt_count = len(rows)
        unique_instance_count = len(instances)
        raw_resolution_rate = _safe_rate(resolved_instance_count, unique_instance_count)
        all_total_cost = _safe_cost(
            [value for costs in instance_costs.values() for value in costs]
        )
        resolved_total_cost = _safe_cost(
            [
                min(costs)
                for instance_id, costs in instance_costs.items()
                if instance_id in resolved_instances and costs
            ]
        )
        cost_per_resolved_issue = (
            resolved_total_cost / resolved_instance_count
            if resolved_instance_count > 0
            else None
        )
        latency_ms_per_resolved_issue = (
            resolved_duration_ms / resolved_instance_count
            if resolved_instance_count > 0
            else None
        )

        exclusive_expensive_instances = 0
        for instance_id in resolved_instances:
            resolved_attempt_costs = sorted(instance_costs.get(instance_id, []))
            if not resolved_attempt_costs:
                continue
            if resolved_attempt_costs[0] >= expensive_cost_threshold and (
                instance_id in expensive_resolved_instances
            ):
                exclusive_expensive_instances += 1

        exclusive_expensive_win_rate = _safe_rate(
            exclusive_expensive_instances, resolved_instance_count
        )
        expensive_coverage = _safe_rate(
            len(expensive_resolved_instances), resolved_instance_count
        )

        marginal_cost_per_extra_resolve = None
        if resolved_instance_count >= 2:
            per_instance_min_costs = [
                min(instance_costs[instance_id])
                for instance_id in resolved_instances
                if instance_costs.get(instance_id)
            ]
            if len(per_instance_min_costs) >= 2:
                per_instance_min_costs.sort()
                deltas = [
                    right - left
                    for left, right in zip(
                        per_instance_min_costs, per_instance_min_costs[1:]
                    )
                ]
                if deltas:
                    marginal_cost_per_extra_resolve = sum(deltas) / len(deltas)

        summaries.append(
            ResolutionMetrics(
                group=group_key,
                attempt_count=attempt_count,
                unique_instance_count=unique_instance_count,
                resolved_instance_count=resolved_instance_count,
                raw_resolution_rate=raw_resolution_rate,
                total_duration_ms=duration_ms_sum,
                resolved_duration_ms=resolved_duration_ms,
                total_cost_usd=all_total_cost,
                resolved_cost_usd=resolved_total_cost,
                cost_per_resolved_issue=cost_per_resolved_issue,
                latency_ms_per_resolved_issue=latency_ms_per_resolved_issue,
                expensive_coverage=expensive_coverage,
                exclusive_expensive_win_rate=exclusive_expensive_win_rate,
                marginal_cost_per_extra_resolve=marginal_cost_per_extra_resolve,
            )
        )

    summaries.sort(key=lambda item: item.group)
    return summaries
