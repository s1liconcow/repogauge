"""Validation module regression tests."""

from contextlib import contextmanager
import json
from io import StringIO
from pathlib import Path
import threading
from unittest.mock import patch

import pytest
from repogauge.exec import CommandResult
from repogauge.validation.validate import (
    PytestExecutionError,
    TestExecutionError,
    _reset_checkout_for_pass,
    run_eval,
    _eval_instance,
    _pytest_command_attempts,
    _run_pytest,
    _run_test,
    _run_validation_pass,
    _test_command_attempts,
)


def _stub_eval_instance_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    checkout = tmp_path / "checkout"
    checkout.mkdir(exist_ok=True)

    class Handle:
        def __init__(self, path: Path) -> None:
            self.path = path

        def remove(self) -> None:
            return None

    monkeypatch.setattr(
        "repogauge.validation.validate.create_checkout",
        lambda *args, **kwargs: Handle(checkout),
    )
    return checkout


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

    monkeypatch.setattr("repogauge.validation.validate.run_command", fake_run_command)

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
    assert observed["kwargs"]["command"][:2] == [
        "pytest",
        "--junit-xml=/testbed/outside-junit.xml",
    ]


def test_run_test_python_retries_with_file_paths_when_node_collection_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed_cmds: list[list[str]] = []

    class Adapter:
        def name(self) -> str:
            return "python"

        def env_overrides(self, worktree: Path) -> dict[str, str]:
            return {}

        def test_command_attempts(self, test_cmd_base: str) -> list[list[str]]:
            return [["python", "-m", "pytest"]]

        def test_report_filename(self) -> str | None:
            return "junit.xml"

        def parse_test_output(
            self, report: object, test_spec: object | None
        ) -> dict[str, str]:
            assert isinstance(report, Path)
            if len(observed_cmds) == 1:
                return {"tests.unit.test_example": "error"}
            return {"tests/test_example.py::test_ok": "pass"}

        def test_report_glob(self) -> str | None:
            return None

    def fake_run_command(cmd, *, cwd=None, env=None, timeout_seconds=None):  # noqa: ARG001
        observed_cmds.append(list(cmd))
        xml_flag = next(arg for arg in cmd if arg.startswith("--junit-xml="))
        Path(xml_flag.removeprefix("--junit-xml=")).write_text(
            "<testsuite></testsuite>", encoding="utf-8"
        )
        if len(observed_cmds) == 1:
            return CommandResult(
                command=cmd,
                returncode=4,
                stdout="",
                stderr=(
                    "ERROR: found no collectors for "
                    "/repo/tests/test_example.py::test_ok\n"
                ),
            )
        return CommandResult(command=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        "repogauge.validation.validate.run_command",
        fake_run_command,
    )

    outcomes, raw, attempts = _run_test(
        tmp_path,
        test_files=["tests/test_example.py::test_ok"],
        test_report_path=tmp_path / "junit.xml",
        timeout_seconds=5,
        test_cmd_base="python -m pytest",
        adapter=Adapter(),
    )

    assert outcomes == {"tests/test_example.py::test_ok": "pass"}
    assert raw == "[stdout]\n\n[stderr]\n"
    assert observed_cmds[0][-1] == "tests/test_example.py::test_ok"
    assert observed_cmds[1][-1] == "tests/test_example.py"
    assert attempts[0]["status"] == "collector_fallback"
    assert attempts[0]["fallback_test_inputs"] == ["tests/test_example.py"]
    assert attempts[1]["status"] == "success"


