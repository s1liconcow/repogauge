from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Literal

FileRole = Literal[
    "prod",
    "test",
    "test_support",
    "config_build",
    "docs",
    "generated_vendor",
    "unknown",
]


@dataclass(frozen=True)
class FileRoleClassification:
    path: str
    role: FileRole
    reason: str


def classify_file(path: str | Path) -> FileRoleClassification:
    """Classify a repository file path using deterministic path-based rules."""
    path_obj = Path(path)
    normalized = PurePosixPath(path_obj.as_posix())
    normalized_path = str(normalized).strip("/").lower()
    parts = [part for part in normalized.parts if part not in {".", ""}]
    suffix = path_obj.suffix.lower()
    filename = path_obj.name.lower()

    if not normalized_path:
        return FileRoleClassification(path=str(path_obj), role="unknown", reason="empty or missing path")

    def startswith(prefix: str) -> bool:
        return normalized_path.startswith(prefix)

    def in_dir(name: str) -> bool:
        return name in parts[:-1]

    if any(segment in {"__pycache__", ".mypy_cache", ".pytest_cache", "site-packages", "vendor", ".venv", "venv"} for segment in parts):
        return FileRoleClassification(path=str(path_obj), role="generated_vendor", reason="vendor or generated build cache directory")

    if any(segment in {"dist", "build", ".eggs"} for segment in parts):
        return FileRoleClassification(path=str(path_obj), role="generated_vendor", reason="generated build artifact directory")

    if startswith(".github/") or filename in {
        ".travis.yml",
        ".circleci/config.yml",
        "tox.ini",
        "noxfile.py",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "requirements.txt",
        "requirements-dev.txt",
        "requirements.in",
        "pipfile",
        "pipfile.lock",
        "dockerfile",
        "docker-compose.yml",
    }:
        return FileRoleClassification(path=str(path_obj), role="config_build", reason="CI, package, or tooling configuration file")

    if startswith("docs/") or suffix in {".md", ".rst"} and in_dir("docs"):
        return FileRoleClassification(path=str(path_obj), role="docs", reason="documentation file or directory")

    if in_dir("docs") and filename not in {"changelog", "change_log.md"}:
        return FileRoleClassification(path=str(path_obj), role="docs", reason="under docs directory")

    if filename in {"readme", "readme.md", "readme.rst", "changelog", "changelog.md", "history.md", "license", "license.md"}:
        return FileRoleClassification(path=str(path_obj), role="docs", reason="project documentation file")

    if startswith("tests/") or startswith("test/"):
        if any(
            fixture_dir in parts[:-1]
            for fixture_dir in {"fixtures", "fixture", "testdata", "mocks", "mock", "resources", "helpers", "support"}
        ):
            return FileRoleClassification(path=str(path_obj), role="test_support", reason="test-support path under tests")
        if filename in {"conftest.py", "pytest.ini", "tox.ini"}:
            return FileRoleClassification(path=str(path_obj), role="test_support", reason="test harness support file")
        if filename.startswith("test_") or filename.endswith("_test.py") or "_test_" in filename:
            return FileRoleClassification(path=str(path_obj), role="test", reason="test filename convention")
        if in_dir("tests"):
            return FileRoleClassification(path=str(path_obj), role="test", reason="file under tests directory")
        return FileRoleClassification(path=str(path_obj), role="test_support", reason="test-related path outside conventional test naming")

    if suffix in {".py", ".pyi", ".ts", ".tsx", ".js", ".jsx"}:
        return FileRoleClassification(path=str(path_obj), role="prod", reason="runtime source extension outside docs/test paths")

    if in_dir("src") and not startswith("docs/"):
        return FileRoleClassification(path=str(path_obj), role="prod", reason="source directory file")

    return FileRoleClassification(path=str(path_obj), role="unknown", reason="No explicit role rule matched; reviewed later")


def classify_files(paths: Iterable[str | Path]) -> dict[str, FileRoleClassification]:
    """Classify a collection of paths and return results keyed by original path."""
    return {str(path): classify_file(path) for path in paths}

