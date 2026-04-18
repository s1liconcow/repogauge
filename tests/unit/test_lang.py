from __future__ import annotations

from pathlib import Path

import pytest

from repogauge.lang import (
    DetectionResult,
    FileRoleRules,
    LanguageAdapter,
    detect_language,
    find_adapter,
    iter_adapters,
    register_adapter,
)


class FakeAdapter:
    def __init__(self, name: str, detection: DetectionResult) -> None:
        self._name = name
        self._detection = detection

    def name(self) -> str:
        return self._name

    def detect(self, repo_root: Path) -> DetectionResult:
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


def test_detect_language_uses_lexicographic_tie_break_and_sorted_iteration(
    tmp_path: Path,
) -> None:
    register_adapter(
        FakeAdapter(
            "zeta",
            DetectionResult(
                language="zeta",
                confidence=0.8,
                signals=["zeta"],
                runtime_version="1.0",
            ),
        )
    )
    register_adapter(
        FakeAdapter(
            "alpha",
            DetectionResult(
                language="alpha",
                confidence=0.8,
                signals=["alpha"],
                runtime_version="2.0",
            ),
        )
    )

    assert [adapter.name() for adapter in iter_adapters()] == [
        "alpha",
        "go",
        "java",
        "javascript",
        "python",
        "rust",
        "zeta",
    ]
    assert isinstance(FakeAdapter("alpha", DetectionResult("alpha", 1.0, [])), LanguageAdapter)

    result = detect_language(tmp_path)

    assert result == DetectionResult(
        language="alpha",
        confidence=0.8,
        signals=["alpha"],
        runtime_version="2.0",
    )


def test_register_adapter_rejects_duplicates() -> None:
    register_adapter(
        FakeAdapter(
            "alpha",
            DetectionResult(language="alpha", confidence=0.4, signals=[]),
        )
    )

    with pytest.raises(ValueError, match="already registered: alpha"):
        register_adapter(
            FakeAdapter(
                "alpha",
                DetectionResult(language="alpha", confidence=0.6, signals=[]),
            )
        )


def test_find_adapter_unknown_raises_key_error() -> None:
    with pytest.raises(KeyError, match="unknown language adapter: 'missing'"):
        find_adapter("missing")


def test_registry_starts_without_fake_adapters() -> None:
    names = {adapter.name() for adapter in iter_adapters()}
    assert "fake" not in names
    assert {"go", "java", "javascript", "python", "rust"}.issubset(names)


def test_find_adapter_returns_javascript_adapter() -> None:
    from repogauge.lang.javascript import JavaScriptAdapter

    adapter = find_adapter("javascript")

    assert isinstance(adapter, JavaScriptAdapter)
    assert adapter.name() == "javascript"


def test_find_adapter_returns_go_and_rust_adapters() -> None:
    from repogauge.lang.go import GoAdapter
    from repogauge.lang.rust import RustAdapter

    go_adapter = find_adapter("go")
    rust_adapter = find_adapter("rust")

    assert isinstance(go_adapter, GoAdapter)
    assert go_adapter.name() == "go"
    assert isinstance(rust_adapter, RustAdapter)
    assert rust_adapter.name() == "rust"
