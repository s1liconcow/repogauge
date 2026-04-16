"""Deterministic analysis helpers for solver attempts and judge outcomes."""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from html import escape as html_escape
import json
from dataclasses import asdict, dataclass
from itertools import groupby
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


def _display_number(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return str(value)


def _solver_id_from_row(row: Mapping[str, Any]) -> str:
    group_values = row.get("group_values")
    if isinstance(group_values, Mapping):
        solver_id = _coerce_str(group_values.get("solver_id"))
        if solver_id:
            return solver_id
    return _coerce_str(row.get("solver_id"))


def _metric_value_or_inf(value: Any) -> float:
    if value is None:
        return float("inf")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("inf")


def _solver_ranking_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        -_coerce_non_negative_float(row.get("raw_resolution_rate")),
        -_coerce_non_negative_int(row.get("resolved_instance_count")),
        _metric_value_or_inf(row.get("cost_per_resolved_issue")),
        _metric_value_or_inf(row.get("latency_ms_per_resolved_issue")),
        _coerce_non_negative_float(row.get("total_cost_usd")),
        _coerce_str(_solver_id_from_row(row)),
    )


def _solver_cost_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        _metric_value_or_inf(row.get("cost_per_resolved_issue")),
        -_coerce_non_negative_float(row.get("raw_resolution_rate")),
        _metric_value_or_inf(row.get("latency_ms_per_resolved_issue")),
        _solver_id_from_row(row),
    )


def _dominates(candidate: Mapping[str, Any], other: Mapping[str, Any]) -> bool:
    candidate_resolution = _coerce_non_negative_float(
        candidate.get("raw_resolution_rate")
    )
    other_resolution = _coerce_non_negative_float(other.get("raw_resolution_rate"))
    candidate_cost = _metric_value_or_inf(candidate.get("cost_per_resolved_issue"))
    other_cost = _metric_value_or_inf(other.get("cost_per_resolved_issue"))
    candidate_latency = _metric_value_or_inf(
        candidate.get("latency_ms_per_resolved_issue")
    )
    other_latency = _metric_value_or_inf(other.get("latency_ms_per_resolved_issue"))

    return (
        candidate_resolution >= other_resolution
        and candidate_cost <= other_cost
        and candidate_latency <= other_latency
        and (
            candidate_resolution > other_resolution
            or candidate_cost < other_cost
            or candidate_latency < other_latency
        )
    )


