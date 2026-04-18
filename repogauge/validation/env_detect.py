"""Deterministic environment plan contracts and dispatch."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict


@dataclass(frozen=True)
class EnvPlan:
    """Concrete execution plan for repository validation."""

    language: str = "python"
    runtime_version: str = ""
    python_version: str = ""
    pre_install: list[str] = field(default_factory=list)
    install: list[str] = field(default_factory=list)
    build: list[str] = field(default_factory=list)
    test_cmd_base: str = ""
    strategy_name: str = ""
    confidence: float = 0.0
    provenance: list[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        return payload


def build_environment_plan(profile: Any) -> EnvPlan:
    """Build a deterministic environment plan from an inspection profile."""

    payload = profile if isinstance(profile, dict) else {}
    language = payload.get("language", "python")
    if not isinstance(language, str):
        language = "python"
    language = language.strip().lower() or "python"

    from repogauge.lang import find_adapter

    return find_adapter(language).build_env_plan(profile)


__all__ = ["EnvPlan", "build_environment_plan"]
