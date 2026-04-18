"""Rust language adapter helpers."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from repogauge.lang._rust_test_parser import parse_cargo_human
from repogauge.mining.signature import REPO_VERSION_UNKNOWN
from repogauge.validation.env_detect import EnvPlan

from . import DetectionResult, FileRoleRules

try:
    import tomllib
except Exception:  # pragma: no cover
    import tomli as tomllib  # type: ignore[import-not-found]

_DEFAULT_RUST_VERSION = "stable"
_DEFAULT_TEST_CMD = "cargo test --no-fail-fast"


def _sorted_unique(values: Any) -> list[str]:
    return sorted({str(value).strip() for value in values if str(value).strip()})


def _coerce_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        payload = tomllib.loads(_safe_read_text(path))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _cargo_metadata(repo_root: Path) -> dict[str, Any]:
    cargo_toml = _read_toml(repo_root / "Cargo.toml")
    package = _coerce_mapping(cargo_toml.get("package"))
    workspace = _coerce_mapping(cargo_toml.get("workspace"))
    workspace_package = _coerce_mapping(workspace.get("package"))
    workspace_members = workspace.get("members")
    if not isinstance(workspace_members, list):
        workspace_members = []
    return {
        "name": str(package.get("name") or "").strip(),
        "version": str(package.get("version") or workspace_package.get("version") or "").strip()
        or REPO_VERSION_UNKNOWN,
        "edition": str(package.get("edition") or "").strip(),
        "workspace_members": [str(member).strip() for member in workspace_members if str(member).strip()],
        "is_workspace": bool(workspace),
    }


def _toolchain_channel(repo_root: Path) -> str:
    toolchain_toml = _read_toml(repo_root / "rust-toolchain.toml")
    if toolchain_toml:
        toolchain = _coerce_mapping(toolchain_toml.get("toolchain"))
        channel = str(toolchain.get("channel") or "").strip()
        if channel:
            return channel
    legacy = _safe_read_text(repo_root / "rust-toolchain").strip()
    return legacy or _DEFAULT_RUST_VERSION


def build_env_plan(profile: Any) -> EnvPlan:
    payload = _coerce_mapping(profile)
    language_hints = _coerce_mapping(payload.get("language_hints"))
    runtime_version = str(
        payload.get("language_version")
        or language_hints.get("runtime_version")
        or _DEFAULT_RUST_VERSION
    ).strip() or _DEFAULT_RUST_VERSION
    return EnvPlan(
        language="rust",
        runtime_version=runtime_version,
        python_version="",
        pre_install=[],
        install=["cargo fetch"],
        build=[],
        test_cmd_base=_DEFAULT_TEST_CMD,
        strategy_name="cargo:cargo-test",
        confidence=0.9,
        provenance=["cargo"],
    )


class RustAdapter:
    def name(self) -> str:
        return "rust"

    def detect(self, repo_root: Path) -> DetectionResult:
        cargo_toml = repo_root / "Cargo.toml"
        if not cargo_toml.exists():
            return DetectionResult(language="rust", confidence=0.0, signals=[])
        metadata = _cargo_metadata(repo_root)
        signals = ["Cargo.toml"]
        if metadata["is_workspace"]:
            signals.append("workspace")
        return DetectionResult(
            language="rust",
            confidence=1.0,
            signals=signals,
            runtime_version=None,
        )

    def inspect(self, repo_root: Path) -> dict[str, Any]:
        metadata = _cargo_metadata(repo_root)
        runtime_version = _toolchain_channel(repo_root)
        signals = ["Cargo.toml"]
        if metadata["is_workspace"]:
            signals.append("workspace")
        if (repo_root / "rust-toolchain.toml").exists():
            signals.append("rust-toolchain.toml")
        elif (repo_root / "rust-toolchain").exists():
            signals.append("rust-toolchain")

        language_hints = {
            "edition": metadata["edition"],
            "runtime_version": runtime_version,
            "versions": [runtime_version],
            "workspace_members": metadata["workspace_members"],
            "signals": signals,
            "package_managers": ["cargo"],
            "package_style": "cargo",
        }
        return {
            "language": "rust",
            "language_version": runtime_version,
            "repo_version": metadata["version"],
            "language_hints": language_hints,
            "install_hints": ["cargo fetch"],
            "test_runner_hints": {"commands": [_DEFAULT_TEST_CMD], "signals": []},
            "test_paths": ["tests"] if (repo_root / "tests").exists() else [],
            "profile_warnings": [],
        }

    def build_env_plan(self, profile: dict[str, Any]) -> EnvPlan:
        return build_env_plan(profile)

    def parse_test_output(
        self, report: object, test_spec: object | None
    ) -> dict[str, str]:
        return parse_cargo_human(report, test_spec)

    def file_role_rules(self) -> FileRoleRules:
        return FileRoleRules(
            prod_extensions={".rs"},
            test_filename_patterns=[],
            test_dir_names={"tests", "benches"},
            config_build_filenames={
                "Cargo.toml",
                "Cargo.lock",
                "rust-toolchain",
                "rust-toolchain.toml",
                "rustfmt.toml",
                "clippy.toml",
            },
            vendor_dir_names={"target"},
        )

    def harness_template_vars(self, spec: dict[str, Any]) -> dict[str, Any]:
        return {
            "parser_import": "repogauge.lang._rust_test_parser.parse_cargo_human",
            "parser_name": "cargo_human",
            "ext": "rs",
            "install_str_join": " && ",
        }

    def signature_labels(self, profile: dict[str, Any]) -> dict[str, str]:
        language_hints = _coerce_mapping(profile.get("language_hints"))
        edition = str(language_hints.get("edition") or "").strip() or "unknown"
        runtime_version = str(language_hints.get("runtime_version") or _DEFAULT_RUST_VERSION)
        return {
            "runtime_label": f"rust-{runtime_version}",
            "test_label": "cargo-test",
            "package_label": edition,
        }

    def dependency_signature_inputs(
        self, repo_root: Path, profile: dict[str, Any]
    ) -> list[str]:
        inputs: list[str] = []
        for candidate in ("Cargo.toml", "Cargo.lock", "rust-toolchain", "rust-toolchain.toml"):
            path = repo_root / candidate
            if path.exists():
                inputs.append(f"{candidate}\n{_safe_read_text(path).strip()}")
        return _sorted_unique(inputs)

    def env_overrides(self, worktree: Path) -> dict[str, str]:
        return {
            "CARGO_HOME": str(worktree / ".cargo"),
            "CARGO_TARGET_DIR": str(worktree / "target"),
        }

    def test_command_attempts(self, test_cmd_base: str) -> list[list[str]]:
        return [shlex.split(test_cmd_base.strip() or _DEFAULT_TEST_CMD)]

    def test_report_filename(self) -> str | None:
        return None

    def test_report_glob(self) -> str | None:
        return None


__all__ = ["RustAdapter", "build_env_plan"]
