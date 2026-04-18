"""Go language adapter helpers."""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Any

from repogauge.lang._go_test_parser import parse_go_test_json
from repogauge.mining.signature import REPO_VERSION_UNKNOWN
from repogauge.validation.env_detect import EnvPlan

from . import DetectionResult, FileRoleRules

_DEFAULT_GO_VERSION = "1.22"
_DEFAULT_TEST_CMD = "go test -json ./..."


def _sorted_unique(values: Any) -> list[str]:
    return sorted({str(value).strip() for value in values if str(value).strip()})


def _coerce_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _has_go_source(repo_root: Path, *, max_depth: int = 4) -> bool:
    def walk(path: Path, depth: int) -> bool:
        if depth > max_depth:
            return False
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    if entry.name.startswith(".git"):
                        continue
                    if entry.is_file() and entry.name.endswith(".go"):
                        return True
                    if entry.is_dir() and walk(Path(entry.path), depth + 1):
                        return True
        except OSError:
            return False
        return False

    return walk(repo_root, 0)


def _parse_go_mod(repo_root: Path) -> dict[str, Any]:
    go_mod = repo_root / "go.mod"
    raw = _safe_read_text(go_mod)
    module_path = ""
    runtime_version = _DEFAULT_GO_VERSION
    require_count = 0
    in_require_block = False
    for line in raw.splitlines():
        stripped = line.split("//", 1)[0].strip()
        if not stripped:
            continue
        if stripped.startswith("module "):
            module_path = stripped.split(None, 1)[1].strip()
            continue
        if stripped.startswith("go "):
            version_match = re.search(r"\d+\.\d+", stripped)
            if version_match:
                runtime_version = version_match.group(0)
            continue
        if stripped.startswith("require ("):
            in_require_block = True
            continue
        if in_require_block and stripped == ")":
            in_require_block = False
            continue
        if in_require_block:
            require_count += 1
            continue
        if stripped.startswith("require "):
            require_count += 1

    repo_version = REPO_VERSION_UNKNOWN
    module_match = re.search(r"/v(\d+)$", module_path)
    if module_match:
        repo_version = f"v{module_match.group(1)}"

    return {
        "module_path": module_path,
        "runtime_version": runtime_version,
        "require_count": require_count,
        "repo_version": repo_version,
    }


def build_env_plan(profile: Any) -> EnvPlan:
    payload = _coerce_mapping(profile)
    language_hints = _coerce_mapping(payload.get("language_hints"))
    signals = _sorted_unique(language_hints.get("signals", []))
    vendor_present = "vendor" in signals
    runtime_version = str(
        payload.get("language_version")
        or language_hints.get("runtime_version")
        or _DEFAULT_GO_VERSION
    ).strip() or _DEFAULT_GO_VERSION

    return EnvPlan(
        language="go",
        runtime_version=runtime_version,
        python_version="",
        pre_install=[],
        install=[] if vendor_present else ["go mod download"],
        build=[],
        test_cmd_base=_DEFAULT_TEST_CMD,
        strategy_name="go-modules:go-test",
        confidence=0.9,
        provenance=["vendor" if vendor_present else "go-mod-download"],
    )


class GoAdapter:
    def name(self) -> str:
        return "go"

    def detect(self, repo_root: Path) -> DetectionResult:
        if (repo_root / "go.mod").exists():
            return DetectionResult(
                language="go",
                confidence=1.0,
                signals=["go.mod"],
                runtime_version=None,
            )
        if _has_go_source(repo_root):
            return DetectionResult(
                language="go",
                confidence=0.6,
                signals=["go-source"],
                runtime_version=None,
            )
        return DetectionResult(language="go", confidence=0.0, signals=[])

    def inspect(self, repo_root: Path) -> dict[str, Any]:
        metadata = _parse_go_mod(repo_root)
        signals = ["go.mod"]
        if (repo_root / "go.sum").exists():
            signals.append("go.sum")
        if (repo_root / "vendor").is_dir():
            signals.append("vendor")

        language_hints = {
            "module_path": metadata["module_path"],
            "runtime_version": metadata["runtime_version"],
            "versions": [metadata["runtime_version"]],
            "signals": signals,
            "require_count": metadata["require_count"],
            "package_managers": ["go-modules"],
            "package_style": "go-modules",
        }
        return {
            "language": "go",
            "language_version": metadata["runtime_version"],
            "repo_version": metadata["repo_version"],
            "language_hints": language_hints,
            "install_hints": [] if "vendor" in signals else ["go mod download"],
            "test_runner_hints": {"commands": [_DEFAULT_TEST_CMD], "signals": []},
            "test_paths": [],
            "profile_warnings": [],
        }

    def build_env_plan(self, profile: dict[str, Any]) -> EnvPlan:
        return build_env_plan(profile)

    def parse_test_output(
        self, report: object, test_spec: object | None
    ) -> dict[str, str]:
        return parse_go_test_json(report, test_spec)

    def file_role_rules(self) -> FileRoleRules:
        return FileRoleRules(
            prod_extensions={".go"},
            test_filename_patterns=["*_test.go"],
            test_dir_names=set(),
            config_build_filenames={
                "go.mod",
                "go.sum",
                "go.work",
                "go.work.sum",
                ".golangci.yml",
                ".golangci.yaml",
            },
            vendor_dir_names={"vendor"},
        )

    def harness_template_vars(self, spec: dict[str, Any]) -> dict[str, Any]:
        return {
            "parser_import": "repogauge.lang._go_test_parser.parse_go_test_json",
            "parser_name": "go_json",
            "ext": "go",
            "install_str_join": " && ",
        }

    def signature_labels(self, profile: dict[str, Any]) -> dict[str, str]:
        language_hints = _coerce_mapping(profile.get("language_hints"))
        runtime_version = str(language_hints.get("runtime_version") or _DEFAULT_GO_VERSION)
        digits = runtime_version.replace(".", "")
        return {
            "runtime_label": f"go{digits}",
            "test_label": "go-test",
            "package_label": "go-modules",
        }

    def dependency_signature_inputs(
        self, repo_root: Path, profile: dict[str, Any]
    ) -> list[str]:
        inputs: list[str] = []
        for candidate in ("go.mod", "go.sum", "go.work", "go.work.sum"):
            path = repo_root / candidate
            if path.exists():
                inputs.append(f"{candidate}\n{_safe_read_text(path).strip()}")
        return _sorted_unique(inputs)

    def env_overrides(self, worktree: Path) -> dict[str, str]:
        return {"GOCACHE": str(worktree / ".gocache")}

    def test_command_attempts(self, test_cmd_base: str) -> list[list[str]]:
        return [shlex.split(test_cmd_base.strip() or _DEFAULT_TEST_CMD)]

    def test_report_filename(self) -> str | None:
        return None

    def test_report_glob(self) -> str | None:
        return None


__all__ = ["GoAdapter", "build_env_plan"]