def _build_budget_frontier(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    eligible = [
        row
        for row in rows
        if row.get("cost_per_resolved_issue") is not None
        and _coerce_non_negative_int(row.get("resolved_instance_count")) > 0
    ]
    ordered = sorted(eligible, key=_solver_cost_key)
    if not ordered:
        return []

    frontier: list[dict[str, Any]] = []
    best_row: dict[str, Any] | None = None

    for budget, bucket in groupby(
        ordered,
        key=lambda row: _metric_value_or_inf(row.get("cost_per_resolved_issue")),
    ):
        bucket_rows = list(bucket)
        for row in bucket_rows:
            if best_row is None or _solver_ranking_key(row) < _solver_ranking_key(
                best_row
            ):
                best_row = row
        if best_row is None:
            continue
        frontier.append(
            {
                "budget": budget,
                "best_solver_id": _solver_id_from_row(best_row),
                "best_raw_resolution_rate": best_row.get("raw_resolution_rate"),
                "best_resolved_instance_count": best_row.get("resolved_instance_count"),
                "best_cost_per_resolved_issue": best_row.get("cost_per_resolved_issue"),
                "best_latency_ms_per_resolved_issue": best_row.get(
                    "latency_ms_per_resolved_issue"
                ),
                "best_total_cost_usd": best_row.get("total_cost_usd"),
                "best_total_duration_ms": best_row.get("total_duration_ms"),
                "affordable_solver_ids": [
                    _solver_id_from_row(row) for row in bucket_rows
                ],
            }
        )

    return frontier


def _build_pareto_frontier(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    frontier: list[dict[str, Any]] = []
    for row in rows:
        dominated = False
        for other in rows:
            if other is row:
                continue
            if _dominates(other, row):
                dominated = True
                break
        if not dominated:
            frontier.append(row)
    return sorted(frontier, key=_solver_cost_key)


def _build_failure_breakdown(
    unresolved_rows: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for row in unresolved_rows:
        reason = _coerce_str(row.get("failure_reason"))
        if not reason:
            reason = _coerce_str(row.get("harness_outcome"))
        if not reason:
            reason = "unknown"
        counts[reason] += 1

    total = sum(counts.values())
    breakdown = [
        {
            "reason": reason,
            "count": count,
            "share": (_safe_rate(count, total) if total else 0.0),
        }
        for reason, count in sorted(
            counts.items(), key=lambda item: (-item[1], item[0])
        )
    ]
    return breakdown


def _build_unresolved_samples(
    unresolved_rows: list[Mapping[str, Any]],
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    ordered = sorted(
        unresolved_rows,
        key=lambda row: (
            -_coerce_non_negative_float(row.get("attempt_cost_usd")),
            -_coerce_non_negative_int(row.get("duration_ms")),
            _solver_id_from_row(row),
            _coerce_str(row.get("instance_id")),
        ),
    )
    samples: list[dict[str, Any]] = []
    for row in ordered[:limit]:
        samples.append(
            {
                "instance_id": _coerce_str(row.get("instance_id")),
                "solver_id": _solver_id_from_row(row),
                "harness_outcome": _coerce_str(row.get("harness_outcome")),
                "failure_reason": _coerce_str(row.get("failure_reason")),
                "attempt_state": _coerce_str(row.get("attempt_state")),
                "duration_ms": _coerce_non_negative_int(row.get("duration_ms")),
                "attempt_cost_usd": row.get("attempt_cost_usd"),
                "task_cluster": _coerce_str(row.get("task_cluster")),
                "problem_statement": _coerce_str(row.get("problem_statement")),
            }
        )
    return samples


def _build_solver_summary_payloads(
    summaries: list[ResolutionMetrics],
) -> list[dict[str, Any]]:
    payloads = [_resolution_summary_to_dict(summary) for summary in summaries]
    return sorted(payloads, key=_solver_ranking_key)


def build_analysis_report(
    *,
    attempts: list[dict[str, Any]],
    instance_results: list[dict[str, Any]],
    grouped_summaries: list[ResolutionMetrics],
    solver_summaries: list[ResolutionMetrics],
    group_by: tuple[str, ...],
    expensive_cost_threshold: float,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the structured payload that feeds the static analysis report."""
    joined_rows = join_attempt_rows(attempts, instance_results)
    unresolved_rows = [row for row in joined_rows if not row.get("resolved")]
    resolved_instances = {
        _coerce_str(row.get("instance_id"))
        for row in joined_rows
        if _coerce_str(row.get("instance_id")) and row.get("resolved")
    }
    unique_instances = {
        _coerce_str(row.get("instance_id"))
        for row in joined_rows
        if _coerce_str(row.get("instance_id"))
    }
    solver_payloads = _build_solver_summary_payloads(solver_summaries)
    primary_payloads = [
        _resolution_summary_to_dict(summary) for summary in grouped_summaries
    ]
    best_solver = solver_payloads[0] if solver_payloads else None
    cheapest_solver = min(
        solver_payloads,
        key=_solver_cost_key,
        default=None,
    )

    report = {
        "metadata": metadata or {},
        "top_line": {
            "group_by": list(group_by),
            "group_count": len(primary_payloads),
            "solver_count": len(solver_payloads),
            "attempt_rows": len(attempts),
            "instance_result_rows": len(instance_results),
            "joined_rows": len(joined_rows),
            "unique_instance_count": len(unique_instances),
            "resolved_instance_count": len(resolved_instances),
            "unresolved_instance_count": len(unique_instances - resolved_instances),
            "best_solver_id": _solver_id_from_row(best_solver) if best_solver else "",
            "cheapest_solver_id": (
                _solver_id_from_row(cheapest_solver) if cheapest_solver else ""
            ),
            "expensive_cost_threshold": expensive_cost_threshold,
        },
        "grouped_summary": {
            "group_by": list(group_by),
            "rows": primary_payloads,
        },
        "solver_comparison": {
            "group_by": ["solver_id"],
            "rows": solver_payloads,
        },
        "budget_frontier": _build_budget_frontier(solver_payloads),
        "pareto_frontier": _build_pareto_frontier(solver_payloads),
        "failure_reason_breakdown": _build_failure_breakdown(unresolved_rows),
        "unresolved_samples": _build_unresolved_samples(unresolved_rows),
    }

    if best_solver is not None and cheapest_solver is not None:
        report["marginal_win_analysis"] = {
            "best_solver_id": _solver_id_from_row(best_solver),
            "best_solver_raw_resolution_rate": best_solver.get("raw_resolution_rate"),
            "best_solver_cost_per_resolved_issue": best_solver.get(
                "cost_per_resolved_issue"
            ),
            "cheapest_solver_id": _solver_id_from_row(cheapest_solver),
            "cheapest_solver_raw_resolution_rate": cheapest_solver.get(
                "raw_resolution_rate"
            ),
            "cheapest_solver_cost_per_resolved_issue": cheapest_solver.get(
                "cost_per_resolved_issue"
            ),
            "expensive_cost_threshold": expensive_cost_threshold,
            "best_solver_expensive_coverage": best_solver.get("expensive_coverage"),
            "best_solver_exclusive_expensive_win_rate": best_solver.get(
                "exclusive_expensive_win_rate"
            ),
            "best_solver_marginal_cost_per_extra_resolve": best_solver.get(
                "marginal_cost_per_extra_resolve"
            ),
        }

    return report


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
    report: dict[str, Any] | None = None,
) -> None:
    """Serialize summary records for API/automation consumption."""
    payload: dict[str, Any] = {
        "summary": [_resolution_summary_to_dict(summary) for summary in summaries],
        "metadata": metadata or {},
    }
    if report is not None:
        payload["report"] = report
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
    report: dict[str, Any] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    if report is None:
        report = {
            "metadata": metadata or {},
            "top_line": {
                "group_by": list(group_by),
                "group_count": len(summaries),
                "solver_count": len(summaries),
                "attempt_rows": 0,
                "instance_result_rows": 0,
                "joined_rows": 0,
                "unique_instance_count": 0,
                "resolved_instance_count": 0,
                "unresolved_instance_count": 0,
                "best_solver_id": "",
                "cheapest_solver_id": "",
                "expensive_cost_threshold": 1.0,
            },
            "grouped_summary": {
                "group_by": list(group_by),
                "rows": [_resolution_summary_to_dict(summary) for summary in summaries],
            },
            "solver_comparison": {
                "group_by": list(group_by),
                "rows": [_resolution_summary_to_dict(summary) for summary in summaries],
            },
            "budget_frontier": [],
            "pareto_frontier": [],
            "failure_reason_breakdown": [],
            "unresolved_samples": [],
        }

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

    def render_grouped_table(
        title: str,
        table_rows: list[Mapping[str, Any]],
        table_group_by: tuple[str, ...] | list[str],
    ) -> list[str]:
        headers = list(table_group_by) + metric_headers
        lines: list[str] = [f"<h2>{html_escape(title)}</h2>"]
        if not table_rows:
            lines.append('<p class="empty">No rows.</p>')
            return lines
        lines.append("<table>")
        lines.append(
            "<tr>" + "".join(f"<th>{html_escape(h)}</th>" for h in headers) + "</tr>"
        )
        for row in table_rows:
            group_values = row.get("group_values", {})
            lines.append("<tr>")
            for column in table_group_by:
                value = ""
                if isinstance(group_values, Mapping):
                    value = _coerce_str(group_values.get(column))
                lines.append(f"<td>{html_escape(value)}</td>")
            for header in metric_headers:
                lines.append(
                    f"<td>{html_escape(_display_number(row.get(header)))}</td>"
                )
            lines.append("</tr>")
        lines.append("</table>")
        return lines

    def render_key_values(items: list[tuple[str, Any]]) -> list[str]:
        lines = ['<dl class="kv">']
        for label, value in items:
            lines.append(
                f"<dt>{html_escape(label)}</dt><dd>{html_escape(_display_number(value))}</dd>"
            )
        lines.append("</dl>")
        return lines

    budget_frontier = report.get("budget_frontier", [])
    failure_breakdown = report.get("failure_reason_breakdown", [])
    unresolved_samples = report.get("unresolved_samples", [])
    top_line = report.get("top_line", {})
    grouped_summary = report.get("grouped_summary", {})
    solver_comparison = report.get("solver_comparison", {})
    pareto_frontier = report.get("pareto_frontier", [])

    lines = [
        '<html><head><meta charset="utf-8"/>',
        "<title>RepoGauge Analysis</title>",
        "<style>body{font-family:Inter,Arial,sans-serif;max-width:1100px;padding:24px;line-height:1.4}"
        "h1,h2{margin-bottom:0.4rem}"
        "p{max-width:80ch}"
        "table{border-collapse:collapse;width:100%;margin-top:12px;margin-bottom:24px}"
        "th,td{border:1px solid #ccc;padding:6px;font-size:12px;text-align:left;vertical-align:top}"
        "th{background:#f5f5f5}"
        ".kv{display:grid;grid-template-columns:max-content 1fr;gap:4px 12px;max-width:900px}"
        ".kv dt{font-weight:700}"
        ".kv dd{margin:0}"
        ".empty{color:#666;font-style:italic}",
        "</style>",
        "</head><body>",
    ]
    lines.append("<h1>RepoGauge Analysis Report</h1>")
    lines.append(
        "<p>The budget table is ordered by increasing cost per resolved issue. "
        "To answer a budget ceiling question, find the last row with budget <= X "
        "and read its best solver.</p>"
    )
    lines.extend(
        render_key_values(
            [
                ("group_by", ", ".join(top_line.get("group_by", []))),
                ("group_count", top_line.get("group_count")),
                ("solver_count", top_line.get("solver_count")),
                ("attempt_rows", top_line.get("attempt_rows")),
                ("instance_result_rows", top_line.get("instance_result_rows")),
                ("unique_instance_count", top_line.get("unique_instance_count")),
                ("resolved_instance_count", top_line.get("resolved_instance_count")),
                (
                    "unresolved_instance_count",
                    top_line.get("unresolved_instance_count"),
                ),
                ("best_solver_id", top_line.get("best_solver_id")),
                ("cheapest_solver_id", top_line.get("cheapest_solver_id")),
                ("expensive_cost_threshold", top_line.get("expensive_cost_threshold")),
            ]
        )
    )

    if metadata:
        lines.append("<h2>Run Metadata</h2>")
        lines.append(
            "<pre>{}</pre>".format(
                html_escape(json.dumps(metadata, sort_keys=True, indent=2))
            )
        )

    lines.extend(
        render_grouped_table(
            "Requested Summary",
            grouped_summary.get("rows", []),
            grouped_summary.get("group_by", group_by),
        )
    )
    lines.extend(
        render_grouped_table(
            "Solver Comparison",
            solver_comparison.get("rows", []),
            solver_comparison.get("group_by", ["solver_id"]),
        )
    )

    lines.append("<h2>Budget Frontier</h2>")
    if budget_frontier:
        lines.append(
            "<p>The <code>best_solver_id</code> column is the best affordable solver at or below each budget ceiling.</p>"
        )
        lines.append("<table>")
        headers = [
            "budget",
            "best_solver_id",
            "best_raw_resolution_rate",
            "best_resolved_instance_count",
            "best_cost_per_resolved_issue",
            "best_latency_ms_per_resolved_issue",
            "best_total_cost_usd",
            "best_total_duration_ms",
        ]
        lines.append(
            "<tr>"
            + "".join(f"<th>{html_escape(header)}</th>" for header in headers)
            + "</tr>"
        )
        for row in budget_frontier:
            lines.append("<tr>")
            for header in headers:
                lines.append(
                    f"<td>{html_escape(_display_number(row.get(header)))}</td>"
                )
            lines.append("</tr>")
        lines.append("</table>")
    else:
        lines.append(
            '<p class="empty">No affordable solver rows with a resolved issue count.</p>'
        )

    lines.append("<h2>Pareto Frontier</h2>")
    if pareto_frontier:
        lines.append(
            "<p>Rows on this frontier are not dominated on resolution, cost, and latency.</p>"
        )
        lines.append("<table>")
        headers = [
            "solver_id",
            "raw_resolution_rate",
            "cost_per_resolved_issue",
            "latency_ms_per_resolved_issue",
            "resolved_instance_count",
        ]
        lines.append(
            "<tr>"
            + "".join(f"<th>{html_escape(header)}</th>" for header in headers)
            + "</tr>"
        )
        for row in pareto_frontier:
            lines.append("<tr>")
            for header in headers:
                value = row.get(header, "")
                if header == "solver_id":
                    value = _solver_id_from_row(row)
                lines.append(f"<td>{html_escape(_display_number(value))}</td>")
            lines.append("</tr>")
        lines.append("</table>")
    else:
        lines.append('<p class="empty">No frontier rows.</p>')

    lines.append("<h2>Failure Reasons</h2>")
    if failure_breakdown:
        lines.append("<table>")
        headers = ["reason", "count", "share"]
        lines.append(
            "<tr>"
            + "".join(f"<th>{html_escape(header)}</th>" for header in headers)
            + "</tr>"
        )
        for row in failure_breakdown:
            lines.append("<tr>")
            for header in headers:
                value = row.get(header, "")
                if header == "share" and isinstance(value, (int, float)):
                    value = f"{value:.3f}"
                lines.append(f"<td>{html_escape(_display_number(value))}</td>")
            lines.append("</tr>")
        lines.append("</table>")
    else:
        lines.append('<p class="empty">No unresolved rows.</p>')

    lines.append("<h2>Unresolved Samples</h2>")
    if unresolved_samples:
        lines.append("<table>")
        headers = [
            "instance_id",
            "solver_id",
            "harness_outcome",
            "failure_reason",
            "attempt_state",
            "duration_ms",
            "attempt_cost_usd",
            "task_cluster",
            "problem_statement",
        ]
        lines.append(
            "<tr>"
            + "".join(f"<th>{html_escape(header)}</th>" for header in headers)
            + "</tr>"
        )
        for row in unresolved_samples:
            lines.append("<tr>")
            for header in headers:
                value = row.get(header, "")
                if (
                    header == "problem_statement"
                    and isinstance(value, str)
                    and len(value) > 220
                ):
                    value = f"{value[:217]}..."
                lines.append(f"<td>{html_escape(_display_number(value))}</td>")
            lines.append("</tr>")
        lines.append("</table>")
    else:
        lines.append('<p class="empty">No unresolved sample rows.</p>')

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
