from __future__ import annotations

import json
from pathlib import Path

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
) -> dict[str, object]:
    return {
        "solver_id": solver_id,
        "instance_id": instance_id,
        "duration_ms": duration_ms,
        "cost": {"total_cost": cost},
        "problem_statement": problem_statement,
    }


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
        ),
        _attempt_row(
            "solver-cheap",
            "inst-2",
            duration_ms=12,
            cost=0.5,
            problem_statement="Cheap path misses the timeout case.",
        ),
        _attempt_row(
            "solver-expensive",
            "inst-1",
            duration_ms=18,
            cost=6.0,
            problem_statement="Expensive path resolves the hard case.",
        ),
        _attempt_row(
            "solver-expensive",
            "inst-2",
            duration_ms=20,
            cost=7.0,
            problem_statement="Expensive path resolves the timeout case.",
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
    )

    assert report["top_line"]["best_solver_id"] == "solver-expensive"
    assert report["budget_frontier"][0]["best_solver_id"] == "solver-cheap"
    assert report["budget_frontier"][-1]["best_solver_id"] == "solver-expensive"
    assert report["failure_reason_breakdown"][0]["reason"] == "timeout"
    assert report["unresolved_samples"][0]["instance_id"] == "inst-2"

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
    assert "best_solver_id" in html
    assert "task_cluster" in html
    assert "solver_id" in html
