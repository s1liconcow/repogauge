"""Java language adapter and environment-plan helpers."""

from __future__ import annotations

import re
import shlex
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from repogauge.mining.signature import REPO_VERSION_UNKNOWN
from repogauge.parsers.junit import register_parser
from repogauge.validation.junit_parser import parse_junit_xml, parse_junit_xml_content
from repogauge.validation.env_detect import EnvPlan

from . import DetectionResult, FileRoleRules

_DEFAULT_JAVA_VERSION = "17"
_DEFAULT_FRAMEWORK = "junit5"
_DEFAULT_MAVEN_INSTALL_CMD = "mvn -B -DskipTests install"
_DEFAULT_MAVEN_TEST_CMD = "mvn -B test"
_DEFAULT_GRADLE_INSTALL_CMD = "./gradlew assemble"
_DEFAULT_GRADLE_INSTALL_CMD_FALLBACK = "gradle assemble"
_DEFAULT_GRADLE_TEST_CMD = "./gradlew test"
_DEFAULT_GRADLE_TEST_CMD_FALLBACK = "gradle test"


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


def _report_from_payload(report: object) -> tuple[str | None, list[Path]]:
    if isinstance(report, Path):
        if "*" in report.name:
            return None, sorted(report.parent.glob(report.name))
        return None, [report]
    if isinstance(report, (bytes, bytearray)):
        return report.decode("utf-8"), []
    if isinstance(report, str):
        text = report.strip()
        if not text:
            return "", []
        if "\n" not in text and "\r" not in text:
            candidate = Path(text)
            if candidate.exists():
                return None, [candidate]
            if "*" in candidate.name and candidate.parent.exists():
                return None, sorted(candidate.parent.glob(candidate.name))
        return report, []
    if isinstance(report, dict):
        for key in (
            "junit_xml",
            "junit_xml_path",
            "junit_xml_file",
            "output",
            "log",
            "result",
            "stdout",
            "stderr",
            "raw",
        ):
            value = report.get(key)
            if value is None:
                continue
            return _report_from_payload(value)
    raise TypeError(f"unsupported report payload for parser: {type(report).__name__}")


def parse_java_junit(report: object, test_spec: object | None = None) -> dict[str, str]:
    del test_spec
    xml_text, xml_paths = _report_from_payload(report)
    if xml_paths:
        merged: dict[str, str] = {}
        for xml_path in xml_paths:
            merged.update(parse_junit_xml(xml_path, style="java"))
        return merged
    return parse_junit_xml_content(xml_text or "", style="java")


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


def _gradle_commands(repo_root: Path) -> tuple[list[str], str, list[dict[str, str]]]:
    warnings: list[dict[str, str]] = []
    wrapper = repo_root / "gradlew"
    if wrapper.exists() and not wrapper.stat().st_mode & 0o111:
        warnings.append(
            {
                "type": "gradle_wrapper_not_executable",
                "message": "gradlew is not executable; falling back to system gradle",
            }
        )
        return (
            [_DEFAULT_GRADLE_INSTALL_CMD_FALLBACK],
            _DEFAULT_GRADLE_TEST_CMD_FALLBACK,
            warnings,
        )
    if wrapper.exists():
        return ([_DEFAULT_GRADLE_INSTALL_CMD], _DEFAULT_GRADLE_TEST_CMD, warnings)
    return (
        [_DEFAULT_GRADLE_INSTALL_CMD_FALLBACK],
        _DEFAULT_GRADLE_TEST_CMD_FALLBACK,
        warnings,
    )


def _default_install_commands(build_tool: str, repo_root: Path) -> tuple[list[str], str, list[dict[str, str]]]:
    if build_tool == "gradle":
        install, test_cmd, warnings = _gradle_commands(repo_root)
        return install, test_cmd, warnings
    return ([_DEFAULT_MAVEN_INSTALL_CMD], _DEFAULT_MAVEN_TEST_CMD, [])


