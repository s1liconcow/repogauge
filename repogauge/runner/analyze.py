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
    direct_cost = read_cost_usd(row.get("cost"))
    if direct_cost is not None:
        return direct_cost

    metadata = row.get("metadata")
    if isinstance(metadata, Mapping):
        metadata_cost = read_cost_usd(metadata.get("cost"))
        if metadata_cost is not None:
            return metadata_cost

    telemetry = _telemetry_events(row)
    if not telemetry:
        return None
    total_cost = 0.0
    saw_cost = False
    for event in telemetry:
        part = event.get("part")
        part_mapping = part if isinstance(part, Mapping) else {}
        for candidate in (event, part_mapping):
            mapped_cost = read_cost_usd(candidate.get("cost"))
            if mapped_cost is not None and mapped_cost > 0:
                total_cost += mapped_cost
                saw_cost = True
                break
            try:
                numeric_cost = max(0.0, float(candidate.get("cost")))
            except (TypeError, ValueError):
                continue
            if numeric_cost > 0:
                total_cost += numeric_cost
                saw_cost = True
                break
    return total_cost if saw_cost else None


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
    if isinstance(usage, Mapping) and usage:
        return usage

    metadata = row.get("metadata")
    if isinstance(metadata, Mapping):
        metadata_usage = metadata.get("usage")
        if isinstance(metadata_usage, Mapping) and metadata_usage:
            return metadata_usage

    telemetry = _telemetry_events(row)
    if not telemetry:
        return {}

    uncached_input_tokens = 0
    cached_input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    reasoning_tokens = 0
    saw_tokens = False

    for event in telemetry:
        part = event.get("part")
        part_mapping = part if isinstance(part, Mapping) else {}
        for candidate in (event, part_mapping):
            explicit_usage = candidate.get("usage")
            if isinstance(explicit_usage, Mapping) and explicit_usage:
                return explicit_usage

        tokens = part_mapping.get("tokens")
        if not isinstance(tokens, Mapping):
            continue
        saw_tokens = True
        uncached_input_tokens += _int_from_candidates(tokens, ("input",)) or 0
        output_tokens += _int_from_candidates(tokens, ("output",)) or 0
        total_tokens += _int_from_candidates(tokens, ("total",)) or 0
        reasoning_tokens += _int_from_candidates(tokens, ("reasoning",)) or 0
        cache = tokens.get("cache")
        if isinstance(cache, Mapping):
            cached_input_tokens += _int_from_candidates(cache, ("read",)) or 0

    if not saw_tokens:
        return {}

    usage = {
        "input_tokens": uncached_input_tokens + cached_input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens
        if total_tokens > 0
        else uncached_input_tokens + cached_input_tokens + output_tokens,
    }
    if cached_input_tokens > 0:
        usage["cached_input_tokens"] = cached_input_tokens
    if reasoning_tokens > 0:
        usage["reasoning_tokens"] = reasoning_tokens
    return usage


