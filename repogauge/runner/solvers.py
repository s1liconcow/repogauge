"""Solver behavior/config normalization and adapter compatibility checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .providers import (
    PROVIDER_KIND_ANTHROPIC_API,
    PROVIDER_KIND_CODEX_CLI,
    PROVIDER_KIND_MOCK,
    PROVIDER_KIND_OPENAI_COMPATIBLE,
    PROVIDER_KIND_OPENAI_RESPONSES,
    PROVIDER_KIND_OPENCODE_SERVER,
)


class SolverConfigurationError(ValueError):
    """Raised when solver normalization fails."""


SOLVER_ADAPTER_CLAUDE = "claude_agent_sdk"
SOLVER_ADAPTER_OPENAI_RESPONSES = "openai_responses"
SOLVER_ADAPTER_CODEX_CLI = "codex_cli"
SOLVER_ADAPTER_OPEN_CODEX_SERVER = "opencode_server"
SOLVER_ADAPTER_OPENAI_COMPATIBLE = "openai_compatible"
SOLVER_ADAPTER_MOCK = "mock"


SOLVER_ADAPTER_ALIAS = {
    "claude": SOLVER_ADAPTER_CLAUDE,
    "codex": SOLVER_ADAPTER_CODEX_CLI,
    "opencode": SOLVER_ADAPTER_OPEN_CODEX_SERVER,
    "openai": SOLVER_ADAPTER_OPENAI_RESPONSES,
    "mock": SOLVER_ADAPTER_MOCK,
}

SOLVER_ADAPTER_COMPATIBILITY = {
    PROVIDER_KIND_ANTHROPIC_API: {
        SOLVER_ADAPTER_CLAUDE,
    },
    PROVIDER_KIND_OPENAI_RESPONSES: {
        SOLVER_ADAPTER_OPENAI_RESPONSES,
    },
    PROVIDER_KIND_CODEX_CLI: {
        SOLVER_ADAPTER_CODEX_CLI,
    },
    PROVIDER_KIND_OPENCODE_SERVER: {
        SOLVER_ADAPTER_OPEN_CODEX_SERVER,
    },
    PROVIDER_KIND_OPENAI_COMPATIBLE: {
        SOLVER_ADAPTER_OPENAI_COMPATIBLE,
    },
    PROVIDER_KIND_MOCK: {
        SOLVER_ADAPTER_MOCK,
    },
}

DEFAULT_SOLVER_ADAPTER_BY_PROVIDER = {
    PROVIDER_KIND_ANTHROPIC_API: SOLVER_ADAPTER_CLAUDE,
    PROVIDER_KIND_OPENAI_RESPONSES: SOLVER_ADAPTER_OPENAI_RESPONSES,
    PROVIDER_KIND_CODEX_CLI: SOLVER_ADAPTER_CODEX_CLI,
    PROVIDER_KIND_OPENCODE_SERVER: SOLVER_ADAPTER_OPEN_CODEX_SERVER,
    PROVIDER_KIND_OPENAI_COMPATIBLE: SOLVER_ADAPTER_OPENAI_COMPATIBLE,
    PROVIDER_KIND_MOCK: SOLVER_ADAPTER_MOCK,
}

KNOWN_SOLVER_FIELDS = {"id", "provider", "adapter", "prompt_policy", "tool_policy"}


def _coerce_text(value: Any, *, field_name: str) -> str:
    if value is None:
        raise SolverConfigurationError(f"{field_name} cannot be null")
    if not isinstance(value, str):
        raise SolverConfigurationError(
            f"{field_name} must be a string, got {type(value).__name__}"
        )
    candidate = value.strip()
    if not candidate:
        raise SolverConfigurationError(f"{field_name} cannot be empty")
    return candidate


def _coerce_mapping(value: Any, *, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SolverConfigurationError(f"{field_name} must be a mapping")
    return dict(value)


@dataclass(frozen=True)
class SolverConfig:
    """Normalized solver configuration."""

    solver_id: str
    provider_id: str
    adapter: str
    prompt_policy: dict[str, Any]
    tool_policy: dict[str, Any]
    behavior: dict[str, Any]
    raw: dict[str, Any]

    def to_run_manifest_dict(self) -> dict[str, Any]:
        return {
            "solver_id": self.solver_id,
            "provider_id": self.provider_id,
            "adapter": self.adapter,
            "prompt_policy": self.prompt_policy,
            "tool_policy": self.tool_policy,
            "config": self.behavior,
        }


def _normalize_adapter(value: Any, *, provider_id: str, provider_kind: str) -> str:
    if value is None:
        adapter = DEFAULT_SOLVER_ADAPTER_BY_PROVIDER[provider_kind]
    else:
        adapter = _coerce_text(
            value, field_name=f"solver '{provider_id}'.adapter"
        ).lower()

        if adapter in SOLVER_ADAPTER_ALIAS:
            adapter = SOLVER_ADAPTER_ALIAS[adapter]

    compatibility = SOLVER_ADAPTER_COMPATIBILITY.get(provider_kind)
    if compatibility is None:
        raise SolverConfigurationError(
            f"provider '{provider_id}' has unsupported kind '{provider_kind}'"
        )
    if adapter not in compatibility:
        raise SolverConfigurationError(
            f"solver '{provider_id}' adapter '{adapter}' is incompatible with "
            f"provider kind '{provider_kind}'"
        )
    return adapter


def normalize_solver(
    raw: Any,
    *,
    provider_kinds: Mapping[str, str],
) -> SolverConfig:
    payload = _coerce_mapping(raw, field_name="solver")
    solver_id = _coerce_text(payload.get("id"), field_name="solver.id")
    provider_id = _coerce_text(payload.get("provider"), field_name="solver.provider")

    if provider_id not in provider_kinds:
        raise SolverConfigurationError(
            f"solver '{solver_id}' references unknown provider '{provider_id}'"
        )

    provider_kind = provider_kinds[provider_id]
    adapter = _normalize_adapter(
        payload.get("adapter"),
        provider_id=solver_id,
        provider_kind=provider_kind,
    )

    prompt_policy = _coerce_mapping(
        payload.get("prompt_policy"), field_name="solver.prompt_policy"
    )
    tool_policy = _coerce_mapping(
        payload.get("tool_policy"), field_name="solver.tool_policy"
    )

    behavior = dict(payload)
    for field in KNOWN_SOLVER_FIELDS:
        behavior.pop(field, None)

    return SolverConfig(
        solver_id=solver_id,
        provider_id=provider_id,
        adapter=adapter,
        prompt_policy=prompt_policy,
        tool_policy=tool_policy,
        behavior=behavior,
        raw=payload,
    )
