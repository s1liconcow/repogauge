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
from .pricing import estimate_public_api_cost_usd, normalize_model_name, read_cost_usd


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
    avg_attempt_duration_ms: float
    p50_attempt_duration_ms: int | None
    p95_attempt_duration_ms: int | None
    total_input_tokens: int
    total_output_tokens: int
    total_tokens: int
    avg_total_tokens_per_attempt: float
    tokens_per_resolved_issue: float | None
    total_tool_calls: int
    avg_tool_calls_per_attempt: float
    tool_calls_per_resolved_issue: float | None


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
    return read_cost_usd(row.get("cost"))


def _normalize_model_name(value: Any) -> str:
    return normalize_model_name(_coerce_str(value))


def _read_model_name(row: Mapping[str, Any]) -> str:
    direct = _normalize_model_name(
        row.get("model") or row.get("model_name_or_path") or row.get("model_id")
    )
    if direct:
        return direct
    metadata = row.get("metadata")
    if isinstance(metadata, Mapping):
        metadata_model = _normalize_model_name(
            metadata.get("model")
            or metadata.get("model_name_or_path")
            or metadata.get("model_id")
        )
        if metadata_model:
            return metadata_model
    return ""


def _estimate_row_cost_from_tokens(row: Mapping[str, Any]) -> float | None:
    return estimate_public_api_cost_usd(
        model_name=_read_model_name(row),
        usage=_usage_mapping(row),
    )


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


def _safe_json_parse(value: str) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return None


def _usage_mapping(row: Mapping[str, Any]) -> Mapping[str, Any]:
    usage = row.get("usage")
    return usage if isinstance(usage, Mapping) else {}


