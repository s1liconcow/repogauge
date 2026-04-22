from __future__ import annotations

import json
from pathlib import Path
import re

from repogauge.runner.analyze import (
    build_analysis_report,
    summarize_attempt_metrics,
    write_summary_html,
    write_summary_json,
)


def _attempt_row(
    solver_id: str,
    instance_id: str,
    *,
    duration_ms: int,
    cost: float,
    problem_statement: str,
    usage: dict[str, object] | None = None,
    raw_output: str = "",
    **extra: object,
) -> dict[str, object]:
    row: dict[str, object] = {
        "solver_id": solver_id,
        "instance_id": instance_id,
        "duration_ms": duration_ms,
        "cost": {"total_cost": cost},
        "problem_statement": problem_statement,
        "raw_output": raw_output,
    }
    if usage is not None:
        row["usage"] = usage
    row.update(extra)
    return row


def _eval_row(
    solver_id: str,
    instance_id: str,
    *,
    harness_outcome: str,
    resolved: object,
    failure_reason: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "solver_id": solver_id,
        "instance_id": instance_id,
        "harness_outcome": harness_outcome,
        "resolved": resolved,
    }
    if failure_reason is not None:
        payload["failure_reason"] = failure_reason
    return payload


def test_analysis_report_includes_budget_and_failure_sections(tmp_path: Path) -> None:
    attempts = [
        _attempt_row(
            "solver-cheap",
            "inst-1",
            duration_ms=10,
            cost=1.0,
            problem_statement="Cheap path resolves one issue.",
            usage={"input_tokens": 1000, "output_tokens": 100},
            attempt_id="attempt-cheap-1",
            attempt_state="succeeded",
            instance_repo="owner/repo",
            model_patch="diff --git a/a.py b/a.py\n+print('cheap')\n",
        ),
        _attempt_row(
            "solver-cheap",
            "inst-2",
            duration_ms=12,
            cost=0.5,
            problem_statement="Cheap path misses the timeout case.",
            usage={"input_tokens": 900, "output_tokens": 80},
            attempt_id="attempt-cheap-2",
            attempt_state="failed",
            instance_repo="owner/repo",
            model_patch="diff --git a/b.py b/b.py\n+print('cheap-timeout')\n",
        ),
        _attempt_row(
            "solver-expensive",
            "inst-1",
            duration_ms=18,
            cost=6.0,
            problem_statement="Expensive path resolves the hard case.",
            usage={"input_tokens": 3200, "output_tokens": 210},
            raw_output='{"type":"item.started","item":{"type":"command_execution"}}',
            attempt_id="attempt-expensive-1",
            attempt_state="succeeded",
            instance_repo="owner/repo",
            model_patch="diff --git a/a.py b/a.py\n+print('expensive')\n",
        ),
        _attempt_row(
            "solver-expensive",
            "inst-2",
            duration_ms=20,
            cost=7.0,
            problem_statement="Expensive path resolves the timeout case.",
            usage={"input_tokens": 3400, "output_tokens": 260},
            raw_output='{"type":"response.output_item.added","item":{"type":"tool_call"}}',
            attempt_id="attempt-expensive-2",
            attempt_state="succeeded",
            instance_repo="owner/repo",
            model_patch="diff --git a/b.py b/b.py\n+print('expensive-timeout')\n",
        ),
    ]
    instance_results = [
        _eval_row("solver-cheap", "inst-1", harness_outcome="resolved", resolved=True),
        _eval_row(
            "solver-cheap",
            "inst-2",
            harness_outcome="timeout",
            resolved=False,
            failure_reason="timeout",
        ),
        _eval_row(
            "solver-expensive",
            "inst-1",
            harness_outcome="resolved",
            resolved=True,
        ),
        _eval_row(
            "solver-expensive",
            "inst-2",
            harness_outcome="resolved",
            resolved=True,
        ),
    ]

    grouped_summaries = summarize_attempt_metrics(
        attempts=attempts,
        instance_results=instance_results,
        group_by=("task_cluster",),
        expensive_cost_threshold=5.0,
    )
    solver_summaries = summarize_attempt_metrics(
        attempts=attempts,
        instance_results=instance_results,
        group_by=("solver_id",),
        expensive_cost_threshold=5.0,
    )
    report = build_analysis_report(
        attempts=attempts,
        instance_results=instance_results,
        grouped_summaries=grouped_summaries,
        solver_summaries=solver_summaries,
        group_by=("task_cluster",),
        expensive_cost_threshold=5.0,
        metadata={"run_root": "/tmp/run"},
        llm_judge_report={
            "enabled": True,
            "top_line": {
                "judged_attempt_count": 2,
                "judged_job_count": 2,
                "scored_job_count": 2,
                "error_job_count": 0,
                "cache_hit_count": 0,
                "avg_overall_delta": 0.4,
                "better_share": 0.5,
                "worse_share": 0.0,
                "best_solver_id": "solver-expensive",
            },
            "solver_rows": [
                {
                    "solver_id": "solver-expensive",
                    "judged_job_count": 2,
                    "avg_overall_delta": 0.4,
                    "better_share": 0.5,
                    "worse_share": 0.0,
                    "resolved_but_worse_count": 0,
                    "unresolved_but_promising_count": 1,
                }
            ],
            "dimension_rows": [
                {
                    "name": "maintainability",
                    "weight": 0.2,
                    "avg_delta": 0.5,
                    "better_share": 0.5,
                    "worse_share": 0.0,
                }
            ],
            "resolved_but_worse_than_gold": [],
            "unresolved_but_promising": [
                {
                    "attempt_id": "a-1",
                    "instance_id": "inst-2",
                    "solver_id": "solver-expensive",
                    "resolved": False,
                    "harness_outcome": "timeout",
                    "attempt_state": "succeeded",
                    "overall_delta": 0.4,
                    "overall_label": "better",
                    "confidence": 0.8,
                    "summary": "Directionally good.",
                }
            ],
            "best_samples": [],
            "worst_samples": [],
        },
        llm_judge_rows=[
            {
                "attempt_id": "attempt-expensive-2",
                "job_id": "job-expensive-2",
                "instance_id": "inst-2",
                "solver_id": "solver-expensive",
                "resolved": True,
                "harness_outcome": "resolved",
                "attempt_state": "succeeded",
                "overall_delta": 0.4,
                "overall_label": "better",
                "confidence": 0.8,
                "summary": "Directionally good.",
                "dimensions": [
                    {
                        "name": "maintainability",
                        "weight": 0.2,
                        "delta": 1,
                        "label": "better",
                        "rationale": "Cleaner structure.",
                    }
                ],
                "metadata": {"judge_status": "scored"},
            }
        ],
    )

    assert report["top_line"]["best_solver_id"] == "solver-expensive"
    assert report["budget_frontier"][0]["best_solver_id"] == "solver-cheap"
    assert report["budget_frontier"][-1]["best_solver_id"] == "solver-expensive"
    assert report["failure_reason_breakdown"][0]["reason"] == "timeout"
    assert report["unresolved_samples"][0]["instance_id"] == "inst-2"
    assert report["top_line"]["total_tokens"] == 9150
    assert report["top_line"]["total_tool_calls"] == 2
    assert report["cost_opportunity"]["portfolio_cost_floor_usd"] == 8.0
    assert report["cost_opportunity"]["best_solver_mixed_routing_gap_usd"] == 5.0
    assert report["llm_judge"]["top_line"]["best_solver_id"] == "solver-expensive"
    assert report["attempt_browser"]["judge_available"] is True
    assert report["attempt_browser"]["instance_count"] == 2
    assert (
        report["cost_opportunity"]["solver_savings"][0]["solver_id"]
        == "solver-expensive"
    )

    summary_path = tmp_path / "summary.json"
    html_path = tmp_path / "report.html"
    write_summary_json(
        summary_path,
        grouped_summaries,
        metadata={"run_root": "/tmp/run"},
        report=report,
    )
    write_summary_html(
        html_path,
        grouped_summaries,
        group_by=("task_cluster",),
        metadata={"run_root": "/tmp/run"},
        report=report,
    )

    summary_payload = json.loads(summary_path.read_text(encoding="utf-8"))
    assert "report" in summary_payload
    assert (
        summary_payload["report"]["budget_frontier"][0]["best_solver_id"]
        == "solver-cheap"
    )

    html = html_path.read_text(encoding="utf-8")
    assert "Budget Frontier" in html
    assert "Failure Reasons" in html
    assert "Unresolved Samples" in html
    assert "Cost Opportunities" in html
    assert "Solver Frontier" in html
    assert "Total Tokens" in html
    assert "Avg Tool Calls" in html
    assert "Best Solver" in html
    assert "LLM Judge Solver View" in html
    assert "Unresolved But Promising" in html
    assert "Attempt Browser" in html
    assert "LLM Judge included" in html
    assert "solver-tab" in html
    assert "https://esm.sh/@pierre/diffs" in html
    assert "data-diff-viewer" in html
    assert "task_cluster" in html
    assert "Solver" in html


