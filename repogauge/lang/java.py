"""Java language adapter and environment-plan helpers."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from repogauge.mining.signature import REPO_VERSION_UNKNOWN
from repogauge.parsers.junit import parse_repogauge_junit
from repogauge.validation.env_detect import EnvPlan

from . import DetectionResult, FileRoleRules

_DEFAULT_MAVEN_INSTALL_CMD = "mvn -q -DskipTests compile"
_DEFAULT_MAVEN_TEST_CMD = "mvn -q test"
_DEFAULT_GRADLE_INSTALL_CMD = "./gradlew --no-daemon classes"
_DEFAULT_GRADLE_TEST_CMD = "./gradlew --no-daemon test"


def _sorted_unique(values: Any) -> list[str]:
    return sorted({str(value).strip() for value in values if str(value).strip()})


def _coerce_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _coerce_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return _sorted_unique(value)
    return []


def _safe_read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _has_root_file(repo_root: Path, name: str) -> bool:
    return (repo_root / name).is_file()


def _has_any_files(repo_root: Path, pattern: str) -> bool:
    try:
        return any(repo_root.rglob(pattern))
    except OSError:
        return False


def _detect_build_tool(repo_root: Path) -> tuple[str, list[str], float]:
    signals: list[str] = []
    has_maven = _has_root_file(repo_root, "pom.xml")
    has_gradle = False

    for candidate in ("build.gradle", "build.gradle.kts"):
        if _has_root_file(repo_root, candidate):
            has_gradle = True
            signals.append(candidate)

    if has_maven:
        signals.append("pom.xml")

    if has_maven and has_gradle:
        return "maven", _sorted_unique(signals), 0.95
    if has_maven:
        return "maven", _sorted_unique(signals), 1.0
    if has_gradle:
        return "gradle", _sorted_unique(signals), 1.0
    return "", [], 0.0


def _detect_kotlin_present(repo_root: Path) -> bool:
    return _has_root_file(repo_root, "build.gradle.kts") or _has_any_files(
        repo_root, "*.kt"
    )


def _detect_test_paths(repo_root: Path) -> list[str]:
    candidates = (
        "src/test/java",
        "src/test/kotlin",
        "test",
        "tests",
    )
    paths: list[str] = []
    for candidate in candidates:
        path = repo_root / candidate
        if path.exists():
            paths.append(candidate)
    return _sorted_unique(paths)


def _default_install_commands(build_tool: str) -> list[str]:
    if build_tool == "gradle":
        return [_DEFAULT_GRADLE_INSTALL_CMD]
    return [_DEFAULT_MAVEN_INSTALL_CMD]


def _default_test_command(build_tool: str) -> str:
    if build_tool == "gradle":
        return _DEFAULT_GRADLE_TEST_CMD
    return _DEFAULT_MAVEN_TEST_CMD


def _test_label(test_cmd_base: str) -> str:
    normalized = test_cmd_base.strip().lower()
    if not normalized:
        return "test"
    if "gradle" in normalized:
        return "gradle-test"
    if "mvn" in normalized or "maven" in normalized:
        return "maven-test"
    return "test"


def _read_dependency_signature(repo_root: Path, profile: dict[str, Any]) -> list[str]:
    if not repo_root.exists():
        build_tool = ""
        language_hints = _coerce_mapping(profile.get("language_hints"))
        if isinstance(language_hints.get("build_tool"), str):
            build_tool = str(language_hints["build_tool"]).strip().lower()
        return [build_tool] if build_tool else []

    candidates = (
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "settings.gradle.kts",
        "gradle.properties",
        "gradle/wrapper/gradle-wrapper.properties",
        "mvnw",
        "mvnw.cmd",
        "gradlew",
        "gradlew.bat",
    )
    inputs: list[str] = []
    for candidate in candidates:
        path = repo_root / candidate
        if not path.exists():
            continue
        content = _safe_read_text(path).strip()
        inputs.append(f"{candidate}\n{content}")

    if not inputs:
        language_hints = _coerce_mapping(profile.get("language_hints"))
        build_tool = language_hints.get("build_tool")
        if isinstance(build_tool, str) and build_tool.strip():
            inputs.append(build_tool.strip().lower())

    return _sorted_unique(inputs)


def build_env_plan(profile: Any) -> EnvPlan:
    payload = _coerce_mapping(profile)
    language_hints = _coerce_mapping(payload.get("language_hints"))
    test_hints = _coerce_mapping(payload.get("test_runner_hints"))

    build_tool_hint = str(language_hints.get("build_tool") or "").strip().lower()
    has_build_tool_hint = build_tool_hint in {"maven", "gradle"}
    build_tool = build_tool_hint if has_build_tool_hint else "maven"

    kotlin_present = bool(language_hints.get("kotlin_present"))
    test_commands = _coerce_list(
        test_hints.get("commands") or language_hints.get("test_commands")
    )
    install_hints = _coerce_list(payload.get("install_hints"))
    if not install_hints:
        install_hints = _default_install_commands(build_tool)

    test_cmd_base = test_commands[0] if test_commands else _default_test_command(
        build_tool
    )

    provenance = [
        f"build_tool:{build_tool}" if has_build_tool_hint else "build_tool:default"
    ]
    if kotlin_present:
        provenance.append("kotlin_present")
    if test_commands:
        provenance.append(f"test_runner:{_test_label(test_cmd_base)}")
    else:
        provenance.append("test_runner:default")

    return EnvPlan(
        language="java",
        runtime_version="",
        python_version="",
        pre_install=[],
        install=install_hints,
        build=[],
        test_cmd_base=test_cmd_base,
        strategy_name=f"{build_tool}:{_test_label(test_cmd_base)}",
        confidence=0.9 if build_tool else 0.5,
        provenance=provenance,
    )


class JavaAdapter:
    def name(self) -> str:
        return "java"

    def detect(self, repo_root: Path) -> DetectionResult:
        build_tool, signals, confidence = _detect_build_tool(repo_root)
        return DetectionResult(
            language="java",
            confidence=confidence,
            signals=signals,
            runtime_version=None if confidence <= 0.0 else build_tool,
        )

    def inspect(self, repo_root: Path) -> dict[str, Any]:
        build_tool, signals, confidence = _detect_build_tool(repo_root)
        kotlin_present = _detect_kotlin_present(repo_root)
        has_maven = "pom.xml" in signals
        has_gradle = any(name in signals for name in ("build.gradle", "build.gradle.kts"))
        build_tools: list[str] = []
        if has_maven:
            build_tools.append("maven")
        if has_gradle:
            build_tools.append("gradle")
        test_commands = [_default_test_command(build_tool or "maven")]
        install_hints = _default_install_commands(build_tool or "maven")
        warnings: list[dict[str, str]] = []
        if confidence <= 0.0:
            warnings.append(
                {
                    "type": "missing_package_manager",
                    "message": "No recognized Java build manifest found",
                }
            )
            warnings.append(
                {
                    "type": "missing_test_hints",
                    "message": "No recognized Java test runner signature found",
                }
            )

        language_hints = {
            "build_tool": build_tool or "unknown",
            "build_tools": build_tools,
            "kotlin_present": kotlin_present,
            "signals": signals,
            "package_managers": [build_tool] if build_tool else [],
            "package_style": build_tool or "unknown",
            "install_hints": install_hints,
            "test_commands": test_commands,
        }
        return {
            "language": "java",
            "language_version": "",
            "repo_version": REPO_VERSION_UNKNOWN,
            "language_hints": language_hints,
            "install_hints": install_hints,
            "test_runner_hints": {"commands": test_commands, "signals": []},
            "test_paths": _detect_test_paths(repo_root),
            "profile_warnings": warnings,
        }

    def build_env_plan(self, profile: dict[str, Any]) -> EnvPlan:
        return build_env_plan(profile)

    def parse_test_output(
        self, report: object, test_spec: object | None
    ) -> dict[str, str]:
        return parse_repogauge_junit(report, test_spec)

    def file_role_rules(self) -> FileRoleRules:
        return FileRoleRules(
            prod_extensions={".java", ".kt"},
            test_filename_patterns=[
                "*Test.java",
                "*Tests.java",
                "*IT.java",
                "*Test.kt",
                "*Tests.kt",
                "*IT.kt",
            ],
            test_dir_names={"test", "tests"},
            config_build_filenames={
                "pom.xml",
                "build.gradle",
                "build.gradle.kts",
                "settings.gradle",
                "settings.gradle.kts",
                "gradle.properties",
                "gradle-wrapper.properties",
                "gradlew",
                "gradlew.bat",
                "mvnw",
                "mvnw.cmd",
            },
            vendor_dir_names={
                "__pycache__",
                ".mypy_cache",
                ".pytest_cache",
                "site-packages",
                "vendor",
                ".venv",
                "venv",
                ".eggs",
                "build",
                "target",
                ".gradle",
                ".m2",
                "out",
            },
            test_support_filenames={
                "junit-platform.properties",
                "surefire.properties",
                "testng.xml",
            },
        )

    def harness_template_vars(self, spec: dict[str, Any]) -> dict[str, Any]:
        return {
            "parser_import": "repogauge.parsers.junit.parse_repogauge_junit",
            "parser_name": "junit",
            "ext": "java",
            "install_str_join": " && ",
        }

    def signature_labels(self, profile: dict[str, Any]) -> dict[str, str]:
        language_hints = _coerce_mapping(profile.get("language_hints"))
        build_tool = str(language_hints.get("build_tool") or "").strip().lower()
        kotlin_present = bool(language_hints.get("kotlin_present"))
        test_runner_hints = _coerce_mapping(profile.get("test_runner_hints"))
        commands = _coerce_list(test_runner_hints.get("commands"))
        test_cmd_base = commands[0] if commands else _default_test_command(
            build_tool or "maven"
        )
        runtime_label = "java-kotlin" if kotlin_present else "java"
        return {
            "runtime_label": runtime_label,
            "test_label": _test_label(test_cmd_base),
            "package_label": build_tool or "unknown",
        }

    def dependency_signature_inputs(
        self, repo_root: Path, profile: dict[str, Any]
    ) -> list[str]:
        return _read_dependency_signature(repo_root, profile)

    def env_overrides(self, worktree: Path) -> dict[str, str]:
        return {}

    def test_command_attempts(self, test_cmd_base: str) -> list[list[str]]:
        base = test_cmd_base.strip() or _DEFAULT_MAVEN_TEST_CMD
        try:
            parts = shlex.split(base)
        except ValueError:
            parts = [base]
        return [parts] if parts else [shlex.split(_DEFAULT_MAVEN_TEST_CMD)]

    def test_report_filename(self) -> str | None:
        return None

    def test_report_glob(self) -> str | None:
        return None


__all__ = [
    "JavaAdapter",
    "build_env_plan",
]
