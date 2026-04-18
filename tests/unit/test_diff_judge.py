from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from repogauge.exec import CommandResult
from repogauge.runner.diff_judge import (
    _normalize_provider,
    build_diff_judge_report,
    run_diff_judge,
    validate_llm_judge_policy,
)


def test_validate_llm_judge_policy_rejects_remote_provider_in_local_only_mode() -> None:
    with pytest.raises(ValueError, match="requires --llm-mode allow_remote"):
        validate_llm_judge_policy(llm_mode="local_only", provider="openai")


def test_validate_llm_judge_policy_allows_codex_in_local_only_mode() -> None:
    validate_llm_judge_policy(llm_mode="local_only", provider="codex")


def test_normalize_provider_defaults_to_codex() -> None:
    assert _normalize_provider(None) == "codex"
    assert _normalize_provider("") == "codex"


def test_run_diff_judge_writes_rows_and_reuses_cache(tmp_path: Path) -> None:
    joined_rows = [
        {
            "attempt_id": "run:inst-1:solver-a:1:attempt-1",
            "job_id": "run:inst-1:solver-a:1",
            "instance_id": "inst-1",
            "solver_id": "solver-a",
            "resolved": True,
            "harness_outcome": "resolved",
            "attempt_state": "succeeded",
            "model_patch": "diff --git a/src.py b/src.py\n+print('candidate')\n",
        }
    ]
    dataset_rows = {
        "inst-1": {
            "instance_id": "inst-1",
            "problem_statement": "Fix the parser regression.",
            "patch": "diff --git a/src.py b/src.py\n+print('gold')\n",
            "test_patch": "diff --git a/test_src.py b/test_src.py\n+assert True\n",
        }
    }
    response_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "summary": "Cleaner than the gold patch.",
                            "confidence": 0.9,
                            "dimensions": [
                                {
                                    "name": "task_fit",
                                    "delta": 1,
                                    "label": "better",
                                    "rationale": "Still addresses the same bug.",
                                },
                                {
                                    "name": "correctness_safety",
                                    "delta": 1,
                                    "label": "better",
                                    "rationale": "Less regression risk.",
                                },
                                {
                                    "name": "maintainability",
                                    "delta": 1,
                                    "label": "better",
                                    "rationale": "Simpler control flow.",
                                },
                                {
                                    "name": "test_quality",
                                    "delta": 0,
                                    "label": "same",
                                    "rationale": "Comparable test posture.",
                                },
                                {
                                    "name": "change_focus",
                                    "delta": 1,
                                    "label": "better",
                                    "rationale": "Tighter scope.",
                                },
                            ],
                        }
                    )
                }
            }
        ]
    }

    with patch(
        "repogauge.runner.diff_judge._invoke_model",
        return_value=(
            {"model": "judge-unit"},
            response_payload,
            {"input_tokens": 100},
            "response.usage",
            {"total_cost": 0.01},
            "response.cost",
        ),
    ) as mock_invoke:
        result = run_diff_judge(
            joined_rows=joined_rows,
            dataset_rows=dataset_rows,
            out_root=tmp_path,
            llm_mode="local_only",
            model_name="judge-unit",
            provider="local",
        )

    assert mock_invoke.call_count == 1
    rows = [
        json.loads(line)
        for line in Path(result.rows_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["overall_label"] == "better"
    assert rows[0]["metadata"]["judge_status"] == "scored"
    assert Path(rows[0]["metadata"]["request_ref"]).exists()
    assert Path(rows[0]["metadata"]["response_ref"]).exists()

    with patch("repogauge.runner.diff_judge._invoke_model") as mock_cached_invoke:
        cached = run_diff_judge(
            joined_rows=joined_rows,
            dataset_rows=dataset_rows,
            out_root=tmp_path,
            llm_mode="local_only",
            model_name="judge-unit",
            provider="local",
        )
    assert mock_cached_invoke.call_count == 0
    assert cached.rows[0]["metadata"]["cache_hit"] is True


def test_run_diff_judge_emits_progress_updates(tmp_path: Path) -> None:
    joined_rows = [
        {
            "attempt_id": "run:inst-1:solver-a:1:attempt-1",
            "job_id": "run:inst-1:solver-a:1",
            "instance_id": "inst-1",
            "solver_id": "solver-a",
            "resolved": True,
            "harness_outcome": "resolved",
            "attempt_state": "succeeded",
            "model_patch": "diff --git a/src.py b/src.py\n+print('candidate')\n",
        }
    ]
    dataset_rows = {
        "inst-1": {
            "instance_id": "inst-1",
            "problem_statement": "Fix the parser regression.",
            "patch": "diff --git a/src.py b/src.py\n+print('gold')\n",
            "test_patch": "diff --git a/test_src.py b/test_src.py\n+assert True\n",
        }
    }
    response_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "summary": "Cleaner than the gold patch.",
                            "confidence": 0.9,
                            "dimensions": [
                                {
                                    "name": "task_fit",
                                    "delta": 1,
                                    "label": "better",
                                    "rationale": "Still addresses the same bug.",
                                },
                                {
                                    "name": "correctness_safety",
                                    "delta": 1,
                                    "label": "better",
                                    "rationale": "Less regression risk.",
                                },
                                {
                                    "name": "maintainability",
                                    "delta": 1,
                                    "label": "better",
                                    "rationale": "Simpler control flow.",
                                },
                                {
                                    "name": "test_quality",
                                    "delta": 0,
                                    "label": "same",
                                    "rationale": "Comparable test posture.",
                                },
                                {
                                    "name": "change_focus",
                                    "delta": 1,
                                    "label": "better",
                                    "rationale": "Tighter scope.",
                                },
                            ],
                        }
                    )
                }
            }
        ]
    }
    progress_stream = io.StringIO()

    with patch(
        "repogauge.runner.diff_judge._invoke_model",
        return_value=(
            {"model": "judge-unit"},
            response_payload,
            {"input_tokens": 100},
            "response.usage",
            {"total_cost": 0.01},
            "response.cost",
        ),
    ):
        run_diff_judge(
            joined_rows=joined_rows,
            dataset_rows=dataset_rows,
            out_root=tmp_path,
            llm_mode="local_only",
            model_name="judge-unit",
            provider="local",
            progress_stream=progress_stream,
        )

    progress_output = progress_stream.getvalue()
    assert "repogauge analyze: llm judge" in progress_output
    assert "calling judge for solver-a inst-1" in progress_output
    assert "scored solver-a inst-1" in progress_output


