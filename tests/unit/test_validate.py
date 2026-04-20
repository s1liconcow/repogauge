"""Validation module regression tests."""

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from repogauge.exec import CommandResult
from repogauge.validation.validate import (
    PytestExecutionError,
    TestExecutionError,
    run_eval,
    _eval_instance,
    _pytest_command_attempts,
    _run_pytest,
    _run_test,
    _test_command_attempts,
)


def test_pytest_command_attempts_falls_back_to_interpreter_invocation() -> None:
    attempts = _pytest_command_attempts("pytest --tb=no tests/unit")
    assert attempts[0] == ["pytest", "--tb=no", "tests/unit"]
    assert attempts[1][0].endswith("python") or attempts[1][0].endswith("python3")


def test_pytest_command_attempts_falls_back_for_path_based_pytest() -> None:
    attempts = _pytest_command_attempts(".venv/bin/pytest --tb=no tests/unit")
    assert attempts[0] == [".venv/bin/pytest", "--tb=no", "tests/unit"]
    assert attempts[1][0].endswith("python") or attempts[1][0].endswith("python3")
    assert attempts[1][1:] == ["-m", "pytest", "--tb=no", "tests/unit"]


def test_run_pytest_falls_back_when_junit_xml_missing(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    junit_xml = tmp_path / "junit.xml"

    calls = {"count": 0}

    def fake_run_command(cmd, *, cwd=None, env=None, timeout_seconds=None):  # noqa: ARG001
        calls["count"] += 1
        if calls["count"] == 2:
            xml_flag = next(arg for arg in cmd if arg.startswith("--junit-xml="))
            Path(xml_flag.removeprefix("--junit-xml=")).write_text(
                "<testsuite><testcase classname='tests.test_mod' name='ok' /></testsuite>"
            )
            return CommandResult(
                command=cmd,
                returncode=0,
                stdout="",
                stderr="",
            )
        return CommandResult(
            command=cmd,
            returncode=127,
            stdout="",
            stderr="pytest: not found",
        )

    with patch(
        "repogauge.validation.validate.run_command", side_effect=fake_run_command
    ):
        outcomes, _, attempts = _run_pytest(
            repo_root,
            test_files=[],
            junit_xml=junit_xml,
            timeout_seconds=10,
            test_cmd_base="pytest",
        )

    assert calls["count"] == 2
    assert len(outcomes) == 1
    assert outcomes["tests/test_mod.py::ok"] == "pass"
    assert len(attempts) == 2
    assert attempts[0]["status"] == "parse_error"
    assert attempts[1]["status"] == "success"


def test_test_command_attempts_defers_to_adapter() -> None:
    class Adapter:
        def test_command_attempts(self, test_cmd_base: str) -> list[list[str]]:
            return [["go", "test", "./..."], ["go", "test", "./...", "-run", "Foo"]]

    attempts = _test_command_attempts("ignored", adapter=Adapter())

    assert attempts == [["go", "test", "./..."], ["go", "test", "./...", "-run", "Foo"]]


def test_run_test_uses_adapter_env_and_parser(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed: dict[str, object] = {}

    class Adapter:
        def name(self) -> str:
            return "go"

        def env_overrides(self, worktree: Path) -> dict[str, str]:
            observed["worktree"] = worktree
            return {"WORKTREE": str(worktree), "ADAPTER_FLAG": "1"}

        def test_command_attempts(self, test_cmd_base: str) -> list[list[str]]:
            observed["test_cmd_base"] = test_cmd_base
            return [["go", "test", "./..."]]

        def parse_test_output(
            self, report: object, test_spec: object | None
        ) -> dict[str, str]:
            observed["report"] = report
            observed["test_spec"] = test_spec
            assert isinstance(report, Path)
            assert report.name == "go-report.json"
            return {"pkg.Test": "pass"}

        def test_report_filename(self) -> str | None:
            return "go-report.json"

    def fake_run_command(cmd, *, cwd=None, env=None, timeout_seconds=None):  # noqa: ARG001
        observed["cmd"] = cmd
        observed["cwd"] = cwd
        observed["env"] = env
        return CommandResult(command=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        "repogauge.validation.validate.run_command", fake_run_command
    )

    outcomes, raw, attempts = _run_test(
        tmp_path,
        test_files=[],
        test_report_path=tmp_path / "go-report.json",
        timeout_seconds=5,
        test_cmd_base="go test ./...",
        adapter=Adapter(),
        test_spec={"suite": "go"},
    )

    assert outcomes == {"pkg.Test": "pass"}
    assert raw == "[stdout]\n\n[stderr]\n"
    assert len(attempts) == 1
    assert attempts[0]["status"] == "success"
    assert observed["cmd"] == ["go", "test", "./..."]
    assert observed["cwd"] == str(tmp_path)
    assert observed["env"]["WORKTREE"] == str(tmp_path)
    assert observed["env"]["ADAPTER_FLAG"] == "1"
    assert observed["test_spec"] == {"suite": "go"}


def test_run_test_uses_container_runtime_when_requested(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed: dict[str, object] = {}
    monkeypatch.setenv("HOME", "/host/home")
    monkeypatch.setenv("GOPATH", "/host/go")
    monkeypatch.setenv("GOMODCACHE", "/host/go/pkg/mod")

    class Adapter:
        def name(self) -> str:
            return "go"

        def env_overrides(self, worktree: Path) -> dict[str, str]:
            return {"WORKTREE": str(worktree), "GOCACHE": str(worktree / ".gocache")}

        def test_command_attempts(self, test_cmd_base: str) -> list[list[str]]:
            observed["test_cmd_base"] = test_cmd_base
            return [["go", "test", "./..."]]

        def parse_test_output(
            self, report: object, test_spec: object | None
        ) -> dict[str, str]:
            observed["report"] = report
            observed["test_spec"] = test_spec
            return {"pkg.Test": "pass"}

        def test_report_filename(self) -> str | None:
            return "go-report.json"

    def fake_container_exec(**kwargs):
        observed["kwargs"] = kwargs
        return CommandResult(
            command=kwargs["command"],
            returncode=0,
            stdout="ok",
            stderr="",
        )

    monkeypatch.setattr(
        "repogauge.validation.validate.run_workspace_command_in_container",
        fake_container_exec,
    )

    outcomes, raw, attempts = _run_test(
        tmp_path,
        test_files=[],
        test_report_path=tmp_path / "go-report.json",
        timeout_seconds=5,
        test_cmd_base="go test ./...",
        adapter=Adapter(),
        test_spec={"suite": "go"},
        attempt_id_prefix="eval-inst-b",
        container_host="unix:///tmp/podman.sock",
        adapter_spec={
            "repo": "owner/repo",
            "version": "1.0",
            "language": "go",
            "docker_specs": {"go_version": "1.22.12"},
            "install": ["go mod download"],
        },
        instance_row={"instance_id": "inst-1", "repo": "owner/repo", "version": "1.0"},
    )

    assert outcomes == {"pkg.Test": "pass"}
    assert raw == "[stdout]\nok\n[stderr]\n"
    assert len(attempts) == 1
    kwargs = observed["kwargs"]
    assert kwargs["attempt_id"] == "eval-inst-b-attempt-1"
    assert kwargs["container_host"] == "unix:///tmp/podman.sock"
    assert kwargs["artifacts_root"] == tmp_path
    assert kwargs["command"] == ["go", "test", "./..."]
    assert kwargs["environment"]["WORKTREE"] == str(tmp_path)
    assert kwargs["environment"]["GOCACHE"] == str(tmp_path / ".gocache")
    assert "HOME" not in kwargs["environment"]
    assert "GOPATH" not in kwargs["environment"]
    assert "GOMODCACHE" not in kwargs["environment"]
    assert kwargs["adapter_spec"]["language"] == "go"
    assert observed["test_spec"] == {"suite": "go"}


def test_run_test_python_container_uses_container_visible_report_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report_path = tmp_path / "outside-junit.xml"
    observed: dict[str, object] = {}

    def fake_container_exec(**kwargs):
        observed["kwargs"] = kwargs
        junit_arg = next(
            part for part in kwargs["command"] if part.startswith("--junit-xml=")
        )
        assert junit_arg == "--junit-xml=/testbed/outside-junit.xml"
        (tmp_path / "outside-junit.xml").write_text(
            "<testsuite><testcase classname='tests.test_mod' name='ok' /></testsuite>",
            encoding="utf-8",
        )
        return CommandResult(
            command=kwargs["command"],
            returncode=0,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(
        "repogauge.validation.validate.run_workspace_command_in_container",
        fake_container_exec,
    )

    outcomes, _, attempts = _run_test(
        tmp_path,
        test_files=[],
        test_report_path=report_path,
        timeout_seconds=5,
        test_cmd_base="pytest",
        adapter=None,
        attempt_id_prefix="eval-inst-a",
        container_host="unix:///tmp/podman.sock",
        adapter_spec={
            "repo": "owner/repo",
            "version": "1.0",
            "language": "python",
            "docker_specs": {"python_version": "3.11"},
            "install": [],
        },
        instance_row={"instance_id": "inst-1", "repo": "owner/repo", "version": "1.0"},
    )

    assert outcomes == {"tests/test_mod.py::ok": "pass"}
    assert len(attempts) == 1
    assert observed["kwargs"]["command"][:2] == ["pytest", "--junit-xml=/testbed/outside-junit.xml"]


def test_pytest_execution_error_alias_is_preserved() -> None:
    assert PytestExecutionError is TestExecutionError


def test_eval_instance_executes_four_passes_in_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []

    def fake_build_targeted_test_plan(
        test_cmd: str, test_patch: str
    ) -> tuple[str, list[str]]:
        assert test_cmd == "pytest --config"
        assert test_patch
        return ("python -m pytest", ["tests/test_eval.py"])

    def fake_pass(label: str, outcomes: dict[str, str], log: str) -> dict[str, object]:
        calls.append(label)
        return {
            "status": "passed",
            "error": None,
            "outcomes": outcomes,
            "log": log,
            "attempts": [{"attempt": 1, "command": ["pytest"]}],
        }

    def fake_run_validation_pass(*, label: str, **kwargs: object) -> dict[str, object]:
        # Labels are expected in deterministic sequence: a -> b -> c -> b_rerun -> c_rerun.
        if label == "a":
            return fake_pass("a", {"tests/test_eval.py::ok": "pass"}, "run-a")
        if label == "b":
            return fake_pass("b", {"tests/test_eval.py::fail": "fail"}, "run-b")
        if label == "c":
            return fake_pass("c", {"tests/test_eval.py::fail": "pass"}, "run-c")
        if label == "b_rerun":
            return fake_pass(
                "b_rerun", {"tests/test_eval.py::fail": "fail"}, "run-b-rerun"
            )
        if label == "c_rerun":
            return fake_pass(
                "c_rerun", {"tests/test_eval.py::fail": "pass"}, "run-c-rerun"
            )
        raise AssertionError(f"unexpected label {label}")

    monkeypatch.setattr(
        "repogauge.validation.validate.build_targeted_test_plan",
        fake_build_targeted_test_plan,
    )
    monkeypatch.setattr(
        "repogauge.validation.validate._run_validation_pass", fake_run_validation_pass
    )

    outcome = _eval_instance(
        repo_root=tmp_path,
        base_commit="deadbeef",
        pred_patch="diff --git a.py b.py\n+ok",
        test_patch="diff --git a_test.py b_test.py\n+ok",
        declared_ftp=[],
        declared_ptp=[],
        timeout_seconds=120,
        test_cmd_base="pytest --config",
    )

    assert outcome["status"] == "resolved"
    assert calls == ["a", "b", "c", "b_rerun", "c_rerun"]
    assert outcome["flake_runs"] == 2


def test_eval_instance_marks_flaky_when_reruns_differ(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_build_targeted_test_plan(
        test_cmd: str, test_patch: str
    ) -> tuple[str, list[str]]:
        return ("python -m pytest", ["tests/test_eval.py"])

    def fake_run_validation_pass(*, label: str, **kwargs: object) -> dict[str, object]:
        if label == "a":
            return {
                "status": "passed",
                "error": None,
                "outcomes": {"tests/test_eval.py::ok": "pass"},
                "log": "",
                "attempts": [],
            }
        if label == "b":
            return {
                "status": "passed",
                "error": None,
                "outcomes": {"tests/test_eval.py::ok": "fail"},
                "log": "",
                "attempts": [],
            }
        if label == "c":
            return {
                "status": "passed",
                "error": None,
                "outcomes": {"tests/test_eval.py::ok": "pass"},
                "log": "",
                "attempts": [],
            }
        if label == "b_rerun":
            # Unstable outcome vs run_b.
            return {
                "status": "passed",
                "error": None,
                "outcomes": {"tests/test_eval.py::ok": "pass"},
                "log": "",
                "attempts": [],
            }
        if label == "c_rerun":
            return {
                "status": "passed",
                "error": None,
                "outcomes": {"tests/test_eval.py::ok": "pass"},
                "log": "",
                "attempts": [],
            }
        raise AssertionError(f"unexpected label {label}")

    monkeypatch.setattr(
        "repogauge.validation.validate.build_targeted_test_plan",
        fake_build_targeted_test_plan,
    )
    monkeypatch.setattr(
        "repogauge.validation.validate._run_validation_pass", fake_run_validation_pass
    )

    outcome = _eval_instance(
        repo_root=tmp_path,
        base_commit="deadbeef",
        pred_patch="diff --git a.py b.py\n+ok",
        test_patch="diff --git a_test.py b_test.py\n+ok",
        declared_ftp=[],
        declared_ptp=[],
        timeout_seconds=120,
        test_cmd_base="pytest --config",
    )

    assert outcome["status"] == "flaky"
    assert outcome["flake_runs"] == 2
    assert outcome["resolved"] is False
    assert outcome["run_b_rerun"] == {"tests/test_eval.py::ok": "pass"}


def test_eval_instance_errors_before_run_c_on_pass_b_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_build_targeted_test_plan(
        test_cmd: str, test_patch: str
    ) -> tuple[str, list[str]]:
        return ("python -m pytest", ["tests/test_eval.py"])

    calls: list[str] = []

    def fake_run_validation_pass(*, label: str, **kwargs: object) -> dict[str, object]:
        calls.append(label)
        if label == "a":
            return {
                "status": "passed",
                "error": None,
                "outcomes": {"tests/test_eval.py::ok": "pass"},
                "log": "",
                "attempts": [],
            }
        if label == "b":
            return {
                "status": "failed",
                "error": "b failed",
                "outcomes": {},
                "log": "b fail",
                "attempts": [],
            }
        raise AssertionError(f"unexpected label {label}")

    monkeypatch.setattr(
        "repogauge.validation.validate.build_targeted_test_plan",
        fake_build_targeted_test_plan,
    )
    monkeypatch.setattr(
        "repogauge.validation.validate._run_validation_pass", fake_run_validation_pass
    )

    outcome = _eval_instance(
        repo_root=tmp_path,
        base_commit="deadbeef",
        pred_patch="diff --git a.py b.py\n+ok",
        test_patch="diff --git a_test.py b_test.py\n+ok",
        declared_ftp=[],
        declared_ptp=[],
        timeout_seconds=120,
        test_cmd_base="pytest --config",
    )

    assert outcome["status"] == "error"
    assert outcome["error"] == "b failed"
    assert calls == ["a", "b"]


def test_eval_instance_rejects_without_fail_to_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_build_targeted_test_plan(
        test_cmd: str, test_patch: str
    ) -> tuple[str, list[str]]:
        return ("python -m pytest", ["tests/test_eval.py"])

    def fake_run_validation_pass(*, label: str, **kwargs: object) -> dict[str, object]:
        if label == "a":
            return {
                "status": "passed",
                "error": None,
                "outcomes": {"tests/test_eval.py::ok": "pass"},
                "log": "",
                "attempts": [],
            }
        if label == "b":
            return {
                "status": "passed",
                "error": None,
                "outcomes": {"tests/test_eval.py::ok": "pass"},
                "log": "",
                "attempts": [],
            }
        if label == "c":
            return {
                "status": "passed",
                "error": None,
                "outcomes": {"tests/test_eval.py::ok": "pass"},
                "log": "",
                "attempts": [],
            }
        if label == "b_rerun":
            return {
                "status": "passed",
                "error": None,
                "outcomes": {"tests/test_eval.py::ok": "pass"},
                "log": "",
                "attempts": [],
            }
        if label == "c_rerun":
            return {
                "status": "passed",
                "error": None,
                "outcomes": {"tests/test_eval.py::ok": "pass"},
                "log": "",
                "attempts": [],
            }
        raise AssertionError(f"unexpected label {label}")

    monkeypatch.setattr(
        "repogauge.validation.validate.build_targeted_test_plan",
        fake_build_targeted_test_plan,
    )
    monkeypatch.setattr(
        "repogauge.validation.validate._run_validation_pass", fake_run_validation_pass
    )

    outcome = _eval_instance(
        repo_root=tmp_path,
        base_commit="deadbeef",
        pred_patch="diff --git a.py b.py\n+ok",
        test_patch="diff --git a_test.py b_test.py\n+ok",
        declared_ftp=[],
        declared_ptp=[],
        timeout_seconds=120,
        test_cmd_base="pytest --config",
    )

    assert outcome["status"] == "not_resolved"
    assert outcome["reason"] == "no_fail_to_pass"
    assert outcome["resolved"] is False


def test_eval_instance_rejects_on_pass_to_pass_regression(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_build_targeted_test_plan(
        test_cmd: str, test_patch: str
    ) -> tuple[str, list[str]]:
        return ("python -m pytest", ["tests/test_eval.py"])

    def fake_run_validation_pass(*, label: str, **kwargs: object) -> dict[str, object]:
        if label == "a":
            return {
                "status": "passed",
                "error": None,
                "outcomes": {
                    "tests/test_eval.py::regression": "pass",
                    "tests/test_eval.py::existing": "pass",
                },
                "log": "",
                "attempts": [],
            }
        if label == "b":
            return {
                "status": "passed",
                "error": None,
                "outcomes": {
                    "tests/test_eval.py::regression": "fail",
                    "tests/test_eval.py::existing": "pass",
                },
                "log": "",
                "attempts": [],
            }
        if label == "c":
            return {
                "status": "passed",
                "error": None,
                "outcomes": {
                    "tests/test_eval.py::regression": "pass",
                    "tests/test_eval.py::existing": "fail",
                },
                "log": "",
                "attempts": [],
            }
        if label == "b_rerun":
            return {
                "status": "passed",
                "error": None,
                "outcomes": {
                    "tests/test_eval.py::regression": "fail",
                    "tests/test_eval.py::existing": "pass",
                },
                "log": "",
                "attempts": [],
            }
        if label == "c_rerun":
            return {
                "status": "passed",
                "error": None,
                "outcomes": {
                    "tests/test_eval.py::regression": "pass",
                    "tests/test_eval.py::existing": "fail",
                },
                "log": "",
                "attempts": [],
            }
        raise AssertionError(f"unexpected label {label}")

    monkeypatch.setattr(
        "repogauge.validation.validate.build_targeted_test_plan",
        fake_build_targeted_test_plan,
    )
    monkeypatch.setattr(
        "repogauge.validation.validate._run_validation_pass", fake_run_validation_pass
    )

    outcome = _eval_instance(
        repo_root=tmp_path,
        base_commit="deadbeef",
        pred_patch="diff --git a.py b.py\n+ok",
        test_patch="diff --git a_test.py b_test.py\n+ok",
        declared_ftp=[],
        declared_ptp=["tests/test_eval.py::existing"],
        timeout_seconds=120,
        test_cmd_base="pytest --config",
    )

    assert outcome["status"] == "not_resolved"
    assert outcome["reason"] == "pass_to_pass_regression"
    assert outcome["resolved"] is False


def test_eval_instance_rejects_when_declared_ftp_not_resolved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_build_targeted_test_plan(
        test_cmd: str, test_patch: str
    ) -> tuple[str, list[str]]:
        return ("python -m pytest", ["tests/test_eval.py"])

    def fake_run_validation_pass(*, label: str, **kwargs: object) -> dict[str, object]:
        if label == "a":
            return {
                "status": "passed",
                "error": None,
                "outcomes": {"tests/test_eval.py::regression": "pass"},
                "log": "",
                "attempts": [],
            }
        if label == "b":
            return {
                "status": "passed",
                "error": None,
                "outcomes": {"tests/test_eval.py::regression": "fail"},
                "log": "",
                "attempts": [],
            }
        if label == "c":
            return {
                "status": "passed",
                "error": None,
                "outcomes": {"tests/test_eval.py::regression": "error"},
                "log": "",
                "attempts": [],
            }
        if label == "b_rerun":
            return {
                "status": "passed",
                "error": None,
                "outcomes": {"tests/test_eval.py::regression": "fail"},
                "log": "",
                "attempts": [],
            }
        if label == "c_rerun":
            return {
                "status": "passed",
                "error": None,
                "outcomes": {"tests/test_eval.py::regression": "error"},
                "log": "",
                "attempts": [],
            }
        raise AssertionError(f"unexpected label {label}")

    monkeypatch.setattr(
        "repogauge.validation.validate.build_targeted_test_plan",
        fake_build_targeted_test_plan,
    )
    monkeypatch.setattr(
        "repogauge.validation.validate._run_validation_pass", fake_run_validation_pass
    )

    outcome = _eval_instance(
        repo_root=tmp_path,
        base_commit="deadbeef",
        pred_patch="diff --git a.py b.py\n+ok",
        test_patch="diff --git a_test.py b_test.py\n+ok",
        declared_ftp=["tests/test_eval.py::regression"],
        declared_ptp=[],
        timeout_seconds=120,
        test_cmd_base="pytest --config",
    )

    assert outcome["status"] == "not_resolved"
    assert outcome["reason"] == "declared_ftp_not_resolved"
    assert outcome["resolved"] is False


def test_eval_instance_marks_flaky_reason_for_b_rerun_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_build_targeted_test_plan(
        test_cmd: str, test_patch: str
    ) -> tuple[str, list[str]]:
        return ("python -m pytest", ["tests/test_eval.py"])

    def fake_run_validation_pass(*, label: str, **kwargs: object) -> dict[str, object]:
        if label == "a":
            return {
                "status": "passed",
                "error": None,
                "outcomes": {"tests/test_eval.py::ok": "pass"},
                "log": "",
                "attempts": [],
            }
        if label == "b":
            return {
                "status": "passed",
                "error": None,
                "outcomes": {"tests/test_eval.py::ok": "fail"},
                "log": "",
                "attempts": [],
            }
        if label == "c":
            return {
                "status": "passed",
                "error": None,
                "outcomes": {"tests/test_eval.py::ok": "pass"},
                "log": "",
                "attempts": [],
            }
        if label == "b_rerun":
            return {
                "status": "passed",
                "error": None,
                "outcomes": {"tests/test_eval.py::ok": "pass"},
                "log": "",
                "attempts": [],
            }
        if label == "c_rerun":
            return {
                "status": "passed",
                "error": None,
                "outcomes": {"tests/test_eval.py::ok": "pass"},
                "log": "",
                "attempts": [],
            }
        raise AssertionError(f"unexpected label {label}")

    monkeypatch.setattr(
        "repogauge.validation.validate.build_targeted_test_plan",
        fake_build_targeted_test_plan,
    )
    monkeypatch.setattr(
        "repogauge.validation.validate._run_validation_pass", fake_run_validation_pass
    )

    outcome = _eval_instance(
        repo_root=tmp_path,
        base_commit="deadbeef",
        pred_patch="diff --git a.py b.py\n+ok",
        test_patch="diff --git a_test.py b_test.py\n+ok",
        declared_ftp=[],
        declared_ptp=[],
        timeout_seconds=120,
        test_cmd_base="pytest --config",
    )

    assert outcome["status"] == "flaky"
    assert outcome["reason"] == "flaky_outcomes"


def test_run_eval_includes_missing_prediction_reason(tmp_path: Path) -> None:
    dataset_path = tmp_path / "dataset.jsonl"
    predictions_path = tmp_path / "predictions.jsonl"
    out_root = tmp_path / "out"

    dataset_path.write_text(
        json.dumps(
            {
                "instance_id": "i-1",
                "base_commit": "deadbeef",
                "problem_statement": "broken",
                "FAIL_TO_PASS": [],
                "PASS_TO_PASS": [],
                "test_patch": "",
                "patch": "",
                "version": "v1",
                "repo": "r",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    predictions_path.write_text("", encoding="utf-8")

    summary = run_eval(
        dataset_path=dataset_path,
        predictions_path=predictions_path,
        out_root=out_root,
        repo_root=tmp_path,
    )

    output_rows = [
        json.loads(line)
        for line in (out_root / "validation.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert summary.skipped == 1
    assert len(output_rows) == 1
    assert output_rows[0]["reason"] == "missing_prediction"
    assert output_rows[0]["status"] == "skipped"
    assert (out_root / "instance_results.jsonl").read_text(encoding="utf-8") == (
        out_root / "validation.jsonl"
    ).read_text(encoding="utf-8")
    assert (out_root / "dataset.resolved.jsonl").read_text(encoding="utf-8") == ""
    assert (out_root / "predictions.resolved.jsonl").read_text(encoding="utf-8") == ""


def test_run_eval_includes_reason_from_eval_instance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dataset_path = tmp_path / "dataset.jsonl"
    predictions_path = tmp_path / "predictions.jsonl"
    out_root = tmp_path / "out"

    dataset_path.write_text(
        json.dumps(
            {
                "instance_id": "i-1",
                "base_commit": "deadbeef",
                "problem_statement": "broken",
                "FAIL_TO_PASS": [],
                "PASS_TO_PASS": [],
                "test_patch": "",
                "patch": "",
                "version": "v1",
                "repo": "r",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    predictions_path.write_text(
        json.dumps(
            {
                "instance_id": "i-1",
                "model_name_or_path": "dummy",
                "model_patch": "diff --git a.py b.py\n+ok",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    def fake_eval_instance(**kwargs: object) -> dict[str, object]:
        assert kwargs["repo_root"] == tmp_path
        return {
            "status": "not_resolved",
            "reason": "no_fail_to_pass",
            "error": None,
            "failure_code": None,
            "environment_strategy": "default",
            "test_strategy": "full_pytest",
            "targeted_test_cmd": "python -m pytest",
            "targeted_test_inputs": [],
            "log_a": "",
            "log_b": "",
            "log_c": "",
            "log_b_rerun": "",
            "log_c_rerun": "",
            "run_a": {},
            "run_b": {},
            "run_c": {},
            "run_b_rerun": {},
            "run_c_rerun": {},
            "run_a_attempts": [],
            "run_b_attempts": [],
            "run_c_attempts": [],
            "run_b_rerun_attempts": [],
            "run_c_rerun_attempts": [],
            "flake_runs": 0,
            "FAIL_TO_PASS": [],
            "PASS_TO_PASS": [],
            "resolved": False,
        }

    monkeypatch.setattr(
        "repogauge.validation.validate._eval_instance", fake_eval_instance
    )

    summary = run_eval(
        dataset_path=dataset_path,
        predictions_path=predictions_path,
        out_root=out_root,
        repo_root=tmp_path,
    )

    output_rows = [
        json.loads(line)
        for line in (out_root / "validation.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert output_rows[0]["status"] == "not_resolved"
    assert output_rows[0]["reason"] == "no_fail_to_pass"
    assert output_rows[0]["metadata"]["run_a"] == {}
    assert summary.instance_results_path == str(out_root / "instance_results.jsonl")
    assert summary.dataset_path == str(out_root / "dataset.resolved.jsonl")
    assert summary.predictions_path == str(out_root / "predictions.resolved.jsonl")


def test_run_eval_writes_resolved_dataset_and_prediction_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dataset_path = tmp_path / "dataset.jsonl"
    predictions_path = tmp_path / "predictions.jsonl"
    out_root = tmp_path / "out"

    dataset_path.write_text(
        json.dumps(
            {
                "instance_id": "i-1",
                "base_commit": "deadbeef",
                "problem_statement": "broken",
                "FAIL_TO_PASS": [],
                "PASS_TO_PASS": [],
                "test_patch": "",
                "patch": "",
                "version": "v1",
                "repo": "r",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    predictions_path.write_text(
        json.dumps(
            {
                "instance_id": "i-1",
                "model_name_or_path": "gold",
                "model_patch": "diff --git a.py b.py\n+ok",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    def fake_eval_instance(**kwargs: object) -> dict[str, object]:
        return {
            "status": "resolved",
            "reason": None,
            "error": None,
            "failure_code": None,
            "environment_strategy": "default",
            "test_strategy": "full_pytest",
            "targeted_test_cmd": "python -m pytest",
            "targeted_test_inputs": [],
            "log_a": "",
            "log_b": "",
            "log_c": "",
            "log_b_rerun": "",
            "log_c_rerun": "",
            "run_a": {},
            "run_b": {},
            "run_c": {},
            "run_b_rerun": {},
            "run_c_rerun": {},
            "run_a_attempts": [],
            "run_b_attempts": [],
            "run_c_attempts": [],
            "run_b_rerun_attempts": [],
            "run_c_rerun_attempts": [],
            "flake_runs": 0,
            "FAIL_TO_PASS": [],
            "PASS_TO_PASS": [],
            "resolved": True,
        }

    monkeypatch.setattr(
        "repogauge.validation.validate._eval_instance", fake_eval_instance
    )

    summary = run_eval(
        dataset_path=dataset_path,
        predictions_path=predictions_path,
        out_root=out_root,
        repo_root=tmp_path,
    )

    resolved_datasets = [
        json.loads(line)
        for line in (out_root / "dataset.resolved.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    resolved_predictions = [
        json.loads(line)
        for line in (out_root / "predictions.resolved.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]

    assert summary.resolved == 1
    assert resolved_datasets[0]["instance_id"] == "i-1"
    assert resolved_predictions[0]["instance_id"] == "i-1"
    assert resolved_predictions[0]["model_name_or_path"] == "gold"


def test_run_eval_emits_progress_updates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dataset_path = tmp_path / "dataset.jsonl"
    predictions_path = tmp_path / "predictions.jsonl"
    out_root = tmp_path / "out"

    dataset_path.write_text(
        "\n".join(
            json.dumps(
                {
                    "instance_id": iid,
                    "base_commit": "deadbeef",
                    "problem_statement": "broken",
                    "FAIL_TO_PASS": [],
                    "PASS_TO_PASS": [],
                    "test_patch": "",
                    "patch": "",
                    "version": "v1",
                    "repo": "r",
                }
            )
            for iid in ("i-1", "i-2")
        )
        + "\n",
        encoding="utf-8",
    )
    predictions_path.write_text(
        json.dumps(
            {
                "instance_id": "i-1",
                "model_name_or_path": "gold",
                "model_patch": "diff --git a.py b.py\n+ok",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    def fake_eval_instance(**kwargs: object) -> dict[str, object]:
        return {
            "status": "resolved",
            "reason": None,
            "error": None,
            "failure_code": None,
            "environment_strategy": "default",
            "test_strategy": "full_pytest",
            "targeted_test_cmd": "python -m pytest",
            "targeted_test_inputs": [],
            "log_a": "",
            "log_b": "",
            "log_c": "",
            "log_b_rerun": "",
            "log_c_rerun": "",
            "run_a": {},
            "run_b": {},
            "run_c": {},
            "run_b_rerun": {},
            "run_c_rerun": {},
            "run_a_attempts": [],
            "run_b_attempts": [],
            "run_c_attempts": [],
            "run_b_rerun_attempts": [],
            "run_c_rerun_attempts": [],
            "flake_runs": 0,
            "FAIL_TO_PASS": [],
            "PASS_TO_PASS": [],
            "resolved": True,
        }

    monkeypatch.setattr(
        "repogauge.validation.validate._eval_instance", fake_eval_instance
    )

    progress = StringIO()
    run_eval(
        dataset_path=dataset_path,
        predictions_path=predictions_path,
        out_root=out_root,
        repo_root=tmp_path,
        container_host="unix:///tmp/podman.sock",
        progress_stream=progress,
    )

    lines = [line for line in progress.getvalue().splitlines() if line.strip()]
    assert lines[0] == "repogauge eval: evaluating 2 instances locally via containers"
    assert "repogauge eval: starting [1/2] i-1" in lines
    assert any("[1/2] i-1 resolved" in line for line in lines)
    assert "repogauge eval: starting [2/2] i-2" in lines
    assert any("[2/2] i-2 skipped (missing prediction)" in line for line in lines)
