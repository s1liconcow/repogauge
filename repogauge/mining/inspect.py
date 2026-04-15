from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List
import re

from repogauge.exec import run_command
from repogauge.utils.git import get_default_branch, get_repo_root


def _as_sorted_unique(values: list[str]) -> list[str]:
    return sorted(set(values))


def _to_repo_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _safe_read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""

def _parse_version_tokens(raw: str) -> list[str]:
    found = re.findall(r"\b3\.\d+(?:\.\d+)?\b", raw)
    normalized = []
    for value in found:
        major, minor, *rest = value.split(".")
        normalized.append(f"{major}.{minor}")
    return sorted(set(normalized))


def _parse_requires_python(raw: str) -> list[str]:
    return _parse_version_tokens(raw.replace(",", " "))


def _detect_repo_name(repo_root: Path, warnings: list[dict]) -> str:
    remote_result = run_command(["git", "-C", str(repo_root), "config", "--get", "remote.origin.url"])
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


def _detect_package_and_install_hints(repo_root: Path) -> tuple[list[str], list[str], list[str]]:
    install_hints: list[str] = []
    package_managers: list[str] = []
    hints: list[str] = []

    pyproject = repo_root / "pyproject.toml"
    setup_py = repo_root / "setup.py"
    setup_cfg = repo_root / "setup.cfg"
    requirements_files = sorted(repo_root.glob("requirements*.txt"))
    pipenv_files = [repo_root / "Pipfile"]
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

    if pipenv_files[0].exists():
        package_managers.append("pipenv")
        install_hints.append("pipenv install")

    if uv_lock.exists():
        package_managers.append("uv")
        install_hints.append("uv sync")

    if not install_hints:
        install_hints.append("pip install -e .")

    return (
        _as_sorted_unique(package_managers),
        _as_sorted_unique(install_hints),
        _as_sorted_unique(hints),
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
    return _as_sorted_unique(commands)


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


def _detect_test_paths(repo_root: Path) -> list[str]:
    paths = []
    if (repo_root / "tests").is_dir():
        paths.append("tests")
    if (repo_root / "test").is_dir():
        paths.append("test")
    return paths


def _detect_package_style(repo_root: Path) -> str:
    if (repo_root / "src").is_dir():
        return "src"
    if any((repo_root / p).is_dir() for p in ["lib", "package"]):
        return "flat"
    return "unknown"


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

    package_managers, install_hints, package_hints = _detect_package_and_install_hints(repo_root_resolved)
    test_commands = _detect_test_runner_hints(repo_root_resolved)

    python_versions: list[str] = []
    if (repo_root_resolved / ".python-version").exists():
        python_versions.extend(_parse_version_tokens(_safe_read_text(repo_root_resolved / ".python-version")))
    if (repo_root_resolved / "pyproject.toml").exists():
        python_versions.extend(_parse_requires_python(_safe_read_text(repo_root_resolved / "pyproject.toml")))

    if (repo_root_resolved / "tox.ini").exists():
        tox_text = _safe_read_text(repo_root_resolved / "tox.ini")
        python_versions.extend(_parse_version_tokens(tox_text))
    python_versions = _as_sorted_unique(python_versions)

    warnings.extend(
        _parse_repo_profile_warnings(
            repo_root_resolved,
            package_managers=package_managers,
            test_commands=test_commands,
            python_versions=python_versions,
        )
    )

    return {
        "repo_name": repo_name,
        "repo_root": str(repo_root_resolved),
        "default_branch": default_branch,
        "commit_range": {
            "from": f"{default_branch}~100",
            "to": "HEAD",
        },
        "python_hints": {
            "versions": python_versions,
            "package_managers": package_managers,
            "package_style": _detect_package_style(repo_root_resolved),
            "signals": package_hints,
        },
        "install_hints": install_hints,
        "test_runner_hints": {
            "commands": test_commands,
            "signals": [],
        },
        "ci_files": _detect_ci_files(repo_root_resolved),
        "test_paths": _detect_test_paths(repo_root_resolved),
        "profile_warnings": warnings,
    }