def test_analysis_report_prefers_local_eval_reason_over_unknown(tmp_path: Path) -> None:
    attempts = [
        _attempt_row(
            "solver-a",
            "inst-1",
            duration_ms=10,
            cost=1.0,
            problem_statement="Local eval reason should surface.",
            model_patch="diff --git a/a.py b/a.py\n+print('x')\n",
        )
    ]
    instance_results = [
        {
            "solver_id": "solver-a",
            "instance_id": "inst-1",
            "status": "not_resolved",
            "reason": "no_fail_to_pass",
            "resolved": False,
        }
    ]

    grouped_summaries = summarize_attempt_metrics(
        attempts=attempts,
        instance_results=instance_results,
        group_by=("solver_id",),
        expensive_cost_threshold=5.0,
    )
    solver_summaries = summarize_attempt_metrics(
        attempts=attempts,
        instance_results=instance_results,
        group_by=("solver_id",),
        expensive_cost_threshold=5.0,
    )
    report = build_analysis_report(
        attempts=attempts,
        instance_results=instance_results,
        grouped_summaries=grouped_summaries,
        solver_summaries=solver_summaries,
        group_by=("solver_id",),
        expensive_cost_threshold=5.0,
        metadata={"run_root": "/tmp/run"},
    )

    assert report["failure_reason_breakdown"][0]["reason"] == "no_fail_to_pass"

    html_path = tmp_path / "report.html"
    write_summary_html(
        html_path,
        grouped_summaries,
        group_by=("solver_id",),
        metadata={"run_root": "/tmp/run"},
        report=report,
    )
    html = html_path.read_text(encoding="utf-8")
    assert "no_fail_to_pass" in html


