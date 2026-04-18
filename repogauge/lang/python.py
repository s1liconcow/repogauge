"""Python language adapter and environment-plan helpers."""

from __future__ import annotations

import re
import shlex
import sys
from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from repogauge.mining.signature import REPO_VERSION_UNKNOWN
from repogauge.mining.signature import _to_pkg_label
from repogauge.mining.signature import _to_python_label
from repogauge.mining.signature import _to_test_label
from repogauge.validation.env_detect import EnvPlan
from repogauge.parsers.junit import parse_repogauge_junit

from . import DetectionResult, FileRoleRules

try:  # Python 3.11+ has stdlib tomllib.
    import tomllib
except Exception:  # pragma: no cover
    import tomli as tomllib  # type: ignore[import-not-found]


_DEFAULT_PYTHON_VERSION = "3.11"
_DEFAULT_TEST_CMD = "python -m pytest"
_SELF_MANAGING_INSTALL_PREFIXES = ("poetry install", "pipenv install")


def _sorted_unique(values: Any) -> list[str]:
    return sorted({str(value).strip() for value in values if str(value).strip()})


def _coerce_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _coerce_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return _sorted_unique(value)
    return []


def _version_tuple(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (999,)


def _choose_python_version(
    versions: list[str], provenance: list[str]
) -> tuple[str, float]:
    if not versions:
        provenance.append("python_version:default-3.11")
        return _DEFAULT_PYTHON_VERSION, 0.75

    normalized = _sorted_unique(versions)
    if len(normalized) > 1:
        provenance.append("python_version:conflict")
        provenance.append("python_version:chose-minimum")
        return min(normalized, key=_version_tuple), 0.6

    provenance.append(f"python_version:{normalized[0]}")
    return normalized[0], 1.0


def _build_test_command(
    test_commands: list[str], provenance: list[str]
) -> tuple[str, float, str]:
    if "pytest" in test_commands:
        provenance.append("test_runner:pytest")
        return "pytest", 1.0, "pytest"

    if "python -m unittest" in test_commands:
        provenance.append("test_runner:unittest")
        return "python -m unittest", 0.9, "unittest"

    if "tox" in test_commands:
        provenance.append("test_runner:tox")
        return "tox", 0.7, "tox"

    if "nox" in test_commands:
        provenance.append("test_runner:nox")
        return "nox", 0.55, "nox"

    provenance.append("test_runner:default")
    return _DEFAULT_TEST_CMD, 0.5, "pytest-default"


def _build_install_commands(
    package_managers: list[str],
    install_hints: list[str],
    provenance: list[str],
) -> tuple[list[str], list[str], float, str]:
    build: list[str] = []

    if "poetry" in package_managers:
        provenance.append("install_strategy:poetry")
        return ["poetry install"], build, 1.0, "poetry"

    if "uv" in package_managers:
        provenance.append("install_strategy:uv")
        return ["uv sync --active"], build, 0.95, "uv"

    if "pipenv" in package_managers:
        provenance.append("install_strategy:pipenv")
        return ["pipenv install"], build, 0.9, "pipenv"

    if "setuptools" in package_managers:
        provenance.append("install_strategy:setuptools")
        return ["pip install -e ."], build, 0.88, "setuptools"

    requirements_commands = [
        hint
        for hint in install_hints
        if hint.startswith("pip install -r ") and "requirements" in hint
    ]
    if requirements_commands:
        selected = _sorted_unique(requirements_commands)[0]
        provenance.append("install_strategy:requirements")
        provenance.append(f"install_file:{selected}")
        return [selected], build, 0.75, "requirements"

    if install_hints:
        provenance.append("install_strategy:first-hint")
        return [install_hints[0]], build, 0.6, "fallback"

    provenance.append("install_strategy:editable-default")
    return ["pip install -e ."], build, 0.5, "fallback"


def _augment_uv_install_for_pytest(
    install: list[str],
    provenance: list[str],
) -> list[str]:
    augmented: list[str] = []
    for command in install:
        if not command.startswith("uv sync"):
            augmented.append(command)
            continue
        if any(
            flag in command
            for flag in ("--all-groups", "--group ", "--only-group", "--only-dev")
        ):
            augmented.append(command)
            continue
        provenance.append("install:test-dependency:uv-all-groups")
        augmented.append(f"{command} --all-groups")
    return augmented


def _augment_for_pytest(
    install: list[str], test_cmd_base: str, provenance: list[str], confidence: float
) -> tuple[list[str], float]:
    if test_cmd_base not in {"pytest", "python -m pytest"}:
        return install, confidence

    if any(cmd.startswith("uv sync") for cmd in install):
        install = _augment_uv_install_for_pytest(install, provenance)
        if not any("pip install pytest" in command for command in install):
            provenance.append("install:test-dependency:pytest")
            install = install + ["python -m pip install pytest"]
        return install, confidence

    if any(
        cmd.startswith(p) for cmd in install for p in _SELF_MANAGING_INSTALL_PREFIXES
    ):
        return install, confidence

    has_pytest_hint = any("pytest" in command for command in install)
    if has_pytest_hint:
        return install, confidence

    provenance.append("install:test-dependency:pytest")
    return install + ["pip install pytest"], max(0.0, confidence - 0.05)


def _safe_read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _extract_toml_value(path: Path, sections: list[str], key: str) -> str | None:
    try:
        payload = tomllib.loads(_safe_read_text(path))
    except Exception:
        return None

    cursor: Any = payload
    for section in sections:
        if not isinstance(cursor, dict) or section not in cursor:
            return None
        cursor = cursor.get(section)
    if not isinstance(cursor, dict):
        return None

    value = cursor.get(key)
    if isinstance(value, str):
        cleaned = value.strip().strip("\"'")
        return cleaned if cleaned else None
    return None


def _extract_setup_cfg_version(path: Path) -> str | None:
    parser = ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except Exception:
        return None
    if parser.has_option("metadata", "version"):
        value = parser.get("metadata", "version")
        if value.strip():
            return value.strip()
    return None


def _extract_setup_py_version(path: Path) -> str | None:
    content = _safe_read_text(path)
    if not content:
        return None
    match = re.search(r"version\s*=\s*(['\"])([^'\"]+)\1", content)
    if match:
        return match.group(2).strip()
    return None


def _detect_package_version(repo_root: Path) -> str:
    pyproject = repo_root / "pyproject.toml"
    if pyproject.exists():
        value = _extract_toml_value(pyproject, ["tool", "poetry"], "version")
        if not value:
            value = _extract_toml_value(pyproject, ["project"], "version")
        if value:
            return value

    setup_cfg = repo_root / "setup.cfg"
    if setup_cfg.exists():
        value = _extract_setup_cfg_version(setup_cfg)
        if value:
            return value

    setup_py = repo_root / "setup.py"
    if setup_py.exists():
        value = _extract_setup_py_version(setup_py)
        if value:
            return value

    return REPO_VERSION_UNKNOWN


def _parse_version_tokens(raw: str) -> list[str]:
    values: set[str] = set()
    for chunk in re.split(r"[\s,]+", raw.replace(";", " ")):
        value = chunk.strip()
        if not value:
            continue
        if value.lower().startswith("py") and value[2:].isdigit() and len(value) >= 4:
            py = value[2:]
            if len(py) == 3:
                values.add(f"3.{py[1:]}")
                continue

        match = re.match(
            r"(?P<op>>=|<=|>|<|==|=|~=)?\s*(?P<ver>3\.\d+(?:\.\d+)?)", value
        )
        if not match:
            continue
        op = match.group("op") or "=="
        if op in {"<", "<="}:
            continue
        major, minor, *_rest = match.group("ver").split(".")
        values.add(f"{major}.{minor}")
    return sorted(values)


def _parse_requires_python(raw: str) -> list[str]:
    python_lines = re.findall(
        r"(?m)^\s*(?:python|requires-python)\s*=\s*['\"]([^'\"]+)['\"]", raw
    )
    if not python_lines:
        return _parse_version_tokens(raw.replace(",", " "))
    versions: list[str] = []
    for line in python_lines:
        versions.extend(_parse_version_tokens(line))
    return sorted(set(versions))


def _detect_package_and_install_hints(
    repo_root: Path,
) -> tuple[list[str], list[str], list[str]]:
    install_hints: list[str] = []
    package_managers: list[str] = []
    hints: list[str] = []

    pyproject = repo_root / "pyproject.toml"
    setup_py = repo_root / "setup.py"
    setup_cfg = repo_root / "setup.cfg"
    requirements_files = sorted(repo_root.glob("requirements*.txt"))
    uv_lock = repo_root / "uv.lock"

    if pyproject.exists():
        text = _safe_read_text(pyproject)
        package_managers.append("pyproject")
        if "tool.poetry" in text:
            package_managers.append("poetry")
            install_hints.append("poetry install")
            hints.append("pyproject:poetry")
        elif "[project]" in text:
            package_managers.append("pep621")
            install_hints.append("pip install -e .")
            hints.append("pyproject:project")
        else:
            install_hints.append("pip install -e .")
            hints.append("pyproject:generic")

    if setup_py.exists():
        package_managers.append("setuptools")
        install_hints.append("pip install -e .")
        hints.append("setup.py")

    if setup_cfg.exists():
        package_managers.append("setuptools")
        install_hints.append("pip install -e .")
        if "tool.pytest" in _safe_read_text(setup_cfg):
            hints.append("setup.cfg:tool.pytest")

    for req in requirements_files:
        package_managers.append("requirements")
        install_hints.append(f"pip install -r {req.name}")
        hints.append(f"requirements:{req.name}")

    if (repo_root / "Pipfile").exists():
        package_managers.append("pipenv")
        install_hints.append("pipenv install")

    if uv_lock.exists():
        package_managers.append("uv")
        install_hints.append("uv sync")

    if not install_hints:
        install_hints.append("pip install -e .")

    return (
        _sorted_unique(package_managers),
        _sorted_unique(install_hints),
        _sorted_unique(hints),
    )


def _detect_test_runner_hints(repo_root: Path) -> list[str]:
    commands: list[str] = []
    if (repo_root / "tox.ini").exists():
        commands.append("tox")
    if (repo_root / "noxfile.py").exists():
        commands.append("nox")

    pyproject = _safe_read_text(repo_root / "pyproject.toml")
    setup_cfg = _safe_read_text(repo_root / "setup.cfg")

    if (repo_root / "pytest.ini").exists():
        commands.append("pytest")
    if "tool.pytest.ini_options" in pyproject or "[tool.pytest" in setup_cfg:
        commands.append("pytest")
    if "unittest" in pyproject.lower() or "unittest" in setup_cfg.lower():
        commands.append("python -m unittest")

    if "testpath" in setup_cfg or "addopts" in setup_cfg:
        commands.append("pytest")
    if not commands:
        commands.append("python -m pytest")
    return _sorted_unique(commands)


def _detect_package_style(repo_root: Path) -> str:
    if (repo_root / "src").is_dir():
        return "src"
    if any((repo_root / p).is_dir() for p in ["lib", "package"]):
        return "flat"
    return "unknown"


def _detect_test_paths(repo_root: Path) -> list[str]:
    paths: list[str] = []
    if (repo_root / "tests").is_dir():
        paths.append("tests")
    if (repo_root / "test").is_dir():
        paths.append("test")
    return paths


def _detect_python_versions(repo_root: Path) -> list[str]:
    versions: list[str] = []
    if (repo_root / ".python-version").exists():
        versions.extend(_parse_version_tokens(_safe_read_text(repo_root / ".python-version")))
    if (repo_root / "pyproject.toml").exists():
        versions.extend(
            _parse_requires_python(_safe_read_text(repo_root / "pyproject.toml"))
        )
    if (repo_root / "tox.ini").exists():
        versions.extend(_parse_version_tokens(_safe_read_text(repo_root / "tox.ini")))
    return _sorted_unique(versions)


def _parse_repo_profile_warnings(
    package_managers: list[str],
    test_commands: list[str],
    python_versions: list[str],
) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    if not package_managers:
        warnings.append(
            {
                "type": "missing_package_manager",
                "message": "No recognized packaging manifest found",
            }
        )
    if not test_commands:
        warnings.append(
            {
                "type": "missing_test_hints",
                "message": "No recognized test runner signature found",
            }
        )
    if len(python_versions) > 1:
        warnings.append(
            {
                "type": "python_version_conflict",
                "message": f"Conflicting python versions detected: {', '.join(python_versions)}",
            }
        )
    return warnings


def _detect_python(repo_root: Path) -> tuple[float, list[str]]:
    signals: list[str] = []
    confidence = 0.0
    markers = [
        ("pyproject.toml", repo_root / "pyproject.toml"),
        ("setup.py", repo_root / "setup.py"),
        ("setup.cfg", repo_root / "setup.cfg"),
        ("tox.ini", repo_root / "tox.ini"),
        ("noxfile.py", repo_root / "noxfile.py"),
        ("pytest.ini", repo_root / "pytest.ini"),
        (".python-version", repo_root / ".python-version"),
        ("uv.lock", repo_root / "uv.lock"),
        ("Pipfile", repo_root / "Pipfile"),
    ]
    for signal, path in markers:
        if path.exists():
            signals.append(signal)

    if any((repo_root / name).is_dir() for name in ("tests", "test")):
        signals.append("tests")
    if list(repo_root.glob("requirements*.txt")):
        signals.append("requirements")
    if any(repo_root.rglob("*.py")):
        signals.append("python_files")

    if "pyproject.toml" in signals:
        confidence = 1.0
    elif any(signal in signals for signal in ("setup.py", "setup.cfg")):
        confidence = 0.9
    elif "requirements" in signals:
        confidence = 0.8
    elif "python_files" in signals:
        confidence = 0.5
    elif signals:
        confidence = 0.6
    return confidence, _sorted_unique(signals)


def build_env_plan(profile: Any) -> EnvPlan:
    payload = _coerce_mapping(profile)
    python_hints = _coerce_mapping(payload.get("python_hints"))
    language_hints = _coerce_mapping(payload.get("language_hints"))
    test_hints = _coerce_mapping(payload.get("test_runner_hints"))

    package_managers = _coerce_list(
        python_hints.get("package_managers") or language_hints.get("package_managers")
    )
    install_hints = _coerce_list(payload.get("install_hints"))
    if not install_hints:
        install_hints = _coerce_list(language_hints.get("install_hints"))
    test_commands = _coerce_list(
        test_hints.get("commands") or language_hints.get("test_commands")
    )
    versions = _coerce_list(python_hints.get("versions") or language_hints.get("versions"))

    provenance: list[str] = []
    python_version, python_confidence = _choose_python_version(versions, provenance)

    test_cmd_base, test_confidence, test_name = _build_test_command(
        test_commands, provenance
    )
    install, build_cmds, install_confidence, install_name = _build_install_commands(
        package_managers,
        install_hints,
        provenance,
    )

    install, adjusted_install_confidence = _augment_for_pytest(
        install,
        test_cmd_base,
        provenance,
        install_confidence,
    )

    confidence = min(
        1.0, (python_confidence + test_confidence + adjusted_install_confidence) / 3.0
    )
    strategy_name = f"{install_name}:{test_name}"

    return EnvPlan(
        language="python",
        runtime_version=python_version,
        python_version=python_version,
        pre_install=[],
        install=install,
        build=build_cmds,
        test_cmd_base=test_cmd_base,
        strategy_name=strategy_name,
        confidence=round(confidence, 3),
        provenance=provenance,
    )


@dataclass
class PythonAdapter:
    def name(self) -> str:
        return "python"

    def detect(self, repo_root: Path) -> DetectionResult:
        confidence, signals = _detect_python(repo_root)
        runtime_version = _choose_python_version(
            _detect_python_versions(repo_root), []
        )[0]
        return DetectionResult(
            language="python",
            confidence=confidence,
            signals=signals,
            runtime_version=runtime_version if confidence > 0.0 else None,
        )

    def inspect(self, repo_root: Path) -> dict[str, Any]:
        package_managers, install_hints, package_hints = _detect_package_and_install_hints(
            repo_root
        )
        test_commands = _detect_test_runner_hints(repo_root)
        python_versions = _detect_python_versions(repo_root)
        repo_version = _detect_package_version(repo_root)
        runtime_version, _confidence = _choose_python_version(python_versions, [])
        warnings = _parse_repo_profile_warnings(
            package_managers, test_commands, python_versions
        )
        language_hints = {
            "versions": python_versions,
            "package_managers": package_managers,
            "package_style": _detect_package_style(repo_root),
            "signals": package_hints,
        }
        return {
            "language": "python",
            "language_version": runtime_version,
            "repo_version": repo_version,
            "python_hints": language_hints,
            "language_hints": language_hints,
            "install_hints": install_hints,
            "test_runner_hints": {
                "commands": test_commands,
                "signals": [],
            },
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
            prod_extensions={".py", ".pyi"},
            test_filename_patterns=["test_*.py", "*_test.py", "*_test_*.py"],
            test_dir_names={"test", "tests"},
            config_build_filenames={
                ".github",
                "pyproject.toml",
                "setup.py",
                "setup.cfg",
                "tox.ini",
                "noxfile.py",
                "requirements.txt",
                "requirements-dev.txt",
                "requirements.in",
                "pipfile",
                "pipfile.lock",
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
            },
        )

    def harness_template_vars(self, spec: dict[str, Any]) -> dict[str, Any]:
        return {
            "parser_import": "repogauge.parsers.junit.parse_repogauge_junit",
            "parser_name": "junit",
            "ext": "py",
            "install_str_join": " && ",
        }

    def signature_labels(self, profile: dict[str, Any]) -> dict[str, str]:
        python_hints = profile.get("python_hints") if isinstance(profile, dict) else {}
        if not isinstance(python_hints, dict):
            python_hints = {}
        test_runner_hints = (
            profile.get("test_runner_hints") if isinstance(profile, dict) else {}
        )
        if not isinstance(test_runner_hints, dict):
            test_runner_hints = {}
        versions = _coerce_list(python_hints.get("versions"))
        if not versions:
            version = profile.get("language_version") or profile.get("runtime_version")
            if version:
                versions = [str(version)]
        commands = _coerce_list(test_runner_hints.get("commands"))
        managers = _coerce_list(python_hints.get("package_managers"))
        return {
            "runtime_label": _to_python_label(versions),
            "test_label": _to_test_label(commands),
            "package_label": _to_pkg_label(managers),
        }

    def dependency_signature_inputs(
        self, repo_root: Path, profile: dict[str, Any]
    ) -> list[str]:
        return _detect_package_and_install_hints(repo_root)[1]

    def env_overrides(self, worktree: Path) -> dict[str, str]:
        return {"PYTHONPATH": str(worktree)}

    def test_command_attempts(self, test_cmd_base: str) -> list[list[str]]:
        base = test_cmd_base.strip() or _DEFAULT_TEST_CMD
        attempts = [shlex.split(base)]
        if attempts[0]:
            command_name = Path(attempts[0][0]).name.lower()
        else:
            command_name = ""
        if command_name in {"pytest", "pytest.exe"}:
            attempts.append([sys.executable, "-m", "pytest", *attempts[0][1:]])
        return attempts

    def test_report_filename(self) -> str | None:
        return "junit.xml"

    def test_report_glob(self) -> str | None:
        return "junit.xml"


__all__ = [
    "PythonAdapter",
    "build_env_plan",
    "_augment_for_pytest",
    "_augment_uv_install_for_pytest",
    "_build_install_commands",
    "_build_test_command",
    "_choose_python_version",
]
