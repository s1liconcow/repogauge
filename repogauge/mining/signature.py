"""Environment signature helpers for stable dataset versioning."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from repogauge.lang import find_adapter


REPO_VERSION_UNKNOWN = "repover_unknown"


def _as_sorted_unique(values: Iterable[str]) -> list[str]:
    return sorted({str(v).strip() for v in values if str(v).strip()})


def _dependency_hash(parts: dict[str, Any]) -> str:
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return digest[:16]


def build_environment_signature(profile: dict[str, Any]) -> dict[str, Any]:
    repo_root = Path(profile.get("repo_root", "")).resolve()
    language_name = str(profile.get("language", "python")).strip() or "python"
    adapter = find_adapter(language_name)
    python_hints = profile.get("python_hints", {}) or {}
    language_hints = profile.get("language_hints", {}) or {}
    test_runner_hints = profile.get("test_runner_hints", {}) or {}

    hint_source = language_hints or python_hints
    python_versions = _as_sorted_unique(hint_source.get("versions", []))
    package_managers = _as_sorted_unique(hint_source.get("package_managers", []))
    install_cmds = _as_sorted_unique(
        profile.get("install_hints", []) or hint_source.get("install_hints", [])
    )
    test_commands = _as_sorted_unique(
        test_runner_hints.get("commands", []) or hint_source.get("test_commands", [])
    )
    package_style = str(
        hint_source.get("package_style", profile.get("package_style", "unknown"))
    ).strip() or "unknown"
    repo_name = str(profile.get("repo_name", "")).strip()
    repo_version = str(profile.get("repo_version", "")).strip() or REPO_VERSION_UNKNOWN

    dependency_payload = {
        "package_managers": package_managers,
        "install_cmds": install_cmds,
        "test_commands": test_commands,
        "package_style": package_style,
        "requirements": adapter.dependency_signature_inputs(repo_root, profile),
    }
    fingerprint = _dependency_hash(dependency_payload)

    signature_labels = adapter.signature_labels(profile)
    runtime_label = (
        str(signature_labels.get("runtime_label", "pyunknown")).strip()
        or "pyunknown"
    )
    test_label = (
        str(signature_labels.get("test_label", "testunknown")).strip()
        or "testunknown"
    )
    package_label = (
        str(signature_labels.get("package_label", "pkgunknown")).strip()
        or "pkgunknown"
    )

    return {
        "repo_name": repo_name,
        "repo_version": repo_version,
        "python_versions": python_versions,
        "package_style": package_style,
        "package_managers": package_managers,
        "install_cmds": install_cmds,
        "test_commands": test_commands,
        "dependency_signature": fingerprint,
        "signature": f"{repo_version}__{runtime_label}__{test_label}__{package_label}__reqhash_{fingerprint}",
        "version": f"{repo_version}__{runtime_label}__{test_label}__{package_label}__reqhash_{fingerprint}",
    }


__all__ = ["REPO_VERSION_UNKNOWN", "build_environment_signature"]