def test_run_validation_pass_collects_runtime_pytest_nodes_from_file_targets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    checkout = tmp_path / "checkout"
    test_file = checkout / "tests" / "test_example.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_example():\n    assert True\n", encoding="utf-8")
    observed: dict[str, object] = {}

    class Handle:
        def __init__(self, path: Path) -> None:
            self.path = path

        def remove(self) -> None:
            return None

    class Adapter:
        def name(self) -> str:
            return "python"

        def env_overrides(self, worktree: Path) -> dict[str, str]:
            return {}

        def test_command_attempts(self, test_cmd_base: str) -> list[list[str]]:
            return [["python", "-m", "pytest"]]

        def test_report_filename(self) -> str | None:
            return "junit.xml"

    monkeypatch.setattr(
        "repogauge.validation.validate.create_checkout",
        lambda *args, **kwargs: Handle(checkout),
    )
    monkeypatch.setattr(
        "repogauge.validation.validate.apply_patch_text",
        lambda *args, **kwargs: None,
    )

    def fake_run_command(cmd, *, cwd=None, env=None, timeout_seconds=None):  # noqa: ARG001
        observed["collect_cmd"] = list(cmd)
        return CommandResult(
            command=cmd,
            returncode=0,
            stdout=(
                "tests/test_example.py::test_one\ntests/test_example.py::test_two\n"
            ),
            stderr="",
        )

    def fake_run_test(
        worktree: Path,
        *,
        test_files: list[str],
        test_report_path: Path,
        timeout_seconds: int = 120,
        test_cmd_base: str = "",
        adapter=None,
        test_spec=None,
        attempt_id_prefix: str = "eval",
        container_host: str | None = None,
        adapter_spec=None,
        instance_row=None,
        environment_strategy: str = "default",
        workspace_session=None,
    ):
        observed["resolved_test_files"] = list(test_files)
        observed["workspace_session"] = workspace_session
        return (
            {
                "tests/test_example.py::test_one": "fail",
                "tests/test_example.py::test_two": "pass",
            },
            "[stdout]\n..\n[stderr]\n",
            [
                {
                    "attempt": 1,
                    "status": "success",
                    "command": ["python", "-m", "pytest"],
                    "returncode": 0,
                    "timed_out": False,
                    "elapsed_ms": 1,
                    "tests_run": 2,
                }
            ],
        )

    monkeypatch.setattr(
        "repogauge.validation.validate.run_command",
        fake_run_command,
    )
    monkeypatch.setattr(
        "repogauge.validation.validate._run_test",
        fake_run_test,
    )

    outcome = _run_validation_pass(
        label="b",
        temp_root=tmp_path,
        repo_root=tmp_path,
        base_commit="deadbeef",
        test_patch="",
        pred_patch="",
        test_files=["tests/test_example.py::legacy_selector"],
        timeout_seconds=5,
        test_cmd_base="python -m pytest --junit-xml={junit_xml}",
        adapter=Adapter(),
    )

    assert outcome["status"] == "passed"
    assert observed["collect_cmd"][-1] == "tests/test_example.py"
    assert observed["resolved_test_files"] == [
        "tests/test_example.py::test_one",
        "tests/test_example.py::test_two",
    ]
    assert observed["workspace_session"] is None
    assert outcome["attempts"][0]["status"] == "collection_resolved"


