"""Unit tests for deterministic solver attempt analysis metrics."""

from __future__ import annotations

import json

import pytest

from repogauge.runner.analyze import (
    build_predictions_from_attempts,
    join_attempt_rows,
    summarize_attempt_metrics,
)


def test_build_predictions_from_attempts_skips_empty_patches(tmp_path):
    attempts = [
        {
            "instance_id": "inst-1",
            "solver_id": "solver-a",
            "model_patch": "diff --git a/x b/x\n+ok\n",
        },
        {"instance_id": "inst-2", "solver_id": "solver-a", "model_patch": None},
        {"instance_id": "inst-3", "solver_id": "solver-a", "model_patch": ""},
        {
            "instance_id": "inst-4",
            "solver_id": "solver-b",
            "model_patch": "diff --git a/y b/y\n+ok\n",
        },
    ]
    attempts_path = tmp_path / "attempts.jsonl"
    attempts_path.write_text(
        "".join(json.dumps(row) + "\n" for row in attempts), encoding="utf-8"
    )
    predictions_path = tmp_path / "predictions.jsonl"

    written = build_predictions_from_attempts(attempts_path, predictions_path)

    assert written == 2
    rows = [
        json.loads(line)
        for line in predictions_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [row["instance_id"] for row in rows] == ["inst-1", "inst-4"]
    assert all(
        set(row.keys()) == {"instance_id", "model_patch", "model_name_or_path"}
        for row in rows
    )
    assert rows[0]["model_name_or_path"] == "solver-a"


def _attempt_row(
    solver_id: str,
    instance_id: str,
    *,
    duration_ms: int,
    cost: float | None,
    usage: dict[str, object] | None = None,
    raw_output: str = "",
) -> dict:
    row: dict[str, object] = {
        "solver_id": solver_id,
        "instance_id": instance_id,
        "duration_ms": duration_ms,
        "raw_output": raw_output,
    }
    if cost is not None:
        row["cost"] = {"total_cost": cost}
    if usage is not None:
        row["usage"] = usage
    return row


def _eval_row(
    solver_id: str,
    instance_id: str,
    *,
    harness_outcome: str,
    resolved: object,
) -> dict:
    return {
        "solver_id": solver_id,
        "instance_id": instance_id,
        "harness_outcome": harness_outcome,
        "resolved": resolved,
    }


def test_join_attempt_rows_merges_solver_instance_rows_and_costs() -> None:
    attempts = [
        _attempt_row("solver-a", "inst-1", duration_ms=25, cost=2.5),
        _attempt_row("solver-a", "inst-2", duration_ms=10, cost=0.75),
        _attempt_row("solver-b", "inst-1", duration_ms=17, cost=None),
    ]
    instance_results = [
        _eval_row("solver-a", "inst-1", harness_outcome="resolved", resolved=True),
        _eval_row(
            "solver-a", "inst-2", harness_outcome="not_resolved", resolved="false"
        ),
        _eval_row("solver-b", "inst-1", harness_outcome="passed", resolved=1),
    ]

    joined = join_attempt_rows(attempts, instance_results)

    assert len(joined) == 3
    first = joined[0]
    assert first["resolved"] is True
    assert first["harness_outcome"] == "resolved"
    assert first["attempt_cost_usd"] == 2.5
    assert joined[1]["resolved"] is False
    assert joined[2]["resolved"] is True
    assert joined[2]["harness_outcome"] == "passed"


def test_summarize_attempt_metrics_with_zero_resolutions() -> None:
    attempts = [
        _attempt_row("solver-a", "inst-1", duration_ms=17, cost=1.5),
        _attempt_row("solver-a", "inst-1", duration_ms=23, cost=2.0),
        _attempt_row("solver-a", "inst-2", duration_ms=40, cost=4.0),
    ]
    instance_results = [
        _eval_row("solver-a", "inst-1", harness_outcome="not_resolved", resolved=False),
        _eval_row(
            "solver-a",
            "inst-2",
            harness_outcome="error",
            resolved="0",
        ),
    ]

    summaries = summarize_attempt_metrics(
        attempts=attempts,
        instance_results=instance_results,
        expensive_cost_threshold=1.0,
    )

    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.attempt_count == 3
    assert summary.unique_instance_count == 2
    assert summary.resolved_instance_count == 0
    assert summary.raw_resolution_rate == 0.0
    assert summary.total_cost_usd == 7.5
    assert summary.resolved_cost_usd == 0.0
    assert summary.cost_per_resolved_issue is None
    assert summary.latency_ms_per_resolved_issue is None
    assert summary.expensive_coverage == 0.0
    assert summary.exclusive_expensive_win_rate == 0.0
    assert summary.marginal_cost_per_extra_resolve is None
    assert summary.total_tokens == 0
    assert summary.total_tool_calls == 0
    assert summary.p50_attempt_duration_ms == 23
    assert summary.p95_attempt_duration_ms == 40


def test_summarize_attempt_metrics_expensive_coverage_and_exclusive_win_rate() -> None:
    attempts = [
        _attempt_row("solver-a", "cheap", duration_ms=10, cost=0.5),
        _attempt_row("solver-a", "cheap", duration_ms=22, cost=1.0),
        _attempt_row("solver-a", "borderline", duration_ms=30, cost=11.0),
        _attempt_row("solver-a", "expensive", duration_ms=40, cost=12.0),
        _attempt_row("solver-a", "fail", duration_ms=8, cost=15.0),
    ]
    instance_results = [
        _eval_row("solver-a", "cheap", harness_outcome="resolved", resolved=True),
        _eval_row("solver-a", "borderline", harness_outcome="passed", resolved=True),
        _eval_row("solver-a", "expensive", harness_outcome="resolved", resolved=1),
        _eval_row("solver-a", "fail", harness_outcome="not_resolved", resolved=False),
    ]

    summaries = summarize_attempt_metrics(
        attempts=attempts,
        instance_results=instance_results,
        expensive_cost_threshold=10.0,
    )

    summary = summaries[0]
    assert summary.resolved_instance_count == 3
    assert summary.unique_instance_count == 4
    assert summary.expensive_coverage == 2.0 / 3
    assert summary.exclusive_expensive_win_rate == 2.0 / 3
    assert summary.cost_per_resolved_issue == (0.5 + 11.0 + 12.0) / 3


def test_summarize_attempt_metrics_marginal_cost_uses_only_resolved_instances() -> None:
    attempts = [
        _attempt_row("solver-a", "cheap", duration_ms=10, cost=2.0),
        _attempt_row("solver-a", "cheap", duration_ms=7, cost=3.0),
        _attempt_row("solver-a", "middle", duration_ms=11, cost=12.0),
        _attempt_row("solver-a", "expensive", duration_ms=13, cost=18.0),
        _attempt_row("solver-a", "missed", duration_ms=13, cost=100.0),
    ]
    instance_results = [
        _eval_row("solver-a", "cheap", harness_outcome="resolved", resolved=True),
        _eval_row("solver-a", "middle", harness_outcome="resolved", resolved=True),
        _eval_row(
            "solver-a", "expensive", harness_outcome="not_resolved", resolved="0"
        ),
        _eval_row("solver-a", "missed", harness_outcome="not_resolved", resolved=False),
    ]

    summaries = summarize_attempt_metrics(
        attempts=attempts,
        instance_results=instance_results,
        expensive_cost_threshold=5.0,
    )

    summary = summaries[0]
    # Cheap and middle resolve; marginal is mean difference of sorted minimum resolved costs:
    # min(cheap)=2.0, min(middle)=12.0 -> (12 - 2) / 1 = 10
    assert summary.marginal_cost_per_extra_resolve == 10.0
    assert summary.resolved_instance_count == 2


def test_summarize_attempt_metrics_can_stratify_by_task_cluster() -> None:
    attempts = [
        {
            "solver_id": "solver-a",
            "instance_id": "inst-stack",
            "duration_ms": 10,
            "cost": {"total_cost": 1.5},
            "repo": "owner/repo",
            "version": "1.0.0",
            "problem_statement": "Traceback when loading cached settings from disk.",
        },
        {
            "solver_id": "solver-a",
            "instance_id": "inst-plain",
            "duration_ms": 12,
            "cost": {"total_cost": 2.0},
            "repo": "owner/repo",
            "version": "1.0.0",
            "problem_statement": "Update the cache key handling.",
        },
    ]
    instance_results = [
        _eval_row("solver-a", "inst-stack", harness_outcome="resolved", resolved=True),
        _eval_row("solver-a", "inst-plain", harness_outcome="resolved", resolved=True),
    ]

    summaries = summarize_attempt_metrics(
        attempts=attempts,
        instance_results=instance_results,
        group_by=("task_cluster",),
        expensive_cost_threshold=1.0,
    )

    assert len(summaries) == 2
    labels = {summary.group[0][1] for summary in summaries}
    assert "len=short|signal=stacktrace|version=semantic" in labels
    assert "len=short|signal=neutral|version=semantic" in labels


def test_summarize_attempt_metrics_tracks_tokens_and_tool_calls() -> None:
    attempts = [
        _attempt_row(
            "solver-a",
            "inst-1",
            duration_ms=100,
            cost=0.4,
            usage={"input_tokens": 1200, "output_tokens": 90},
            raw_output="\n".join(
                [
                    '{"type":"item.started","item":{"type":"command_execution"}}',
                    '{"type":"item.started","item":{"type":"command_execution"}}',
                ]
            ),
        ),
        _attempt_row(
            "solver-a",
            "inst-2",
            duration_ms=250,
            cost=0.9,
            usage={"prompt_tokens": 2000, "completion_tokens": 200},
            raw_output='{"type":"response.output_item.added","item":{"type":"tool_call"}}',
        ),
    ]
    instance_results = [
        _eval_row("solver-a", "inst-1", harness_outcome="resolved", resolved=True),
        _eval_row("solver-a", "inst-2", harness_outcome="resolved", resolved=True),
    ]

    summaries = summarize_attempt_metrics(
        attempts=attempts,
        instance_results=instance_results,
    )

    summary = summaries[0]
    assert summary.total_input_tokens == 3200
    assert summary.total_output_tokens == 290
    assert summary.total_tokens == 3490
    assert summary.avg_total_tokens_per_attempt == 1745.0
    assert summary.tokens_per_resolved_issue == 1745.0
    assert summary.total_tool_calls == 3
    assert summary.avg_tool_calls_per_attempt == 1.5
    assert summary.tool_calls_per_resolved_issue == 1.5
    assert summary.avg_attempt_duration_ms == 175.0
    assert summary.p50_attempt_duration_ms == 250
    assert summary.p95_attempt_duration_ms == 250


def test_join_attempt_rows_estimates_cost_from_tokens_when_explicit_cost_missing() -> (
    None
):
    attempts = [
        {
            "solver_id": "solver-a",
            "instance_id": "inst-1",
            "duration_ms": 100,
            "usage": {
                "input_tokens": 100_000,
                "output_tokens": 5_000,
                "cached_input_tokens": 20_000,
            },
            "metadata": {"model": "gpt-5.4-mini"},
        }
    ]
    instance_results = [
        _eval_row("solver-a", "inst-1", harness_outcome="resolved", resolved=True)
    ]

    joined = join_attempt_rows(attempts, instance_results)

    assert len(joined) == 1
    assert joined[0]["attempt_cost_source"] == "estimated_from_tokens"
    assert joined[0]["attempt_cost_usd"] == pytest.approx(0.084, rel=1e-9)

    summaries = summarize_attempt_metrics(
        attempts=attempts,
        instance_results=instance_results,
    )
    assert summaries[0].total_cost_usd == pytest.approx(0.084, rel=1e-9)
    assert summaries[0].cost_per_resolved_issue == pytest.approx(0.084, rel=1e-9)


def test_join_attempt_rows_reads_total_cost_usd_field() -> None:
    attempts = [
        {
            "solver_id": "solver-a",
            "instance_id": "inst-1",
            "duration_ms": 100,
            "cost": {"total_cost_usd": 0.41},
        }
    ]
    instance_results = [
        _eval_row("solver-a", "inst-1", harness_outcome="resolved", resolved=True)
    ]

    joined = join_attempt_rows(attempts, instance_results)

    assert joined[0]["attempt_cost_source"] == "explicit"
    assert joined[0]["attempt_cost_usd"] == pytest.approx(0.41, rel=1e-9)


def test_join_attempt_rows_estimates_claude_cost_from_tokens_when_missing() -> None:
    attempts = [
        {
            "solver_id": "solver-a",
            "instance_id": "inst-1",
            "duration_ms": 100,
            "usage": {
                "input_tokens": 15,
                "output_tokens": 11153,
                "cache_read_input_tokens": 424394,
                "cache_creation_input_tokens": 30589,
                "cache_creation": {
                    "ephemeral_1h_input_tokens": 30589,
                    "ephemeral_5m_input_tokens": 0,
                },
            },
            "metadata": {"model": "claude-sonnet-4-6"},
        }
    ]
    instance_results = [
        _eval_row("solver-a", "inst-1", harness_outcome="resolved", resolved=True)
    ]

    joined = join_attempt_rows(attempts, instance_results)

    assert joined[0]["attempt_cost_source"] == "estimated_from_tokens"
    assert joined[0]["attempt_cost_usd"] == pytest.approx(0.4781922, rel=1e-9)


def test_summarize_attempt_metrics_counts_claude_tool_use_blocks() -> None:
    attempts = [
        {
            "solver_id": "solver-a",
            "instance_id": "inst-1",
            "duration_ms": 100,
            "cost": {"total_cost": 1.0},
            "metadata": {
                "telemetry": [
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {"type": "text", "text": "working"},
                                {"type": "tool_use", "name": "Read"},
                                {"type": "tool_use", "name": "Bash"},
                            ]
                        },
                    }
                ]
            },
        }
    ]
    instance_results = [
        _eval_row("solver-a", "inst-1", harness_outcome="resolved", resolved=True)
    ]

    summaries = summarize_attempt_metrics(
        attempts=attempts,
        instance_results=instance_results,
    )

    assert summaries[0].total_tool_calls == 2
    assert summaries[0].avg_tool_calls_per_attempt == 2.0
