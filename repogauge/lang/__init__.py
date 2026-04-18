"""Language adapter contract and registry for RepoGauge."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol, runtime_checkable

from repogauge.validation.env_detect import EnvPlan


@dataclass
class DetectionResult:
    language: str
    confidence: float
    signals: list[str]
    runtime_version: str | None = None


@dataclass
class FileRoleRules:
    prod_extensions: set[str]
    test_filename_patterns: list[str]
    test_dir_names: set[str]
    config_build_filenames: set[str]
    vendor_dir_names: set[str]
    test_support_filenames: set[str] = field(default_factory=set)


@runtime_checkable
class LanguageAdapter(Protocol):
    def name(self) -> str: ...

    def detect(self, repo_root: Path) -> DetectionResult: ...

    def inspect(self, repo_root: Path) -> dict[str, Any]: ...

    def build_env_plan(self, profile: dict[str, Any]) -> EnvPlan: ...

    def parse_test_output(
        self, report: object, test_spec: object | None
    ) -> dict[str, str]: ...

    def file_role_rules(self) -> FileRoleRules: ...

    def harness_template_vars(self, spec: dict[str, Any]) -> dict[str, Any]: ...

    def signature_labels(self, profile: dict[str, Any]) -> dict[str, str]: ...

    def dependency_signature_inputs(
        self, repo_root: Path, profile: dict[str, Any]
    ) -> list[str]: ...

    def env_overrides(self, worktree: Path) -> dict[str, str]: ...

    def test_command_attempts(self, test_cmd_base: str) -> list[list[str]]: ...

    def test_report_filename(self) -> str | None: ...

    def test_report_glob(self) -> str | None: ...


_REGISTERED_ADAPTERS: list[LanguageAdapter] = []
_BUILTINS_REGISTERED = False


def _adapter_name(adapter: LanguageAdapter) -> str:
    name = adapter.name()
    if not isinstance(name, str):
        raise TypeError("adapter.name() must return a string")
    normalized = name.strip()
    if not normalized:
        raise ValueError("adapter.name() cannot be empty")
    if normalized != normalized.lower():
        raise ValueError(f"adapter names must be lowercase: {normalized!r}")
    return normalized


def _sorted_adapters() -> list[LanguageAdapter]:
    return sorted(_REGISTERED_ADAPTERS, key=_adapter_name)


def _store_adapter(adapter: LanguageAdapter) -> None:
    adapter_name = _adapter_name(adapter)
    if any(_adapter_name(existing) == adapter_name for existing in _REGISTERED_ADAPTERS):
        raise ValueError(f"language adapter already registered: {adapter_name}")
    _REGISTERED_ADAPTERS.append(adapter)
    _REGISTERED_ADAPTERS.sort(key=_adapter_name)
    try:
        from repogauge.mining.file_roles import reset_cache
    except Exception:
        return
    reset_cache()


def _register_builtin_adapters() -> None:
    """Register built-in adapters once.

    Keeping the hook explicit avoids import-time discovery and the circular
    imports it causes.
    """

    global _BUILTINS_REGISTERED
    if _BUILTINS_REGISTERED:
        return
    from .go import GoAdapter
    from .java import JavaAdapter
    from .javascript import JavaScriptAdapter
    from .python import PythonAdapter
    from .rust import RustAdapter

    if not any(_adapter_name(existing) == "go" for existing in _REGISTERED_ADAPTERS):
        _store_adapter(GoAdapter())
    if not any(_adapter_name(existing) == "python" for existing in _REGISTERED_ADAPTERS):
        _store_adapter(PythonAdapter())
    if not any(_adapter_name(existing) == "java" for existing in _REGISTERED_ADAPTERS):
        _store_adapter(JavaAdapter())
    if not any(
        _adapter_name(existing) == "javascript" for existing in _REGISTERED_ADAPTERS
    ):
        _store_adapter(JavaScriptAdapter())
    if not any(_adapter_name(existing) == "rust" for existing in _REGISTERED_ADAPTERS):
        _store_adapter(RustAdapter())
    _BUILTINS_REGISTERED = True


def register_adapter(adapter: LanguageAdapter) -> None:
    _register_builtin_adapters()
    _store_adapter(adapter)


def iter_adapters() -> Iterable[LanguageAdapter]:
    _register_builtin_adapters()
    return tuple(_sorted_adapters())


def find_adapter(name: str) -> LanguageAdapter:
    _register_builtin_adapters()
    normalized = name.strip().lower()
    for adapter in _sorted_adapters():
        if _adapter_name(adapter) == normalized:
            return adapter
    raise KeyError(f"unknown language adapter: {normalized!r}")


def detect_language(repo_root: Path) -> DetectionResult:
    _register_builtin_adapters()

    best_name: str | None = None
    best_result: DetectionResult | None = None

    for adapter in _sorted_adapters():
        result = adapter.detect(repo_root)
        if result.confidence <= 0.0:
            continue

        adapter_name = _adapter_name(adapter)
        if best_result is None:
            best_name = adapter_name
            best_result = result
            continue

        if result.confidence > best_result.confidence:
            best_name = adapter_name
            best_result = result
            continue

        if (
            result.confidence == best_result.confidence
            and best_name is not None
            and adapter_name < best_name
        ):
            best_name = adapter_name
            best_result = result

    if best_result is None or best_name is None:
        return DetectionResult(language="unknown", confidence=0.0, signals=[])

    return DetectionResult(
        language=best_name,
        confidence=best_result.confidence,
        signals=list(best_result.signals),
        runtime_version=best_result.runtime_version,
    )


__all__ = [
    "DetectionResult",
    "FileRoleRules",
    "LanguageAdapter",
    "_register_builtin_adapters",
    "detect_language",
    "find_adapter",
    "iter_adapters",
    "register_adapter",
]
