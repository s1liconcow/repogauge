"""Deterministic environment detection heuristics.

This module converts inspection hints into an explicit environment plan that can be
consumed by later deterministic validation stages.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable


_DEFAULT_PYTHON_VERSION = "3.11"
_DEFAULT_TEST_CMD = "python -m pytest"


@dataclass(frozen=True)
class EnvPlan:
    """Concrete execution plan for repository validation."""

    python_version: str
    pre_install: list[str]
    install: list[str]
    build: list[str]
    test_cmd_base: str
    strategy_name: str
    confidence: float
    provenance: list[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        return payload


def _sorted_unique(values: Iterable[str]) -> list[str]:
    return sorted({str(value).strip() for value in values if str(value).strip()})


def _coerce_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _coerce_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return _sorted_unique(value)
    return []


def _version_tuple(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (999,)


def _choose_python_version(
    versions: list[str], provenance: list[str]
) -> tuple[str, float]:
    if not versions:
        provenance.append("python_version:default-3.11")
        return _DEFAULT_PYTHON_VERSION, 0.75

    normalized = _sorted_unique(versions)
    if len(normalized) > 1:
        provenance.append("python_version:conflict")
        provenance.append("python_version:chose-minimum")
        # Deterministic tie-break: pick the minimum by semantic version ordering.
        return min(normalized, key=_version_tuple), 0.6

    provenance.append(f"python_version:{normalized[0]}")
    return normalized[0], 1.0


def _build_test_command(
    test_commands: list[str], provenance: list[str]
) -> tuple[str, float, str]:
    if "pytest" in test_commands:
        provenance.append("test_runner:pytest")
        return "pytest", 1.0, "pytest"

    if "python -m unittest" in test_commands:
        provenance.append("test_runner:unittest")
        return "python -m unittest", 0.9, "unittest"

    if "tox" in test_commands:
        provenance.append("test_runner:tox")
        return "tox", 0.7, "tox"

    if "nox" in test_commands:
        provenance.append("test_runner:nox")
        return "nox", 0.55, "nox"

    provenance.append("test_runner:default")
    return _DEFAULT_TEST_CMD, 0.5, "pytest-default"


def _build_install_commands(
    package_managers: list[str],
    install_hints: list[str],
    provenance: list[str],
) -> tuple[list[str], list[str], float, str]:
    build: list[str] = []

    if "poetry" in package_managers:
        provenance.append("install_strategy:poetry")
        return ["poetry install"], build, 1.0, "poetry"

    if "uv" in package_managers:
        provenance.append("install_strategy:uv")
        return ["uv sync"], build, 0.95, "uv"

    if "pipenv" in package_managers:
        provenance.append("install_strategy:pipenv")
        return ["pipenv install"], build, 0.9, "pipenv"

    if "setuptools" in package_managers:
        provenance.append("install_strategy:setuptools")
        return ["pip install -e ."], build, 0.88, "setuptools"

    requirements_commands = [
        hint
        for hint in install_hints
        if hint.startswith("pip install -r ") and "requirements" in hint
    ]
    if requirements_commands:
        # Do not blindly run every requirements file; prefer a single deterministic file.
        selected = _sorted_unique(requirements_commands)[0]
        provenance.append("install_strategy:requirements")
        provenance.append(f"install_file:{selected}")
        return [selected], build, 0.75, "requirements"

    if install_hints:
        provenance.append("install_strategy:first-hint")
        return [install_hints[0]], build, 0.6, "fallback"

    provenance.append("install_strategy:editable-default")
    return ["pip install -e ."], build, 0.5, "fallback"


_SELF_MANAGING_INSTALL_PREFIXES = ("poetry install", "uv sync", "pipenv install")


def _augment_for_pytest(
    install: list[str], test_cmd_base: str, provenance: list[str], confidence: float
) -> tuple[list[str], float]:
    if test_cmd_base not in {"pytest", "python -m pytest"}:
        return install, confidence

    # Package managers like poetry, uv, and pipenv install dev dependencies
    # (including pytest) automatically; no separate pip install step is needed.
    if any(
        cmd.startswith(p) for cmd in install for p in _SELF_MANAGING_INSTALL_PREFIXES
    ):
        return install, confidence

    has_pytest_hint = any("pytest" in command for command in install)
    if has_pytest_hint:
        return install, confidence

    provenance.append("install:test-dependency:pytest")
    return install + ["pip install pytest"], max(0.0, confidence - 0.05)


def build_environment_plan(profile: Any) -> EnvPlan:
    """Build a deterministic environment plan from an inspection profile."""

    payload = _coerce_mapping(profile)
    python_hints = _coerce_mapping(payload.get("python_hints"))
    test_hints = _coerce_mapping(payload.get("test_runner_hints"))

    package_managers = _coerce_list(python_hints.get("package_managers"))
    install_hints = _coerce_list(payload.get("install_hints"))
    test_commands = _coerce_list(test_hints.get("commands"))
    versions = _coerce_list(python_hints.get("versions"))

    provenance: list[str] = []
    python_version, python_confidence = _choose_python_version(versions, provenance)

    test_cmd_base, test_confidence, test_name = _build_test_command(
        test_commands, provenance
    )
    install, build_cmds, install_confidence, install_name = _build_install_commands(
        package_managers,
        install_hints,
        provenance,
    )

    install, adjusted_install_confidence = _augment_for_pytest(
        install,
        test_cmd_base,
        provenance,
        install_confidence,
    )

    confidence = min(
        1.0, (python_confidence + test_confidence + adjusted_install_confidence) / 3.0
    )
    strategy_name = f"{install_name}:{test_name}"

    return EnvPlan(
        python_version=python_version,
        pre_install=[],
        install=install,
        build=build_cmds,
        test_cmd_base=test_cmd_base,
        strategy_name=strategy_name,
        confidence=round(confidence, 3),
        provenance=provenance,
    )


__all__ = ["EnvPlan", "build_environment_plan"]
