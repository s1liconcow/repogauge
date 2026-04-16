"""Validation module regression tests."""

from pathlib import Path
from unittest.mock import patch

from repogauge.exec import CommandResult
from repogauge.validation.validate import _pytest_command_attempts, _run_pytest


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