def test_run_validation_pass_container_collection_uses_separate_artifacts_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    checkout = tmp_path / "checkout"
    test_file = checkout / "tests" / "test_example.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_example():\n    assert True\n", encoding="utf-8")
    observed: dict[str, object] = {}

    class Handle:
        def __init__(self, path: Path) -> None:
            self.path = path

        def remove(self) -> None:
            return None

    class Adapter:
        def name(self) -> str:
            return "python"

        def env_overrides(self, worktree: Path) -> dict[str, str]:
            return {}

        def test_command_attempts(self, test_cmd_base: str) -> list[list[str]]:
            return [["python", "-m", "pytest"]]

        def test_report_filename(self) -> str | None:
            return "junit.xml"

    monkeypatch.setattr(
        "repogauge.validation.validate.create_checkout",
        lambda *args, **kwargs: Handle(checkout),
    )
    monkeypatch.setattr(
        "repogauge.validation.validate.apply_patch_text",
        lambda *args, **kwargs: None,
    )

    def fake_container_exec(**kwargs):
        observed["collection_artifacts_root"] = kwargs["artifacts_root"]
        observed["collection_workspace_path"] = kwargs["workspace_path"]
        observed["collection_command"] = list(kwargs["command"])
        return CommandResult(
            command=kwargs["command"],
            returncode=0,
            stdout="tests/test_example.py::test_one\n",
            stderr="",
        )

    def fake_run_test(
        worktree: Path,
        *,
        test_files: list[str],
        test_report_path: Path,
        timeout_seconds: int = 120,
        test_cmd_base: str = "",
        adapter=None,
        test_spec=None,
        attempt_id_prefix: str = "eval",
        container_host: str | None = None,
        adapter_spec=None,
        instance_row=None,
        environment_strategy: str = "default",
        workspace_session=None,
    ):
        observed["resolved_test_files"] = list(test_files)
        observed["workspace_session"] = workspace_session
        return (
            {"tests/test_example.py::test_one": "pass"},
            "[stdout]\n.\n[stderr]\n",
            [
                {
                    "attempt": 1,
                    "status": "success",
                    "command": ["python", "-m", "pytest"],
                    "returncode": 0,
                    "timed_out": False,
                    "elapsed_ms": 1,
                    "tests_run": 1,
                }
            ],
        )

    monkeypatch.setattr(
        "repogauge.validation.validate.run_workspace_command_in_container",
        fake_container_exec,
    )
    monkeypatch.setattr(
        "repogauge.validation.validate._run_test",
        fake_run_test,
    )

    outcome = _run_validation_pass(
        label="b",
        temp_root=tmp_path,
        repo_root=tmp_path,
        base_commit="deadbeef",
        test_patch="",
        pred_patch="",
        test_files=["tests/test_example.py"],
        timeout_seconds=5,
        test_cmd_base="python -m pytest --junit-xml={junit_xml}",
        adapter=Adapter(),
        container_host="unix:///tmp/podman.sock",
        adapter_spec={"language": "python", "repo": "owner/repo", "version": "1.0"},
        instance_row={"instance_id": "inst-1", "repo": "owner/repo", "version": "1.0"},
    )

    assert outcome["status"] == "passed"
    assert observed["collection_workspace_path"] == checkout
    assert observed["collection_artifacts_root"] == tmp_path
    assert observed["collection_artifacts_root"] != observed["collection_workspace_path"]
    assert observed["resolved_test_files"] == ["tests/test_example.py::test_one"]
    assert observed["workspace_session"] is None


def test_reset_checkout_for_pass_cleans_untracked_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands: list[list[str]] = []

    def fake_run_command(command: list[str], **kwargs: object):
        commands.append(command)
        return CommandResult(command=tuple(command), returncode=0, stdout="", stderr="")

    monkeypatch.setattr("repogauge.validation.validate.run_command", fake_run_command)
    monkeypatch.setattr(
        "repogauge.validation.validate._path_exists_at_ref",
        lambda worktree, ref, rel_path: True,
    )

    _reset_checkout_for_pass(
        worktree=tmp_path,
        base_commit="deadbeef",
        cleanup_paths=[],
    )

    assert commands == [
        ["git", "-C", str(tmp_path), "reset", "--hard", "deadbeef"],
        ["git", "-C", str(tmp_path), "clean", "-fdx"],
    ]


def test_run_validation_pass_skips_missing_targeted_pytest_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    class Handle:
        def __init__(self, path: Path) -> None:
            self.path = path

        def remove(self) -> None:
            return None

    class Adapter:
        def name(self) -> str:
            return "python"

        def env_overrides(self, worktree: Path) -> dict[str, str]:
            return {}

        def test_command_attempts(self, test_cmd_base: str) -> list[list[str]]:
            return [["python", "-m", "pytest"]]

    monkeypatch.setattr(
        "repogauge.validation.validate.create_checkout",
        lambda *args, **kwargs: Handle(checkout),
    )
    monkeypatch.setattr(
        "repogauge.validation.validate.apply_patch_text",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "repogauge.validation.validate._run_test",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("_run_test should not run")
        ),
    )

    outcome = _run_validation_pass(
        label="a",
        temp_root=tmp_path,
        repo_root=tmp_path,
        base_commit="deadbeef",
        test_patch="",
        pred_patch="",
        test_files=["tests/test_missing.py"],
        timeout_seconds=5,
        test_cmd_base="python -m pytest --junit-xml={junit_xml}",
        adapter=Adapter(),
    )

    assert outcome["status"] == "passed"
    assert outcome["outcomes"] == {}
    assert outcome["attempts"][0]["status"] == "collection_missing"


def test_pytest_execution_error_alias_is_preserved() -> None:
    assert PytestExecutionError is TestExecutionError


def test_eval_instance_executes_four_passes_in_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []
    _stub_eval_instance_checkout(monkeypatch, tmp_path)

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


