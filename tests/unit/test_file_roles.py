from __future__ import annotations

from pathlib import Path

import pytest

from repogauge.lang import DetectionResult, FileRoleRules, iter_adapters
from repogauge.mining import classify_file, reset_cache


class FakeGoAdapter:
    def name(self) -> str:
        return "fake-go"

    def detect(self, repo_root: Path) -> DetectionResult:
        return DetectionResult(language="fake-go", confidence=0.0, signals=[])

    def inspect(self, repo_root: Path) -> dict[str, object]:
        return {"language": "fake-go"}

    def build_env_plan(self, profile: dict[str, object]) -> object:
        return {"profile": profile}

    def parse_test_output(
        self, report: object, test_spec: object | None
    ) -> dict[str, str]:
        return {}

    def file_role_rules(self) -> FileRoleRules:
        return FileRoleRules(
            prod_extensions={".go"},
            test_filename_patterns=["*_test.go"],
            test_dir_names={"test", "tests"},
            config_build_filenames=set(),
            vendor_dir_names=set(),
        )

    def harness_template_vars(self, spec: dict[str, object]) -> dict[str, object]:
        return {"name": "fake-go", "spec": spec}

    def signature_labels(self, profile: dict[str, object]) -> dict[str, str]:
        return {
            "runtime_label": "fake-go",
            "test_label": "fake-go",
            "package_label": "fake-go",
        }

    def dependency_signature_inputs(
        self, repo_root: Path, profile: dict[str, object]
    ) -> list[str]:
        return ["fake-go"]

    def env_overrides(self, worktree: Path) -> dict[str, str]:
        return {}

    def test_command_attempts(self, test_cmd_base: str) -> list[list[str]]:
        return [[test_cmd_base]]

    def test_report_filename(self) -> str | None:
        return None

    def test_report_glob(self) -> str | None:
        return None


def test_classify_prod_path():
    result = classify_file("repogauge/mining/inspect.py")
    assert result.role == "prod"
    assert "runtime source extension" in result.reason


def test_classify_test_file():
    result = classify_file("tests/unit/test_cli.py")
    assert result.role == "test"
    assert "test filename convention" in result.reason


def test_classify_test_support_fixture():
    result = classify_file("tests/fixtures/cli_input.json")
    assert result.role == "test_support"
    assert "test-support path under tests" in result.reason


def test_classify_python_support_files():
    for path in ("conftest.py", "pytest.ini", "tox.ini"):
        result = classify_file(path)
        assert result.role == "test_support"
        assert "test harness support file" in result.reason


def test_classify_config_build_file():
    result = classify_file(".github/workflows/test.yml")
    assert result.role == "config_build"
    assert "CI, package, or tooling configuration file" in result.reason


def test_classify_docs_file():
    result = classify_file("docs/notes/implementation.md")
    assert result.role == "docs"
    assert "documentation file or directory" in result.reason


def test_classify_generated_vendor_file():
    result = classify_file(".venv/lib/site-packages/pkg/__init__.py")
    assert result.role == "generated_vendor"
    assert "vendor or generated build cache directory" in result.reason


def test_classify_language_specific_test_pattern_is_scoped_to_detected_languages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "repogauge.mining.file_roles.iter_adapters",
        lambda: (*iter_adapters(), FakeGoAdapter()),
    )
    monkeypatch.setattr(
        "repogauge.mining.file_roles.inspect_repository",
        lambda repo_root: {
            "language_detection": [
                {"name": "python", "confidence": 1.0},
                {"name": "fake-go", "confidence": 0.0},
            ]
        },
    )

    reset_cache()
    try:
        result = classify_file("tests/unit/example_test.go")
        assert result.role == "test_support"
        assert "test-related path outside conventional test naming" in result.reason
    finally:
        reset_cache()


def test_classify_unknown_file():
    result = classify_file("random/binary.bin")
    assert result.role == "unknown"
    assert "No explicit role rule matched" in result.reason