def test_run_diff_judge_does_not_reuse_cached_error_rows(tmp_path: Path) -> None:
    joined_rows = [
        {
            "attempt_id": "run:inst-1:solver-a:1:attempt-1",
            "job_id": "run:inst-1:solver-a:1",
            "instance_id": "inst-1",
            "solver_id": "solver-a",
            "resolved": True,
            "harness_outcome": "resolved",
            "attempt_state": "succeeded",
            "model_patch": "diff --git a/src.py b/src.py\n+print('candidate')\n",
        }
    ]
    dataset_rows = {
        "inst-1": {
            "instance_id": "inst-1",
            "problem_statement": "Fix the parser regression.",
            "patch": "diff --git a/src.py b/src.py\n+print('gold')\n",
            "test_patch": "diff --git a/test_src.py b/test_src.py\n+assert True\n",
        }
    }
    response_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "summary": "Cleaner than the gold patch.",
                            "confidence": 0.9,
                            "dimensions": [
                                {
                                    "name": "task_fit",
                                    "delta": 1,
                                    "label": "better",
                                    "rationale": "Still addresses the same bug.",
                                },
                                {
                                    "name": "correctness_safety",
                                    "delta": 1,
                                    "label": "better",
                                    "rationale": "Less regression risk.",
                                },
                                {
                                    "name": "maintainability",
                                    "delta": 1,
                                    "label": "better",
                                    "rationale": "Simpler control flow.",
                                },
                                {
                                    "name": "test_quality",
                                    "delta": 0,
                                    "label": "same",
                                    "rationale": "Comparable test posture.",
                                },
                                {
                                    "name": "change_focus",
                                    "delta": 1,
                                    "label": "better",
                                    "rationale": "Tighter scope.",
                                },
                            ],
                        }
                    )
                }
            }
        ]
    }

    with patch(
        "repogauge.runner.diff_judge._invoke_model",
        side_effect=RuntimeError("judge unavailable"),
    ) as mock_first:
        errored = run_diff_judge(
            joined_rows=joined_rows,
            dataset_rows=dataset_rows,
            out_root=tmp_path,
            llm_mode="local_only",
            model_name="judge-unit",
            provider="local",
        )
    assert mock_first.call_count == 1
    assert errored.rows[0]["metadata"]["judge_status"] == "error"

    with patch(
        "repogauge.runner.diff_judge._invoke_model",
        return_value=(
            {"model": "judge-unit"},
            response_payload,
            {"input_tokens": 100},
            "response.usage",
            {"total_cost": 0.01},
            "response.cost",
        ),
    ) as mock_second:
        retried = run_diff_judge(
            joined_rows=joined_rows,
            dataset_rows=dataset_rows,
            out_root=tmp_path,
            llm_mode="local_only",
            model_name="judge-unit",
            provider="local",
        )

    assert mock_second.call_count == 1
    assert retried.rows[0]["metadata"]["judge_status"] == "scored"
    assert retried.rows[0]["metadata"]["cache_hit"] is False


