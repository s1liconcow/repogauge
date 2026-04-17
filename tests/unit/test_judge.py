from __future__ import annotations

import json
import os
from pathlib import Path
import pytest
import sys
import types
from unittest.mock import patch

from repogauge.runner.judge import (
    HarnessRunSummary,
    HarnessEvaluationError,
    JudgeBatchResult,
    JudgeSchedulerConfig,
    _augment_instance_rows_with_harness_logs,
    _ensure_container_runtime,
    _harness_run_id,
    _invoke_swebench_harness,
    _register_adapter_maps,
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
            container_runtime="docker",
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
            container_runtime="docker",
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


def test_run_harness_evaluation_writes_resolved_only_dataset_slice(
    tmp_path: Path,
) -> None:
    dataset_rows = [
        _dataset_row(instance_id="inst-a", model="solver-x"),
        _dataset_row(instance_id="inst-b", model="solver-x"),
        _dataset_row(instance_id="inst-c", model="solver-x"),
    ]
    predictions_rows = [
        {
            "instance_id": "inst-a",
            "model_name_or_path": "solver-x",
            "model_patch": "diff-a",
        },
        {
            "instance_id": "inst-b",
            "model_name_or_path": "solver-x",
            "model_patch": "diff-b",
        },
        {
            "instance_id": "inst-c",
            "model_name_or_path": "solver-x",
            "model_patch": "diff-c",
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
        instance_rows = []
        for row, _ in rows:
            status = (
                "resolved"
                if row["instance_id"] in {"inst-a", "inst-c"}
                else "not_resolved"
            )
            instance_rows.append(
                _result_row_from_instance(dataset_row=row, status=status)
            )
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
            container_runtime="docker",
            judge_config=JudgeSchedulerConfig(batch_size=32),
        )

    assert mock_run_batch.call_count == 1
    assert summary.dataset_path == str(tmp_path / "dataset.resolved.jsonl")
    assert summary.predictions_path == str(tmp_path / "predictions.resolved.jsonl")

    resolved_dataset = [
        json.loads(line)
        for line in (tmp_path / "dataset.resolved.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [row["instance_id"] for row in resolved_dataset] == ["inst-a", "inst-c"]

    resolved_predictions = [
        json.loads(line)
        for line in (tmp_path / "predictions.resolved.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [row["instance_id"] for row in resolved_predictions] == [
        "inst-a",
        "inst-c",
    ]

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


def test_invoke_swebench_harness_uses_local_instance_images(tmp_path: Path) -> None:
    dataset_rows = [_dataset_row(instance_id="inst-a", model="solver-x")]
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

    out_root = tmp_path / "eval"
    out_root.mkdir()
    report_path = out_root / "report.json"
    report_path.write_text(json.dumps({"resolved_ids": ["inst-a"]}), encoding="utf-8")
    podman_socket = tmp_path / "podman.sock"
    podman_socket.write_text("", encoding="utf-8")

    fake_client = object()
    docker_module = types.ModuleType("docker")
    docker_env: dict[str, str | None] = {}

    def fake_from_env():
        docker_env["DOCKER_HOST"] = os.environ.get("DOCKER_HOST")
        return fake_client

    docker_module.from_env = fake_from_env

    run_module = types.ModuleType("swebench.harness.run_evaluation")

    calls: dict[str, dict] = {}

    def fake_build_env_images(client, instances, **kwargs):
        calls["build_env_images"] = {
            "client": client,
            "instances": instances,
            **kwargs,
        }

    def fake_run_instances(**kwargs):
        calls["run_instances"] = kwargs

    def fake_make_run_report(predictions, instances, run_id, **kwargs):
        calls["make_run_report"] = {
            "predictions": predictions,
            "instances": instances,
            "run_id": run_id,
            **kwargs,
        }
        return report_path

    run_module.build_env_images = fake_build_env_images
    run_module.run_instances = fake_run_instances
    run_module.make_run_report = fake_make_run_report

    swebench_pkg = types.ModuleType("swebench")
    harness_pkg = types.ModuleType("swebench.harness")
    swebench_pkg.harness = harness_pkg
    harness_pkg.run_evaluation = run_module

    with patch.dict(
        sys.modules,
        {
            "docker": docker_module,
            "swebench": swebench_pkg,
            "swebench.harness": harness_pkg,
            "swebench.harness.run_evaluation": run_module,
        },
    ):
        result = _invoke_swebench_harness(
            dataset_path=dataset_path,
            predictions_path=predictions_path,
            out_root=out_root,
            workers=2,
            timeout_seconds=120,
        )

    assert result == {"resolved_ids": ["inst-a"]}
    assert docker_env["DOCKER_HOST"] == "unix:///tmp/podman.sock"
    assert calls["build_env_images"]["client"] is fake_client
    assert calls["build_env_images"]["namespace"] is None
    assert calls["run_instances"]["namespace"] is None
    assert calls["make_run_report"]["namespace"] is None


def test_invoke_swebench_harness_uses_local_instance_images_with_docker_runtime(
    tmp_path: Path,
) -> None:
    dataset_rows = [_dataset_row(instance_id="inst-a", model="solver-x")]
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

    out_root = tmp_path / "eval"
    out_root.mkdir()
    report_path = out_root / "report.json"
    report_path.write_text(json.dumps({"resolved_ids": ["inst-a"]}), encoding="utf-8")

    fake_client = object()
    docker_module = types.ModuleType("docker")
    docker_env: dict[str, str | None] = {}

    def fake_from_env():
        docker_env["DOCKER_HOST"] = os.environ.get("DOCKER_HOST")
        return fake_client

    docker_module.from_env = fake_from_env

    run_module = types.ModuleType("swebench.harness.run_evaluation")

    calls: dict[str, dict] = {}

    def fake_build_env_images(client, instances, **kwargs):
        calls["build_env_images"] = {
            "client": client,
            "instances": instances,
            **kwargs,
        }

    def fake_run_instances(**kwargs):
        calls["run_instances"] = kwargs

    def fake_make_run_report(predictions, instances, run_id, **kwargs):
        calls["make_run_report"] = {
            "predictions": predictions,
            "instances": instances,
            "run_id": run_id,
            **kwargs,
        }
        return report_path

    run_module.build_env_images = fake_build_env_images
    run_module.run_instances = fake_run_instances
    run_module.make_run_report = fake_make_run_report

    swebench_pkg = types.ModuleType("swebench")
    harness_pkg = types.ModuleType("swebench.harness")
    swebench_pkg.harness = harness_pkg
    harness_pkg.run_evaluation = run_module

    with patch.dict(
        sys.modules,
        {
            "docker": docker_module,
            "swebench": swebench_pkg,
            "swebench.harness": harness_pkg,
            "swebench.harness.run_evaluation": run_module,
        },
    ):
        with patch.dict(os.environ, {"DOCKER_HOST": "tcp://remote-docker:2375"}):
            result = _invoke_swebench_harness(
                dataset_path=dataset_path,
                predictions_path=predictions_path,
                out_root=out_root,
                workers=2,
                timeout_seconds=120,
                container_runtime="docker",
            )

    assert result == {"resolved_ids": ["inst-a"]}
    assert docker_env["DOCKER_HOST"] == "tcp://remote-docker:2375"
    assert calls["build_env_images"]["namespace"] is None
    assert calls["run_instances"]["namespace"] is None
    assert calls["make_run_report"]["namespace"] is None


def test_invoke_swebench_harness_uses_podman_socket_override(tmp_path: Path) -> None:
    dataset_rows = [_dataset_row(instance_id="inst-a", model="solver-x")]
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

    out_root = tmp_path / "eval"
    out_root.mkdir()
    report_path = out_root / "report.json"
    report_path.write_text(json.dumps({"resolved_ids": ["inst-a"]}), encoding="utf-8")
    podman_socket = tmp_path / "podman.sock"
    podman_socket.write_text("", encoding="utf-8")

    fake_client = object()
    docker_module = types.ModuleType("docker")
    docker_env: dict[str, str | None] = {}

    def fake_from_env():
        docker_env["DOCKER_HOST"] = os.environ.get("DOCKER_HOST")
        return fake_client

    docker_module.from_env = fake_from_env

    run_module = types.ModuleType("swebench.harness.run_evaluation")
    run_module.build_env_images = lambda *args, **kwargs: None
    run_module.run_instances = lambda **kwargs: None
    run_module.make_run_report = lambda *args, **kwargs: report_path

    swebench_pkg = types.ModuleType("swebench")
    harness_pkg = types.ModuleType("swebench.harness")
    swebench_pkg.harness = harness_pkg
    harness_pkg.run_evaluation = run_module

    with patch.dict(
        sys.modules,
        {
            "docker": docker_module,
            "swebench": swebench_pkg,
            "swebench.harness": harness_pkg,
            "swebench.harness.run_evaluation": run_module,
        },
    ):
        with patch.dict(os.environ, {}, clear=False):
            result = _invoke_swebench_harness(
                dataset_path=dataset_path,
                predictions_path=predictions_path,
                out_root=out_root,
                workers=2,
                timeout_seconds=120,
                container_runtime="podman",
                container_host=f"unix://{podman_socket}",
            )

    assert result == {"resolved_ids": ["inst-a"]}
    assert docker_env["DOCKER_HOST"] == f"unix://{podman_socket}"
    assert os.environ.get("DOCKER_HOST") is None


def test_ensure_container_runtime_starts_podman_service_when_needed(
    tmp_path: Path,
) -> None:
    podman_socket = tmp_path / "podman.sock"
    service_calls: list[list[str]] = []

    class _FakeProcess:
        def __init__(self) -> None:
            self.stderr = types.SimpleNamespace(read=lambda: "")
            self._terminated = False

        def poll(self) -> int | None:
            return None

        def terminate(self) -> None:
            self._terminated = True

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            self._terminated = True

    process = _FakeProcess()

    def fake_popen(args, **kwargs):
        service_calls.append(list(args))
        return process

    with (
        patch(
            "repogauge.runner.judge._is_unix_socket_reachable",
            side_effect=[False, True],
        ),
        patch("repogauge.runner.judge.subprocess.Popen", side_effect=fake_popen),
    ):
        with _ensure_container_runtime(
            container_runtime="podman",
            container_host=f"unix://{podman_socket}",
        ) as resolved_host:
            assert resolved_host == f"unix://{podman_socket}"

    assert service_calls == [
        ["podman", "system", "service", "--time", "0", f"unix://{podman_socket}"]
    ]
    assert process._terminated is True


def test_ensure_container_runtime_reuses_existing_podman_service(
    tmp_path: Path,
) -> None:
    podman_socket = tmp_path / "podman.sock"

    with (
        patch("repogauge.runner.judge._is_unix_socket_reachable", return_value=True),
        patch("repogauge.runner.judge.subprocess.Popen") as mock_popen,
    ):
        with _ensure_container_runtime(
            container_runtime="podman",
            container_host=f"unix://{podman_socket}",
        ) as resolved_host:
            assert resolved_host == f"unix://{podman_socket}"

    mock_popen.assert_not_called()


def test_ensure_container_runtime_uses_tmp_socket_for_default_podman() -> None:
    with patch("repogauge.runner.judge._is_unix_socket_reachable", return_value=True):
        with _ensure_container_runtime(
            container_runtime="podman",
            container_host=None,
        ) as resolved_host:
            assert resolved_host == "unix:///tmp/podman.sock"


def test_ensure_container_runtime_errors_when_podman_service_fails(
    tmp_path: Path,
) -> None:
    podman_socket = tmp_path / "podman.sock"

    class _FakeProcess:
        def __init__(self) -> None:
            self.stderr = types.SimpleNamespace(read=lambda: "boom")

        def poll(self) -> int | None:
            return 1

        def terminate(self) -> None:
            raise AssertionError("terminate should not be called for exited process")

        def wait(self, timeout: float | None = None) -> int:
            return 1

        def kill(self) -> None:
            raise AssertionError("kill should not be called for exited process")

    with (
        patch("repogauge.runner.judge._is_unix_socket_reachable", return_value=False),
        patch("repogauge.runner.judge.subprocess.Popen", return_value=_FakeProcess()),
        pytest.raises(HarnessEvaluationError) as exc_info,
    ):
        with _ensure_container_runtime(
            container_runtime="podman",
            container_host=f"unix://{podman_socket}",
        ):
            pass

    assert "failed to start podman system service" in str(exc_info.value)


def test_register_adapter_maps_patches_grading_source_modules() -> None:
    constants_module = types.ModuleType("swebench.harness.constants")
    constants_module.MAP_REPO_TO_EXT = {}
    constants_module.MAP_REPO_VERSION_TO_SPECS = {}

    log_parsers_module = types.ModuleType("swebench.harness.log_parsers")
    log_parsers_module.MAP_REPO_TO_PARSER = {}

    test_spec_module = types.ModuleType("swebench.harness.test_spec.test_spec")
    test_spec_module.MAP_REPO_TO_EXT = constants_module.MAP_REPO_TO_EXT
    test_spec_module.MAP_REPO_VERSION_TO_SPECS = (
        constants_module.MAP_REPO_VERSION_TO_SPECS
    )
    test_spec_module.MAP_REPO_TO_PARSER = log_parsers_module.MAP_REPO_TO_PARSER

    test_spec_package = types.ModuleType("swebench.harness.test_spec")
    test_spec_package.MAP_REPO_TO_EXT = constants_module.MAP_REPO_TO_EXT
    test_spec_package.MAP_REPO_VERSION_TO_SPECS = (
        constants_module.MAP_REPO_VERSION_TO_SPECS
    )
    test_spec_package.MAP_REPO_TO_PARSER = log_parsers_module.MAP_REPO_TO_PARSER

    adapter_context = {
        "maps": {
            "repo_to_ext": {"owner/repo": "py"},
            "repo_version_to_specs": {
                "owner/repo": {"v1": {"test_cmd": "python -m pytest"}}
            },
            "repo_to_parser": {"owner/repo": object()},
        }
    }

    with patch.dict(
        sys.modules,
        {
            "swebench.harness.constants": constants_module,
            "swebench.harness.log_parsers": log_parsers_module,
            "swebench.harness.test_spec": test_spec_package,
            "swebench.harness.test_spec.test_spec": test_spec_module,
        },
    ):
        patched = _register_adapter_maps(adapter_context)

    assert patched["MAP_REPO_TO_EXT"] == {"owner/repo": "py"}
    assert constants_module.MAP_REPO_TO_EXT["owner/repo"] == "py"
    assert (
        constants_module.MAP_REPO_VERSION_TO_SPECS["owner/repo"]["v1"]["test_cmd"]
        == "python -m pytest"
    )
    assert "owner/repo" in log_parsers_module.MAP_REPO_TO_PARSER


def test_augment_instance_rows_with_harness_logs_includes_paths_and_summary(
    tmp_path: Path,
) -> None:
    out_root = tmp_path / "batch_0000_gold_repo_0.0.0"
    run_id = _harness_run_id(out_root)
    log_dir = out_root / "logs" / "run_evaluation" / run_id / "gold" / "repo__sample-1"
    log_dir.mkdir(parents=True, exist_ok=True)
    run_instance_log = log_dir / "run_instance.log"
    run_instance_log.write_text(
        "\n".join(
            [
                "2026-04-16 12:41:39,351 - INFO - >>>>> Patch Apply Failed:",
                "patching file repogauge/validation/validate.py",
                "Reversed (or previously applied) patch detected!  Assuming -R.",
            ]
        ),
        encoding="utf-8",
    )
    (log_dir / "patch.diff").write_text("diff", encoding="utf-8")

    instance_rows = [
        {
            "instance_id": "repo__sample-1",
            "status": "error",
            "reason": "harness error",
            "error": "harness error",
            "metadata": {},
        }
    ]
    dataset_rows = [{"instance_id": "repo__sample-1"}]
    prediction_rows = [{"instance_id": "repo__sample-1", "model_name_or_path": "gold"}]

    _augment_instance_rows_with_harness_logs(
        instance_rows=instance_rows,
        dataset_rows=dataset_rows,
        prediction_rows=prediction_rows,
        out_root=out_root,
        run_id=run_id,
    )

    row = instance_rows[0]
    assert "Patch Apply Failed" in row["reason"]
    assert str(run_instance_log) in row["error"]
    assert row["metadata"]["run_instance_log_path"] == str(run_instance_log)
    assert row["metadata"]["patch_path"] == str(log_dir / "patch.diff")