def test_eval_instance_strips_prediction_edits_to_withheld_test_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed_pred_patches: dict[str, str] = {}
    _stub_eval_instance_checkout(monkeypatch, tmp_path)

    def fake_build_targeted_test_plan(
        test_cmd: str, test_patch: str
    ) -> tuple[str, list[str]]:
        return ("python -m pytest", ["tests/test_eval.py"])

    def fake_run_validation_pass(*, label: str, pred_patch: str, **kwargs: object):
        observed_pred_patches[label] = pred_patch
        outcome = {"tests/test_eval.py::regression": "pass"}
        if label in {"b", "b_rerun"}:
            outcome = {"tests/test_eval.py::regression": "fail"}
        return {
            "status": "passed",
            "error": None,
            "outcomes": outcome,
            "log": "",
            "attempts": [],
        }

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
        pred_patch=(
            "diff --git a/src.py b/src.py\n"
            "+ok\n"
            "diff --git a/tests/test_eval.py b/tests/test_eval.py\n"
            "+bad\n"
        ),
        test_patch="diff --git a/tests/test_eval.py b/tests/test_eval.py\n+ok\n",
        declared_ftp=[],
        declared_ptp=[],
        timeout_seconds=120,
        test_cmd_base="pytest --config",
    )

    assert "tests/test_eval.py" not in observed_pred_patches["c"]
    assert "tests/test_eval.py" not in observed_pred_patches["c_rerun"]
    assert "diff --git a/src.py b/src.py" in observed_pred_patches["c"]
    assert outcome["withheld_test_patch_sanitized"] is True
    assert outcome["withheld_test_paths_touched"] == ["tests/test_eval.py"]


def test_eval_instance_reuses_checkout_and_workspace_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed: dict[str, object] = {
        "create_checkout_calls": [],
        "workspace_session_kwargs": None,
        "pass_calls": [],
        "removed": False,
    }

    class Handle:
        def __init__(self, path: Path) -> None:
            self.path = path

        def remove(self) -> None:
            observed["removed"] = True

    session = object()

    def fake_create_checkout(path: Path, *, ref: str, checkout_path: Path) -> Handle:
        checkout_path.mkdir(parents=True, exist_ok=True)
        observed["create_checkout_calls"].append((path, ref, checkout_path))
        return Handle(checkout_path)

    @contextmanager
    def fake_workspace_container_session(**kwargs):
        observed["workspace_session_kwargs"] = kwargs
        yield session

    def fake_build_targeted_test_plan(
        test_cmd: str, test_patch: str
    ) -> tuple[str, list[str]]:
        return ("python -m pytest", ["tests/test_eval.py"])

    def fake_run_validation_pass(*, label: str, **kwargs: object) -> dict[str, object]:
        observed["pass_calls"].append(
            (
                label,
                kwargs["checkout_path"],
                kwargs["workspace_session"],
                tuple(kwargs["cleanup_paths"]),
            )
        )
        if label in {"b", "b_rerun"}:
            outcomes = {"tests/test_eval.py::regression": "fail"}
        else:
            outcomes = {"tests/test_eval.py::regression": "pass"}
        return {
            "status": "passed",
            "error": None,
            "outcomes": outcomes,
            "log": label,
            "attempts": [],
        }

    monkeypatch.setattr(
        "repogauge.validation.validate.create_checkout", fake_create_checkout
    )
    monkeypatch.setattr(
        "repogauge.validation.validate.workspace_container_session",
        fake_workspace_container_session,
    )
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
        pred_patch="diff --git a/src.py b/src.py\n+ok\n",
        test_patch="diff --git a/tests/test_eval.py b/tests/test_eval.py\n+ok\n",
        declared_ftp=[],
        declared_ptp=[],
        timeout_seconds=120,
        test_cmd_base="pytest --config",
        container_host="unix:///tmp/podman.sock",
        instance_id="inst-1",
    )

    assert outcome["status"] == "resolved"
    assert len(observed["create_checkout_calls"]) == 1
    assert observed["removed"] is True
    session_kwargs = observed["workspace_session_kwargs"]
    assert session_kwargs is not None
    assert session_kwargs["attempt_id"] == "eval-inst-1-session"
    checkout_path = observed["create_checkout_calls"][0][2]
    assert session_kwargs["workspace_path"] == checkout_path
    assert [call[0] for call in observed["pass_calls"]] == [
        "a",
        "b",
        "c",
        "b_rerun",
        "c_rerun",
    ]
    assert all(call[1] == checkout_path for call in observed["pass_calls"])
    assert all(call[2] is session for call in observed["pass_calls"])
    assert all(call[3] == ("src.py", "tests/test_eval.py") for call in observed["pass_calls"])