def _property_interpolate(value: str, properties: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        return properties.get(key, match.group(0))

    return re.sub(r"\$\{([^}]+)\}", replace, value)


def _element_text(element: ET.Element | None) -> str:
    if element is None or element.text is None:
        return ""
    return element.text.strip()


def _pom_child(parent: ET.Element, child: str) -> ET.Element | None:
    for element in parent:
        if element.tag.rsplit("}", 1)[-1] == child:
            return element
    return None


def _parse_maven_metadata(repo_root: Path) -> dict[str, Any]:
    pom = repo_root / "pom.xml"
    raw = _safe_read_text(pom)
    if not raw.strip():
        return {}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return {}

    properties: dict[str, str] = {}
    properties_el = _pom_child(root, "properties")
    if properties_el is not None:
        for child in properties_el:
            key = child.tag.rsplit("}", 1)[-1]
            value = _element_text(child)
            if key and value:
                properties[key] = value

    parent_el = _pom_child(root, "parent")
    group_id = _element_text(_pom_child(root, "groupId")) or _element_text(
        _pom_child(parent_el, "groupId") if parent_el is not None else None
    )
    artifact_id = _element_text(_pom_child(root, "artifactId"))
    version = _element_text(_pom_child(root, "version")) or _element_text(
        _pom_child(parent_el, "version") if parent_el is not None else None
    )
    group_id = _property_interpolate(group_id, properties)
    artifact_id = _property_interpolate(artifact_id, properties)
    version = _property_interpolate(version, properties)

    runtime_candidates = [
        properties.get("maven.compiler.release", ""),
        properties.get("maven.compiler.source", ""),
        properties.get("java.version", ""),
    ]
    runtime_version = next(
        (value for value in runtime_candidates if value.strip()),
        _DEFAULT_JAVA_VERSION,
    )
    runtime_version = _property_interpolate(runtime_version, properties) or _DEFAULT_JAVA_VERSION

    dependencies = raw.lower()
    framework = _DEFAULT_FRAMEWORK
    if "testng" in dependencies:
        framework = "testng"
    elif "junit:junit" in dependencies:
        framework = "junit4"
    elif "junit-jupiter" in dependencies:
        framework = "junit5"

    return {
        "name": f"{group_id}:{artifact_id}".strip(":") if artifact_id else "",
        "version": version or REPO_VERSION_UNKNOWN,
        "runtime_version": runtime_version,
        "framework": framework,
    }


def _parse_gradle_metadata(repo_root: Path) -> dict[str, Any]:
    build_file = repo_root / "build.gradle"
    if not build_file.exists():
        build_file = repo_root / "build.gradle.kts"
    raw = _safe_read_text(build_file)
    lowered = raw.lower()

    runtime_version = _DEFAULT_JAVA_VERSION
    for pattern in (
        r"sourceCompatibility\s*=\s*['\"](\d+)['\"]",
        r"JavaVersion\.VERSION_(\d+)",
    ):
        match = re.search(pattern, raw)
        if match:
            runtime_version = match.group(1)
            break

    version = REPO_VERSION_UNKNOWN
    match = re.search(r"(?m)^\s*version\s*=\s*['\"]([^'\"]+)['\"]", raw)
    if match and match.group(1).strip():
        version = match.group(1).strip()

    framework = _DEFAULT_FRAMEWORK
    if "usetestng()" in lowered or "testng" in lowered:
        framework = "testng"
    elif "usejunit()" in lowered or "junit:junit" in lowered:
        framework = "junit4"
    elif "usejunitplatform()" in lowered or "junit-jupiter" in lowered:
        framework = "junit5"

    return {
        "name": repo_root.name,
        "version": version,
        "runtime_version": runtime_version,
        "framework": framework,
    }


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

    build_tool_hint = str(language_hints.get("build_tool") or "").strip().lower()
    has_build_tool_hint = build_tool_hint in {"maven", "gradle"}
    build_tool = build_tool_hint if has_build_tool_hint else "maven"

    kotlin_present = bool(language_hints.get("kotlin_present"))
    test_hints = _coerce_mapping(payload.get("test_runner_hints"))
    test_commands = _coerce_list(test_hints.get("commands") or language_hints.get("test_commands"))
    install_hints = _coerce_list(payload.get("install_hints"))
    if not install_hints:
        if build_tool == "gradle":
            install_hints = [_DEFAULT_GRADLE_INSTALL_CMD]
        else:
            install_hints = [_DEFAULT_MAVEN_INSTALL_CMD]

    test_cmd_base = test_commands[0] if test_commands else _default_test_command(
        build_tool
    )
    framework = str(language_hints.get("framework") or _DEFAULT_FRAMEWORK).strip().lower()
    runtime_version = str(
        payload.get("language_version")
        or language_hints.get("runtime_version")
        or _DEFAULT_JAVA_VERSION
    ).strip() or _DEFAULT_JAVA_VERSION

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
        runtime_version=runtime_version,
        python_version="",
        pre_install=[],
        install=install_hints,
        build=[],
        test_cmd_base=test_cmd_base,
        strategy_name=f"{build_tool}:{framework}",
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
        warnings: list[dict[str, str]] = []
        has_maven = "pom.xml" in signals
        has_gradle = any(name in signals for name in ("build.gradle", "build.gradle.kts"))
        build_tools: list[str] = []
        if has_maven:
            build_tools.append("maven")
        if has_gradle:
            build_tools.append("gradle")
        if build_tool == "maven":
            metadata = _parse_maven_metadata(repo_root)
        else:
            metadata = _parse_gradle_metadata(repo_root)
        install_hints, test_cmd, command_warnings = _default_install_commands(
            build_tool or "maven",
            repo_root,
        )
        warnings.extend(command_warnings)
        runtime_version = str(metadata.get("runtime_version") or _DEFAULT_JAVA_VERSION)
        framework = str(metadata.get("framework") or _DEFAULT_FRAMEWORK)
        test_commands = [test_cmd]
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
            "runtime_version": runtime_version,
            "versions": [runtime_version],
            "framework": framework,
            "install_hints": install_hints,
            "test_commands": test_commands,
        }
        return {
            "language": "java",
            "language_version": runtime_version,
            "repo_version": str(metadata.get("version") or REPO_VERSION_UNKNOWN),
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
        return parse_java_junit(report, test_spec)

    def file_role_rules(self) -> FileRoleRules:
        return FileRoleRules(
            prod_extensions={".java", ".kt"},
            test_filename_patterns=[
                "*Test.java",
                "*Tests.java",
                "*IT.java",
                "*IntegrationTest.java",
                "*Test.kt",
                "*Tests.kt",
            ],
            test_dir_names={"test", "tests"},
            config_build_filenames={
                "pom.xml",
                "build.gradle",
                "build.gradle.kts",
                "settings.gradle",
                "settings.gradle.kts",
                "gradle.properties",
                "gradlew",
                "gradlew.bat",
                "checkstyle.xml",
                "spotbugs.xml",
            },
            vendor_dir_names={
                "build",
                "target",
                ".gradle",
                "out",
            },
        )

    def harness_template_vars(self, spec: dict[str, Any]) -> dict[str, Any]:
        return {
            "parser_import": "repogauge.lang.java.parse_java_junit",
            "parser_name": "junit_java",
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
        return "TEST-results.xml"

    def test_report_glob(self) -> str | None:
        return "**/TEST-*.xml"


register_parser("junit_java", parse_java_junit)


__all__ = [
    "JavaAdapter",
    "build_env_plan",
    "parse_java_junit",
]