def _nested_mapping(mapping: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = mapping.get(key)
    return value if isinstance(value, Mapping) else {}


def _int_from_candidates(
    mapping: Mapping[str, Any], keys: tuple[str, ...]
) -> int | None:
    for key in keys:
        value = mapping.get(key)
        if value is None:
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return None


def _read_usage_tokens(row: Mapping[str, Any]) -> tuple[int, int, int]:
    usage = _usage_mapping(row)
    input_tokens = _int_from_candidates(
        usage,
        (
            "input_tokens",
            "prompt_tokens",
            "input_token_count",
            "prompt_token_count",
        ),
    )
    output_tokens = _int_from_candidates(
        usage,
        (
            "output_tokens",
            "completion_tokens",
            "output_token_count",
            "completion_token_count",
        ),
    )
    total_tokens = _int_from_candidates(
        usage,
        ("total_tokens", "total_token_count"),
    )
    input_total = input_tokens or 0
    output_total = output_tokens or 0
    combined_total = (
        total_tokens if total_tokens is not None else input_total + output_total
    )
    return input_total, output_total, combined_total


def _read_cached_input_tokens(row: Mapping[str, Any]) -> int:
    usage = _usage_mapping(row)
    direct = _int_from_candidates(
        usage,
        (
            "cached_input_tokens",
            "input_cached_tokens",
            "cached_prompt_tokens",
        ),
    )
    if direct is not None:
        return direct
    for nested_key in ("input_tokens_details", "prompt_tokens_details"):
        nested = _nested_mapping(usage, nested_key)
        nested_value = _int_from_candidates(nested, ("cached_tokens",))
        if nested_value is not None:
            return nested_value
    return 0


def _read_tool_call_count_from_mapping(mapping: Mapping[str, Any]) -> int | None:
    direct = _int_from_candidates(
        mapping,
        (
            "tool_calls",
            "tool_call_count",
            "total_tool_calls",
            "num_tool_calls",
        ),
    )
    if direct is not None:
        return direct
    list_value = mapping.get("tool_calls")
    if isinstance(list_value, list):
        return len(list_value)
    return None


def _tool_call_count_in_event(payload: Mapping[str, Any]) -> int:
    payload_type = _coerce_str(payload.get("type"))
    if payload_type in {"tool_call", "function_call"}:
        return 1

    item = payload.get("item")
    if isinstance(item, Mapping):
        item_type = _coerce_str(item.get("type"))
        if payload_type == "item.started" and item_type in {
            "command_execution",
            "function_call",
            "tool_call",
        }:
            return 1
        if payload_type == "response.output_item.added" and item_type in {
            "function_call",
            "tool_call",
        }:
            return 1

    message = payload.get("message")
    if isinstance(message, Mapping):
        content = message.get("content")
        if isinstance(content, list):
            return sum(
                1
                for block in content
                if isinstance(block, Mapping)
                and _coerce_str(block.get("type"))
                in {"tool_use", "server_tool_use", "tool_call", "function_call"}
            )
    return 0


def _read_tool_call_count(row: Mapping[str, Any]) -> int:
    usage_count = _read_tool_call_count_from_mapping(_usage_mapping(row))
    if usage_count is not None:
        return usage_count

    metadata = row.get("metadata")
    if isinstance(metadata, Mapping):
        metadata_count = _read_tool_call_count_from_mapping(metadata)
        if metadata_count is not None:
            return metadata_count
        telemetry = metadata.get("telemetry")
        if isinstance(telemetry, list):
            telemetry_count = sum(
                _tool_call_count_in_event(event)
                for event in telemetry
                if isinstance(event, Mapping)
            )
            if telemetry_count:
                return telemetry_count

    raw_output = row.get("raw_output")
    if not isinstance(raw_output, str) or not raw_output.strip():
        return 0
    count = 0
    for line in raw_output.splitlines():
        payload = _safe_json_parse(line.strip())
        if isinstance(payload, Mapping):
            count += _tool_call_count_in_event(payload)
    return count


def _percentile(values: list[int], percentile: int) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = int(((percentile / 100) * (len(ordered) - 1)) + 0.5)
    index = max(0, min(index, len(ordered) - 1))
    return ordered[index]


def _display_number(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return str(value)


def _format_integer(value: Any) -> str:
    number = _coerce_non_negative_int(value)
    return f"{number:,}"


def _format_percent(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "-"


def _format_currency(value: Any) -> str:
    if value is None:
        return "-"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "-"
    if amount <= 0:
        return "$0.00"
    if amount < 0.01:
        return f"${amount:.4f}"
    return f"${amount:,.2f}"


def _format_duration_ms(value: Any) -> str:
    if value is None:
        return "-"
    try:
        duration_ms = int(value)
    except (TypeError, ValueError):
        return "-"
    if duration_ms >= 60_000:
        minutes = duration_ms / 60_000
        return f"{minutes:.1f}m"
    if duration_ms >= 1_000:
        seconds = duration_ms / 1_000
        return f"{seconds:.1f}s"
    return f"{duration_ms}ms"


def _format_compact_number(value: Any) -> str:
    if value is None:
        return "-"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    sign = "-" if number < 0 else ""
    number = abs(number)
    if number >= 1_000_000_000:
        return f"{sign}{number / 1_000_000_000:.1f}B"
    if number >= 1_000_000:
        return f"{sign}{number / 1_000_000:.1f}M"
    if number >= 1_000:
        return f"{sign}{number / 1_000:.1f}K"
    return f"{sign}{int(number) if number.is_integer() else round(number, 1)}"


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


def _build_cost_opportunity_report(
    joined_rows: list[Mapping[str, Any]],
    solver_payloads: list[dict[str, Any]],
) -> dict[str, Any]:
    cheapest_success_by_instance: dict[str, dict[str, Any]] = {}
    solver_success_by_instance: dict[tuple[str, str], dict[str, Any]] = {}
    offers_by_instance: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in joined_rows:
        if not row.get("resolved"):
            continue
        instance_id = _coerce_str(row.get("instance_id"))
        solver_id = _solver_id_from_row(row)
        cost = row.get("attempt_cost_usd")
        if not instance_id or not solver_id or not isinstance(cost, (int, float)):
            continue
        cost_value = max(0.0, float(cost))
        if cost_value <= 0:
            continue
        offer = {
            "instance_id": instance_id,
            "solver_id": solver_id,
            "cost_usd": cost_value,
            "duration_ms": _coerce_non_negative_int(row.get("duration_ms")),
            "total_tokens": _coerce_non_negative_int(row.get("total_tokens")),
            "tool_calls": _coerce_non_negative_int(row.get("tool_calls")),
        }
        key = (solver_id, instance_id)
        existing = solver_success_by_instance.get(key)
        if existing is None or (
            offer["cost_usd"],
            offer["duration_ms"],
            offer["solver_id"],
        ) < (
            existing["cost_usd"],
            existing["duration_ms"],
            existing["solver_id"],
        ):
            solver_success_by_instance[key] = offer

        current_cheapest = cheapest_success_by_instance.get(instance_id)
        if current_cheapest is None or (
            offer["cost_usd"],
            offer["duration_ms"],
            offer["solver_id"],
        ) < (
            current_cheapest["cost_usd"],
            current_cheapest["duration_ms"],
            current_cheapest["solver_id"],
        ):
            cheapest_success_by_instance[instance_id] = offer
        offers_by_instance[instance_id].append(offer)

    solver_rows: list[dict[str, Any]] = []
    for payload in solver_payloads:
        solver_id = _solver_id_from_row(payload)
        offers = [
            value
            for (candidate_solver_id, _), value in solver_success_by_instance.items()
            if candidate_solver_id == solver_id
        ]
        success_spend = sum(item["cost_usd"] for item in offers)
        cheapest_compatible_spend = sum(
            cheapest_success_by_instance[item["instance_id"]]["cost_usd"]
            for item in offers
            if item["instance_id"] in cheapest_success_by_instance
        )
        avoidable_spend = max(0.0, success_spend - cheapest_compatible_spend)
        avoidable_share = avoidable_spend / success_spend if success_spend > 0 else 0.0
        if not offers:
            continue
        solver_rows.append(
            {
                "solver_id": solver_id,
                "resolved_instance_count": payload.get("resolved_instance_count", 0),
                "success_spend_usd": round(success_spend, 6),
                "cheapest_compatible_spend_usd": round(cheapest_compatible_spend, 6),
                "avoidable_spend_usd": round(avoidable_spend, 6),
                "avoidable_share": avoidable_share,
                "avg_avoidable_spend_per_resolved_issue": (
                    avoidable_spend
                    / _coerce_non_negative_int(payload.get("resolved_instance_count"))
                    if _coerce_non_negative_int(payload.get("resolved_instance_count"))
                    > 0
                    else None
                ),
            }
        )

    instance_rows: list[dict[str, Any]] = []
    for instance_id, offers in offers_by_instance.items():
        if len(offers) < 2:
            continue
        ordered = sorted(
            offers,
            key=lambda item: (item["cost_usd"], item["duration_ms"], item["solver_id"]),
        )
        cheapest = ordered[0]
        priciest = ordered[-1]
        gap = max(0.0, priciest["cost_usd"] - cheapest["cost_usd"])
        if gap <= 0:
            continue
        instance_rows.append(
            {
                "instance_id": instance_id,
                "cheapest_solver_id": cheapest["solver_id"],
                "cheapest_cost_usd": cheapest["cost_usd"],
                "most_expensive_solver_id": priciest["solver_id"],
                "most_expensive_cost_usd": priciest["cost_usd"],
                "savings_gap_usd": gap,
                "available_solver_ids": [item["solver_id"] for item in ordered],
            }
        )

    total_floor = sum(
        item["cost_usd"] for item in cheapest_success_by_instance.values()
    )
    best_solver_row = solver_rows[0] if solver_rows else None
    best_solver_mixed_routing_gap = None
    if best_solver_row is not None:
        best_solver_mixed_routing_gap = max(
            0.0, best_solver_row["success_spend_usd"] - total_floor
        )

    return {
        "portfolio_cost_floor_usd": round(total_floor, 6),
        "solver_savings": sorted(
            solver_rows,
            key=lambda row: (
                -_coerce_non_negative_float(row["avoidable_spend_usd"]),
                row["solver_id"],
            ),
        ),
        "instance_savings_samples": sorted(
            instance_rows,
            key=lambda row: (
                -_coerce_non_negative_float(row["savings_gap_usd"]),
                row["instance_id"],
            ),
        )[:10],
        "best_solver_mixed_routing_gap_usd": (
            round(best_solver_mixed_routing_gap, 6)
            if best_solver_mixed_routing_gap is not None
            else None
        ),
    }


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
            "total_attempt_cost_usd": _safe_cost(
                [
                    float(row.get("attempt_cost_usd"))
                    for row in joined_rows
                    if isinstance(row.get("attempt_cost_usd"), (int, float))
                ]
            ),
            "total_input_tokens": sum(
                _coerce_non_negative_int(row.get("input_tokens")) for row in joined_rows
            ),
            "total_output_tokens": sum(
                _coerce_non_negative_int(row.get("output_tokens"))
                for row in joined_rows
            ),
            "total_tokens": sum(
                _coerce_non_negative_int(row.get("total_tokens")) for row in joined_rows
            ),
            "total_tool_calls": sum(
                _coerce_non_negative_int(row.get("tool_calls")) for row in joined_rows
            ),
            "cost_coverage_rate": _safe_rate(
                sum(
                    1
                    for row in joined_rows
                    if isinstance(row.get("attempt_cost_usd"), (int, float))
                ),
                len(joined_rows),
            ),
            "token_coverage_rate": _safe_rate(
                sum(
                    1
                    for row in joined_rows
                    if _coerce_non_negative_int(row.get("total_tokens")) > 0
                ),
                len(joined_rows),
            ),
            "tool_call_coverage_rate": _safe_rate(
                sum(
                    1
                    for row in joined_rows
                    if _coerce_non_negative_int(row.get("tool_calls")) > 0
                ),
                len(joined_rows),
            ),
            "avg_attempt_duration_ms": (
                sum(
                    _coerce_non_negative_int(row.get("duration_ms"))
                    for row in joined_rows
                )
                / len(joined_rows)
                if joined_rows
                else 0.0
            ),
            "best_solver_resolution_rate": (
                best_solver.get("raw_resolution_rate") if best_solver else None
            ),
            "best_solver_cost_per_resolved_issue": (
                best_solver.get("cost_per_resolved_issue") if best_solver else None
            ),
            "best_solver_total_cost_usd": (
                best_solver.get("total_cost_usd") if best_solver else None
            ),
            "cheapest_solver_resolution_rate": (
                cheapest_solver.get("raw_resolution_rate") if cheapest_solver else None
            ),
            "cheapest_solver_cost_per_resolved_issue": (
                cheapest_solver.get("cost_per_resolved_issue")
                if cheapest_solver
                else None
            ),
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
        "cost_opportunity": _build_cost_opportunity_report(
            joined_rows, solver_payloads
        ),
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


def build_predictions_from_attempts(
    attempts_path: Path,
    predictions_path: Path,
) -> int:
    """Convert a run's ``attempts.jsonl`` into SWE-bench ``predictions.jsonl``.

    Skips attempts without a ``model_patch`` (failed/timed-out attempts produce
    no patch to evaluate). Returns the number of prediction rows written.
    """
    attempts = load_attempt_rows(attempts_path)
    predictions_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with predictions_path.open("w", encoding="utf-8") as out:
        for row in attempts:
            patch = row.get("model_patch")
            instance_id = row.get("instance_id")
            solver_id = row.get("solver_id")
            if not patch or not instance_id or not solver_id:
                continue
            out.write(
                json.dumps(
                    {
                        "instance_id": instance_id,
                        "model_patch": patch,
                        "model_name_or_path": solver_id,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            written += 1
    return written


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
            "avg_attempt_duration_ms": summary.avg_attempt_duration_ms,
            "p50_attempt_duration_ms": summary.p50_attempt_duration_ms,
            "p95_attempt_duration_ms": summary.p95_attempt_duration_ms,
            "total_input_tokens": summary.total_input_tokens,
            "total_output_tokens": summary.total_output_tokens,
            "total_tokens": summary.total_tokens,
            "avg_total_tokens_per_attempt": summary.avg_total_tokens_per_attempt,
            "tokens_per_resolved_issue": summary.tokens_per_resolved_issue,
            "total_tool_calls": summary.total_tool_calls,
            "avg_tool_calls_per_attempt": summary.avg_tool_calls_per_attempt,
            "tool_calls_per_resolved_issue": summary.tool_calls_per_resolved_issue,
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
        "avg_attempt_duration_ms",
        "p50_attempt_duration_ms",
        "p95_attempt_duration_ms",
        "total_input_tokens",
        "total_output_tokens",
        "total_tokens",
        "avg_total_tokens_per_attempt",
        "tokens_per_resolved_issue",
        "total_tool_calls",
        "avg_tool_calls_per_attempt",
        "tool_calls_per_resolved_issue",
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
            "cost_opportunity": {
                "portfolio_cost_floor_usd": 0.0,
                "solver_savings": [],
                "instance_savings_samples": [],
                "best_solver_mixed_routing_gap_usd": None,
            },
        }
    top_line = report.get("top_line", {})
    grouped_summary = report.get("grouped_summary", {})
    solver_comparison = report.get("solver_comparison", {})
    budget_frontier = report.get("budget_frontier", [])
    pareto_frontier = report.get("pareto_frontier", [])
    failure_breakdown = report.get("failure_reason_breakdown", [])
    unresolved_samples = report.get("unresolved_samples", [])
    cost_opportunity = report.get("cost_opportunity", {})

    solver_rows = list(solver_comparison.get("rows", []))
    savings_by_solver = {
        _coerce_str(row.get("solver_id")): row
        for row in cost_opportunity.get("solver_savings", [])
        if _coerce_str(row.get("solver_id"))
    }
    for row in solver_rows:
        row.update(savings_by_solver.get(_solver_id_from_row(row), {}))

    has_cost_telemetry = (
        _coerce_non_negative_float(top_line.get("cost_coverage_rate")) > 0
    )
    has_priced_routing_analysis = bool(cost_opportunity.get("solver_savings"))

    def format_cell(value: Any, formatter: str) -> str:
        if formatter == "percent":
            return _format_percent(value)
        if formatter == "currency":
            return _format_currency(value)
        if formatter == "ms":
            return _format_duration_ms(value)
        if formatter == "int":
            return _format_integer(value)
        if formatter == "compact":
            return _format_compact_number(value)
        if formatter == "text":
            return html_escape(_coerce_str(value))
        return html_escape(_display_number(value))

    def render_metric_card(
        *,
        eyebrow: str,
        title: str,
        value: str,
        supporting: str = "",
        tone: str = "default",
    ) -> str:
        subtitle = (
            f'<p class="metric-card__support">{html_escape(supporting)}</p>'
            if supporting
            else ""
        )
        return (
            f'<article class="metric-card metric-card--{html_escape(tone)}">'
            f'<span class="metric-card__eyebrow">{html_escape(eyebrow)}</span>'
            f'<h3 class="metric-card__title">{html_escape(title)}</h3>'
            f'<div class="metric-card__value">{value}</div>'
            f"{subtitle}"
            "</article>"
        )

    def render_callout(title: str, body: str, *, tone: str = "default") -> str:
        return (
            f'<article class="callout callout--{html_escape(tone)}">'
            f"<h3>{html_escape(title)}</h3>"
            f"<p>{html_escape(body)}</p>"
            "</article>"
        )

    def render_table(
        *,
        title: str,
        subtitle: str,
        table_id: str,
        rows: list[Mapping[str, Any]],
        columns: list[dict[str, str]],
        empty_message: str,
        row_class: str = "",
    ) -> str:
        if not rows:
            return (
                f'<section class="panel"><div class="section-heading"><h2>{html_escape(title)}</h2>'
                f"<p>{html_escape(subtitle)}</p></div>"
                f'<div class="empty-state">{html_escape(empty_message)}</div></section>'
            )

        header_cells = []
        for column in columns:
            header_cells.append(
                '<th scope="col"><button type="button" class="sort-button" '
                f'data-table="{html_escape(table_id)}" '
                f'data-key="{html_escape(column["key"])}">'
                f'{html_escape(column["label"])}<span class="sort-button__glyph">↕</span>'
                "</button></th>"
            )

        body_rows = []
        extra_class = f" {row_class}" if row_class else ""
        for row in rows:
            cells = []
            for column in columns:
                raw_value = row.get(column["key"])
                if column["key"] == "solver_id":
                    raw_value = _solver_id_from_row(row)
                if column["key"] == "available_solver_ids" and isinstance(
                    raw_value, list
                ):
                    display = ", ".join(str(item) for item in raw_value)
                    sort_value = len(raw_value)
                else:
                    display = format_cell(raw_value, column.get("format", "default"))
                    sort_value = raw_value
                if (
                    column["key"] == "problem_statement"
                    and isinstance(raw_value, str)
                    and len(raw_value) > 220
                ):
                    display = html_escape(f"{raw_value[:217]}...")
                cells.append(
                    f'<td data-key="{html_escape(column["key"])}" '
                    f'data-sort-value="{html_escape(str(sort_value if sort_value is not None else ""))}">{display}</td>'
                )
            body_rows.append(f"<tr>{''.join(cells)}</tr>")

        return (
            f'<section class="panel"><div class="section-heading"><h2>{html_escape(title)}</h2>'
            f"<p>{html_escape(subtitle)}</p></div>"
            f'<div class="table-shell{extra_class}"><table id="{html_escape(table_id)}">'
            f"<thead><tr>{''.join(header_cells)}</tr></thead>"
            f"<tbody>{''.join(body_rows)}</tbody></table></div></section>"
        )

    def render_bar_list(
        *,
        title: str,
        subtitle: str,
        rows: list[Mapping[str, Any]],
        label_key: str,
        value_key: str,
        formatter: str,
        accent: str = "blue",
        empty_message: str,
    ) -> str:
        if not rows:
            return (
                f'<section class="panel"><div class="section-heading"><h2>{html_escape(title)}</h2>'
                f"<p>{html_escape(subtitle)}</p></div>"
                f'<div class="empty-state">{html_escape(empty_message)}</div></section>'
            )
        max_value = (
            max(_coerce_non_negative_float(row.get(value_key)) for row in rows) or 1.0
        )
        items = []
        for row in rows:
            label = _coerce_str(row.get(label_key)) or "unknown"
            value = row.get(value_key)
            width = (_coerce_non_negative_float(value) / max_value) * 100
            items.append(
                '<li class="bar-list__item">'
                f'<div class="bar-list__header"><span>{html_escape(label)}</span><strong>{format_cell(value, formatter)}</strong></div>'
                f'<div class="bar-list__track"><span class="bar-list__fill bar-list__fill--{html_escape(accent)}" style="width:{width:.2f}%"></span></div>'
                "</li>"
            )
        return (
            f'<section class="panel"><div class="section-heading"><h2>{html_escape(title)}</h2>'
            f"<p>{html_escape(subtitle)}</p></div>"
            f'<ul class="bar-list">{"".join(items)}</ul></section>'
        )

    def render_scatter_plot(rows: list[Mapping[str, Any]]) -> str:
        eligible = [
            row
            for row in rows
            if row.get("cost_per_resolved_issue") is not None
            and row.get("raw_resolution_rate") is not None
        ]
        if not eligible:
            return (
                '<section class="panel"><div class="section-heading"><h2>Solver Frontier</h2>'
                "<p>No solver rows include both cost and success data yet.</p></div>"
                '<div class="empty-state">Run `analyze` on solver attempts with cost telemetry to unlock the frontier plot.</div></section>'
            )

        width = 760
        height = 360
        margin_left = 70
        margin_right = 36
        margin_top = 32
        margin_bottom = 58
        plot_width = width - margin_left - margin_right
        plot_height = height - margin_top - margin_bottom
        costs = [
            _coerce_non_negative_float(row.get("cost_per_resolved_issue"))
            for row in eligible
        ]
        rates = [
            _coerce_non_negative_float(row.get("raw_resolution_rate"))
            for row in eligible
        ]
        min_cost = min(costs)
        max_cost = max(costs)
        min_rate = min(rates)
        max_rate = max(rates)
        palette = (
            "#0f4c81",
            "#1f7a8c",
            "#ef8354",
            "#4b7bec",
            "#20bf6b",
            "#eb3b5a",
            "#3867d6",
            "#8854d0",
        )

        def x_pos(cost: float) -> float:
            if max_cost == min_cost:
                return margin_left + plot_width / 2
            return (
                margin_left + ((cost - min_cost) / (max_cost - min_cost)) * plot_width
            )

        def y_pos(rate: float) -> float:
            if max_rate == min_rate:
                return margin_top + plot_height / 2
            return (
                margin_top
                + plot_height
                - (((rate - min_rate) / (max_rate - min_rate)) * plot_height)
            )

        y_ticks = []
        for step in range(0, 5):
            tick_rate = (
                step / 4
                if max_rate <= 1
                else min_rate + (((max_rate - min_rate) / 4) * step)
            )
            y = margin_top + plot_height - (plot_height / 4) * step
            y_ticks.append(
                f'<line x1="{margin_left}" y1="{y:.1f}" x2="{width - margin_right}" y2="{y:.1f}" class="chart-grid"/>'
                f'<text x="{margin_left - 12}" y="{y + 4:.1f}" text-anchor="end" class="chart-axis-label">{html_escape(_format_percent(tick_rate))}</text>'
            )

        x_ticks = []
        for step in range(0, 5):
            tick_cost = min_cost + (((max_cost - min_cost) / 4) * step)
            x = margin_left + (plot_width / 4) * step
            x_ticks.append(
                f'<line x1="{x:.1f}" y1="{margin_top}" x2="{x:.1f}" y2="{height - margin_bottom}" class="chart-grid chart-grid--vertical"/>'
                f'<text x="{x:.1f}" y="{height - margin_bottom + 24}" text-anchor="middle" class="chart-axis-label">{html_escape(_format_currency(tick_cost))}</text>'
            )

        points = []
        legend = []
        for index, row in enumerate(sorted(eligible, key=_solver_ranking_key)):
            solver_id = _solver_id_from_row(row)
            color = palette[index % len(palette)]
            cx = x_pos(_coerce_non_negative_float(row.get("cost_per_resolved_issue")))
            cy = y_pos(_coerce_non_negative_float(row.get("raw_resolution_rate")))
            radius = 8 + min(
                16,
                (_coerce_non_negative_int(row.get("resolved_instance_count")) * 1.8),
            )
            tooltip = (
                f"{solver_id} | resolve {_format_percent(row.get('raw_resolution_rate'))} | "
                f"cost {_format_currency(row.get('cost_per_resolved_issue'))} | "
                f"latency {_format_duration_ms(row.get('avg_attempt_duration_ms'))} | "
                f"tokens {_format_compact_number(row.get('avg_total_tokens_per_attempt'))}"
            )
            points.append(
                f'<g class="chart-point"><circle cx="{cx:.1f}" cy="{cy:.1f}" r="{radius:.1f}" '
                f'fill="{color}" fill-opacity="0.78" stroke="white" stroke-width="2">'
                f"<title>{html_escape(tooltip)}</title></circle>"
                f'<text x="{cx:.1f}" y="{cy - radius - 8:.1f}" text-anchor="middle" class="chart-point-label">{html_escape(solver_id)}</text></g>'
            )
            legend.append(
                '<li class="legend__item">'
                f'<span class="legend__swatch" style="background:{color}"></span>'
                f"<span>{html_escape(solver_id)}</span>"
                "</li>"
            )

        chart = (
            '<section class="panel panel--chart"><div class="section-heading"><h2>Solver Frontier</h2>'
            "<p>Resolution rate versus cost per resolved issue, with point size weighted by resolved instances.</p></div>"
            '<div class="chart-shell"><svg viewBox="0 0 760 360" role="img" aria-label="Solver frontier chart">'
            f'<rect x="0" y="0" width="{width}" height="{height}" rx="24" class="chart-surface"/>'
            f"{''.join(y_ticks)}{''.join(x_ticks)}"
            f'<line x1="{margin_left}" y1="{height - margin_bottom}" x2="{width - margin_right}" y2="{height - margin_bottom}" class="chart-axis"/>'
            f'<line x1="{margin_left}" y1="{margin_top}" x2="{margin_left}" y2="{height - margin_bottom}" class="chart-axis"/>'
            f"{''.join(points)}"
            f'<text x="{margin_left}" y="{margin_top - 10}" class="chart-title">Higher and farther left is better.</text>'
            f'<text x="{width / 2:.1f}" y="{height - 12}" text-anchor="middle" class="chart-axis-label">Cost Per Resolved Issue</text>'
            f'<text x="18" y="{height / 2:.1f}" text-anchor="middle" transform="rotate(-90 18 {height / 2:.1f})" class="chart-axis-label">Resolution Rate</text>'
            "</svg>"
            f'<ul class="legend">{"".join(legend)}</ul></div></section>'
        )
        return chart

    def render_unresolved_cards(rows: list[Mapping[str, Any]]) -> str:
        if not rows:
            return (
                '<section class="panel"><div class="section-heading"><h2>Unresolved Samples</h2>'
                "<p>No unresolved rows remain in this analysis window.</p></div>"
                '<div class="empty-state">Everything in the selected slice resolved successfully.</div></section>'
            )
        cards = []
        for row in rows:
            cards.append(
                '<article class="incident-card">'
                f'<div class="incident-card__top"><span class="chip">{html_escape(_coerce_str(row.get("solver_id")))}</span>'
                f'<span class="chip chip--warning">{html_escape(_coerce_str(row.get("failure_reason") or row.get("harness_outcome") or "unknown"))}</span></div>'
                f"<h3>{html_escape(_coerce_str(row.get('instance_id')))}</h3>"
                f"<p>{html_escape(_coerce_str(row.get('problem_statement'))[:260])}</p>"
                '<dl class="incident-card__meta">'
                f"<div><dt>Attempt State</dt><dd>{html_escape(_coerce_str(row.get('attempt_state')))}</dd></div>"
                f"<div><dt>Latency</dt><dd>{html_escape(_format_duration_ms(row.get('duration_ms')))}</dd></div>"
                f"<div><dt>Spend</dt><dd>{html_escape(_format_currency(row.get('attempt_cost_usd')))}</dd></div>"
                f"<div><dt>Cluster</dt><dd>{html_escape(_coerce_str(row.get('task_cluster')))}</dd></div>"
                "</dl></article>"
            )
        return (
            '<section class="panel"><div class="section-heading"><h2>Unresolved Samples</h2>'
            "<p>The highest-cost unresolved rows, surfaced to make debugging the next tranche straightforward.</p></div>"
            f'<div class="incident-grid">{"".join(cards)}</div></section>'
        )

    summary_cards = "".join(
        [
            render_metric_card(
                eyebrow="Coverage",
                title="Resolved Instances",
                value=_format_integer(top_line.get("resolved_instance_count")),
                supporting=f"out of {_format_integer(top_line.get('unique_instance_count'))} unique tasks",
                tone="success",
            ),
            render_metric_card(
                eyebrow="Efficiency",
                title="Total Spend",
                value=(
                    _format_currency(top_line.get("total_attempt_cost_usd"))
                    if has_cost_telemetry
                    else "Unavailable"
                ),
                supporting=(
                    f"cost telemetry on {_format_percent(top_line.get('cost_coverage_rate'))} of attempts"
                    if has_cost_telemetry
                    else "this run recorded tokens and latency, but not spend"
                ),
                tone="accent",
            ),
            render_metric_card(
                eyebrow="Latency",
                title="Average Attempt",
                value=_format_duration_ms(top_line.get("avg_attempt_duration_ms")),
                supporting=f"{_format_integer(top_line.get('attempt_rows'))} attempt rows analyzed",
                tone="cool",
            ),
            render_metric_card(
                eyebrow="Tokens",
                title="Total Tokens",
                value=_format_compact_number(top_line.get("total_tokens")),
                supporting=f"in {_format_compact_number(top_line.get('total_input_tokens'))} input and {_format_compact_number(top_line.get('total_output_tokens'))} output tokens",
                tone="warm",
            ),
            render_metric_card(
                eyebrow="Tooling",
                title="Tool Calls",
                value=_format_integer(top_line.get("total_tool_calls")),
                supporting=f"visible on {_format_percent(top_line.get('tool_call_coverage_rate'))} of attempts",
                tone="default",
            ),
            render_metric_card(
                eyebrow="Routing",
                title="Portfolio Cost Floor",
                value=(
                    _format_currency(cost_opportunity.get("portfolio_cost_floor_usd"))
                    if has_priced_routing_analysis
                    else "Unavailable"
                ),
                supporting=(
                    f"best solver gap {_format_currency(cost_opportunity.get('best_solver_mixed_routing_gap_usd'))}"
                    if has_priced_routing_analysis
                    and cost_opportunity.get("best_solver_mixed_routing_gap_usd")
                    is not None
                    else "not enough priced successful overlap to estimate routing savings"
                ),
                tone="highlight",
            ),
        ]
    )

    best_solver_body = (
        f"{top_line.get('best_solver_id') or 'n/a'} leads at "
        f"{_format_percent(top_line.get('best_solver_resolution_rate'))} resolution with "
        f"{_format_currency(top_line.get('best_solver_cost_per_resolved_issue'))} per resolved issue."
        if _coerce_non_negative_int(top_line.get("resolved_instance_count")) > 0
        else "No solver resolved a task in this analysis slice yet, so there is no credible quality leader."
    )
    cheapest_solver_body = (
        f"{top_line.get('cheapest_solver_id') or 'n/a'} is the low-cost anchor at "
        f"{_format_currency(top_line.get('cheapest_solver_cost_per_resolved_issue'))} per resolved issue."
        if has_priced_routing_analysis
        else "A cheapest successful solver only appears once priced successful attempts exist in the joined data."
    )
    cost_opportunity_body = (
        "If you route each already-solved task to the cheapest solver that also solved it, "
        f"the current best-solver spend could drop by {_format_currency(cost_opportunity.get('best_solver_mixed_routing_gap_usd'))}."
        if has_priced_routing_analysis
        else "Spend telemetry is missing or there is no priced overlap yet, so routing savings are not estimable for this run."
    )

    solver_spotlights = "".join(
        [
            render_callout(
                "Best Solver",
                best_solver_body,
                tone="success",
            ),
            render_callout(
                "Cheapest Successful Solver",
                cheapest_solver_body,
                tone="accent",
            ),
            render_callout(
                "Cost Opportunity",
                cost_opportunity_body,
                tone="warning",
            ),
        ]
    )

    solver_columns = [
        {"key": "solver_id", "label": "Solver", "format": "text"},
        {"key": "raw_resolution_rate", "label": "Resolution", "format": "percent"},
        {"key": "resolved_instance_count", "label": "Resolved", "format": "int"},
        {
            "key": "cost_per_resolved_issue",
            "label": "Cost / Resolve",
            "format": "currency",
        },
        {"key": "total_cost_usd", "label": "Total Spend", "format": "currency"},
        {"key": "avg_attempt_duration_ms", "label": "Avg Latency", "format": "ms"},
        {"key": "p95_attempt_duration_ms", "label": "P95 Latency", "format": "ms"},
        {
            "key": "avg_total_tokens_per_attempt",
            "label": "Avg Tokens / Attempt",
            "format": "compact",
        },
        {
            "key": "tokens_per_resolved_issue",
            "label": "Tokens / Resolve",
            "format": "compact",
        },
        {
            "key": "avg_tool_calls_per_attempt",
            "label": "Avg Tool Calls",
            "format": "default",
        },
        {
            "key": "avoidable_spend_usd",
            "label": "Avoidable Spend",
            "format": "currency",
        },
        {"key": "avoidable_share", "label": "Avoidable Share", "format": "percent"},
    ]
    requested_summary_columns = [
        *[
            {"key": column, "label": column, "format": "text"}
            for column in grouped_summary.get("group_by", list(group_by))
        ],
        {"key": "raw_resolution_rate", "label": "Resolution", "format": "percent"},
        {"key": "resolved_instance_count", "label": "Resolved", "format": "int"},
        {
            "key": "cost_per_resolved_issue",
            "label": "Cost / Resolve",
            "format": "currency",
        },
        {"key": "avg_attempt_duration_ms", "label": "Avg Latency", "format": "ms"},
        {
            "key": "avg_total_tokens_per_attempt",
            "label": "Avg Tokens / Attempt",
            "format": "compact",
        },
        {
            "key": "avg_tool_calls_per_attempt",
            "label": "Avg Tool Calls",
            "format": "default",
        },
        {
            "key": "marginal_cost_per_extra_resolve",
            "label": "Marginal Cost",
            "format": "currency",
        },
    ]
    budget_columns = [
        {"key": "budget", "label": "Budget", "format": "currency"},
        {"key": "best_solver_id", "label": "Best Solver", "format": "text"},
        {"key": "best_raw_resolution_rate", "label": "Resolution", "format": "percent"},
        {
            "key": "best_cost_per_resolved_issue",
            "label": "Cost / Resolve",
            "format": "currency",
        },
        {
            "key": "best_latency_ms_per_resolved_issue",
            "label": "Latency / Resolve",
            "format": "ms",
        },
        {"key": "best_total_cost_usd", "label": "Total Spend", "format": "currency"},
        {
            "key": "affordable_solver_ids",
            "label": "Affordable Solvers",
            "format": "text",
        },
    ]
    pareto_columns = [
        {"key": "solver_id", "label": "Solver", "format": "text"},
        {"key": "raw_resolution_rate", "label": "Resolution", "format": "percent"},
        {
            "key": "cost_per_resolved_issue",
            "label": "Cost / Resolve",
            "format": "currency",
        },
        {
            "key": "latency_ms_per_resolved_issue",
            "label": "Latency / Resolve",
            "format": "ms",
        },
        {"key": "resolved_instance_count", "label": "Resolved", "format": "int"},
    ]
    cost_columns = [
        {"key": "solver_id", "label": "Solver", "format": "text"},
        {"key": "resolved_instance_count", "label": "Resolved", "format": "int"},
        {
            "key": "success_spend_usd",
            "label": "Current Success Spend",
            "format": "currency",
        },
        {
            "key": "cheapest_compatible_spend_usd",
            "label": "Cheapest Equivalent Spend",
            "format": "currency",
        },
        {
            "key": "avoidable_spend_usd",
            "label": "Avoidable Spend",
            "format": "currency",
        },
        {"key": "avoidable_share", "label": "Avoidable Share", "format": "percent"},
        {
            "key": "avg_avoidable_spend_per_resolved_issue",
            "label": "Avoidable / Resolve",
            "format": "currency",
        },
    ]
    instance_savings_columns = [
        {"key": "instance_id", "label": "Instance", "format": "text"},
        {"key": "cheapest_solver_id", "label": "Cheapest Solver", "format": "text"},
        {"key": "cheapest_cost_usd", "label": "Cheapest Cost", "format": "currency"},
        {
            "key": "most_expensive_solver_id",
            "label": "Most Expensive Solver",
            "format": "text",
        },
        {
            "key": "most_expensive_cost_usd",
            "label": "Highest Cost",
            "format": "currency",
        },
        {"key": "savings_gap_usd", "label": "Savings Gap", "format": "currency"},
        {
            "key": "available_solver_ids",
            "label": "Successful Solvers",
            "format": "text",
        },
    ]

    metadata_block = ""
    if metadata:
        metadata_block = (
            '<section class="panel"><details class="metadata-panel">'
            "<summary>Run Metadata</summary>"
            f"<pre>{html_escape(json.dumps(metadata, sort_keys=True, indent=2))}</pre>"
            "</details></section>"
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>RepoGauge Analysis</title>
  <style>
    :root {{
      --bg: #f6f3ee;
      --bg-2: #fffdf9;
      --ink: #102135;
      --muted: #5b6879;
      --line: rgba(18, 33, 53, 0.12);
      --panel: rgba(255, 255, 255, 0.82);
      --panel-strong: rgba(255, 255, 255, 0.94);
      --shadow: 0 24px 80px rgba(15, 33, 53, 0.12);
      --blue: #0f4c81;
      --teal: #0f7c82;
      --orange: #eb8f47;
      --green: #198754;
      --rose: #d94f70;
      --violet: #6a52d3;
      --gold: #d5a021;
      --radius-xl: 28px;
      --radius-lg: 20px;
      --radius-md: 14px;
      --font-sans: "Avenir Next", "Segoe UI Variable Text", "SF Pro Display", "Helvetica Neue", sans-serif;
      --font-mono: "SFMono-Regular", "JetBrains Mono", "Cascadia Code", monospace;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; padding: 0; }}
    body {{
      font-family: var(--font-sans);
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(15, 124, 130, 0.16), transparent 28%),
        radial-gradient(circle at top right, rgba(235, 143, 71, 0.14), transparent 26%),
        linear-gradient(180deg, #fbf8f3 0%, #f6f3ee 46%, #f0ece5 100%);
      min-height: 100vh;
    }}
    .page {{
      max-width: 1440px;
      margin: 0 auto;
      padding: 32px 24px 56px;
    }}
    .hero {{
      position: relative;
      overflow: hidden;
      border-radius: 32px;
      padding: 36px;
      background:
        linear-gradient(135deg, rgba(11, 52, 87, 0.96), rgba(18, 76, 129, 0.92) 52%, rgba(15, 124, 130, 0.88));
      color: white;
      box-shadow: var(--shadow);
      isolation: isolate;
    }}
    .hero::before,
    .hero::after {{
      content: "";
      position: absolute;
      border-radius: 999px;
      filter: blur(10px);
      opacity: 0.58;
      z-index: -1;
    }}
    .hero::before {{
      width: 320px;
      height: 320px;
      right: -60px;
      top: -90px;
      background: radial-gradient(circle, rgba(255,255,255,0.22), transparent 68%);
    }}
    .hero::after {{
      width: 240px;
      height: 240px;
      left: -30px;
      bottom: -70px;
      background: radial-gradient(circle, rgba(235,143,71,0.45), transparent 66%);
    }}
    .hero__eyebrow {{
      display: inline-flex;
      align-items: center;
      gap: 10px;
      font-size: 12px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: rgba(255,255,255,0.75);
      margin-bottom: 16px;
    }}
    .hero__eyebrow::before {{
      content: "";
      width: 28px;
      height: 1px;
      background: rgba(255,255,255,0.45);
    }}
    .hero h1 {{
      margin: 0 0 14px;
      font-size: clamp(2.4rem, 4vw, 4rem);
      line-height: 0.96;
      letter-spacing: -0.04em;
      max-width: 12ch;
    }}
    .hero p {{
      margin: 0;
      max-width: 68ch;
      color: rgba(255,255,255,0.86);
      font-size: 1.04rem;
      line-height: 1.6;
    }}
    .hero__stats {{
      margin-top: 26px;
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
    }}
    .hero__stat {{
      padding: 16px 18px;
      border-radius: 18px;
      background: rgba(255,255,255,0.08);
      backdrop-filter: blur(10px);
      border: 1px solid rgba(255,255,255,0.12);
    }}
    .hero__stat-label {{
      font-size: 0.72rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: rgba(255,255,255,0.68);
    }}
    .hero__stat-value {{
      display: block;
      margin-top: 10px;
      font-size: 1.3rem;
      font-weight: 700;
      letter-spacing: -0.03em;
    }}
    .section-grid {{
      display: grid;
      gap: 22px;
      margin-top: 24px;
    }}
    .metric-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 18px;
    }}
    .spotlight-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 18px;
    }}
    .two-up {{
      display: grid;
      grid-template-columns: 1.45fr 1fr;
      gap: 22px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid rgba(16, 33, 53, 0.08);
      border-radius: var(--radius-xl);
      box-shadow: var(--shadow);
      padding: 24px;
      backdrop-filter: blur(12px);
      animation: panel-enter 480ms ease both;
    }}
    .panel--chart {{
      overflow: hidden;
    }}
    .section-heading {{
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 16px;
    }}
    .section-heading h2 {{
      margin: 0;
      font-size: 1.28rem;
      letter-spacing: -0.03em;
    }}
    .section-heading p {{
      margin: 0;
      color: var(--muted);
      max-width: 76ch;
      line-height: 1.55;
    }}
    .metric-card {{
      position: relative;
      overflow: hidden;
      min-height: 188px;
      border-radius: 24px;
      padding: 22px;
      background: var(--panel-strong);
      border: 1px solid rgba(16, 33, 53, 0.08);
      box-shadow: 0 18px 45px rgba(16, 33, 53, 0.08);
    }}
    .metric-card::before {{
      content: "";
      position: absolute;
      inset: 0 auto auto 0;
      width: 100%;
      height: 4px;
      background: linear-gradient(90deg, rgba(15, 76, 129, 0.18), rgba(15, 124, 130, 0.68), rgba(235, 143, 71, 0.68));
    }}
    .metric-card--success::before {{ background: linear-gradient(90deg, rgba(25,135,84,0.25), rgba(25,135,84,0.9)); }}
    .metric-card--accent::before {{ background: linear-gradient(90deg, rgba(15,76,129,0.3), rgba(15,124,130,0.88)); }}
    .metric-card--cool::before {{ background: linear-gradient(90deg, rgba(106,82,211,0.24), rgba(75,123,236,0.92)); }}
    .metric-card--warm::before {{ background: linear-gradient(90deg, rgba(235,143,71,0.28), rgba(235,143,71,0.95)); }}
    .metric-card--highlight::before {{ background: linear-gradient(90deg, rgba(213,160,33,0.28), rgba(213,160,33,0.95)); }}
    .metric-card__eyebrow {{
      display: inline-block;
      font-size: 0.72rem;
      font-weight: 700;
      letter-spacing: 0.09em;
      text-transform: uppercase;
      color: var(--muted);
    }}
    .metric-card__title {{
      margin: 12px 0 10px;
      font-size: 1rem;
      font-weight: 600;
      color: var(--ink);
    }}
    .metric-card__value {{
      font-size: clamp(1.7rem, 2.5vw, 2.6rem);
      line-height: 0.95;
      letter-spacing: -0.05em;
      font-weight: 700;
    }}
    .metric-card__support {{
      margin: 14px 0 0;
      color: var(--muted);
      line-height: 1.5;
      max-width: 26ch;
    }}
    .callout {{
      border-radius: 22px;
      padding: 22px;
      background: rgba(255,255,255,0.72);
      border: 1px solid rgba(16,33,53,0.08);
    }}
    .callout h3 {{
      margin: 0 0 8px;
      font-size: 0.9rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
    }}
    .callout p {{
      margin: 0;
      font-size: 1.08rem;
      line-height: 1.6;
      letter-spacing: -0.015em;
    }}
    .callout--success {{ background: linear-gradient(180deg, rgba(25,135,84,0.08), rgba(255,255,255,0.88)); }}
    .callout--accent {{ background: linear-gradient(180deg, rgba(15,76,129,0.08), rgba(255,255,255,0.88)); }}
    .callout--warning {{ background: linear-gradient(180deg, rgba(235,143,71,0.11), rgba(255,255,255,0.88)); }}
    .table-shell {{
      overflow: auto;
      border-radius: 18px;
      border: 1px solid rgba(16,33,53,0.08);
      background: rgba(255,255,255,0.72);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      min-width: 820px;
    }}
    th, td {{
      padding: 14px 16px;
      border-bottom: 1px solid rgba(16,33,53,0.08);
      text-align: left;
      font-size: 0.94rem;
      vertical-align: top;
    }}
    thead th {{
      position: sticky;
      top: 0;
      background: rgba(255,255,255,0.96);
      z-index: 1;
      font-size: 0.79rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
    }}
    tbody tr:hover {{
      background: rgba(15,124,130,0.04);
    }}
    .sort-button {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      border: 0;
      padding: 0;
      background: transparent;
      color: inherit;
      font: inherit;
      text-transform: inherit;
      letter-spacing: inherit;
      cursor: pointer;
    }}
    .sort-button__glyph {{
      font-size: 0.92em;
      opacity: 0.55;
    }}
    .bar-list {{
      list-style: none;
      margin: 0;
      padding: 0;
      display: grid;
      gap: 14px;
    }}
    .bar-list__item {{
      padding: 0;
    }}
    .bar-list__header {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 8px;
      color: var(--ink);
    }}
    .bar-list__track {{
      height: 12px;
      border-radius: 999px;
      background: rgba(16,33,53,0.08);
      overflow: hidden;
    }}
    .bar-list__fill {{
      display: block;
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, var(--blue), var(--teal));
    }}
    .bar-list__fill--blue {{ background: linear-gradient(90deg, var(--blue), var(--teal)); }}
    .bar-list__fill--orange {{ background: linear-gradient(90deg, var(--orange), #f5c257); }}
    .bar-list__fill--rose {{ background: linear-gradient(90deg, var(--rose), #ff8fa3); }}
    .chart-shell {{
      display: grid;
      gap: 18px;
    }}
    .chart-surface {{
      fill: rgba(255,255,255,0.68);
      stroke: rgba(16,33,53,0.08);
    }}
    .chart-grid {{
      stroke: rgba(16,33,53,0.08);
      stroke-width: 1;
    }}
    .chart-grid--vertical {{
      stroke-dasharray: 4 7;
    }}
    .chart-axis {{
      stroke: rgba(16,33,53,0.22);
      stroke-width: 1.5;
    }}
    .chart-axis-label {{
      fill: rgba(16,33,53,0.64);
      font-size: 11px;
      font-family: var(--font-sans);
    }}
    .chart-title {{
      fill: rgba(16,33,53,0.9);
      font-size: 13px;
      font-weight: 600;
      font-family: var(--font-sans);
    }}
    .chart-point-label {{
      fill: rgba(16,33,53,0.84);
      font-size: 11px;
      font-weight: 700;
      font-family: var(--font-sans);
      letter-spacing: 0.01em;
    }}
    .legend {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px 18px;
      list-style: none;
      margin: 0;
      padding: 0;
    }}
    .legend__item {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
    }}
    .legend__swatch {{
      width: 12px;
      height: 12px;
      border-radius: 999px;
    }}
    .chip {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 7px 11px;
      border-radius: 999px;
      background: rgba(15,76,129,0.10);
      color: var(--blue);
      font-size: 0.77rem;
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }}
    .chip--warning {{
      background: rgba(235,143,71,0.12);
      color: #a55a00;
    }}
    .incident-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 18px;
    }}
    .incident-card {{
      border-radius: 20px;
      padding: 20px;
      background: rgba(255,255,255,0.78);
      border: 1px solid rgba(16,33,53,0.08);
    }}
    .incident-card__top {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
    }}
    .incident-card h3 {{
      margin: 16px 0 8px;
      font-size: 1.02rem;
      letter-spacing: -0.02em;
    }}
    .incident-card p {{
      margin: 0;
      color: var(--muted);
      line-height: 1.55;
    }}
    .incident-card__meta {{
      margin: 18px 0 0;
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }}
    .incident-card__meta div {{
      padding: 12px;
      border-radius: 14px;
      background: rgba(16,33,53,0.04);
    }}
    .incident-card__meta dt {{
      font-size: 0.76rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      margin-bottom: 6px;
    }}
    .incident-card__meta dd {{
      margin: 0;
      font-weight: 600;
      line-height: 1.45;
    }}
    .metadata-panel {{
      border-radius: 18px;
      background: rgba(255,255,255,0.5);
    }}
    .metadata-panel summary {{
      cursor: pointer;
      font-weight: 700;
    }}
    pre {{
      margin: 16px 0 0;
      padding: 18px;
      overflow: auto;
      background: rgba(16,33,53,0.92);
      color: #f4f5f7;
      border-radius: 18px;
      font-family: var(--font-mono);
      font-size: 12px;
      line-height: 1.6;
    }}
    .empty-state {{
      border-radius: 18px;
      padding: 22px;
      background: rgba(16,33,53,0.04);
      color: var(--muted);
    }}
    @keyframes panel-enter {{
      from {{
        opacity: 0;
        transform: translateY(8px);
      }}
      to {{
        opacity: 1;
        transform: translateY(0);
      }}
    }}
    @media (max-width: 1120px) {{
      .metric-grid,
      .spotlight-grid,
      .two-up,
      .incident-grid,
      .hero__stats {{
        grid-template-columns: 1fr 1fr;
      }}
    }}
    @media (max-width: 760px) {{
      .page {{ padding: 18px 14px 32px; }}
      .hero {{ padding: 24px; }}
      .metric-grid,
      .spotlight-grid,
      .two-up,
      .incident-grid,
      .hero__stats {{
        grid-template-columns: 1fr;
      }}
      .section-heading {{
        flex-direction: column;
        align-items: flex-start;
      }}
    }}
  </style>