def _telemetry_events(row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    metadata = row.get("metadata")
    if not isinstance(metadata, Mapping):
        return []
    telemetry = metadata.get("telemetry")
    if not isinstance(telemetry, list):
        return []
    return [event for event in telemetry if isinstance(event, Mapping)]


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
    if payload_type in {"tool_call", "function_call", "tool_use"}:
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
        telemetry = _telemetry_events(row)
        if telemetry:
            telemetry_count = sum(
                _tool_call_count_in_event(event) for event in telemetry
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


def _row_outcome_label(row: Mapping[str, Any]) -> str:
    outcome = _coerce_str(row.get("harness_outcome"))
    if outcome:
        return outcome
    outcome = _coerce_str(row.get("status"))
    if outcome:
        return outcome
    return "unknown"


def _normalize_failure_reason_label(value: Any) -> str:
    raw = _coerce_str(value).strip()
    if not raw:
        return ""
    lowered = raw.lower()
    normalized = lowered.replace("-", "_").replace(" ", "_")
    if lowered.startswith("invalid patch:") or normalized == "invalid_patch":
        return "invalid_patch"
    if "timeout" in lowered or normalized == "timed_out":
        return "timeout"
    if "hit your limit" in lowered or "rate limit" in lowered:
        return "rate_limit"
    if "model not found" in lowered:
        return "model_not_found"
    return raw


def _telemetry_failure_reason(row: Mapping[str, Any]) -> str:
    for event in _telemetry_events(row):
        error = event.get("error")
        if not isinstance(error, Mapping):
            continue
        data = error.get("data")
        message = ""
        if isinstance(data, Mapping):
            message = _coerce_str(data.get("message"))
        reason = _normalize_failure_reason_label(message or error.get("name"))
        if reason:
            return reason
    return ""


def _has_error_only_raw_output(row: Mapping[str, Any]) -> bool:
    raw_output = _coerce_str(row.get("raw_output")).strip()
    if not raw_output:
        return False
    payloads: list[Mapping[str, Any]] = []
    for line in raw_output.splitlines():
        payload = _safe_json_parse(line.strip())
        if not isinstance(payload, Mapping):
            return False
        payloads.append(payload)
    return bool(payloads) and all(
        _coerce_str(payload.get("type")).strip().lower() == "error"
        for payload in payloads
    )


def _should_prefer_telemetry_failure_reason(
    row: Mapping[str, Any], telemetry_reason: str
) -> bool:
    if telemetry_reason == "model_not_found":
        _, _, total_tokens = _read_usage_tokens(row)
        tool_calls = _coerce_non_negative_int(row.get("tool_calls"))
        if total_tokens == 0 and tool_calls == 0 and _has_error_only_raw_output(row):
            return True
    exit_reason = _normalize_failure_reason_label(row.get("exit_reason"))
    return bool(telemetry_reason and exit_reason == "invalid_patch")


def _row_failure_reason(row: Mapping[str, Any]) -> str:
    telemetry_reason = _telemetry_failure_reason(row)
    if _should_prefer_telemetry_failure_reason(row, telemetry_reason):
        return telemetry_reason
    reason = _coerce_str(row.get("failure_reason"))
    if reason:
        return reason
    reason = _coerce_str(row.get("reason"))
    if reason:
        return reason
    exit_reason = _normalize_failure_reason_label(row.get("exit_reason"))
    if exit_reason:
        return exit_reason
    if telemetry_reason:
        return telemetry_reason
    attempt_state = _coerce_str(row.get("attempt_state")).strip().lower()
    if attempt_state in {"failed", "invalid_patch", "timed_out"}:
        return _normalize_failure_reason_label(attempt_state)
    return ""


def _build_failure_breakdown(
    unresolved_rows: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for row in unresolved_rows:
        reason = _row_failure_reason(row)
        if not reason:
            reason = _row_outcome_label(row)
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
        failure_reason = _row_failure_reason(row) or _row_outcome_label(row)
        samples.append(
            {
                "instance_id": _coerce_str(row.get("instance_id")),
                "solver_id": _solver_id_from_row(row),
                "harness_outcome": _row_outcome_label(row),
                "failure_reason": failure_reason,
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


def build_attempt_browser(
    *,
    attempts: list[dict[str, Any]],
    instance_results: list[dict[str, Any]],
    llm_judge_rows: list[Mapping[str, Any]] | None = None,
    max_instances: int = 60,
    max_patch_chars: int = 60000,
    max_problem_chars: int = 8000,
) -> dict[str, Any]:
    """Produce a per-instance payload for the report's attempt browser tab."""
    judge_by_key: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in llm_judge_rows or []:
        solver_id = _coerce_str(row.get("solver_id"))
        instance_id = _coerce_str(row.get("instance_id"))
        if solver_id and instance_id:
            judge_by_key[(solver_id, instance_id)] = row

    joined = join_attempt_rows(attempts, instance_results)
    latest_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in joined:
        solver_id = _solver_id_from_row(row)
        instance_id = _coerce_str(row.get("instance_id"))
        if not solver_id or not instance_id:
            continue
        key = (solver_id, instance_id)
        existing = latest_by_key.get(key)
        row_ended = _coerce_str(row.get("attempt_ended_at"))
        existing_ended = (
            _coerce_str(existing.get("attempt_ended_at")) if existing else ""
        )
        row_idx = _coerce_non_negative_int(row.get("attempt_index"))
        existing_idx = (
            _coerce_non_negative_int(existing.get("attempt_index")) if existing else -1
        )
        if (
            existing is None
            or row_ended > existing_ended
            or (row_ended == existing_ended and row_idx > existing_idx)
        ):
            latest_by_key[key] = dict(row)

    by_instance: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (solver_id, instance_id), row in latest_by_key.items():
        patch = _coerce_str(row.get("model_patch"))
        patch_truncated = False
        if len(patch) > max_patch_chars:
            patch = patch[:max_patch_chars]
            patch_truncated = True

        judge_row = judge_by_key.get((solver_id, instance_id))
        judge_payload: dict[str, Any] | None = None
        if judge_row is not None:
            dimensions_raw = judge_row.get("dimensions") or []
            dimensions: list[dict[str, Any]] = []
            if isinstance(dimensions_raw, list):
                for dim in dimensions_raw:
                    if not isinstance(dim, Mapping):
                        continue
                    dimensions.append(
                        {
                            "name": _coerce_str(dim.get("name")),
                            "weight": dim.get("weight"),
                            "delta": dim.get("delta"),
                            "label": _coerce_str(dim.get("label")),
                            "rationale": _coerce_str(dim.get("rationale")),
                        }
                    )
            judge_payload = {
                "summary": _coerce_str(judge_row.get("summary")),
                "overall_label": _coerce_str(judge_row.get("overall_label")),
                "overall_delta": judge_row.get("overall_delta"),
                "confidence": judge_row.get("confidence"),
                "dimensions": dimensions,
            }

        by_instance[instance_id].append(
            {
                "solver_id": solver_id,
                "attempt_id": _coerce_str(row.get("attempt_id")),
                "attempt_state": _coerce_str(row.get("attempt_state")),
                "exit_reason": _coerce_str(row.get("exit_reason")),
                "resolved": bool(row.get("resolved")),
                "harness_outcome": _row_outcome_label(row),
                "failure_reason": _row_failure_reason(row),
                "duration_ms": _coerce_non_negative_int(row.get("duration_ms")),
                "attempt_cost_usd": row.get("attempt_cost_usd"),
                "input_tokens": _coerce_non_negative_int(row.get("input_tokens")),
                "output_tokens": _coerce_non_negative_int(row.get("output_tokens")),
                "total_tokens": _coerce_non_negative_int(row.get("total_tokens")),
                "tool_calls": _coerce_non_negative_int(row.get("tool_calls")),
                "model_patch": patch,
                "model_patch_truncated": patch_truncated,
                "model_patch_length": _coerce_non_negative_int(row.get("patch_length"))
                or len(_coerce_str(row.get("model_patch"))),
                "judge": judge_payload,
            }
        )

    instances_with_problem: dict[str, str] = {}
    instance_repo: dict[str, str] = {}
    for attempt in attempts:
        instance_id = _coerce_str(attempt.get("instance_id"))
        if not instance_id:
            continue
        statement = _coerce_str(attempt.get("problem_statement"))
        if statement and instance_id not in instances_with_problem:
            instances_with_problem[instance_id] = statement
        repo = _coerce_str(attempt.get("instance_repo"))
        if repo and instance_id not in instance_repo:
            instance_repo[instance_id] = repo

    instances: list[dict[str, Any]] = []
    for instance_id, solver_rows in by_instance.items():
        solver_rows.sort(key=lambda item: item["solver_id"])
        resolved_solvers = sum(1 for item in solver_rows if item["resolved"])
        problem_text = instances_with_problem.get(instance_id, "")
        problem_truncated = False
        if len(problem_text) > max_problem_chars:
            problem_text = problem_text[:max_problem_chars]
            problem_truncated = True
        instances.append(
            {
                "instance_id": instance_id,
                "instance_repo": instance_repo.get(instance_id, ""),
                "problem_statement": problem_text,
                "problem_statement_truncated": problem_truncated,
                "solver_count": len(solver_rows),
                "resolved_count": resolved_solvers,
                "solvers": solver_rows,
            }
        )

    def _sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
        share_denom = max(item["solver_count"], 1)
        resolution_bucket = int((item["resolved_count"] / share_denom) * 100)
        return (-resolution_bucket, -item["solver_count"], item["instance_id"])

    instances.sort(key=_sort_key)
    truncated = len(instances) > max_instances
    if truncated:
        instances = instances[:max_instances]

    return {
        "instances": instances,
        "instance_count": len(by_instance),
        "rendered_count": len(instances),
        "truncated": truncated,
        "judge_available": bool(judge_by_key),
    }


def build_analysis_report(
    *,
    attempts: list[dict[str, Any]],
    instance_results: list[dict[str, Any]],
    grouped_summaries: list[ResolutionMetrics],
    solver_summaries: list[ResolutionMetrics],
    group_by: tuple[str, ...],
    expensive_cost_threshold: float,
    metadata: dict[str, Any] | None = None,
    llm_judge_report: dict[str, Any] | None = None,
    llm_judge_rows: list[Mapping[str, Any]] | None = None,
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
    if llm_judge_report is not None:
        report["llm_judge"] = llm_judge_report

    report["attempt_browser"] = build_attempt_browser(
        attempts=attempts,
        instance_results=instance_results,
        llm_judge_rows=llm_judge_rows,
    )

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
    for dimension, value in summary.group:
        payload.setdefault(dimension, value)
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
    attempt_browser = report.get("attempt_browser", {})
    failure_breakdown = report.get("failure_reason_breakdown", [])
    unresolved_samples = report.get("unresolved_samples", [])
    cost_opportunity = report.get("cost_opportunity", {})
    llm_judge = report.get("llm_judge", {})

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
        use_absolute_rate_scale = 0.0 <= min_rate and max_rate <= 1.0
        palette = (
            "#635bff",
            "#22d3ee",
            "#f472b6",
            "#a78bfa",
            "#10b981",
            "#fbbf24",
            "#f43f5e",
            "#38bdf8",
        )

        def x_pos(cost: float) -> float:
            if max_cost == min_cost:
                return margin_left + plot_width / 2
            return (
                margin_left + ((cost - min_cost) / (max_cost - min_cost)) * plot_width
            )

        def y_pos(rate: float) -> float:
            if use_absolute_rate_scale:
                return margin_top + plot_height - (rate * plot_height)
            if max_rate == min_rate:
                return margin_top + plot_height / 2
            return margin_top + plot_height - (
                ((rate - min_rate) / (max_rate - min_rate)) * plot_height
            )

        y_ticks = []
        for step in range(0, 5):
            tick_rate = (
                step / 4
                if use_absolute_rate_scale
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
                f'<span class="chip chip--warning">{html_escape(_row_failure_reason(row) or _row_outcome_label(row))}</span></div>'
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

    def render_diff(patch: str, truncated: bool) -> str:
        if not patch:
            return (
                '<div class="empty-state">This attempt produced no patch. '
                "The harness saw an empty diff, so there is nothing to compare.</div>"
            )
        line_html: list[str] = []
        for raw_line in patch.splitlines():
            line_html.append(html_escape(raw_line) or "&nbsp;")
        trailer = (
            '<div class="diff-viewer__truncated">Diff truncated for display — '
            "download the run artifacts for the full patch.</div>"
            if truncated
            else ""
        )
        diff_body = "".join(f"{line}\n" for line in line_html)
        payload = json.dumps({"patch": patch, "truncated": truncated}).replace(
            "</", "<\\/"
        )
        return (
            '<div class="diff-viewer" data-diff-viewer>'
            '<div class="diff-viewer__loading" data-diff-loading>'
            "Loading Diffs viewer…</div>"
            '<div class="diff-viewer__mount" data-diff-mount hidden></div>'
            '<div class="diff-viewer__fallback" data-diff-fallback hidden>'
            '<div class="diff-viewer__fallback-note">'
            "Diffs failed to load, showing the raw patch instead.</div>"
            f'<pre class="diff-viewer__fallback-pre">{diff_body}</pre>'
            "</div>"
            f'<script type="application/json" data-diff-payload>{payload}</script>'
            "</div>"
            f"{trailer}"
        )

    def render_judge_block(judge: Mapping[str, Any] | None) -> str:
        if not judge:
            return (
                '<div class="judge-card judge-card--muted">'
                '<div class="judge-card__title">LLM Judge</div>'
                '<p class="judge-card__empty">No judge analysis was produced for this attempt.</p>'
                "</div>"
            )
        label = _coerce_str(judge.get("overall_label")) or "same"
        delta = judge.get("overall_delta")
        confidence = judge.get("confidence")
        summary = _coerce_str(judge.get("summary")) or "—"
        tone = label.replace("_", "-") or "same"
        dims = judge.get("dimensions") or []
        dim_html = ""
        if dims:
            chips = []
            for dim in dims:
                if not isinstance(dim, Mapping):
                    continue
                name = _coerce_str(dim.get("name")).replace("_", " ").title() or "—"
                dlabel = _coerce_str(dim.get("label")) or "same"
                dtone = dlabel.replace("_", "-") or "same"
                dvalue = dim.get("delta")
                dvalue_str = (
                    f"Δ{int(dvalue):+d}" if isinstance(dvalue, (int, float)) else ""
                )
                rationale = _coerce_str(dim.get("rationale"))
                chips.append(
                    '<div class="dim">'
                    f'<div class="dim__head">'
                    f'<span class="dim__name">{html_escape(name)}</span>'
                    f'<span class="chip chip--judge chip--judge-{html_escape(dtone)}">'
                    f"{html_escape(dlabel.replace('_', ' '))}"
                    f" <em>{html_escape(dvalue_str)}</em></span>"
                    "</div>"
                    f'<p class="dim__rationale">{html_escape(rationale) or "—"}</p>'
                    "</div>"
                )
            dim_html = f'<div class="dim-grid">{"".join(chips)}</div>'
        delta_str = f"{float(delta):+.2f}" if isinstance(delta, (int, float)) else "—"
        conf_str = (
            f"{float(confidence):.2f}"
            if isinstance(confidence, (int, float)) and confidence > 0
            else "—"
        )
        return (
            '<div class="judge-card">'
            '<div class="judge-card__head">'
            '<span class="judge-card__title">LLM Judge</span>'
            f'<span class="chip chip--judge chip--judge-{html_escape(tone)}">'
            f"{html_escape(label.replace('_', ' '))}</span>"
            "</div>"
            '<dl class="judge-card__meta">'
            f"<div><dt>Overall Δ</dt><dd>{html_escape(delta_str)}</dd></div>"
            f"<div><dt>Confidence</dt><dd>{html_escape(conf_str)}</dd></div>"
            "</dl>"
            f'<p class="judge-card__summary">{html_escape(summary)}</p>'
            f"{dim_html}"
            "</div>"
        )

    def render_attempt_browser(payload: Mapping[str, Any]) -> str:
        instances = list(payload.get("instances") or [])
        if not instances:
            return (
                '<section class="panel"><div class="section-heading"><h2>Attempt Browser</h2>'
                "<p>No attempts were available to inspect.</p></div>"
                '<div class="empty-state">Run <code>repogauge analyze</code> on a run with '
                "attempts to populate this browser.</div></section>"
            )

        judge_available = bool(payload.get("judge_available"))
        rail_items: list[str] = []
        panels: list[str] = []
        for idx, inst in enumerate(instances):
            iid = _coerce_str(inst.get("instance_id"))
            resolved = _coerce_non_negative_int(inst.get("resolved_count"))
            total = _coerce_non_negative_int(inst.get("solver_count")) or 1
            share = resolved / total if total else 0.0
            if resolved == total:
                status_tone = "success"
                status_label = "all pass"
            elif resolved == 0:
                status_tone = "fail"
                status_label = "all fail"
            else:
                status_tone = "mixed"
                status_label = f"{resolved}/{total}"
            rail_items.append(
                '<button type="button" class="browser__row" '
                f'data-browser-row="{idx}" aria-selected="{"true" if idx == 0 else "false"}">'
                f'<div class="browser__row-head">'
                f'<span class="browser__row-id">{html_escape(iid)}</span>'
                f'<span class="chip chip--status chip--status-{status_tone}">'
                f"{html_escape(status_label)}</span>"
                "</div>"
                f'<div class="browser__row-meta">'
                f"<span>{html_escape(_coerce_str(inst.get('instance_repo')) or '—')}</span>"
                f'<span class="browser__row-bar"><span style="width:{share * 100:.0f}%"></span></span>'
                "</div>"
                "</button>"
            )

            solvers = list(inst.get("solvers") or [])
            tab_buttons: list[str] = []
            tab_panels: list[str] = []
            for sidx, solver in enumerate(solvers):
                solver_id = _coerce_str(solver.get("solver_id"))
                resolved_row = bool(solver.get("resolved"))
                chip_tone = "success" if resolved_row else "fail"
                chip_label = "resolved" if resolved_row else "unresolved"
                tab_buttons.append(
                    '<button type="button" class="solver-tab" '
                    f'data-attempt-tab="{idx}-{sidx}" '
                    f'aria-selected="{"true" if sidx == 0 else "false"}">'
                    f'<span class="solver-tab__name">{html_escape(solver_id)}</span>'
                    f'<span class="chip chip--status chip--status-{chip_tone}">'
                    f"{html_escape(chip_label)}</span>"
                    "</button>"
                )
                duration_display = _format_duration_ms(solver.get("duration_ms"))
                cost_display = _format_currency(solver.get("attempt_cost_usd"))
                token_display = _format_compact_number(solver.get("total_tokens"))
                tool_calls_display = _format_integer(solver.get("tool_calls"))
                patch_length_display = _format_integer(solver.get("model_patch_length"))
                harness = _row_outcome_label(solver)
                attempt_state = _coerce_str(solver.get("attempt_state")) or "unknown"
                failure_reason = _row_failure_reason(solver)
                exit_reason = _coerce_str(solver.get("exit_reason")).strip()
                meta_rows = [
                    ("Harness", harness),
                    ("Attempt State", attempt_state),
                    ("Latency", duration_display),
                    ("Spend", cost_display),
                    ("Tokens", token_display),
                    ("Tool Calls", tool_calls_display),
                    ("Patch Size", f"{patch_length_display} chars"),
                ]
                if failure_reason:
                    meta_rows.append(("Failure Reason", failure_reason))
                if exit_reason and exit_reason != failure_reason:
                    meta_rows.append(("Exit Detail", exit_reason.splitlines()[0]))
                meta_html = "".join(
                    f"<div><dt>{html_escape(k)}</dt><dd>{html_escape(v)}</dd></div>"
                    for k, v in meta_rows
                )
                tab_panels.append(
                    '<div class="solver-panel" '
                    f'data-attempt-panel="{idx}-{sidx}" '
                    f"{'' if sidx == 0 else 'hidden'}>"
                    '<dl class="attempt-meta">'
                    f"{meta_html}"
                    "</dl>"
                    '<div class="attempt-sections">'
                    f'<div class="attempt-sections__diff">'
                    '<div class="section-label">Diff</div>'
                    f"{render_diff(_coerce_str(solver.get('model_patch')), bool(solver.get('model_patch_truncated')))}"
                    "</div>"
                    f'<div class="attempt-sections__judge">'
                    f"{render_judge_block(solver.get('judge'))}"
                    "</div>"
                    "</div>"
                    "</div>"
                )

            problem_truncated_hint = (
                '<span class="problem__hint">(truncated for display)</span>'
                if inst.get("problem_statement_truncated")
                else ""
            )
            problem_body = (
                html_escape(_coerce_str(inst.get("problem_statement")))
                or "<em>No problem statement was recorded for this instance.</em>"
            )
            panels.append(
                f'<section class="browser__panel" data-browser-panel="{idx}" '
                f"{'' if idx == 0 else 'hidden'}>"
                '<header class="browser__panel-head">'
                f'<div class="browser__panel-id">{html_escape(iid)}</div>'
                f'<div class="browser__panel-repo">{html_escape(_coerce_str(inst.get("instance_repo")) or "—")}</div>'
                "</header>"
                '<div class="problem">'
                '<div class="section-label">Problem Statement '
                f"{problem_truncated_hint}</div>"
                f'<div class="problem__body">{problem_body}</div>'
                "</div>"
                f'<nav class="solver-tabs" role="tablist" aria-label="Solvers for {html_escape(iid)}">'
                f"{''.join(tab_buttons)}</nav>"
                f'<div class="solver-panels">{"".join(tab_panels)}</div>'
                "</section>"
            )

        rendered_count = _coerce_non_negative_int(payload.get("rendered_count"))
        total_count = _coerce_non_negative_int(payload.get("instance_count"))
        truncated_note = ""
        if payload.get("truncated"):
            truncated_note = (
                '<p class="browser__truncated">Showing the first '
                f"{rendered_count} of {total_count} instances, sorted by resolution "
                "rate. Rerun <code>analyze</code> with a narrower attempt set to "
                "focus on a specific slice.</p>"
            )
        judge_note = (
            '<span class="chip chip--judge chip--judge-better">LLM Judge included</span>'
            if judge_available
            else '<span class="chip chip--judge chip--judge-same">No judge analysis in this run</span>'
        )
        return (
            '<section class="panel panel--browser"><div class="section-heading">'
            "<h2>Attempt Browser</h2>"
            f"<p>Inspect each instance, the solvers that attempted it, and the diffs they produced. {judge_note}</p>"
            "</div>"
            f"{truncated_note}"
            '<div class="browser">'
            f'<aside class="browser__rail" role="tablist" aria-label="Instances">'
            f"{''.join(rail_items)}</aside>"
            f'<div class="browser__panels">{"".join(panels)}</div>'
            "</div></section>"
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
    judge_solver_columns = [
        {"key": "solver_id", "label": "Solver", "format": "text"},
        {"key": "judged_job_count", "label": "Judged Jobs", "format": "int"},
        {"key": "avg_overall_delta", "label": "Avg Delta", "format": "default"},
        {"key": "better_share", "label": "Better Share", "format": "percent"},
        {"key": "worse_share", "label": "Worse Share", "format": "percent"},
        {
            "key": "resolved_but_worse_count",
            "label": "Resolved But Worse",
            "format": "int",
        },
        {
            "key": "unresolved_but_promising_count",
            "label": "Unresolved But Promising",
            "format": "int",
        },
    ]
    judge_dimension_columns = [
        {"key": "name", "label": "Dimension", "format": "text"},
        {"key": "weight", "label": "Weight", "format": "default"},
        {"key": "avg_delta", "label": "Avg Delta", "format": "default"},
        {"key": "better_share", "label": "Better Share", "format": "percent"},
        {"key": "worse_share", "label": "Worse Share", "format": "percent"},
    ]
    judge_sample_columns = [
        {"key": "solver_id", "label": "Solver", "format": "text"},
        {"key": "instance_id", "label": "Instance", "format": "text"},
        {"key": "overall_label", "label": "Verdict", "format": "text"},
        {"key": "overall_delta", "label": "Delta", "format": "default"},
        {"key": "confidence", "label": "Confidence", "format": "default"},
        {"key": "harness_outcome", "label": "Harness", "format": "text"},
        {"key": "attempt_state", "label": "Attempt", "format": "text"},
        {"key": "summary", "label": "Summary", "format": "text"},
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

    llm_judge_sections = ""
    if llm_judge:
        judge_top_line = llm_judge.get("top_line", {})
        judge_summary_body = (
            f"{_format_integer(judge_top_line.get('scored_job_count'))} jobs judged. "
            f"Average delta {_display_number(judge_top_line.get('avg_overall_delta'))}. "
            f"Better {_format_percent(judge_top_line.get('better_share'))}, "
            f"worse {_format_percent(judge_top_line.get('worse_share'))}."
        )
        judge_best_solver_body = f"{_coerce_str(judge_top_line.get('best_solver_id') or 'n/a')} leads the advisory code-health comparison."
        judge_error_body = f"{_format_integer(judge_top_line.get('error_job_count'))} latest-attempt rows could not be scored."
        llm_judge_sections = (
            '<div class="spotlight-grid">'
            + render_callout("LLM Judge", judge_summary_body, tone="accent")
            + render_callout(
                "Best Judge Solver", judge_best_solver_body, tone="success"
            )
            + render_callout("Judge Errors", judge_error_body, tone="warning")
            + "</div>"
            + render_table(
                title="LLM Judge Solver View",
                subtitle="Advisory diff-versus-gold scoring aggregated on the latest attempt per job.",
                table_id="llm-judge-solvers",
                rows=llm_judge.get("solver_rows", []),
                columns=judge_solver_columns,
                empty_message="No judge rows were available.",
            )
            + '<div class="two-up">'
            + render_table(
                title="Judge Dimensions",
                subtitle="Average better-or-worse signal per rubric dimension on the latest attempt per job.",
                table_id="llm-judge-dimensions",
                rows=llm_judge.get("dimension_rows", []),
                columns=judge_dimension_columns,
                empty_message="No rubric dimension data was available.",
            )
            + render_table(
                title="Resolved But Worse Than Gold",
                subtitle="Successful attempts that still looked worse than the reference patch on code-health grounds.",
                table_id="llm-judge-resolved-worse",
                rows=llm_judge.get("resolved_but_worse_than_gold", []),
                columns=judge_sample_columns,
                empty_message="No resolved attempts were judged worse than gold.",
            )
            + "</div>"
            + '<div class="two-up">'
            + render_table(
                title="Unresolved But Promising",
                subtitle="Attempts that failed the harness but still looked directionally better than the gold patch on code quality.",
                table_id="llm-judge-unresolved-promising",
                rows=llm_judge.get("unresolved_but_promising", []),
                columns=judge_sample_columns,
                empty_message="No unresolved attempts looked promising against gold.",
            )
            + render_table(
                title="Best Diff Samples",
                subtitle="The strongest candidate diffs according to the advisory judge.",
                table_id="llm-judge-best-samples",
                rows=llm_judge.get("best_samples", []),
                columns=judge_sample_columns,
                empty_message="No judged samples were available.",
            )
            + "</div>"
            + render_table(
                title="Worst Diff Samples",
                subtitle="The weakest candidate diffs according to the advisory judge.",
                table_id="llm-judge-worst-samples",
                rows=llm_judge.get("worst_samples", []),
                columns=judge_sample_columns,
                empty_message="No judged samples were available.",
            )
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>RepoGauge Analysis</title>
  <link rel="preconnect" href="https://fonts.googleapis.com"/>
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap"/>
  <style>
    :root {{
      --ink: #0a0c17;
      --ink-soft: #1a1f33;
      --body: #2a3145;
      --muted: #5a6379;
      --muted-2: #8892a6;
      --hairline: rgba(10, 12, 23, 0.08);
      --hairline-strong: rgba(10, 12, 23, 0.14);
      --paper: #ffffff;
      --paper-warm: #fafbff;
      --canvas: #f4f5fb;
      --accent: #635bff;
      --accent-2: #22d3ee;
      --accent-3: #f472b6;
      --accent-glow: #a78bfa;
      --amber: #fbbf24;
      --emerald: #10b981;
      --rose: #f43f5e;
      --night: #06070f;
      --night-2: #0b0e1c;
      --r-sm: 10px;
      --r-md: 14px;
      --r-lg: 20px;
      --r-xl: 28px;
      --r-xxl: 36px;
      --shadow-xs: 0 1px 2px rgba(10,12,23,0.04), 0 2px 6px rgba(10,12,23,0.04);
      --shadow-sm: 0 2px 6px rgba(10,12,23,0.05), 0 8px 24px rgba(10,12,23,0.06);
      --shadow-md: 0 10px 32px rgba(10,12,23,0.08), 0 2px 6px rgba(10,12,23,0.04);
      --shadow-lg: 0 24px 60px rgba(10,12,23,0.12);
      --font-sans: "Inter", system-ui, -apple-system, "Segoe UI", "Helvetica Neue", sans-serif;
      --font-mono: "JetBrains Mono", "SFMono-Regular", ui-monospace, "Cascadia Code", monospace;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; padding: 0; }}
    body {{
      font-family: var(--font-sans);
      color: var(--body);
      background:
        radial-gradient(1100px 540px at 12% -4%, rgba(99,91,255,0.06), transparent 60%),
        radial-gradient(900px 420px at 92% 8%, rgba(34,211,238,0.05), transparent 55%),
        linear-gradient(180deg, #f8f9fd 0%, #f3f4fa 100%);
      min-height: 100vh;
      -webkit-font-smoothing: antialiased;
      font-feature-settings: "ss01", "cv11";
    }}
    .page {{
      max-width: 1440px;
      margin: 0 auto;
      padding: 32px 24px 64px;
    }}
    .hero {{
      position: relative;
      overflow: hidden;
      border-radius: var(--r-xxl);
      padding: 44px 44px 40px;
      background:
        radial-gradient(1100px 480px at 85% -12%, rgba(34,211,238,0.28), transparent 55%),
        radial-gradient(900px 520px at 10% 115%, rgba(244,114,182,0.22), transparent 55%),
        radial-gradient(720px 420px at 48% 48%, rgba(99,91,255,0.34), transparent 60%),
        linear-gradient(160deg, var(--night) 0%, var(--night-2) 55%, #141932 100%);
      color: #ffffff;
      box-shadow: var(--shadow-lg);
      isolation: isolate;
    }}
    .hero::before {{
      content: "";
      position: absolute;
      inset: 0;
      background-image:
        linear-gradient(90deg, rgba(255,255,255,0.035) 1px, transparent 1px),
        linear-gradient(180deg, rgba(255,255,255,0.035) 1px, transparent 1px);
      background-size: 48px 48px;
      mask-image: radial-gradient(65% 60% at 50% 40%, #000 60%, transparent 100%);
      opacity: 0.6;
      z-index: -1;
    }}
    .hero__eyebrow {{
      display: inline-flex;
      align-items: center;
      gap: 10px;
      font-family: var(--font-mono);
      font-size: 11px;
      letter-spacing: 0.18em;
      text-transform: uppercase;
      color: rgba(255,255,255,0.72);
      margin-bottom: 22px;
      padding: 6px 12px;
      border: 1px solid rgba(255,255,255,0.14);
      border-radius: 999px;
      background: rgba(255,255,255,0.04);
    }}
    .hero__eyebrow::before {{
      content: "";
      width: 6px;
      height: 6px;
      border-radius: 999px;
      background: var(--emerald);
      box-shadow: 0 0 10px rgba(16,185,129,0.85);
    }}
    .hero h1 {{
      margin: 0 0 14px;
      font-size: clamp(2.1rem, 3.6vw, 3.4rem);
      line-height: 1.03;
      letter-spacing: -0.035em;
      max-width: 16ch;
      font-weight: 700;
      background: linear-gradient(180deg, #ffffff 42%, rgba(255,255,255,0.72) 100%);
      -webkit-background-clip: text;
      background-clip: text;
      -webkit-text-fill-color: transparent;
    }}
    .hero p {{
      margin: 0;
      max-width: 72ch;
      color: rgba(220, 226, 245, 0.78);
      font-size: 1.02rem;
      line-height: 1.65;
    }}
    .hero__stats {{
      margin-top: 30px;
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
    }}
    .hero__stat {{
      padding: 16px 18px;
      border-radius: var(--r-md);
      background: rgba(255,255,255,0.05);
      backdrop-filter: blur(14px) saturate(140%);
      -webkit-backdrop-filter: blur(14px) saturate(140%);
      border: 1px solid rgba(255,255,255,0.10);
    }}
    .hero__stat-label {{
      font-family: var(--font-mono);
      font-size: 0.7rem;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: rgba(220, 226, 245, 0.56);
    }}
    .hero__stat-value {{
      display: block;
      margin-top: 10px;
      font-size: 1.12rem;
      font-weight: 600;
      letter-spacing: -0.015em;
      color: #ffffff;
    }}
    .section-grid {{
      display: grid;
      gap: 20px;
      margin-top: 22px;
    }}
    .metric-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 16px;
    }}
    .spotlight-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 16px;
    }}
    .two-up {{
      display: grid;
      grid-template-columns: 1.45fr 1fr;
      gap: 20px;
    }}
    .panel {{
      background: var(--paper);
      border: 1px solid var(--hairline);
      border-radius: var(--r-xl);
      box-shadow: var(--shadow-sm);
      padding: 24px;
      animation: panel-enter 460ms cubic-bezier(0.2, 0.7, 0.2, 1) both;
    }}
    .panel--chart {{ overflow: hidden; }}
    .section-heading {{
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 18px;
    }}
    .section-heading h2 {{
      margin: 0;
      font-size: 1.2rem;
      letter-spacing: -0.025em;
      color: var(--ink);
      font-weight: 600;
    }}
    .section-heading p {{
      margin: 4px 0 0;
      color: var(--muted);
      max-width: 76ch;
      line-height: 1.55;
      font-size: 0.92rem;
    }}
    .metric-card {{
      position: relative;
      overflow: hidden;
      min-height: 180px;
      border-radius: var(--r-lg);
      padding: 22px;
      background: var(--paper);
      border: 1px solid var(--hairline);
      box-shadow: var(--shadow-xs);
      transition: transform 160ms ease, box-shadow 160ms ease;
    }}
    .metric-card:hover {{
      transform: translateY(-1px);
      box-shadow: var(--shadow-md);
    }}
    .metric-card::before {{
      content: "";
      position: absolute;
      inset: 0 auto auto 0;
      width: 100%;
      height: 3px;
      background: linear-gradient(90deg, var(--accent), var(--accent-2), var(--accent-3));
    }}
    .metric-card--success::before   {{ background: linear-gradient(90deg, var(--emerald), #34d399); }}
    .metric-card--accent::before    {{ background: linear-gradient(90deg, var(--accent), var(--accent-glow)); }}
    .metric-card--cool::before      {{ background: linear-gradient(90deg, var(--accent-2), #67e8f9); }}
    .metric-card--warm::before      {{ background: linear-gradient(90deg, var(--accent-3), #fb7185); }}
    .metric-card--highlight::before {{ background: linear-gradient(90deg, var(--amber), #fde68a); }}
    .metric-card__eyebrow {{
      display: inline-block;
      font-family: var(--font-mono);
      font-size: 0.68rem;
      font-weight: 600;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--muted);
    }}
    .metric-card__title {{
      margin: 10px 0 14px;
      font-size: 0.95rem;
      font-weight: 500;
      color: var(--ink);
    }}
    .metric-card__value {{
      font-size: clamp(1.7rem, 2.5vw, 2.4rem);
      line-height: 0.96;
      letter-spacing: -0.045em;
      font-weight: 700;
      color: var(--ink);
      font-variant-numeric: tabular-nums;
    }}
    .metric-card__support {{
      margin: 12px 0 0;
      color: var(--muted);
      line-height: 1.5;
      max-width: 28ch;
      font-size: 0.88rem;
    }}
    .callout {{
      border-radius: var(--r-lg);
      padding: 22px;
      background: var(--paper);
      border: 1px solid var(--hairline);
      box-shadow: var(--shadow-xs);
    }}
    .callout h3 {{
      margin: 0 0 10px;
      font-family: var(--font-mono);
      font-size: 0.7rem;
      text-transform: uppercase;
      letter-spacing: 0.14em;
      color: var(--muted);
      font-weight: 600;
    }}
    .callout p {{
      margin: 0;
      font-size: 1.02rem;
      line-height: 1.55;
      letter-spacing: -0.01em;
      color: var(--ink);
    }}
    .callout--success {{
      background: linear-gradient(180deg, rgba(16,185,129,0.08), var(--paper) 70%);
      border-color: rgba(16,185,129,0.22);
    }}
    .callout--accent {{
      background: linear-gradient(180deg, rgba(99,91,255,0.07), var(--paper) 70%);
      border-color: rgba(99,91,255,0.20);
    }}
    .callout--warning {{
      background: linear-gradient(180deg, rgba(244,114,182,0.09), var(--paper) 70%);
      border-color: rgba(244,114,182,0.22);
    }}
    .table-shell {{
      overflow: auto;
      border-radius: var(--r-md);
      border: 1px solid var(--hairline);
      background: var(--paper);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      min-width: 820px;
    }}
    th, td {{
      padding: 14px 18px;
      border-bottom: 1px solid var(--hairline);
      text-align: left;
      font-size: 0.92rem;
      vertical-align: top;
      color: var(--ink);
    }}
    td {{ font-variant-numeric: tabular-nums; }}
    thead th {{
      position: sticky;
      top: 0;
      background: var(--paper-warm);
      z-index: 1;
      font-family: var(--font-mono);
      font-size: 0.7rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      color: var(--muted);
      border-bottom: 1px solid var(--hairline-strong);
    }}
    tbody tr {{ transition: background 120ms ease; }}
    tbody tr:hover {{ background: rgba(99,91,255,0.04); }}
    tbody tr:last-child td {{ border-bottom: 0; }}
    .sort-button {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border: 0;
      padding: 0;
      background: transparent;
      color: inherit;
      font: inherit;
      text-transform: inherit;
      letter-spacing: inherit;
      cursor: pointer;
    }}
    .sort-button:hover {{ color: var(--accent); }}
    .sort-button__glyph {{
      font-size: 0.9em;
      opacity: 0.5;
    }}
    .bar-list {{
      list-style: none;
      margin: 0;
      padding: 0;
      display: grid;
      gap: 14px;
    }}
    .bar-list__item {{ padding: 0; }}
    .bar-list__header {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 8px;
      color: var(--ink);
      font-size: 0.92rem;
    }}
    .bar-list__header strong {{ font-variant-numeric: tabular-nums; }}
    .bar-list__track {{
      height: 10px;
      border-radius: 999px;
      background: rgba(10,12,23,0.06);
      overflow: hidden;
    }}
    .bar-list__fill {{
      display: block;
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, var(--accent), var(--accent-2));
    }}
    .bar-list__fill--blue   {{ background: linear-gradient(90deg, var(--accent), var(--accent-2)); }}
    .bar-list__fill--orange {{ background: linear-gradient(90deg, var(--accent-3), var(--amber)); }}
    .bar-list__fill--rose   {{ background: linear-gradient(90deg, var(--rose), var(--accent-3)); }}
    .chart-shell {{ display: grid; gap: 16px; }}
    .chart-surface {{
      fill: #fafbff;
      stroke: var(--hairline);
    }}
    .chart-grid {{
      stroke: rgba(10,12,23,0.07);
      stroke-width: 1;
    }}
    .chart-grid--vertical {{ stroke-dasharray: 3 6; }}
    .chart-axis {{
      stroke: rgba(10,12,23,0.22);
      stroke-width: 1.25;
    }}
    .chart-axis-label {{
      fill: var(--muted);
      font-size: 10.5px;
      font-family: var(--font-mono);
      letter-spacing: 0.04em;
    }}
    .chart-title {{
      fill: var(--ink);
      font-size: 12px;
      font-weight: 600;
      font-family: var(--font-sans);
    }}
    .chart-point-label {{
      fill: var(--ink);
      font-size: 10.5px;
      font-weight: 600;
      font-family: var(--font-mono);
      letter-spacing: 0.02em;
    }}
    .legend {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px 18px;
      list-style: none;
      margin: 0;
      padding: 0;
    }}
    .legend__item {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      font-size: 0.88rem;
    }}
    .legend__swatch {{
      width: 10px;
      height: 10px;
      border-radius: 999px;
    }}
    .chip {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 11px;
      border-radius: 999px;
      background: rgba(99,91,255,0.10);
      color: var(--accent);
      font-family: var(--font-mono);
      font-size: 0.72rem;
      font-weight: 600;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}
    .chip--warning {{
      background: rgba(251,191,36,0.14);
      color: #b45309;
    }}
    .incident-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 16px;
    }}
    .incident-card {{
      border-radius: var(--r-lg);
      padding: 22px;
      background: var(--paper);
      border: 1px solid var(--hairline);
      box-shadow: var(--shadow-xs);
    }}
    .incident-card__top {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
    }}
    .incident-card h3 {{
      margin: 16px 0 8px;
      font-size: 1rem;
      letter-spacing: -0.02em;
      color: var(--ink);
      font-weight: 600;
    }}
    .incident-card p {{
      margin: 0;
      color: var(--muted);
      line-height: 1.55;
      font-size: 0.92rem;
    }}
    .incident-card__meta {{
      margin: 18px 0 0;
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}
    .incident-card__meta div {{
      padding: 12px 14px;
      border-radius: var(--r-sm);
      background: var(--canvas);
      border: 1px solid var(--hairline);
    }}
    .incident-card__meta dt {{
      font-family: var(--font-mono);
      font-size: 0.68rem;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      color: var(--muted);
      margin-bottom: 6px;
    }}
    .incident-card__meta dd {{
      margin: 0;
      font-weight: 600;
      line-height: 1.45;
      color: var(--ink);
      font-variant-numeric: tabular-nums;
    }}
    .metadata-panel summary {{
      cursor: pointer;
      font-family: var(--font-mono);
      font-size: 0.8rem;
      font-weight: 600;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      color: var(--muted);
    }}
    .metadata-panel[open] summary {{ color: var(--accent); }}
    pre {{
      margin: 16px 0 0;
      padding: 18px;
      overflow: auto;
      background: var(--night);
      color: #d7dbef;
      border-radius: var(--r-md);
      border: 1px solid rgba(255,255,255,0.08);
      font-family: var(--font-mono);
      font-size: 12px;
      line-height: 1.65;
    }}
    .empty-state {{
      border-radius: var(--r-md);
      padding: 20px 22px;
      background: var(--canvas);
      border: 1px dashed var(--hairline-strong);
      color: var(--muted);
      font-size: 0.92rem;
    }}
    @keyframes panel-enter {{
      from {{ opacity: 0; transform: translateY(8px); }}
      to   {{ opacity: 1; transform: translateY(0); }}
    }}
    @media (prefers-reduced-motion: reduce) {{
      .panel {{ animation: none; }}
      .metric-card {{ transition: none; }}
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
      .hero {{ padding: 26px; }}
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
    .view-tabs {{
      display: flex;
      gap: 6px;
      padding: 6px;
      margin: 24px 0 4px;
      background: var(--paper);
      border: 1px solid var(--hairline);
      border-radius: 999px;
      box-shadow: var(--shadow-xs);
      width: fit-content;
    }}
    .view-tabs__btn {{
      appearance: none;
      border: 0;
      background: transparent;
      padding: 10px 22px;
      border-radius: 999px;
      font-family: var(--font-sans);
      font-size: 0.88rem;
      font-weight: 600;
      letter-spacing: -0.005em;
      color: var(--muted);
      cursor: pointer;
      transition: background 160ms ease, color 160ms ease;
    }}
    .view-tabs__btn:hover {{ color: var(--ink); }}
    .view-tabs__btn[aria-selected="true"] {{
      background: linear-gradient(135deg, var(--accent), var(--accent-glow));
      color: #fff;
      box-shadow: 0 6px 16px rgba(99,91,255,0.30);
    }}
    .view-panel[hidden] {{ display: none; }}
    .panel--browser {{ padding: 24px; }}
    .browser {{
      display: grid;
      grid-template-columns: 320px minmax(0, 1fr);
      gap: 20px;
      margin-top: 8px;
    }}
    .browser__rail {{
      display: flex;
      flex-direction: column;
      gap: 6px;
      max-height: 780px;
      overflow-y: auto;
      padding-right: 4px;
    }}
    .browser__rail::-webkit-scrollbar {{ width: 8px; }}
    .browser__rail::-webkit-scrollbar-thumb {{
      background: rgba(10,12,23,0.16);
      border-radius: 999px;
    }}
    .browser__row {{
      appearance: none;
      text-align: left;
      background: var(--paper);
      border: 1px solid var(--hairline);
      border-radius: var(--r-md);
      padding: 12px 14px;
      cursor: pointer;
      display: flex;
      flex-direction: column;
      gap: 8px;
      transition: border-color 160ms ease, background 160ms ease, transform 160ms ease;
    }}
    .browser__row:hover {{
      border-color: var(--accent);
      transform: translateY(-1px);
    }}
    .browser__row[aria-selected="true"] {{
      border-color: var(--accent);
      background: linear-gradient(135deg, rgba(99,91,255,0.08), rgba(34,211,238,0.06));
      box-shadow: inset 2px 0 0 var(--accent);
    }}
    .browser__row-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }}
    .browser__row-id {{
      font-family: var(--font-mono);
      font-size: 0.78rem;
      color: var(--ink);
      font-weight: 600;
      word-break: break-all;
    }}
    .browser__row-meta {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      font-family: var(--font-mono);
      font-size: 0.7rem;
      color: var(--muted);
      text-transform: lowercase;
    }}
    .browser__row-bar {{
      display: inline-block;
      flex: 0 0 70px;
      height: 4px;
      border-radius: 999px;
      background: rgba(10,12,23,0.06);
      overflow: hidden;
    }}
    .browser__row-bar > span {{
      display: block;
      height: 100%;
      background: linear-gradient(90deg, var(--accent), var(--accent-2));
    }}
    .browser__panels {{ min-width: 0; }}
    .browser__panel[hidden] {{ display: none; }}
    .browser__panel-head {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 16px;
      padding-bottom: 14px;
      margin-bottom: 18px;
      border-bottom: 1px solid var(--hairline);
    }}
    .browser__panel-id {{
      font-family: var(--font-mono);
      font-size: 0.98rem;
      font-weight: 600;
      color: var(--ink);
    }}
    .browser__panel-repo {{
      font-family: var(--font-mono);
      font-size: 0.78rem;
      color: var(--muted);
    }}
    .browser__truncated {{
      margin: 4px 0 12px;
      padding: 10px 14px;
      border-radius: var(--r-md);
      background: rgba(99,91,255,0.06);
      border: 1px solid rgba(99,91,255,0.18);
      color: var(--ink);
      font-size: 0.84rem;
    }}
    .section-label {{
      font-family: var(--font-mono);
      font-size: 0.72rem;
      letter-spacing: 0.16em;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: 8px;
    }}
    .problem {{
      background: var(--canvas);
      border: 1px solid var(--hairline);
      border-radius: var(--r-md);
      padding: 16px 18px;
      margin-bottom: 20px;
    }}
    .problem__body {{
      white-space: pre-wrap;
      font-size: 0.92rem;
      line-height: 1.6;
      color: var(--ink);
      max-height: 260px;
      overflow-y: auto;
    }}
    .problem__hint {{
      font-family: var(--font-mono);
      font-size: 0.66rem;
      color: var(--muted);
      text-transform: none;
      letter-spacing: 0;
      margin-left: 10px;
    }}
    .solver-tabs {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-bottom: 14px;
      border-bottom: 1px solid var(--hairline);
      padding-bottom: 8px;
    }}
    .solver-tab {{
      appearance: none;
      border: 1px solid var(--hairline);
      background: var(--paper);
      padding: 8px 12px;
      border-radius: 999px;
      cursor: pointer;
      font-size: 0.82rem;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: var(--body);
      transition: border-color 160ms ease, background 160ms ease, color 160ms ease;
    }}
    .solver-tab:hover {{ border-color: var(--accent); color: var(--ink); }}
    .solver-tab[aria-selected="true"] {{
      background: var(--ink);
      border-color: var(--ink);
      color: #fff;
    }}
    .solver-tab[aria-selected="true"] .chip--status {{
      background: rgba(255,255,255,0.14);
      color: #fff;
    }}
    .solver-tab__name {{
      font-family: var(--font-mono);
      font-weight: 600;
    }}
    .solver-panel[hidden] {{ display: none; }}
    .attempt-meta {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      padding: 14px;
      margin: 0 0 16px;
      background: var(--canvas);
      border: 1px solid var(--hairline);
      border-radius: var(--r-md);
      font-variant-numeric: tabular-nums;
    }}
    .attempt-meta > div {{ display: flex; flex-direction: column; gap: 4px; }}
    .attempt-meta dt {{
      font-family: var(--font-mono);
      font-size: 0.62rem;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--muted);
    }}
    .attempt-meta dd {{
      margin: 0;
      font-size: 0.92rem;
      color: var(--ink);
      font-weight: 500;
    }}
    .attempt-sections {{
      display: grid;
      grid-template-columns: minmax(0, 1.45fr) minmax(0, 1fr);
      gap: 16px;
      align-items: start;
    }}
    .diff-viewer {{
      display: flex;
      flex-direction: column;
      gap: 10px;
      min-height: 220px;
    }}
    .diff-viewer__loading,
    .diff-viewer__fallback-note {{
      font-family: var(--font-mono);
      font-size: 0.72rem;
      color: var(--muted);
    }}
    .diff-viewer__mount {{
      min-height: 220px;
      border-radius: var(--r-md);
      overflow: hidden;
      background: var(--night);
      border: 1px solid rgba(255,255,255,0.06);
    }}
    .diff-viewer__mount[hidden],
    .diff-viewer__fallback[hidden],
    .diff-viewer__loading[hidden] {{
      display: none;
    }}
    .diff-viewer__fallback {{
      display: flex;
      flex-direction: column;
      gap: 8px;
    }}
    .diff-viewer__fallback-pre {{
      margin: 0;
      padding: 14px 16px;
      background: var(--night);
      color: #d7dbef;
      border-radius: var(--r-md);
      border: 1px solid rgba(255,255,255,0.06);
      font-family: var(--font-mono);
      font-size: 12px;
      line-height: 1.55;
      max-height: 520px;
      overflow: auto;
      white-space: pre;
    }}
    .diff-viewer__truncated {{
      margin-top: 8px;
      font-family: var(--font-mono);
      font-size: 0.72rem;
      color: var(--muted);
    }}
    .judge-card {{
      background: linear-gradient(160deg, rgba(99,91,255,0.06), rgba(34,211,238,0.04));
      border: 1px solid rgba(99,91,255,0.16);
      border-radius: var(--r-md);
      padding: 16px 18px;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }}
    .judge-card--muted {{
      background: var(--canvas);
      border-color: var(--hairline);
    }}
    .judge-card__head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }}
    .judge-card__title {{
      font-family: var(--font-mono);
      font-size: 0.72rem;
      letter-spacing: 0.16em;
      text-transform: uppercase;
      color: var(--muted);
    }}
    .judge-card__meta {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      margin: 0;
      font-variant-numeric: tabular-nums;
    }}
    .judge-card__meta dt {{
      font-family: var(--font-mono);
      font-size: 0.62rem;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--muted);
    }}
    .judge-card__meta dd {{
      margin: 2px 0 0;
      font-size: 0.92rem;
      font-weight: 600;
      color: var(--ink);
    }}
    .judge-card__summary {{
      margin: 0;
      font-size: 0.92rem;
      line-height: 1.55;
      color: var(--ink);
    }}
    .judge-card__empty {{
      margin: 0;
      font-size: 0.88rem;
      color: var(--muted);
    }}
    .dim-grid {{
      display: flex;
      flex-direction: column;
      gap: 10px;
      padding-top: 6px;
      border-top: 1px dashed var(--hairline);
    }}
    .dim {{ display: flex; flex-direction: column; gap: 4px; }}
    .dim__head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }}
    .dim__name {{
      font-family: var(--font-mono);
      font-size: 0.74rem;
      letter-spacing: 0.06em;
      color: var(--ink);
      font-weight: 600;
    }}
    .dim__rationale {{
      margin: 0;
      font-size: 0.84rem;
      color: var(--body);
      line-height: 1.5;
    }}
    .chip--status {{
      font-family: var(--font-mono);
      font-size: 0.64rem;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      padding: 3px 8px;
      border-radius: 999px;
      background: rgba(10,12,23,0.06);
      color: var(--muted);
    }}
    .chip--status-success {{ background: rgba(16,185,129,0.14); color: #047857; }}
    .chip--status-fail {{ background: rgba(244,63,94,0.14); color: #be123c; }}
    .chip--status-mixed {{ background: rgba(251,191,36,0.18); color: #92400e; }}
    .chip--judge {{
      font-family: var(--font-mono);
      font-size: 0.66rem;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      padding: 3px 9px;
      border-radius: 999px;
    }}
    .chip--judge em {{ font-style: normal; font-weight: 600; margin-left: 4px; }}
    .chip--judge-better {{ background: rgba(16,185,129,0.14); color: #047857; }}
    .chip--judge-much-better {{ background: rgba(16,185,129,0.24); color: #065f46; }}
    .chip--judge-worse {{ background: rgba(244,63,94,0.14); color: #be123c; }}
    .chip--judge-much-worse {{ background: rgba(244,63,94,0.24); color: #9f1239; }}
    .chip--judge-same {{ background: rgba(10,12,23,0.08); color: var(--muted); }}
    @media (max-width: 1100px) {{
      .browser {{ grid-template-columns: 1fr; }}
      .browser__rail {{ max-height: none; }}
      .attempt-sections {{ grid-template-columns: 1fr; }}
      .attempt-meta {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
  </style>
</head>
<body>
  <main class="page">
    <header class="hero">
      <span class="hero__eyebrow">RepoGauge Analysis</span>
      <h1>Replace vibes with evidence.</h1>
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

    <nav class="view-tabs" role="tablist" aria-label="Report views">
      <button type="button" class="view-tabs__btn" data-view-tab="overview" aria-selected="true" aria-controls="view-overview">Overview</button>
      <button type="button" class="view-tabs__btn" data-view-tab="attempts" aria-selected="false" aria-controls="view-attempts">Attempts</button>
    </nav>

    <section class="section-grid view-panel" id="view-overview" data-view-panel="overview">
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
      {llm_judge_sections}
      {metadata_block}
    </section>

    <section class="section-grid view-panel" id="view-attempts" data-view-panel="attempts" hidden>
      {render_attempt_browser(attempt_browser)}
    </section>
  </main>
  <script type="module">
    (() => {{
      const initDiffViewers = async () => {{
        const viewers = Array.from(document.querySelectorAll("[data-diff-viewer]"));
        let FileDiff;
        let parsePatchFiles;
        try {{
          ({{ FileDiff, parsePatchFiles }} = await import("https://esm.sh/@pierre/diffs"));
        }} catch (error) {{
          viewers.forEach((viewer) => {{
            const loading = viewer.querySelector("[data-diff-loading]");
            const fallback = viewer.querySelector("[data-diff-fallback]");
            if (loading) loading.hidden = true;
            if (fallback) fallback.hidden = false;
          }});
          console.error("Failed to load Diffs viewer bundle", error);
          return;
        }}
        for (const viewer of viewers) {{
          const loading = viewer.querySelector("[data-diff-loading]");
          const mount = viewer.querySelector("[data-diff-mount]");
          const fallback = viewer.querySelector("[data-diff-fallback]");
          const payloadEl = viewer.querySelector("[data-diff-payload]");
          if (!(mount instanceof HTMLElement) || !(payloadEl instanceof HTMLScriptElement)) {{
            continue;
          }}
          try {{
            const payload = JSON.parse(payloadEl.textContent || "{{}}");
            const patch = typeof payload.patch === "string" ? payload.patch : "";
            const parsedGroups = parsePatchFiles(patch);
            const fileDiffs = parsedGroups.flatMap((group) =>
              Array.isArray(group?.files) ? group.files : []
            );
            if (!fileDiffs.length) {{
              throw new Error("No file diffs parsed from patch");
            }}
            mount.innerHTML = "";
            for (const fileDiff of fileDiffs) {{
              const fileMount = document.createElement("div");
              fileMount.className = "diff-viewer__file";
              mount.appendChild(fileMount);
              const diff = new FileDiff({{
                theme: "pierre-dark",
                diffStyle: "split",
                overflow: "scroll",
              }});
              diff.render({{
                fileDiff,
                containerWrapper: fileMount,
              }});
            }}
            mount.hidden = false;
            if (loading) loading.hidden = true;
            if (fallback) fallback.hidden = true;
          }} catch (error) {{
            if (loading) loading.hidden = true;
            if (fallback) fallback.hidden = false;
            console.error("Failed to initialize Diffs viewer", error);
          }}
        }}
      }};

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

      document.querySelectorAll(".view-tabs__btn").forEach((button) => {{
        button.addEventListener("click", () => {{
          const target = button.dataset.viewTab;
          document.querySelectorAll(".view-tabs__btn").forEach((peer) => {{
            peer.setAttribute("aria-selected", peer === button ? "true" : "false");
          }});
          document.querySelectorAll("[data-view-panel]").forEach((panel) => {{
            if (panel.dataset.viewPanel === target) {{
              panel.removeAttribute("hidden");
            }} else {{
              panel.setAttribute("hidden", "");
            }}
          }});
        }});
      }});

      document.querySelectorAll(".browser__row").forEach((row) => {{
        row.addEventListener("click", () => {{
          const idx = row.dataset.browserRow;
          document.querySelectorAll(".browser__row").forEach((peer) => {{
            peer.setAttribute("aria-selected", peer === row ? "true" : "false");
          }});
          document.querySelectorAll("[data-browser-panel]").forEach((panel) => {{
            if (panel.dataset.browserPanel === idx) {{
              panel.removeAttribute("hidden");
            }} else {{
              panel.setAttribute("hidden", "");
            }}
          }});
        }});
      }});

      document.querySelectorAll(".solver-tab").forEach((tab) => {{
        tab.addEventListener("click", () => {{
          const target = tab.dataset.attemptTab;
          if (!target) return;
          const [panelIdx] = target.split("-");
          const panelEl = document.querySelector(
            `[data-browser-panel="${{panelIdx}}"]`
          );
          if (!panelEl) return;
          panelEl.querySelectorAll(".solver-tab").forEach((peer) => {{
            peer.setAttribute("aria-selected", peer === tab ? "true" : "false");
          }});
          panelEl.querySelectorAll("[data-attempt-panel]").forEach((panel) => {{
            if (panel.dataset.attemptPanel === target) {{
              panel.removeAttribute("hidden");
            }} else {{
              panel.setAttribute("hidden", "");
            }}
          }});
        }});
      }});

      initDiffViewers();
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
        outcome = _row_outcome_label(eval_row)
        resolved = _coerce_bool(
            eval_row.get("resolved", outcome.lower() in {"passed", "resolved"})
        )

        item = dict(attempt)
        item.update(
            {
                "resolved": resolved,
                "harness_outcome": outcome,
                "failure_reason": _row_failure_reason(eval_row),
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
