"""Provider configuration normalization and secret resolution helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

PROVIDER_KIND_ANTHROPIC_API = "anthropic_api"
PROVIDER_KIND_CLAUDE_CLI = "claude_cli"
PROVIDER_KIND_OPENAI_RESPONSES = "openai_responses"
PROVIDER_KIND_CODEX_CLI = "codex_cli"
PROVIDER_KIND_OPENCODE_CLI = "opencode_cli"
PROVIDER_KIND_OPENCODE_SERVER = "opencode_server"
PROVIDER_KIND_OPENAI_COMPATIBLE = "openai_compatible"
PROVIDER_KIND_MOCK = "mock"

SUPPORTED_PROVIDER_KINDS = {
    PROVIDER_KIND_ANTHROPIC_API,
    PROVIDER_KIND_CLAUDE_CLI,
    PROVIDER_KIND_OPENAI_RESPONSES,
    PROVIDER_KIND_CODEX_CLI,
    PROVIDER_KIND_OPENCODE_CLI,
    PROVIDER_KIND_OPENCODE_SERVER,
    PROVIDER_KIND_OPENAI_COMPATIBLE,
    PROVIDER_KIND_MOCK,
}

# Backward-compatibility aliases used by matrix snippets.
PROVIDER_KIND_ALIAS = {
    "local": PROVIDER_KIND_MOCK,
}

REDACTION_PLACEHOLDER = "<redacted>"
_SENSITIVE_PROVIDER_KEYS = {
    "access_token",
    "api_key",
    "auth_token",
    "client_secret",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "token",
}
_SENSITIVE_PROVIDER_SUFFIXES = (
    "_access_token",
    "_api_key",
    "_auth_token",
    "_client_secret",
    "_password",
    "_private_key",
    "_refresh_token",
    "_secret",
    "_token",
)


class ProviderConfigurationError(ValueError):
    """Raised when provider normalization fails."""


@dataclass(frozen=True)
class ProviderConfig:
    """Normalized provider configuration used by matrix planning."""

    provider_id: str
    kind: str
    resolved: dict[str, Any]
    redacted: dict[str, Any]
    raw: dict[str, Any]

    def to_run_manifest_dict(self) -> dict[str, Any]:
        """Return a manifest-safe provider snapshot."""
        return {
            "provider_id": self.provider_id,
            "kind": self.kind,
            "config": self.redacted,
        }


def _coerce_text(value: Any, *, field_name: str) -> str:
    if value is None:
        raise ProviderConfigurationError(f"{field_name} cannot be null")
    if not isinstance(value, str):
        raise ProviderConfigurationError(
            f"{field_name} must be a string, got {type(value).__name__}"
        )
    candidate = value.strip()
    if not candidate:
        raise ProviderConfigurationError(f"{field_name} cannot be empty")
    return candidate


def _normalize_provider_kind(value: Any) -> str:
    candidate = _coerce_text(value, field_name="provider.kind").lower()
    if candidate in PROVIDER_KIND_ALIAS:
        return PROVIDER_KIND_ALIAS[candidate]
    if candidate not in SUPPORTED_PROVIDER_KINDS:
        raise ProviderConfigurationError(f"unsupported provider kind: {value}")
    return candidate


def _read_secret_from_env(name: Any) -> str:
    if not isinstance(name, str):
        raise ProviderConfigurationError("provider auth env reference must be a string")
    env_name = name.strip()
    if not env_name:
        raise ProviderConfigurationError("provider env reference cannot be empty")
    value = os.getenv(env_name)
    if not value:
        raise ProviderConfigurationError(
            f"missing required environment variable: {env_name}"
        )
    return value


def _read_secret_from_file(path_value: Any, *, root: Path) -> str:
    if not isinstance(path_value, str):
        raise ProviderConfigurationError(
            "provider auth file reference must be a string"
        )

    path = Path(path_value.strip())
    if not path.is_absolute():
        path = root / path

    if not path.exists():
        raise ProviderConfigurationError(f"provider secret file not found: {path}")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ProviderConfigurationError(f"provider secret file is empty: {path}")
    return value


def _resolve_secret_value(value: Any, *, root: Path) -> tuple[bool, Any]:
    if not isinstance(value, str):
        return False, value

    candidate = value.strip()
    if not candidate:
        return False, value

    lower = candidate.lower()
    if lower.startswith("env:"):
        return True, _read_secret_from_env(candidate[4:])
    if lower.startswith("file:"):
        return True, _read_secret_from_file(candidate[5:], root=root)

    return False, value


def _is_sensitive_provider_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False

    normalized = key.strip().lower()
    if not normalized:
        return False
    if normalized in _SENSITIVE_PROVIDER_KEYS:
        return True
    return any(normalized.endswith(suffix) for suffix in _SENSITIVE_PROVIDER_SUFFIXES)


def _coerce_mapping(value: Any, *, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProviderConfigurationError(f"{field_name} must be a mapping")
    return dict(value)


def normalize_provider(
    provider_id: str, raw: Any, *, matrix_root: Path
) -> ProviderConfig:
    payload = _coerce_mapping(raw, field_name=f"provider '{provider_id}'")

    provider_id = _coerce_text(provider_id, field_name="provider id")
    kind = _normalize_provider_kind(payload.get("kind", PROVIDER_KIND_MOCK))

    resolved: dict[str, Any] = {}
    redacted: dict[str, Any] = {}

    for key, value in payload.items():
        if key == "kind":
            continue

        if key.endswith("_env"):
            secret_key = key[: -len("_env")]
            resolved[secret_key] = _read_secret_from_env(value)
            redacted[secret_key] = REDACTION_PLACEHOLDER
            continue

        if key.endswith("_file"):
            secret_key = key[: -len("_file")]
            resolved[secret_key] = _read_secret_from_file(value, root=matrix_root)
            redacted[secret_key] = REDACTION_PLACEHOLDER
            continue

        resolved_flag, resolved_value = _resolve_secret_value(value, root=matrix_root)
        if resolved_flag:
            resolved[key] = resolved_value
            redacted[key] = REDACTION_PLACEHOLDER
            continue

        resolved[key] = value
        redacted[key] = (
            REDACTION_PLACEHOLDER if _is_sensitive_provider_key(key) else value
        )

    return ProviderConfig(
        provider_id=provider_id,
        kind=kind,
        resolved=dict(resolved),
        redacted=dict(redacted),
        raw=dict(payload),
    )


__all__ = [
    "PROVIDER_KIND_ANTHROPIC_API",
    "PROVIDER_KIND_CLAUDE_CLI",
    "PROVIDER_KIND_OPENAI_RESPONSES",
    "PROVIDER_KIND_CODEX_CLI",
    "PROVIDER_KIND_OPENCODE_CLI",
    "PROVIDER_KIND_OPENCODE_SERVER",
    "PROVIDER_KIND_OPENAI_COMPATIBLE",
    "PROVIDER_KIND_MOCK",
    "SUPPORTED_PROVIDER_KINDS",
    "REDACTION_PLACEHOLDER",
    "ProviderConfigurationError",
    "ProviderConfig",
    "normalize_provider",
]
