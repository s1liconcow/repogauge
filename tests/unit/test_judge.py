from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from repogauge.runner.judge import (
    HarnessRunSummary,
    JudgeBatchResult,
    JudgeSchedulerConfig,
    run_harness_evaluation,
    _parse_harness_results,
    _result_row_from_instance,
)


def _dataset_row(
    *,
    instance_id: str,
    model: str,
    repo: str = "repo",
    version: str = "1",
) -> dict:
    return {
        "instance_id": instance_id,
        "patch": "diff --git a/x b/x\n+print('ok')",
        "repo": repo,
        "version": version,
        "model_name_or_path": model,
    }


def test_prepare_batches_and_ordered_merge(tmp_path: Path) -> None:
    dataset_rows = [
        _dataset_row(
            instance_id="inst-a", model="solver-x", repo="repo-a", version="1"
        ),
        _dataset_row(
            instance_id="inst-b", model="solver-x", repo="repo-a", version="1"
        ),
        _dataset_row(
            instance_id="inst-c", model="solver-y", repo="repo-a", version="1"
        ),
    ]
    predictions_rows = [
        {
            "instance_id": "inst-a",
            "model_name_or_path": "solver-x",
            "model_patch": "diff",
        },
        {
            "instance_id": "inst-b",
            "model_name_or_path": "solver-x",
            "model_patch": "diff",
        },
        {
            "instance_id": "inst-c",
            "model_name_or_path": "solver-y",
            "model_patch": "diff",
        },
    ]

    dataset_path = tmp_path / "dataset.jsonl"
    dataset_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in dataset_rows),
        encoding="utf-8",
    )
    predictions_path = tmp_path / "predictions.jsonl"
    predictions_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in predictions_rows),
        encoding="utf-8",
    )

    def fake_run_batch(*, batch_key: str, rows: list[tuple[dict, dict]], **kwargs):
        instance_rows = [
            _result_row_from_instance(dataset_row=row, status="resolved")
            for row, _ in rows
        ]
        return JudgeBatchResult(
            instance_rows=instance_rows,
            metadata={"batch_key": batch_key},
            batch_key=batch_key,
        )

    with patch("repogauge.runner.judge._run_batch") as mock_run_batch:
        mock_run_batch.side_effect = fake_run_batch
        summary = run_harness_evaluation(
            dataset_path=dataset_path,
            predictions_path=predictions_path,
            out_root=tmp_path,
            adapter_path=None,
            workers=1,
            timeout_seconds=120,
            gold_if_missing=False,
            judge_config=JudgeSchedulerConfig(
                batch_size=2,
                max_parallel_batches=2,
                workers_per_batch=1,
            ),
        )

    assert isinstance(summary, HarnessRunSummary)
    assert summary.total == 3
    assert summary.resolved == 3
    assert summary.skipped == 0
    assert summary.error == 0
    assert mock_run_batch.call_count == 2

    validation = [
        json.loads(line)
        for line in (tmp_path / "validation.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [row["instance_id"] for row in validation] == [
        "inst-a",
        "inst-b",
        "inst-c",
    ]

    instance_results = tmp_path / "instance_results.jsonl"
    assert instance_results.exists()
    assert instance_results.read_text(encoding="utf-8").count("\n") == 3

    batch_results = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    assert batch_results["batch_count"] == 2


def test_run_harness_evaluation_marks_missing_predictions_as_skipped(
    tmp_path: Path,
) -> None:
    dataset_rows = [
        _dataset_row(instance_id="inst-a", model="solver-x"),
        _dataset_row(instance_id="inst-b", model="solver-x"),
    ]
    predictions_rows = [
        {
            "instance_id": "inst-a",
            "model_name_or_path": "solver-x",
            "model_patch": "diff",
        }
    ]

    dataset_path = tmp_path / "dataset.jsonl"
    dataset_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in dataset_rows),
        encoding="utf-8",
    )
    predictions_path = tmp_path / "predictions.jsonl"
    predictions_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in predictions_rows),
        encoding="utf-8",
    )

    def fake_run_batch(*, batch_key: str, rows: list[tuple[dict, dict]], **kwargs):
        instance_rows = [
            _result_row_from_instance(dataset_row=rows[0][0], status="resolved")
        ]
        return JudgeBatchResult(
            instance_rows=instance_rows,
            metadata={"batch_key": batch_key},
            batch_key=batch_key,
        )

    with patch("repogauge.runner.judge._run_batch") as mock_run_batch:
        mock_run_batch.side_effect = fake_run_batch
        summary = run_harness_evaluation(
            dataset_path=dataset_path,
            predictions_path=predictions_path,
            out_root=tmp_path,
            adapter_path=None,
            workers=1,
            timeout_seconds=120,
            gold_if_missing=False,
            judge_config=JudgeSchedulerConfig(batch_size=32),
        )

    assert summary.total == 2
    assert summary.resolved == 1
    assert summary.skipped == 1
    assert summary.not_resolved == 0

    lines = [
        json.loads(line)
        for line in (tmp_path / "validation.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert lines[0]["instance_id"] == "inst-a"
    assert lines[0]["status"] == "resolved"
    assert lines[1]["instance_id"] == "inst-b"
    assert lines[1]["status"] == "skipped"
    assert lines[1]["reason"] == "missing_prediction"


def test_parse_harness_results_supports_swebench_4x_id_lists() -> None:
    dataset_rows = [
        _dataset_row(instance_id="inst-a", model="solver-x"),
        _dataset_row(instance_id="inst-b", model="solver-x"),
        _dataset_row(instance_id="inst-c", model="solver-x"),
        _dataset_row(instance_id="inst-d", model="solver-x"),
    ]
    harness_result = {
        "resolved_ids": ["inst-a"],
        "unresolved_ids": ["inst-b"],
        "error_ids": ["inst-c"],
        "incomplete_ids": ["inst-d"],
    }

    rows, metadata = _parse_harness_results(harness_result, dataset_rows)

    assert metadata == harness_result
    assert [row["instance_id"] for row in rows] == [
        "inst-a",
        "inst-b",
        "inst-c",
        "inst-d",
    ]
    assert rows[0]["status"] == "resolved"
    assert rows[0]["resolved"] is True
    assert rows[1]["status"] == "not_resolved"
    assert rows[1]["resolved"] is False
    assert rows[2]["status"] == "error"
    assert rows[2]["reason"] == "harness error"
    assert rows[3]["status"] == "error"
    assert rows[3]["reason"] == "incomplete"
