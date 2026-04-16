"""Unit tests for matrix parsing and job planning."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from repogauge.runner.matrix import MatrixConfigurationError, load_matrix_config
from repogauge.runner.planner import plan_jobs


def _write_dataset(path: Path, instance_ids: list[str]) -> None:
    rows = []
    for iid in instance_ids:
        rows.append(
            {
                "instance_id": iid,
                "repo": "owner/repo",
                "base_commit": "abc123",
                "problem_statement": "example",
                "version": "v1",
                "patch": f"diff --git a/{iid} b/{iid}",
                "test_patch": "",
                "FAIL_TO_PASS": [],
                "PASS_TO_PASS": [],
            }
        )
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _write_matrix(path: Path, include: bool = False, exclude: bool = False) -> None:
    dataset_section = """dataset:
  path: dataset/dataset.jsonl
"""
    if include:
        dataset_section += "  include_instance_ids:\n    - i-1\n    - i-3\n"
    if exclude:
        dataset_section += "  exclude_instance_ids:\n    - i-3\n"

    path.write_text(
        f"""{dataset_section}
providers:
  mock:
    kind: local
execution:
  repeats: 2
  seed: 11
  shuffle: false
solvers:
  - id: solver-a
    provider: mock
    prompt_policy:
      template: concise
    tool_policy:
      safe: true
  - id: solver-b
    provider: mock
    prompt_policy:
      template: verbose
    tool_policy:
      safe: false
""",
        encoding="utf-8",
    )


def test_load_matrix_normalizes_relative_dataset_path() -> None:
    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)
        dataset_root = root / "dataset"
        dataset_root.mkdir()
        dataset_path = dataset_root / "dataset.jsonl"
        _write_dataset(dataset_path, ["i-1", "i-2"])

        matrix_path = root / "matrix.yaml"
        _write_matrix(matrix_path)

        matrix = load_matrix_config(matrix_path)
        assert matrix.dataset.path == str(dataset_path.resolve())
        assert matrix.run_id == matrix_path.stem
        assert matrix.execution.seeds == (11, 12)
        assert matrix.execution.repeats == 2
        assert len(matrix.solvers) == 2
        assert matrix.solvers[0].solver_id == "solver-a"
        assert matrix.providers[0].provider_id == "mock"


def test_run_matrix_filters_and_expands_jobs() -> None:
    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)
        dataset_root = root / "dataset"
        dataset_root.mkdir()
        dataset_path = dataset_root / "dataset.jsonl"
        _write_dataset(dataset_path, ["i-1", "i-2", "i-3"])

        matrix_path = root / "matrix.yaml"
        _write_matrix(matrix_path, include=True)
        matrix = load_matrix_config(matrix_path)
        jobs = plan_jobs(matrix)

        assert len(jobs) == 8
        assert [job.instance_id for job in jobs][:4] == ["i-1", "i-1", "i-1", "i-1"]
        assert [job.instance_id for job in jobs][4:8] == [
            "i-3",
            "i-3",
            "i-3",
            "i-3",
        ]
        assert len({job.seed for job in jobs}) == 2
        assert jobs[0].prompt_policy_hash and len(jobs[0].prompt_policy_hash) == 64
        assert jobs[0].tool_policy_hash and len(jobs[0].tool_policy_hash) == 64


def test_job_planning_is_deterministic_without_shuffle() -> None:
    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)
        dataset_path = root / "dataset.jsonl"
        _write_dataset(dataset_path, ["x", "y"])

        matrix_text = """
run_id: r0
dataset:
  path: dataset.jsonl
providers:
  mock:
    kind: local
execution:
  repeats: 1
  seeds:
    - 9
    - 5
  shuffle: false
solvers:
  - id: s1
    provider: mock
    prompt_policy:
      template: concise
    tool_policy:
      safe: true
"""
        (root / "matrix.yaml").write_text(matrix_text, encoding="utf-8")

        m1 = load_matrix_config(root / "matrix.yaml", dataset_path=dataset_path)
        m2 = load_matrix_config(root / "matrix.yaml", dataset_path=dataset_path)
        assert [job.to_dict() for job in plan_jobs(m1)] == [
            job.to_dict() for job in plan_jobs(m2)
        ]


def test_unknown_provider_reference_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)
        dataset_path = root / "dataset.jsonl"
        _write_dataset(dataset_path, ["i"])
        matrix_text = """
