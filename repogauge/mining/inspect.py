from __future__ import annotations

from configparser import ConfigParser
from pathlib import Path
from typing import Any, Dict
import re

from repogauge.lang import detect_language, find_adapter, iter_adapters
from repogauge.mining.signature import REPO_VERSION_UNKNOWN, build_environment_signature
from repogauge.exec import run_command
from repogauge.utils.git import get_default_branch, get_repo_root
from repogauge.validation.env_detect import build_environment_plan

try:
    import tomllib
except Exception:  # pragma: no cover
    import tomli as tomllib  # type: ignore[import-not-found]


def _as_sorted_unique(values: list[str]) -> list[str]:
    return sorted(set(values))


def _to_repo_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


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


def _detect_repo_name(repo_root: Path, warnings: list[dict]) -> str:
    remote_result = run_command(
        ["git", "-C", str(repo_root), "config", "--get", "remote.origin.url"]
    )
    if remote_result.success and remote_result.stdout.strip():
        remote = remote_result.stdout.strip().rstrip("/")
        remote = re.sub(r"\.git$", "", remote)
        if "://" in remote:
            remainder = remote.split("://", 1)[1]
            parts = remainder.split("/")
            if len(parts) >= 2 and parts[-1]:
                return f"{parts[-2]}/{parts[-1]}"
        if "@" in remote and ":" in remote:
            _, after = remote.split(":", 1)
            if "/" in after:
                owner_repo = after.rsplit("/", 1)[-2:]
                if len(owner_repo) == 2:
                    return f"{owner_repo[0]}/{owner_repo[1]}"
    warnings.append(
        {
            "type": "remote_parse_failed",
            "message": "Could not parse remote origin URL for repo identity",
        }
    )
    return repo_root.name


def _parse_repo_profile_warnings(
    repo_root: Path,
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


def _detect_ci_files(repo_root: Path) -> list[str]:
    raw = []
    for path in [
        repo_root / ".travis.yml",
        repo_root / ".circleci" / "config.yml",
        repo_root / ".github" / "workflows",
        repo_root / "azure-pipelines.yml",
    ]:
        if path.is_dir():
            for wf in sorted(path.glob("*.yml")):
                raw.append(str(wf.relative_to(repo_root)))
            for wf in sorted(path.glob("*.yaml")):
                raw.append(str(wf.relative_to(repo_root)))
        elif path.exists():
            raw.append(str(path.relative_to(repo_root)))
    return _as_sorted_unique(raw)


def inspect_repository(path: str | Path) -> Dict[str, Any]:
    repo_root = _to_repo_path(path)
    repo_root_resolved = repo_root
    warnings: list[dict[str, str]] = []

    try:
        repo_root_resolved = get_repo_root(repo_root)
    except Exception:
        warnings.append(
            {
                "type": "not_a_git_repo",
                "message": f"{repo_root} is not a git repository",
            }
        )

    repo_name = _detect_repo_name(repo_root_resolved, warnings)

    try:
        default_branch = get_default_branch(repo_root_resolved)
    except Exception:
        default_branch = "main"
        warnings.append(
            {
                "type": "default_branch_unknown",
                "message": "Could not determine default branch",
            }
        )

    detection = detect_language(repo_root_resolved)
    language_detections: list[dict[str, Any]] = []
    for adapter in iter_adapters():
        result = adapter.detect(repo_root_resolved)
        language_detections.append(
            {
                "name": adapter.name(),
                "confidence": result.confidence,
                "signals": list(result.signals),
            }
        )

    try:
        adapter = find_adapter(detection.language)
        language = detection.language
    except KeyError:
        adapter = find_adapter("python")
        language = adapter.name()

    inspected = adapter.inspect(repo_root_resolved)
    language_hints = inspected.get("language_hints")
    if not isinstance(language_hints, dict):
        language_hints = inspected.get("python_hints")
    if not isinstance(language_hints, dict):
        language_hints = {}
    python_hints = inspected.get("python_hints")
    if not isinstance(python_hints, dict):
        python_hints = {}

    install_hints = inspected.get("install_hints")
    if not isinstance(install_hints, list):
        install_hints = []
    test_runner_hints = inspected.get("test_runner_hints")
    if not isinstance(test_runner_hints, dict):
        test_runner_hints = {"commands": [], "signals": []}
    test_paths = inspected.get("test_paths")
    if not isinstance(test_paths, list):
        test_paths = []
    profile_warnings = inspected.get("profile_warnings")
    if not isinstance(profile_warnings, list):
        profile_warnings = []
    repo_version = inspected.get("repo_version", REPO_VERSION_UNKNOWN)
    if not isinstance(repo_version, str) or not repo_version.strip():
        repo_version = REPO_VERSION_UNKNOWN

    warnings.extend(
        [item for item in profile_warnings if isinstance(item, dict)]
    )

    profile = {
        "repo_name": repo_name,
        "repo_root": str(repo_root_resolved),
        "repo_version": repo_version,
        "default_branch": default_branch,
        "language": language,
        "language_version": detection.runtime_version
        or inspected.get("language_version")
        or inspected.get("runtime_version")
        or "",
        "commit_range": {
            "from": f"{default_branch}~100",
            "to": "HEAD",
        },
        "language_hints": language_hints,
        "install_hints": install_hints,
        "test_runner_hints": test_runner_hints,
        "ci_files": _detect_ci_files(repo_root_resolved),
        "test_paths": test_paths,
        "language_detection": language_detections,
        "profile_warnings": warnings,
    }
    if language == "python":
        profile["python_hints"] = python_hints or language_hints
    profile["environment_signature"] = build_environment_signature(profile)
    profile["environment_plan"] = build_environment_plan(profile).to_dict()
    profile["version"] = profile["environment_signature"]["version"]

    return profile
