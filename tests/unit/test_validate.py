"""Validation module regression tests."""

from pathlib import Path
from unittest.mock import patch

import pytest
from repogauge.exec import CommandResult
from repogauge.validation.validate import (
    _eval_instance,
    _pytest_command_attempts,
    _run_pytest,
)


def test_pytest_command_attempts_falls_back_to_interpreter_invocation() -> None:
    attempts = _pytest_command_attempts("pytest --tb=no tests/unit")
    assert attempts[0] == ["pytest", "--tb=no", "tests/unit"]
    assert attempts[1][0].endswith("python") or attempts[1][0].endswith("python3")


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


def test_eval_instance_executes_four_passes_in_order(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_build_targeted_test_plan(test_cmd: str, test_patch: str) -> tuple[str, list[str]]:
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
            return fake_pass("b_rerun", {"tests/test_eval.py::fail": "fail"}, "run-b-rerun")
        if label == "c_rerun":
            return fake_pass("c_rerun", {"tests/test_eval.py::fail": "pass"}, "run-c-rerun")
        raise AssertionError(f"unexpected label {label}")

    monkeypatch.setattr("repogauge.validation.validate.build_targeted_test_plan", fake_build_targeted_test_plan)
    monkeypatch.setattr("repogauge.validation.validate._run_validation_pass", fake_run_validation_pass)

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


def test_eval_instance_marks_flaky_when_reruns_differ(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_build_targeted_test_plan(test_cmd: str, test_patch: str) -> tuple[str, list[str]]:
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

    monkeypatch.setattr("repogauge.validation.validate.build_targeted_test_plan", fake_build_targeted_test_plan)
    monkeypatch.setattr("repogauge.validation.validate._run_validation_pass", fake_run_validation_pass)

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
    def fake_build_targeted_test_plan(test_cmd: str, test_patch: str) -> tuple[str, list[str]]:
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

    monkeypatch.setattr("repogauge.validation.validate.build_targeted_test_plan", fake_build_targeted_test_plan)
    monkeypatch.setattr("repogauge.validation.validate._run_validation_pass", fake_run_validation_pass)

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
