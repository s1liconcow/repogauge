"""JavaScript and TypeScript language adapter helpers."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any

from repogauge.mining.signature import REPO_VERSION_UNKNOWN
from repogauge.parsers.junit import register_parser
from repogauge.validation.junit_parser import parse_junit_xml, parse_junit_xml_content
from repogauge.validation.env_detect import EnvPlan

from . import DetectionResult, FileRoleRules

_DEFAULT_PACKAGE_MANAGER = "npm"
_DEFAULT_NODE_VERSION = "20"
_DEFAULT_TEST_CMD = "npm test"
_DEFAULT_REPORT_FILE = "report.xml"
_DEFAULT_FRAMEWORK = "npm-test"

_LOCKFILE_TO_MANAGER = {
    "package-lock.json": "npm",
    "pnpm-lock.yaml": "pnpm",
    "yarn.lock": "yarn",
    "bun.lockb": "bun",
}
_LOCKFILE_PRIORITY = (
    ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "yarn"),
    ("bun.lockb", "bun"),
    ("package-lock.json", "npm"),
)
_JEST_CONFIGS = (
    "jest.config.js",
    "jest.config.ts",
    "jest.config.mjs",
    "jest.config.cjs",
    "jest.config.json",
)
_VITEST_CONFIGS = (
    "vitest.config.js",
    "vitest.config.ts",
    "vitest.config.mjs",
    "vitest.config.cjs",
)


def _sorted_unique(values: Any) -> list[str]:
    return sorted({str(value).strip() for value in values if str(value).strip()})


def _coerce_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _coerce_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return _sorted_unique(value)
    return []


def _ordered_unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        normalized = str(value).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _safe_read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _read_package_json(repo_root: Path) -> dict[str, Any]:
    package_json = repo_root / "package.json"
    if not package_json.exists():
        return {}
    try:
        payload = json.loads(_safe_read_text(package_json))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _package_dependency_names(package_json: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for key in ("dependencies", "devDependencies"):
        deps = _coerce_mapping(package_json.get(key))
        names.update(name.strip().lower() for name in deps if str(name).strip())
    return names


def _extract_node_version(package_json: dict[str, Any]) -> str:
    engines = _coerce_mapping(package_json.get("engines"))
    node_engine = engines.get("node")
    if isinstance(node_engine, str) and node_engine.strip():
        return node_engine.strip()

    volta = _coerce_mapping(package_json.get("volta"))
    volta_node = volta.get("node")
    if isinstance(volta_node, str) and volta_node.strip():
        return volta_node.strip()

    return ""


def _normalize_runtime_version(raw: str) -> str:
    values = re.findall(r"\d+(?:\.\d+)?", raw)
    if not values:
        return _DEFAULT_NODE_VERSION
    major = values[0].split(".", 1)[0].strip()
    return major or _DEFAULT_NODE_VERSION


def _node_label(version: str) -> str:
    digits = "".join(ch for ch in version if ch.isdigit() or ch == ".")
    if not digits:
        return "node"
    return "node" + digits.replace(".", "")


def _detect_package_manager(
    repo_root: Path, package_json: dict[str, Any]
) -> tuple[str, list[str], list[str]]:
    signals: list[str] = []

    package_manager = package_json.get("packageManager")
    if isinstance(package_manager, str):
        manager_name = package_manager.split("@", 1)[0].strip().lower()
        if manager_name:
            for lockfile, detected_manager in _LOCKFILE_PRIORITY:
                if detected_manager == manager_name:
                    return manager_name, signals, _install_hints_for_package_manager(
                        manager_name, lockfile_present=(repo_root / lockfile).exists()
                    )
            return manager_name, signals, _install_hints_for_package_manager(
                manager_name, lockfile_present=False
            )

    for lockfile, manager_name in _LOCKFILE_PRIORITY:
        if (repo_root / lockfile).exists():
            return manager_name, [lockfile], _install_hints_for_package_manager(
                manager_name, lockfile_present=True
            )

    return _DEFAULT_PACKAGE_MANAGER, signals, _install_hints_for_package_manager(
        _DEFAULT_PACKAGE_MANAGER, lockfile_present=False
    )


def _install_hints_for_package_manager(
    package_manager: str, *, lockfile_present: bool
) -> list[str]:
    if package_manager == "npm":
        return ["npm ci"] if lockfile_present else ["npm install"]
    if package_manager == "pnpm":
        return ["pnpm install --frozen-lockfile"]
    if package_manager == "yarn":
        return ["yarn install --frozen-lockfile"]
    if package_manager == "bun":
        return ["bun install --frozen-lockfile"]
    return ["npm install"]


def _detect_test_paths(repo_root: Path) -> list[str]:
    paths: list[str] = []
    for candidate in ("__tests__", "tests", "test"):
        if (repo_root / candidate).is_dir():
            paths.append(candidate)
    return _sorted_unique(paths)


def _detect_framework(
    repo_root: Path, package_json: dict[str, Any]
) -> tuple[str, list[str], list[str]]:
    deps = _package_dependency_names(package_json)

    jest_signals: list[str] = []
    vitest_signals: list[str] = []
    for config in _JEST_CONFIGS:
        if (repo_root / config).exists():
            jest_signals.append(config)
    for config in _VITEST_CONFIGS:
        if (repo_root / config).exists():
            vitest_signals.append(config)

    if "jest" in deps or package_json.get("jest") is not None:
        jest_signals.append("package.json#jest")
    if "vitest" in deps:
        vitest_signals.append("package.json#vitest")

    framework_signals = _ordered_unique([*vitest_signals, *jest_signals])
    if vitest_signals:
        return "vitest", framework_signals, [
            f"npx vitest run --reporter=junit --outputFile={_DEFAULT_REPORT_FILE}"
        ]
    if jest_signals:
        return "jest", framework_signals, [
            "npx jest --reporters=default --reporters=jest-junit "
            "--testResultsProcessor=jest-junit"
        ]
    return _DEFAULT_FRAMEWORK, framework_signals, [_DEFAULT_TEST_CMD]


def _normalize_label(values: list[str], default: str) -> str:
    selected = [value.strip() for value in values if value and value.strip()]
    if not selected:
        return default
    return "+".join(selected)


def _coerce_test_commands(payload: dict[str, Any], language_hints: dict[str, Any]) -> list[str]:
    test_hints = _coerce_mapping(payload.get("test_runner_hints"))
    commands = _coerce_list(test_hints.get("commands") or language_hints.get("test_commands"))
    return commands or [_DEFAULT_TEST_CMD]


def _report_from_payload(report: object) -> tuple[str | None, Path | None]:
    if isinstance(report, Path):
        return None, report
    if isinstance(report, (bytes, bytearray)):
        return report.decode("utf-8"), None
    if isinstance(report, str):
        text = report.strip()
        if not text:
            return "", None
        if "\n" not in text and "\r" not in text:
            candidate = Path(text)
            if candidate.exists():
                return None, candidate
        return report, None
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


def parse_js_junit(report: object, test_spec: object | None = None) -> dict[str, str]:
    del test_spec
    xml_text, xml_path = _report_from_payload(report)
    if xml_path is not None:
        return parse_junit_xml(xml_path, style="js")
    return parse_junit_xml_content(xml_text or "", style="js")


def build_env_plan(profile: Any) -> EnvPlan:
    payload = _coerce_mapping(profile)
    language_hints = _coerce_mapping(payload.get("language_hints"))

    package_managers = _coerce_list(language_hints.get("package_managers"))
    if not package_managers:
        package_managers = [_DEFAULT_PACKAGE_MANAGER]

    install_hints = _coerce_list(payload.get("install_hints"))
    if not install_hints:
        install_hints = _coerce_list(language_hints.get("install_hints"))
    if not install_hints:
        install_hints = _install_hints_for_package_manager(
            package_managers[0],
            lockfile_present=bool(language_hints.get("lockfiles")),
        )

    framework = str(language_hints.get("framework") or _DEFAULT_FRAMEWORK).strip().lower()
    test_commands = _coerce_test_commands(payload, language_hints)

    runtime_version = str(
        payload.get("language_version")
        or language_hints.get("runtime_version")
        or _DEFAULT_NODE_VERSION
    ).strip() or _DEFAULT_NODE_VERSION
    test_cmd_base = test_commands[0]
    package_manager = package_managers[0]

    return EnvPlan(
        language="javascript",
        runtime_version=runtime_version,
        python_version="",
        pre_install=[],
        install=install_hints,
        build=[],
        test_cmd_base=test_cmd_base,
        strategy_name=f"{package_manager}:{framework}",
        confidence=0.9,
        provenance=[
            f"package_manager:{package_manager}",
            f"framework:{framework}",
        ],
    )


class JavaScriptAdapter:
    def name(self) -> str:
        return "javascript"

    def detect(self, repo_root: Path) -> DetectionResult:
        package_json = repo_root / "package.json"
        if not package_json.exists():
            return DetectionResult(language="javascript", confidence=0.0, signals=[])

        signals = ["package.json"]
        if (repo_root / "tsconfig.json").exists():
            signals.append("tsconfig.json")
        for lockfile in _LOCKFILE_TO_MANAGER:
            if (repo_root / lockfile).exists():
                signals.append(lockfile)

        return DetectionResult(
            language="javascript",
            confidence=0.95,
            signals=_ordered_unique(signals),
            runtime_version=None,
        )

    def inspect(self, repo_root: Path) -> dict[str, Any]:
        package_json = _read_package_json(repo_root)
        package_manager, lockfile_signals, install_hints = _detect_package_manager(
            repo_root, package_json
        )
        dependency_names = _package_dependency_names(package_json)
        tsconfig_present = (repo_root / "tsconfig.json").exists()
        typescript_present = tsconfig_present or "typescript" in dependency_names
        framework, framework_signals, test_commands = _detect_framework(
            repo_root, package_json
        )
        runtime_version = _normalize_runtime_version(_extract_node_version(package_json))
        signals = ["package.json", *lockfile_signals, *framework_signals]
        if tsconfig_present:
            signals.append("tsconfig.json")

        language_hints = {
            "name": str(package_json.get("name") or "").strip(),
            "version": str(package_json.get("version") or "").strip(),
            "scripts": _coerce_mapping(package_json.get("scripts")),
            "package_managers": [package_manager],
            "package_manager": package_manager,
            "package_style": "node",
            "runtime_version": runtime_version,
            "versions": [runtime_version],
            "signals": _ordered_unique(signals),
            "framework": framework,
            "typescript": typescript_present,
            "typescript_present": typescript_present,
            "lockfiles": lockfile_signals,
            "install_hints": install_hints,
            "test_commands": test_commands,
        }
        warnings: list[dict[str, str]] = []
        if not package_json:
            warnings.append(
                {
                    "type": "package_json_parse_failed",
                    "message": "package.json could not be parsed as JSON",
                }
            )

        return {
            "language": "javascript",
            "language_version": runtime_version,
            "repo_version": str(package_json.get("version") or "").strip()
            or REPO_VERSION_UNKNOWN,
            "language_hints": language_hints,
            "install_hints": install_hints,
            "test_runner_hints": {
                "commands": test_commands,
                "signals": framework_signals,
            },
            "test_paths": _detect_test_paths(repo_root),
            "profile_warnings": warnings,
        }

    def build_env_plan(self, profile: dict[str, Any]) -> EnvPlan:
        return build_env_plan(profile)

    def parse_test_output(
        self, report: object, test_spec: object | None
    ) -> dict[str, str]:
        return parse_js_junit(report, test_spec)

    def file_role_rules(self) -> FileRoleRules:
        return FileRoleRules(
            prod_extensions={".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"},
            test_filename_patterns=[
                "*.test.js",
                "*.spec.js",
                "*.test.jsx",
                "*.spec.jsx",
                "*.test.ts",
                "*.spec.ts",
                "*.test.tsx",
                "*.spec.tsx",
            ],
            test_dir_names={"__tests__"},
            config_build_filenames={
                "package.json",
                "package-lock.json",
                "pnpm-lock.yaml",
                "yarn.lock",
                "bun.lockb",
                "tsconfig.json",
                "jest.config.js",
                "jest.config.cjs",
                "jest.config.mjs",
                "jest.config.json",
                "jest.config.ts",
                "vitest.config.js",
                "vitest.config.ts",
                "vitest.config.cjs",
                "vitest.config.mjs",
                ".eslintrc",
                ".eslintrc.js",
                ".eslintrc.json",
                ".prettierrc",
                ".prettierrc.json",
            },
            vendor_dir_names={
                "node_modules",
                ".next",
                ".nuxt",
                ".turbo",
                ".cache",
                "coverage",
                "dist",
                "build",
            },
        )

    def harness_template_vars(self, spec: dict[str, Any]) -> dict[str, Any]:
        language_hints = _coerce_mapping(spec.get("language_hints"))
        ext = "ts" if language_hints.get("typescript") else "js"
        return {
            "parser_import": "repogauge.lang.javascript.parse_js_junit",
            "parser_name": "junit_js",
            "ext": ext,
            "install_str_join": " && ",
        }

    def signature_labels(self, profile: dict[str, Any]) -> dict[str, str]:
        language_hints = profile.get("language_hints") if isinstance(profile, dict) else {}
        if not isinstance(language_hints, dict):
            language_hints = {}
        test_runner_hints = (
            profile.get("test_runner_hints") if isinstance(profile, dict) else {}
        )
        if not isinstance(test_runner_hints, dict):
            test_runner_hints = {}

        runtime_version = str(
            profile.get("language_version")
            or language_hints.get("runtime_version")
            or "node"
        ).strip() or "node"
        package_managers = _coerce_list(language_hints.get("package_managers"))
        commands = _coerce_list(test_runner_hints.get("commands"))

        return {
            "runtime_label": _node_label(runtime_version),
            "test_label": _normalize_label(commands, "test"),
            "package_label": _normalize_label(package_managers, "pkgunknown"),
        }

    def dependency_signature_inputs(
        self, repo_root: Path, profile: dict[str, Any]
    ) -> list[str]:
        package_json = repo_root / "package.json"
        if not package_json.exists():
            return []

        inputs: list[str] = []
        for candidate in (
            package_json,
            repo_root / "tsconfig.json",
            repo_root / "package-lock.json",
            repo_root / "pnpm-lock.yaml",
            repo_root / "yarn.lock",
            repo_root / "bun.lockb",
        ):
            if not candidate.exists():
                continue
            content = _safe_read_text(candidate).strip()
            inputs.append(f"{candidate.name}\n{content}")
        return _sorted_unique(inputs)

    def env_overrides(self, worktree: Path) -> dict[str, str]:
        framework, _signals, _commands = _detect_framework(
            worktree, _read_package_json(worktree)
        )
        env = {
            "CI": "1",
            "NODE_ENV": "test",
        }
        if framework == "jest":
            env["JEST_JUNIT_OUTPUT_FILE"] = str(worktree / _DEFAULT_REPORT_FILE)
        return env

    def test_command_attempts(self, test_cmd_base: str) -> list[list[str]]:
        base = test_cmd_base.strip() or _DEFAULT_TEST_CMD
        return [shlex.split(base)]

    def test_report_filename(self) -> str | None:
        return _DEFAULT_REPORT_FILE

    def test_report_glob(self) -> str | None:
        return _DEFAULT_REPORT_FILE


register_parser("junit_js", parse_js_junit)


__all__ = ["JavaScriptAdapter", "build_env_plan", "parse_js_junit"]