def test_eval_instance_marks_flaky_when_reruns_differ(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_eval_instance_checkout(monkeypatch, tmp_path)

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
    _stub_eval_instance_checkout(monkeypatch, tmp_path)

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
    _stub_eval_instance_checkout(monkeypatch, tmp_path)

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
    _stub_eval_instance_checkout(monkeypatch, tmp_path)

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
    _stub_eval_instance_checkout(monkeypatch, tmp_path)

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
    _stub_eval_instance_checkout(monkeypatch, tmp_path)

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


def test_run_eval_preserves_multiple_predictions_per_instance(
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
        "\n".join(
            [
                json.dumps(
                    {
                        "instance_id": "i-1",
                        "solver_id": "solver-a",
                        "model_name_or_path": "solver-a",
                        "model_patch": "diff --git a.py b.py\n+one\n",
                    }
                ),
                json.dumps(
                    {
                        "instance_id": "i-1",
                        "solver_id": "solver-b",
                        "model_name_or_path": "solver-b",
                        "model_patch": "diff --git a.py b.py\n+two\n",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    seen_solver_ids: list[str] = []

    def fake_eval_instance(**kwargs: object) -> dict[str, object]:
        pred_patch = str(kwargs["pred_patch"])
        solver_id = "solver-a" if "+one" in pred_patch else "solver-b"
        seen_solver_ids.append(solver_id)
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

    output_rows = [
        json.loads(line)
        for line in (out_root / "validation.jsonl")
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

    assert summary.total == 2
    assert summary.resolved == 2
    assert seen_solver_ids == ["solver-a", "solver-b"]
    assert [row["solver_id"] for row in output_rows] == ["solver-a", "solver-b"]
    assert [row["solver_id"] for row in resolved_predictions] == [
        "solver-a",
        "solver-b",
    ]


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
    assert any("[2/2] i-2 skipped (missing prediction)" in line for line in lines)


def test_run_eval_only_reports_started_jobs_when_worker_slot_opens(
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
        "\n".join(
            json.dumps(
                {
                    "instance_id": iid,
                    "model_name_or_path": "gold",
                    "model_patch": "diff --git a.py b.py\n+ok",
                }
            )
            for iid in ("i-1", "i-2")
        )
        + "\n",
        encoding="utf-8",
    )

    first_entered = threading.Event()
    release_first = threading.Event()
    call_order: list[str] = []

    def fake_eval_instance(*, instance_id: str | None = None, **kwargs: object) -> dict[str, object]:
        assert instance_id is not None
        call_order.append(instance_id)
        if instance_id == "i-1":
            first_entered.set()
            release_first.wait(timeout=5)
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
    runner = threading.Thread(
        target=run_eval,
        kwargs={
            "dataset_path": dataset_path,
            "predictions_path": predictions_path,
            "out_root": out_root,
            "repo_root": tmp_path,
            "progress_stream": progress,
            "jobs": 1,
        },
    )
    runner.start()
    assert first_entered.wait(timeout=5)
    pre_release_lines = [line for line in progress.getvalue().splitlines() if line.strip()]
    assert "repogauge eval: starting [1/2] i-1" in pre_release_lines
    assert "repogauge eval: starting [2/2] i-2" not in pre_release_lines

    release_first.set()
    runner.join(timeout=5)
    assert not runner.is_alive()

    lines = [line for line in progress.getvalue().splitlines() if line.strip()]
    start_1 = lines.index("repogauge eval: starting [1/2] i-1")
    resolved_1 = next(i for i, line in enumerate(lines) if "[1/2] i-1 resolved" in line)
    start_2 = lines.index("repogauge eval: starting [2/2] i-2")
    assert start_1 < resolved_1 < start_2
    assert call_order == ["i-1", "i-2"]