</head>
<body>
  <main class="page">
    <header class="hero">
      <span class="hero__eyebrow">RepoGauge Analysis</span>
      <h1>Model performance you can actually route by.</h1>
      <p>
        This report turns raw run and evaluation artifacts into a decision surface:
        which solvers win, which ones are cheap, how their latency and token burn compare,
        and how much spend you could claw back by switching already-solvable work to cheaper models.
      </p>
      <div class="hero__stats">
        <div class="hero__stat"><span class="hero__stat-label">Best Solver</span><span class="hero__stat-value">{
        html_escape(_coerce_str(top_line.get("best_solver_id") or "n/a"))
    }</span></div>
        <div class="hero__stat"><span class="hero__stat-label">Cheapest Solver</span><span class="hero__stat-value">{
        html_escape(_coerce_str(top_line.get("cheapest_solver_id") or "n/a"))
    }</span></div>
        <div class="hero__stat"><span class="hero__stat-label">Group By</span><span class="hero__stat-value">{
        html_escape(", ".join(top_line.get("group_by", [])) or "solver_id")
    }</span></div>
        <div class="hero__stat"><span class="hero__stat-label">Expensive Threshold</span><span class="hero__stat-value">{
        html_escape(_format_currency(top_line.get("expensive_cost_threshold")))
    }</span></div>
      </div>
    </header>

    <section class="section-grid">
      <div class="metric-grid">{summary_cards}</div>
      <div class="spotlight-grid">{solver_spotlights}</div>
      {
        render_table(
            title="Solver Comparison",
            subtitle="Resolution, cost, latency, tokens, and tool usage on one surface. Click any column to sort.",
            table_id="solver-comparison",
            rows=solver_rows,
            columns=solver_columns,
            empty_message="No solver summaries were produced for this run.",
        )
    }
      <div class="two-up">
        {render_scatter_plot(solver_rows)}
        {
        render_bar_list(
            title="Failure Reasons",
            subtitle="Share of unresolved rows by dominant harness outcome or failure reason.",
            rows=failure_breakdown,
            label_key="reason",
            value_key="count",
            formatter="int",
            accent="rose",
            empty_message="No unresolved rows were present in the joined analysis set.",
        )
    }
      </div>
      <div class="two-up">
        {
        render_table(
            title="Cost Opportunities",
            subtitle="How much each solver spends today on the tasks it solved, versus the cheapest solver that also solved the same tasks.",
            table_id="cost-opportunities",
            rows=cost_opportunity.get("solver_savings", []),
            columns=cost_columns,
            empty_message="No priced successful attempts were available to estimate substitution savings.",
        )
    }
        {
        render_bar_list(
            title="Potential Savings By Solver",
            subtitle="Avoidable spend if successful tasks were handed to the cheapest successful model instead.",
            rows=cost_opportunity.get("solver_savings", []),
            label_key="solver_id",
            value_key="avoidable_spend_usd",
            formatter="currency",
            accent="orange",
            empty_message="Savings bars unlock when multiple priced solvers resolve the same tasks.",
        )
    }
      </div>
      {
        render_table(
            title="High-Leverage Instance Savings",
            subtitle="Concrete examples where solver substitution changes spend the most while preserving success.",
            table_id="instance-savings",
            rows=cost_opportunity.get("instance_savings_samples", []),
            columns=instance_savings_columns,
            empty_message="No instance had two priced successful solvers to compare.",
        )
    }
      {
        render_table(
            title="Requested Summary",
            subtitle="The custom grouped rollup that `repogauge analyze` was asked to produce.",
            table_id="requested-summary",
            rows=grouped_summary.get("rows", []),
            columns=requested_summary_columns,
            empty_message="No grouped summary rows were available.",
        )
    }
      <div class="two-up">
        {
        render_table(
            title="Budget Frontier",
            subtitle="At each budget ceiling, this is the best affordable solver. The `best_solver_id` is the answer to the practical question: if this is my budget, what should I run?",
            table_id="budget-frontier",
            rows=budget_frontier,
            columns=budget_columns,
            empty_message="No affordable solver rows had both spend and resolved issues.",
        )
    }
        {
        render_table(
            title="Pareto Frontier",
            subtitle="Rows here are not dominated on resolution, cost, and latency. They are the serious candidates.",
            table_id="pareto-frontier",
            rows=pareto_frontier,
            columns=pareto_columns,
            empty_message="No frontier rows were available for this slice.",
        )
    }
      </div>
      {render_unresolved_cards(unresolved_samples)}
      {metadata_block}
    </section>
  </main>
  <script>
    (() => {{
      const parseValue = (raw) => {{
        if (raw === undefined || raw === null || raw === "") return Number.NEGATIVE_INFINITY;
        const numeric = Number(raw);
        return Number.isNaN(numeric) ? raw.toString().toLowerCase() : numeric;
      }};
      document.querySelectorAll(".sort-button").forEach((button) => {{
        button.addEventListener("click", () => {{
          const table = document.getElementById(button.dataset.table);
          if (!table) return;
          const tbody = table.querySelector("tbody");
          if (!tbody) return;
          const key = button.dataset.key;
          const current = button.dataset.direction === "asc" ? "asc" : "desc";
          const nextDirection = current === "asc" ? "desc" : "asc";
          document
            .querySelectorAll(`.sort-button[data-table="${{button.dataset.table}}"]`)
            .forEach((peer) => (peer.dataset.direction = ""));
          button.dataset.direction = nextDirection;
          const rows = Array.from(tbody.querySelectorAll("tr"));
          rows.sort((left, right) => {{
            const leftCell = left.querySelector(`td[data-key="${{key}}"]`);
            const rightCell = right.querySelector(`td[data-key="${{key}}"]`);
            const leftValue = parseValue(leftCell?.dataset.sortValue);
            const rightValue = parseValue(rightCell?.dataset.sortValue);
            if (leftValue < rightValue) return nextDirection === "asc" ? -1 : 1;
            if (leftValue > rightValue) return nextDirection === "asc" ? 1 : -1;
            return 0;
          }});
          rows.forEach((row) => tbody.appendChild(row));
        }});
      }});
    }})();
  </script>