def test_analysis_report_falls_back_to_attempt_exit_reason(tmp_path: Path) -> None:
    attempts = [
        _attempt_row(
            "solver-a",
            "inst-1",
            duration_ms=10,
            cost=0.0,
            problem_statement="Attempt-side failure reason should surface.",
            attempt_state="invalid_patch",
            exit_reason="invalid patch: no unified diff found in model output",
            model_patch="",
            metadata={
                "telemetry": [
                    {
                        "error": {
                            "name": "UnknownError",
                            "data": {"message": "Model not found: example/model."},
                        }
                    }
                ]
            },
        ),
        _attempt_row(
            "solver-b",
            "inst-1",
            duration_ms=10,
            cost=1.0,
            problem_statement="Control solver resolves the task.",
            usage={"input_tokens": 100, "output_tokens": 10},
            attempt_state="succeeded",
            model_patch="diff --git a/a.py b/a.py\n+print('ok')\n",
        ),
    ]
    instance_results = [
        _eval_row("solver-a", "inst-1", harness_outcome="unknown", resolved=False),
        _eval_row("solver-b", "inst-1", harness_outcome="resolved", resolved=True),
    ]

    summaries = summarize_attempt_metrics(
        attempts=attempts,
        instance_results=instance_results,
        group_by=("solver_id",),
        expensive_cost_threshold=5.0,
    )
    report = build_analysis_report(
        attempts=attempts,
        instance_results=instance_results,
        grouped_summaries=summaries,
        solver_summaries=summaries,
        group_by=("solver_id",),
        expensive_cost_threshold=5.0,
        metadata={"run_root": "/tmp/run"},
    )

    assert report["failure_reason_breakdown"][0]["reason"] == "model_not_found"
    assert (
        report["attempt_browser"]["instances"][0]["solvers"][0]["failure_reason"]
        == "model_not_found"
    )

    html_path = tmp_path / "report.html"
    write_summary_html(
        html_path,
        summaries,
        group_by=("solver_id",),
        metadata={"run_root": "/tmp/run"},
        report=report,
    )
    html = html_path.read_text(encoding="utf-8")
    assert "model_not_found" in html
    assert "invalid patch: no unified diff found in model output" in html