def test_run_diff_judge_defaults_to_codex_cli_provider(tmp_path: Path) -> None:
    joined_rows = [
        {
            "attempt_id": "run:inst-1:solver-a:1:attempt-1",
            "job_id": "run:inst-1:solver-a:1",
            "instance_id": "inst-1",
            "solver_id": "solver-a",
            "resolved": True,
            "harness_outcome": "resolved",
            "attempt_state": "succeeded",
            "model_patch": "diff --git a/src.py b/src.py\n+print('candidate')\n",
        }
    ]
    dataset_rows = {
        "inst-1": {
            "instance_id": "inst-1",
            "problem_statement": "Fix the parser regression.",
            "patch": "diff --git a/src.py b/src.py\n+print('gold')\n",
            "test_patch": "diff --git a/test_src.py b/test_src.py\n+assert True\n",
        }
    }
    judge_payload = {
        "summary": "Candidate is cleaner than gold.",
        "confidence": 0.85,
        "dimensions": [
            {
                "name": "task_fit",
                "delta": 1,
                "label": "better",
                "rationale": "Solves the same task.",
            },
            {
                "name": "correctness_safety",
                "delta": 1,
                "label": "better",
                "rationale": "Lower regression risk.",
            },
            {
                "name": "maintainability",
                "delta": 1,
                "label": "better",
                "rationale": "Simpler code path.",
            },
            {
                "name": "test_quality",
                "delta": 0,
                "label": "same",
                "rationale": "Comparable tests.",
            },
            {
                "name": "change_focus",
                "delta": 1,
                "label": "better",
                "rationale": "More focused diff.",
            },
        ],
    }
    command_output = json.dumps({"output_text": json.dumps(judge_payload)}) + "\n"

    with (
        patch(
            "repogauge.runner.diff_judge.run_command",
            return_value=CommandResult(
                command=["codex"],
                returncode=0,
                stdout=command_output,
                stderr="",
            ),
        ) as mock_run_command,
        patch("repogauge.runner.diff_judge.Path.cwd", return_value=tmp_path),
    ):
        result = run_diff_judge(
            joined_rows=joined_rows,
            dataset_rows=dataset_rows,
            out_root=tmp_path,
            llm_mode="local_only",
            model_name=None,
            provider=None,
        )

    call = mock_run_command.call_args
    assert call is not None
    command = call.args[0]
    assert command[:4] == ["codex", "--ask-for-approval", "never", "exec"]
    assert "--model" in command
    assert command[command.index("--model") + 1] == "gpt-5.4"
    assert result.model["provider"] == "codex"
    assert result.model["model_name"] == "gpt-5.4"
    assert result.rows[0]["overall_label"] == "better"
    assert result.rows[0]["metadata"]["judge_status"] == "scored"
    assert (tmp_path / ".repogauge" / "judge-codex-home" / ".codex").exists()