</body>
</html>
"""
    path.write_text(html + "\n", encoding="utf-8")


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
        explicit_cost = _read_row_cost(item)
        if explicit_cost is not None:
            item["attempt_cost_usd"] = explicit_cost
            item["attempt_cost_source"] = "explicit"
        else:
            item["attempt_cost_usd"] = _estimate_row_cost_from_tokens(item)
            item["attempt_cost_source"] = (
                "estimated_from_tokens" if item["attempt_cost_usd"] is not None else ""
            )
        item["instance_id"] = attempt_instance
        input_tokens, output_tokens, total_tokens = _read_usage_tokens(item)
        item["input_tokens"] = input_tokens
        item["output_tokens"] = output_tokens
        item["total_tokens"] = total_tokens
        item["tool_calls"] = _read_tool_call_count(item)

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
        attempt_durations_ms: list[int] = []
        expensive_resolved_instances = set[str]()
        instance_costs: dict[str, list[float]] = defaultdict(list)
        total_input_tokens = 0
        total_output_tokens = 0
        total_tokens = 0
        total_tool_calls = 0

        for row in rows:
            instance_id = _coerce_str(row.get("instance_id"))
            if not instance_id:
                continue
            instances[instance_id] = True
            duration = _coerce_non_negative_int(row.get("duration_ms"))
            duration_ms_sum += duration
            attempt_durations_ms.append(duration)
            cost = row.get("attempt_cost_usd")
            if isinstance(cost, (int, float)) and cost > 0:
                instance_costs[instance_id].append(float(cost))
            total_input_tokens += _coerce_non_negative_int(row.get("input_tokens"))
            total_output_tokens += _coerce_non_negative_int(row.get("output_tokens"))
            total_tokens += _coerce_non_negative_int(row.get("total_tokens"))
            total_tool_calls += _coerce_non_negative_int(row.get("tool_calls"))
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
        avg_attempt_duration_ms = (
            duration_ms_sum / attempt_count if attempt_count > 0 else 0.0
        )
        avg_total_tokens_per_attempt = (
            total_tokens / attempt_count if attempt_count > 0 else 0.0
        )
        tokens_per_resolved_issue = (
            total_tokens / resolved_instance_count
            if resolved_instance_count > 0
            else None
        )
        avg_tool_calls_per_attempt = (
            total_tool_calls / attempt_count if attempt_count > 0 else 0.0
        )
        tool_calls_per_resolved_issue = (
            total_tool_calls / resolved_instance_count
            if resolved_instance_count > 0
            else None
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
                avg_attempt_duration_ms=avg_attempt_duration_ms,
                p50_attempt_duration_ms=_percentile(attempt_durations_ms, 50),
                p95_attempt_duration_ms=_percentile(attempt_durations_ms, 95),
                total_input_tokens=total_input_tokens,
                total_output_tokens=total_output_tokens,
                total_tokens=total_tokens,
                avg_total_tokens_per_attempt=avg_total_tokens_per_attempt,
                tokens_per_resolved_issue=tokens_per_resolved_issue,
                total_tool_calls=total_tool_calls,
                avg_tool_calls_per_attempt=avg_tool_calls_per_attempt,
                tool_calls_per_resolved_issue=tool_calls_per_resolved_issue,
            )
        )

    summaries.sort(key=lambda item: item.group)
    return summaries
