from __future__ import annotations

import tempfile
from pathlib import Path

from repogauge.runner.router import (
    ROUTER_MODEL_VERSION,
    build_router_training_rows,
    evaluate_router_model,
    load_router_model,
    run_router_training,
    train_router_model,
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
    problem_statement: str,
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
        "problem_statement": problem_statement,
        "prompt_policy_hash": f"prompt-{solver_id}",
        "tool_policy_hash": f"tool-{solver_id}",
        "solver_config_hash": f"config-{solver_id}",
    }


def _build_training_rows() -> list[dict[str, object]]:
    instances = [
        (
            "inst-cheap-1",
            "Traceback when loading cached settings from disk.",
            True,
            True,
        ),
        (
            "inst-cheap-2",
            "Test failure when the parser reads the updated fixture.",
            True,
            True,
        ),
        (
            "inst-exp-1",
            "Stacktrace while parsing configuration path for the cache.",
            False,
            True,
        ),
        (
            "inst-exp-2",
            "Error reported when the path resolver touches the config file.",
            False,
            True,
        ),
        (
            "inst-unknown-1",
            "Long vague regression with no precise repro steps available.",
            False,
            False,
        ),
        (
            "inst-unknown-2",
            "Another broad failure report without a concrete failing test.",
            False,
            False,
        ),
    ]

    attempts: list[dict[str, object]] = []
    instance_results: list[dict[str, object]] = []
    for index, (instance_id, problem_statement, cheap_resolved, expensive_resolved) in enumerate(
        instances,
        start=1,
    ):
        attempts.append(
            _attempt_row(
                solver_id="solver-cheap",
                instance_id=instance_id,
                attempt_id=f"{instance_id}:cheap",
                attempt_index=1,
                duration_ms=10 + index,
                cost=1.0 + index / 10.0,
                attempt_state="succeeded" if cheap_resolved else "invalid_patch",
                resolved=cheap_resolved,
                harness_outcome="resolved" if cheap_resolved else "not_resolved",
                problem_statement=problem_statement,
                failure_reason=None if cheap_resolved else "invalid_patch",
            )
        )
        attempts.append(
            _attempt_row(
                solver_id="solver-expensive",
                instance_id=instance_id,
                attempt_id=f"{instance_id}:expensive",
                attempt_index=1,
                duration_ms=20 + index,
                cost=10.0 + index,
                attempt_state="succeeded" if expensive_resolved else "timeout",
                resolved=expensive_resolved,
                harness_outcome="resolved" if expensive_resolved else "not_resolved",
                problem_statement=problem_statement,
                failure_reason=None if expensive_resolved else "timeout",
            )
        )
        instance_results.append(
            {
                "instance_id": instance_id,
                "solver_id": "solver-cheap",
                "harness_outcome": "resolved" if cheap_resolved else "not_resolved",
                "resolved": cheap_resolved,
            }
        )
        instance_results.append(
            {
                "instance_id": instance_id,
                "solver_id": "solver-expensive",
                "harness_outcome": "resolved" if expensive_resolved else "not_resolved",
                "resolved": expensive_resolved,
            }
        )

    return build_router_training_rows(attempts, instance_results)


def test_train_router_model_and_report_are_versioned() -> None:
    rows = _build_training_rows()

    model = train_router_model(rows, seed=7, train_fraction=0.67, validation_fraction=0.17, max_depth=3)
    learned = evaluate_router_model(rows, model)

    assert model["model_version"] == ROUTER_MODEL_VERSION
    assert model["task_feature_version"] == "task-features-v1"
    assert model["dataset_version"]
    assert model["row_count"] == len(rows)
    assert model["split"]["train_count"] + model["split"]["validation_count"] + model["split"]["test_count"] == len(rows)
    assert model["selected_depth"] >= 1
    assert model["validation_scores"] or model["split"]["validation_count"] == 0

    assert learned["policy"] == "learned_router"
    assert learned["route_label_total"] == len(rows)
    assert 0.0 <= learned["route_label_accuracy"] <= 1.0
    assert learned["p95_latency_ms"] >= 0


def test_run_router_training_writes_model_and_report() -> None:
    rows = _build_training_rows()

    with tempfile.TemporaryDirectory() as workspace:
        run_root = Path(workspace) / "run"
        run_root.mkdir()
        router_train_path = run_root / "router_train.parquet"
        write_router_training_rows(router_train_path, rows)

        report_out = Path(workspace) / "router_report_out"
        result = run_router_training(
            router_train_path,
            out_root=report_out,
            seed=11,
            train_fraction=0.7,
            validation_fraction=0.2,
            max_depth=4,
        )

        model_path = Path(result["router_model_path"])
        report_path = Path(result["router_report_path"])
        assert model_path.exists()
        assert report_path.exists()

        model = load_router_model(model_path)
        report = result["report"]

        assert model["model_version"] == ROUTER_MODEL_VERSION
        assert model["task_feature_version"] == "task-features-v1"
        assert report["dataset_instance_count"] == len(rows)
        assert report["model"]["model_version"] == ROUTER_MODEL_VERSION
        assert report["learned_router"]["policy"] == "learned_router"
        assert report["learned_router"]["model_version"] == ROUTER_MODEL_VERSION
        assert report["learned_router"]["route_label_total"] > 0
        assert len(report["policies"]) == 4