def test_build_diff_judge_report_uses_latest_attempt_per_job() -> None:
    rows = [
        {
            "attempt_id": "run:inst-1:solver-a:1:attempt-1",
            "job_id": "run:inst-1:solver-a:1",
            "instance_id": "inst-1",
            "solver_id": "solver-a",
            "resolved": False,
            "harness_outcome": "not_resolved",
            "attempt_state": "failed",
            "overall_delta": -1.0,
            "overall_label": "worse",
            "confidence": 0.5,
            "summary": "First try was weaker.",
            "dimensions": [
                {"name": "task_fit", "weight": 0.3, "delta": -1, "label": "worse"},
                {
                    "name": "correctness_safety",
                    "weight": 0.25,
                    "delta": -1,
                    "label": "worse",
                },
                {
                    "name": "maintainability",
                    "weight": 0.2,
                    "delta": -1,
                    "label": "worse",
                },
                {"name": "test_quality", "weight": 0.15, "delta": 0, "label": "same"},
                {"name": "change_focus", "weight": 0.1, "delta": -1, "label": "worse"},
            ],
            "metadata": {"judge_status": "scored", "cache_hit": False},
        },
        {
            "attempt_id": "run:inst-1:solver-a:1:attempt-2",
            "job_id": "run:inst-1:solver-a:1",
            "instance_id": "inst-1",
            "solver_id": "solver-a",
            "resolved": True,
            "harness_outcome": "resolved",
            "attempt_state": "succeeded",
            "overall_delta": 0.6,
            "overall_label": "better",
            "confidence": 0.9,
            "summary": "Retry was cleaner.",
            "dimensions": [
                {"name": "task_fit", "weight": 0.3, "delta": 1, "label": "better"},
                {
                    "name": "correctness_safety",
                    "weight": 0.25,
                    "delta": 1,
                    "label": "better",
                },
                {
                    "name": "maintainability",
                    "weight": 0.2,
                    "delta": 1,
                    "label": "better",
                },
                {"name": "test_quality", "weight": 0.15, "delta": 0, "label": "same"},
                {"name": "change_focus", "weight": 0.1, "delta": 1, "label": "better"},
            ],
            "metadata": {"judge_status": "scored", "cache_hit": True},
        },
        {
            "attempt_id": "run:inst-2:solver-b:1:attempt-1",
            "job_id": "run:inst-2:solver-b:1",
            "instance_id": "inst-2",
            "solver_id": "solver-b",
            "resolved": True,
            "harness_outcome": "resolved",
            "attempt_state": "succeeded",
            "overall_delta": -0.7,
            "overall_label": "worse",
            "confidence": 0.7,
            "summary": "Passed but sloppier.",
            "dimensions": [
                {"name": "task_fit", "weight": 0.3, "delta": 0, "label": "same"},
                {
                    "name": "correctness_safety",
                    "weight": 0.25,
                    "delta": -1,
                    "label": "worse",
                },
                {
                    "name": "maintainability",
                    "weight": 0.2,
                    "delta": -1,
                    "label": "worse",
                },
                {
                    "name": "test_quality",
                    "weight": 0.15,
                    "delta": -1,
                    "label": "worse",
                },
                {"name": "change_focus", "weight": 0.1, "delta": 0, "label": "same"},
            ],
            "metadata": {"judge_status": "scored", "cache_hit": False},
        },
    ]

    report = build_diff_judge_report(
        rows,
        model={"model_name": "judge-unit", "provider": "local"},
    )

    assert report["top_line"]["judged_attempt_count"] == 3
    assert report["top_line"]["judged_job_count"] == 2
    assert report["top_line"]["scored_job_count"] == 2
    assert report["top_line"]["best_solver_id"] == "solver-a"
    assert report["top_line"]["cache_hit_count"] == 1
    assert report["solver_rows"][0]["solver_id"] == "solver-a"
    assert report["solver_rows"][0]["judged_job_count"] == 1
    assert report["resolved_but_worse_than_gold"][0]["solver_id"] == "solver-b"
