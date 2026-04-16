from __future__ import annotations

import json
from pathlib import Path
import tempfile

from repogauge.runner.router import (
    build_router_training_rows,
    evaluate_router_baselines,
    load_router_training_rows,
    write_router_training_rows,
)


def _attempt_row(
    *,
    solver_id: str,
    instance_id: str,
    attempt_id: str,
    attempt_index: int,
    duration_ms: int,
    cost: float,
    attempt_state: str,
    resolved: bool,
    harness_outcome: str,
    failure_reason: str | None = None,
) -> dict[str, object]:
    return {
        "attempt_id": attempt_id,
        "attempt_index": attempt_index,
        "instance_id": instance_id,
        "solver_id": solver_id,
        "duration_ms": duration_ms,
        "cost": {"total_cost": cost},
        "attempt_state": attempt_state,
        "resolved": resolved,
        "harness_outcome": harness_outcome,
        "failure_reason": failure_reason,
        "exit_reason": failure_reason or "",
        "attempt_started_at": f"2026-04-16T00:00:{attempt_index:02d}Z",
        "attempt_ended_at": f"2026-04-16T00:00:{attempt_index + 1:02d}Z",
        "repo": "owner/repo",
        "base_commit": "abc123",
        "version": "1.0.0",
        "problem_statement": f"Fix problem for {instance_id}",
        "prompt_policy_hash": f"prompt-{solver_id}",
        "tool_policy_hash": f"tool-{solver_id}",
        "solver_config_hash": f"config-{solver_id}",
        "task_feature_version": "task-features-v1",
        "task_feature_hash": f"hash-{instance_id}",
        "task_cluster": "len=short|signal=neutral|version=semantic",
        "task_features": {
            "repo": "owner/repo",
            "base_commit_present": True,
            "version": "1.0.0",
        },
        "metadata": {
            "source": "unit-test",
        },
    }


def test_build_router_training_rows_exports_baselines_and_labels() -> None:
    attempts = [
        _attempt_row(
            solver_id="solver-cheap",
            instance_id="inst-1",
            attempt_id="a-1",
            attempt_index=1,
            duration_ms=10,
            cost=1.0,
            attempt_state="succeeded",
            resolved=True,
            harness_outcome="resolved",
        ),
        _attempt_row(
            solver_id="solver-expensive",
            instance_id="inst-1",
            attempt_id="b-1",
            attempt_index=1,
            duration_ms=12,
            cost=12.0,
            attempt_state="succeeded",
            resolved=True,
            harness_outcome="resolved",
        ),
        _attempt_row(
            solver_id="solver-cheap",
            instance_id="inst-2",
            attempt_id="a-2",
            attempt_index=1,
            duration_ms=8,
            cost=1.25,
            attempt_state="invalid_patch",
            resolved=False,
            harness_outcome="not_resolved",
            failure_reason="invalid_patch",
        ),
        _attempt_row(
            solver_id="solver-expensive",
            instance_id="inst-2",
            attempt_id="b-2",
            attempt_index=1,
            duration_ms=14,
            cost=11.5,
            attempt_state="succeeded",
            resolved=True,
            harness_outcome="resolved",
        ),
    ]
    instance_results = [
        {
            "instance_id": "inst-1",
            "solver_id": "solver-cheap",
            "harness_outcome": "resolved",
            "resolved": True,
        },
        {
            "instance_id": "inst-1",
            "solver_id": "solver-expensive",
            "harness_outcome": "resolved",
            "resolved": True,
        },
        {
            "instance_id": "inst-2",
            "solver_id": "solver-cheap",
            "harness_outcome": "not_resolved",
            "resolved": False,
        },
        {
            "instance_id": "inst-2",
            "solver_id": "solver-expensive",
            "harness_outcome": "resolved",
            "resolved": True,
        },
    ]

    rows = build_router_training_rows(attempts, instance_results)

    assert len(rows) == 2
    first = rows[0]
    assert first["cheap_solver_id"] == "solver-cheap"
    assert first["expensive_solver_id"] == "solver-expensive"
    assert first["route_label"] == "cheap_is_enough"
    assert first["policy_always_cheap_resolved"] is True
    assert first["policy_always_expensive_resolved"] is True
    assert first["policy_cheap_then_escalate_on_failure_escalated"] is False
    assert (
        first["policy_cheap_then_escalate_on_invalid_patch_timeout_no_progress_escalated"]
        is False
    )
    second = rows[1]
    assert second["route_label"] == "needs_expensive"
    assert second["policy_always_cheap_resolved"] is False
    assert second["policy_cheap_then_escalate_on_failure_resolved"] is True
    assert (
        second["policy_cheap_then_escalate_on_invalid_patch_timeout_no_progress_escalated"]
        is True
    )
    assert second["solver_outcomes"][0]["solver_id"] == "solver-cheap"
    assert second["task_feature_hash"]
    assert second["metadata"]["policy_assumption"]


def test_router_training_rows_roundtrip_and_policy_report() -> None:
    attempts = [
        _attempt_row(
            solver_id="solver-cheap",
            instance_id="inst-1",
            attempt_id="a-1",
            attempt_index=1,
            duration_ms=10,
            cost=1.0,
            attempt_state="succeeded",
            resolved=True,
            harness_outcome="resolved",
        ),
        _attempt_row(
            solver_id="solver-expensive",
            instance_id="inst-1",
            attempt_id="b-1",
            attempt_index=1,
            duration_ms=12,
            cost=12.0,
            attempt_state="succeeded",
            resolved=True,
            harness_outcome="resolved",
        ),
    ]
    instance_results = [
        {
            "instance_id": "inst-1",
            "solver_id": "solver-cheap",
            "harness_outcome": "resolved",
            "resolved": True,
        },
        {
            "instance_id": "inst-1",
            "solver_id": "solver-expensive",
            "harness_outcome": "resolved",
            "resolved": True,
        },
    ]

    rows = build_router_training_rows(attempts, instance_results)

    with tempfile.TemporaryDirectory() as workspace:
        path = Path(workspace) / "router_train.parquet"
        write_router_training_rows(path, rows)

        loaded = load_router_training_rows(path)
        report = evaluate_router_baselines(loaded)

    assert report["instance_count"] == 1
    assert report["cheap_solver_id"] == "solver-cheap"
    assert report["expensive_solver_id"] == "solver-expensive"
    assert len(report["policies"]) == 4
    assert report["policies"][0]["policy"] == "always_cheap"
    assert report["policies"][2]["policy"] == "cheap_then_escalate_on_failure"
