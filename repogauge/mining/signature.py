"""Environment signature helpers for stable dataset versioning."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable


REPO_VERSION_UNKNOWN = "repover_unknown"


def _as_sorted_unique(values: Iterable[str]) -> list[str]:
    return sorted({str(v).strip() for v in values if str(v).strip()})


def _to_test_label(commands: list[str]) -> str:
    if not commands:
        return "testunknown"
    return "+".join(commands)


def _to_pkg_label(managers: list[str]) -> str:
    if not managers:
        return "pkgunknown"
    return "+".join(managers)


def _to_python_label(versions: list[str]) -> str:
    if not versions:
        return "pyunknown"
    return "_".join(f"py{v.replace('.', '')}" for v in versions)


def _dependency_hash(parts: dict[str, Any]) -> str:
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return digest[:16]


def _normalize_dependency_lines(raw: str) -> list[str]:
    lines: list[str] = []
    for raw_line in raw.splitlines():
        value = raw_line.strip()
        if not value or value.startswith("#"):
            continue
        value = value.split("#", 1)[0].strip()
        if value:
            lines.append(value)
    return lines


def _read_requirements_signature(repo_root: Path, profile: dict[str, Any]) -> list[str]:
    if not repo_root.exists():
        if isinstance(profile.get("package_style"), str):
            return _as_sorted_unique([str(profile.get("package_style"))])
        return []

    requirements: list[str] = []
    for candidate in sorted(
        (
            repo_root / "requirements.txt",
            repo_root / "requirements-dev.txt",
            repo_root / "dev-requirements.txt",
        )
    ):
        if not candidate.exists():
            continue
        try:
            normalized_lines = _normalize_dependency_lines(
                candidate.read_text(encoding="utf-8")
            )
            requirements.append("\n".join(_as_sorted_unique(normalized_lines)))
        except OSError:
            requirements.append("")
    pyproject = repo_root / "pyproject.toml"
    if pyproject.exists():
        try:
            normalized_lines = _normalize_dependency_lines(
                pyproject.read_text(encoding="utf-8")
            )
            requirements.append("\n".join(_as_sorted_unique(normalized_lines)))
        except OSError:
            requirements.append("")
    setup_cfg = repo_root / "setup.cfg"
    if setup_cfg.exists():
        try:
            normalized_lines = _normalize_dependency_lines(
                setup_cfg.read_text(encoding="utf-8")
            )
            requirements.append("\n".join(_as_sorted_unique(normalized_lines)))
        except OSError:
            requirements.append("")
    setup_py = repo_root / "setup.py"
    if setup_py.exists():
        try:
            normalized_lines = _normalize_dependency_lines(
                setup_py.read_text(encoding="utf-8")
            )
            requirements.append("\n".join(_as_sorted_unique(normalized_lines)))
        except OSError:
            requirements.append("")
    if not requirements and isinstance(profile.get("package_style"), str):
        requirements.append(profile["package_style"])
    return _as_sorted_unique(requirements)


def build_environment_signature(profile: dict[str, Any]) -> dict[str, Any]:
    repo_root = Path(profile.get("repo_root", "")).resolve()
    python_hints = profile.get("python_hints", {}) or {}
    test_runner_hints = profile.get("test_runner_hints", {}) or {}

    python_versions = _as_sorted_unique(python_hints.get("versions", []))
    package_managers = _as_sorted_unique(python_hints.get("package_managers", []))
    install_cmds = _as_sorted_unique(profile.get("install_hints", []))
    test_commands = _as_sorted_unique(test_runner_hints.get("commands", []))
    package_style = (
        str(python_hints.get("package_style", "unknown")).strip() or "unknown"
    )
    repo_name = str(profile.get("repo_name", "")).strip()
    repo_version = str(profile.get("repo_version", "")).strip() or REPO_VERSION_UNKNOWN

    dependency_payload = {
        "package_managers": package_managers,
        "install_cmds": install_cmds,
        "test_commands": test_commands,
        "package_style": package_style,
        "requirements": _read_requirements_signature(repo_root, profile),
    }
    fingerprint = _dependency_hash(dependency_payload)

    python_label = _to_python_label(python_versions)
    test_label = _to_test_label(test_commands)
    package_label = _to_pkg_label(package_managers)

    return {
        "repo_name": repo_name,
        "repo_version": repo_version,
        "python_versions": python_versions,
        "package_style": package_style,
        "package_managers": package_managers,
        "install_cmds": install_cmds,
        "test_commands": test_commands,
        "dependency_signature": fingerprint,
        "signature": f"{repo_version}__{python_label}__{test_label}__{package_label}__reqhash_{fingerprint}",
        "version": f"{repo_version}__{python_label}__{test_label}__{package_label}__reqhash_{fingerprint}",
    }


__all__ = ["REPO_VERSION_UNKNOWN", "build_environment_signature"]