dataset:
  path: dataset.jsonl
providers:
  known:
    kind: local
solvers:
  - id: solver-a
    provider: missing
"""
        (root / "matrix.yaml").write_text(matrix_text, encoding="utf-8")
        try:
            load_matrix_config(root / "matrix.yaml", dataset_path=dataset_path)
            raise AssertionError("expected MatrixConfigurationError")
        except MatrixConfigurationError as exc:
            assert "unknown provider" in str(exc)


def test_load_matrix_resolves_provider_secrets_from_environment() -> None:
    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)
        dataset_path = root / "dataset.jsonl"
        _write_dataset(dataset_path, ["i"])

        matrix_text = """
run_id: matrix-secrets
dataset:
  path: dataset.jsonl
providers:
  openai:
    kind: openai_responses
    api_key: env:REPOGAUGE_TEST_OPENAI_KEY
solvers:
  - id: solver-a
    provider: openai
    model: gpt-fake
""".strip()

        matrix_path = root / "matrix.yaml"
        matrix_path.write_text(matrix_text + "\n", encoding="utf-8")

        os.environ["REPOGAUGE_TEST_OPENAI_KEY"] = "secret-value"
        try:
            matrix = load_matrix_config(matrix_path)
            provider = matrix.providers[0]
            assert provider.config["api_key"] == "secret-value"
            assert provider.redacted_config["api_key"] == "<redacted>"
        finally:
            os.environ.pop("REPOGAUGE_TEST_OPENAI_KEY", None)


def test_load_matrix_resolves_provider_secrets_from_file() -> None:
    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)
        dataset_path = root / "dataset.jsonl"
        _write_dataset(dataset_path, ["i"])

        secret_path = root / "token.txt"
        secret_path.write_text("file-secret", encoding="utf-8")

        matrix_text = """
dataset:
  path: dataset.jsonl
providers:
  local:
    kind: local
    api_key_file: token.txt
solvers:
  - id: solver-a
    provider: local
    model: fake
""".strip()

        matrix_path = root / "matrix.yaml"
        matrix_path.write_text(matrix_text + "\n", encoding="utf-8")

        matrix = load_matrix_config(matrix_path)
        provider = matrix.providers[0]
        assert provider.config["api_key"] == "file-secret"
        assert provider.redacted_config["api_key"] == "<redacted>"


def test_missing_provider_secret_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)
        dataset_path = root / "dataset.jsonl"
        _write_dataset(dataset_path, ["i"])

        matrix_text = """
dataset:
  path: dataset.jsonl
providers:
  openai:
    kind: openai_responses
    api_key: env:MISSING_REPOGAUGE_TEST_OPENAI_KEY
solvers:
  - id: solver-a
    provider: openai
    model: fake
""".strip()

        matrix_path = root / "matrix.yaml"
        matrix_path.write_text(matrix_text + "\n", encoding="utf-8")

        os.environ.pop("MISSING_REPOGAUGE_TEST_OPENAI_KEY", None)
        try:
            load_matrix_config(matrix_path)
            raise AssertionError("expected MatrixConfigurationError")
        except MatrixConfigurationError as exc:
            assert "missing required environment variable" in str(exc)


def test_solver_adapter_must_be_compatible_with_provider_kind() -> None:
    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)
        dataset_path = root / "dataset.jsonl"
        _write_dataset(dataset_path, ["i"])

        matrix_text = """
dataset:
  path: dataset.jsonl
providers:
  openai:
    kind: openai_responses
solvers:
  - id: solver-a
    provider: openai
    adapter: codex_cli
""".strip()

        matrix_path = root / "matrix.yaml"
        matrix_path.write_text(matrix_text + "\n", encoding="utf-8")

        try:
            load_matrix_config(matrix_path)
            raise AssertionError("expected MatrixConfigurationError")
        except MatrixConfigurationError as exc:
            assert "incompatible" in str(exc)
