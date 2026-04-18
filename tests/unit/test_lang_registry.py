from __future__ import annotations

from pathlib import Path

import pytest

import repogauge.lang.python as python_module
from repogauge.lang import (
    DetectionResult,
    FileRoleRules,
    detect_language,
    find_adapter,
    iter_adapters,
    register_adapter,
)
from repogauge.lang.python import PythonAdapter


class FakeAdapter:
    def __init__(self, name: str, detection: DetectionResult) -> None:
        self._name = name
        self._detection = detection

    def name(self) -> str:
        return self._name

    def detect(self, repo_root: Path) -> DetectionResult:
        marker = repo_root / "sentinel.marker"
        if self._name == "fake" and marker.exists():
            return self._detection
        if self._name == "fake":
            return DetectionResult(language=self._name, confidence=0.0, signals=[])
        return self._detection

    def inspect(self, repo_root: Path) -> dict[str, object]:
        return {"language": self._name}

    def build_env_plan(self, profile: dict[str, object]) -> object:
        return {"profile": profile}

    def parse_test_output(
        self, report: object, test_spec: object | None
    ) -> dict[str, str]:
        return {}

    def file_role_rules(self) -> FileRoleRules:
        return FileRoleRules(set(), [], set(), set(), set())

    def harness_template_vars(self, spec: dict[str, object]) -> dict[str, object]:
        return {"name": self._name, "spec": spec}

    def signature_labels(self, profile: dict[str, object]) -> dict[str, str]:
        return {
            "runtime_label": self._name,
            "test_label": self._name,
            "package_label": self._name,
        }

    def dependency_signature_inputs(
        self, repo_root: Path, profile: dict[str, object]
    ) -> list[str]:
        return [self._name]

    def env_overrides(self, worktree: Path) -> dict[str, str]:
        return {}

    def test_command_attempts(self, test_cmd_base: str) -> list[list[str]]:
        return [[test_cmd_base]]

    def test_report_filename(self) -> str | None:
        return None

    def test_report_glob(self) -> str | None:
        return None


def test_iter_adapters_includes_python_adapter_after_module_import() -> None:
    assert python_module.PythonAdapter is PythonAdapter
    assert any(isinstance(adapter, PythonAdapter) for adapter in iter_adapters())


def test_detect_language_routes_to_fake_adapter(tmp_path: Path) -> None:
    (tmp_path / "sentinel.marker").write_text("hit", encoding="utf-8")
    register_adapter(
        FakeAdapter(
            "fake",
            DetectionResult(
                language="fake",
                confidence=0.95,
                signals=["sentinel.marker"],
                runtime_version="1.0",
            ),
        )
    )

    result = detect_language(tmp_path)

    assert result.language == "fake"
    assert result.confidence == pytest.approx(0.95)
    assert result.signals == ["sentinel.marker"]


def test_detect_language_uses_lexicographic_tie_break(tmp_path: Path) -> None:
    register_adapter(
        FakeAdapter(
            "zeta",
            DetectionResult(language="zeta", confidence=0.8, signals=["zeta"]),
        )
    )
    register_adapter(
        FakeAdapter(
            "alpha",
            DetectionResult(language="alpha", confidence=0.8, signals=["alpha"]),
        )
    )

    result = detect_language(tmp_path)

    assert result.language == "alpha"
    assert result.confidence == pytest.approx(0.8)

    assert [adapter.name() for adapter in iter_adapters()] == [
        "alpha",
        "java",
        "python",
        "zeta",
    ]


def test_find_adapter_returns_python_adapter() -> None:
    adapter = find_adapter("python")

    assert isinstance(adapter, PythonAdapter)
    assert adapter.name() == "python"


def test_find_adapter_returns_java_adapter() -> None:
    from repogauge.lang.java import JavaAdapter

    adapter = find_adapter("java")

    assert isinstance(adapter, JavaAdapter)
    assert adapter.name() == "java"


def test_find_adapter_unknown_raises_key_error() -> None:
    with pytest.raises(KeyError, match="unknown language adapter: 'missing'"):
        find_adapter("missing")


def test_registry_starts_without_fake_adapters() -> None:
    names = {adapter.name() for adapter in iter_adapters()}
    assert "fake" not in names
    assert {"java", "python"}.issubset(names)