def test_solver_frontier_uses_absolute_percent_scale(tmp_path: Path) -> None:
    attempts = [
        _attempt_row(
            "solver-high",
            "inst-1",
            duration_ms=10,
            cost=1.0,
            problem_statement="High solver resolves all tasks.",
            usage={"input_tokens": 100, "output_tokens": 10},
            attempt_state="succeeded",
            model_patch="diff --git a/a.py b/a.py\n+print('high')\n",
        ),
        _attempt_row(
            "solver-high",
            "inst-2",
            duration_ms=11,
            cost=1.0,
            problem_statement="High solver resolves all tasks.",
            usage={"input_tokens": 110, "output_tokens": 12},
            attempt_state="succeeded",
            model_patch="diff --git a/b.py b/b.py\n+print('high-2')\n",
        ),
        _attempt_row(
            "solver-mid",
            "inst-1",
            duration_ms=12,
            cost=2.0,
            problem_statement="Mid solver misses one task.",
            usage={"input_tokens": 120, "output_tokens": 14},
            attempt_state="succeeded",
            model_patch="diff --git a/c.py b/c.py\n+print('mid')\n",
        ),
        _attempt_row(
            "solver-mid",
            "inst-2",
            duration_ms=13,
            cost=2.0,
            problem_statement="Mid solver misses one task.",
            attempt_state="failed",
            exit_reason="rate limit reached",
            model_patch="",
        ),
    ]
    instance_results = [
        _eval_row("solver-high", "inst-1", harness_outcome="resolved", resolved=True),
        _eval_row("solver-high", "inst-2", harness_outcome="resolved", resolved=True),
        _eval_row("solver-mid", "inst-1", harness_outcome="resolved", resolved=True),
        _eval_row("solver-mid", "inst-2", harness_outcome="unknown", resolved=False),
    ]

    summaries = summarize_attempt_metrics(
        attempts=attempts,
        instance_results=instance_results,
        group_by=("solver_id",),
        expensive_cost_threshold=5.0,
    )
    report = build_analysis_report(
        attempts=attempts,
        instance_results=instance_results,
        grouped_summaries=summaries,
        solver_summaries=summaries,
        group_by=("solver_id",),
        expensive_cost_threshold=5.0,
        metadata={"run_root": "/tmp/run"},
    )

    html_path = tmp_path / "report.html"
    write_summary_html(
        html_path,
        summaries,
        group_by=("solver_id",),
        metadata={"run_root": "/tmp/run"},
        report=report,
    )
    html = html_path.read_text(encoding="utf-8")
    match = re.search(
        r'<circle cx="[^"]+" cy="([^"]+)"[^>]*><title>solver-mid \| resolve 50\.0%',
        html,
    )
    assert match is not None
    assert float(match.group(1)) < 250.0
