from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Literal

from repogauge.lang import FileRoleRules, iter_adapters
from repogauge.mining.inspect import inspect_repository
from repogauge.utils.git import get_repo_root


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


_COMMON_RULES = FileRoleRules(
    prod_extensions=set(),
    test_filename_patterns=[],
    test_dir_names=set(),
    config_build_filenames=set(),
    vendor_dir_names={
        ".beads",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        "site-packages",
        "vendor",
        ".venv",
        "venv",
    },
)

_COMMON_BUILD_DIR_NAMES = {"dist", "build", ".eggs"}
_COMMON_CONFIG_BUILD_PATH_PREFIXES = (".github/", ".circleci/")
_COMMON_CONFIG_BUILD_FILENAMES = {
    ".travis.yml",
    "dockerfile",
    "docker-compose.yml",
}
_TEST_FIXTURE_DIR_NAMES = {
    "fixtures",
    "fixture",
    "testdata",
    "mocks",
    "mock",
    "resources",
    "helpers",
    "support",
}


def _normalize_strings(values: Iterable[object]) -> list[str]:
    normalized = {
        str(value).strip().lower()
        for value in values
        if str(value).strip()
    }
    return sorted(normalized)


def _merge_rule_sets(rule_sets: Iterable[FileRoleRules]) -> FileRoleRules:
    prod_extensions: set[str] = set()
    test_filename_patterns: set[str] = set()
    test_dir_names: set[str] = set()
    config_build_filenames: set[str] = set()
    vendor_dir_names: set[str] = set(_COMMON_RULES.vendor_dir_names)
    test_support_filenames: set[str] = set()

    for rules in rule_sets:
        prod_extensions.update(_normalize_strings(rules.prod_extensions))
        test_filename_patterns.update(_normalize_strings(rules.test_filename_patterns))
        test_dir_names.update(_normalize_strings(rules.test_dir_names))
        config_build_filenames.update(_normalize_strings(rules.config_build_filenames))
        vendor_dir_names.update(_normalize_strings(rules.vendor_dir_names))
        test_support_filenames.update(_normalize_strings(rules.test_support_filenames))

    return FileRoleRules(
        prod_extensions=prod_extensions,
        test_filename_patterns=sorted(test_filename_patterns),
        test_dir_names=test_dir_names,
        config_build_filenames=config_build_filenames,
        vendor_dir_names=vendor_dir_names,
        test_support_filenames=test_support_filenames,
    )


@lru_cache(maxsize=1)
def _registered_rule_pairs() -> tuple[tuple[str, FileRoleRules], ...]:
    return tuple(
        (adapter.name(), adapter.file_role_rules()) for adapter in iter_adapters()
    )


@lru_cache(maxsize=1)
def _merged_rules_cached() -> FileRoleRules:
    return _merge_rule_sets([_COMMON_RULES, *[rules for _, rules in _registered_rule_pairs()]])


@lru_cache(maxsize=128)
def _active_language_names(repo_root: str) -> tuple[str, ...]:
    try:
        profile = inspect_repository(repo_root)
    except Exception:
        return ()

    detections = profile.get("language_detection")
    if not isinstance(detections, list):
        return ()

    active_names: set[str] = set()
    for item in detections:
        if not isinstance(item, Mapping):
            continue
        try:
            confidence = float(item.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if confidence <= 0.0:
            continue
        name = item.get("name")
        if isinstance(name, str):
            normalized = name.strip().lower()
            if normalized:
                active_names.add(normalized)
    return tuple(sorted(active_names))


def _repo_root_for_path(path_obj: Path) -> Path | None:
    try:
        return get_repo_root(path_obj.resolve().parent)
    except Exception:
        return None


def _all_rule_sets() -> list[FileRoleRules]:
    return [_COMMON_RULES, *[rules for _, rules in _registered_rule_pairs()]]


def _test_rules_for_repo_path(path_obj: Path) -> list[FileRoleRules]:
    repo_root = _repo_root_for_path(path_obj)
    if repo_root is None:
        return _all_rule_sets()

    active_names = set(_active_language_names(str(repo_root)))
    if not active_names:
        return _all_rule_sets()

    selected = [
        rules
        for name, rules in _registered_rule_pairs()
        if name.lower() in active_names
    ]
    if not selected:
        return _all_rule_sets()
    return [_COMMON_RULES, *selected]


def _matches_any_pattern(filename: str, patterns: Sequence[str]) -> bool:
    return any(fnmatchcase(filename, pattern) for pattern in patterns)


def merged_rules() -> FileRoleRules:
    return _merged_rules_cached()


def reset_cache() -> None:
    _registered_rule_pairs.cache_clear()
    _merged_rules_cached.cache_clear()
    _active_language_names.cache_clear()


def rules_for_language_detections(
    language_detections: Iterable[Mapping[str, object]],
) -> list[FileRoleRules]:
    active_names: set[str] = set()
    for item in language_detections:
        if not isinstance(item, Mapping):
            continue
        try:
            confidence = float(item.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if confidence <= 0.0:
            continue
        name = item.get("name")
        if isinstance(name, str):
            normalized = name.strip().lower()
            if normalized:
                active_names.add(normalized)

    if not active_names:
        return _all_rule_sets()

    selected = [
        rules
        for name, rules in _registered_rule_pairs()
        if name.lower() in active_names
    ]
    if not selected:
        return _all_rule_sets()
    return [_COMMON_RULES, *selected]


def classify_file(
    path: str | Path, *, rules: list[FileRoleRules] | None = None
) -> FileRoleClassification:
    """Classify a repository file path using deterministic path-based rules."""
    path_obj = Path(path)
    normalized = PurePosixPath(path_obj.as_posix())
    normalized_path = str(normalized).strip("/").lower()
    parts = [part.lower() for part in normalized.parts if part not in {".", ""}]
    suffix = path_obj.suffix.lower()
    filename = path_obj.name.lower()

    if not normalized_path:
        return FileRoleClassification(
            path=str(path_obj), role="unknown", reason="empty or missing path"
        )

    if rules is not None:
        merged = _merge_rule_sets([_COMMON_RULES, *rules])
        test_merged = merged
    else:
        merged = merged_rules()
        test_merged = _merge_rule_sets(_test_rules_for_repo_path(path_obj))

    def startswith(prefix: str) -> bool:
        return normalized_path.startswith(prefix)

    def in_dir(name: str) -> bool:
        return name in parts[:-1]

    if any(segment in merged.vendor_dir_names for segment in parts):
        return FileRoleClassification(
            path=str(path_obj),
            role="generated_vendor",
            reason="vendor or generated build cache directory",
        )

    if any(segment in _COMMON_BUILD_DIR_NAMES for segment in parts):
        return FileRoleClassification(
            path=str(path_obj),
            role="generated_vendor",
            reason="generated build artifact directory",
        )

    if (
        any(startswith(prefix) for prefix in _COMMON_CONFIG_BUILD_PATH_PREFIXES)
        or filename in _COMMON_CONFIG_BUILD_FILENAMES
        or filename in merged.config_build_filenames
    ):
        return FileRoleClassification(
            path=str(path_obj),
            role="config_build",
            reason="CI, package, or tooling configuration file",
        )

    if filename in test_merged.test_support_filenames:
        return FileRoleClassification(
            path=str(path_obj),
            role="test_support",
            reason="test harness support file",
        )

    if startswith("docs/") or suffix in {".md", ".rst"} and in_dir("docs"):
        return FileRoleClassification(
            path=str(path_obj), role="docs", reason="documentation file or directory"
        )

    if in_dir("docs") and filename not in {"changelog", "change_log.md"}:
        return FileRoleClassification(
            path=str(path_obj), role="docs", reason="under docs directory"
        )

    if filename in {
        "readme",
        "readme.md",
        "readme.rst",
        "changelog",
        "changelog.md",
        "history.md",
        "license",
        "license.md",
    }:
        return FileRoleClassification(
            path=str(path_obj), role="docs", reason="project documentation file"
        )

    if any(part in test_merged.test_dir_names for part in parts[:-1]):
        if any(part in _TEST_FIXTURE_DIR_NAMES for part in parts[:-1]):
            return FileRoleClassification(
                path=str(path_obj),
                role="test_support",
                reason="test-support path under tests",
            )
        if _matches_any_pattern(filename, test_merged.test_filename_patterns):
            return FileRoleClassification(
                path=str(path_obj), role="test", reason="test filename convention"
            )
        if suffix in test_merged.prod_extensions:
            return FileRoleClassification(
                path=str(path_obj),
                role="test",
                reason="test source file under language-specific test directory",
            )
        return FileRoleClassification(
            path=str(path_obj),
            role="test_support",
            reason="test-related path outside conventional test naming",
        )

    if _matches_any_pattern(filename, test_merged.test_filename_patterns):
        return FileRoleClassification(
            path=str(path_obj), role="test", reason="test filename convention"
        )

    if suffix in merged.prod_extensions:
        return FileRoleClassification(
            path=str(path_obj),
            role="prod",
            reason="runtime source extension outside docs/test paths",
        )

    if in_dir("src") and not startswith("docs/"):
        return FileRoleClassification(
            path=str(path_obj), role="prod", reason="source directory file"
        )

    return FileRoleClassification(
        path=str(path_obj),
        role="unknown",
        reason="No explicit role rule matched; reviewed later",
    )


def classify_files(
    paths: Iterable[str | Path], *, rules: list[FileRoleRules] | None = None
) -> dict[str, FileRoleClassification]:
    """Classify a collection of paths and return results keyed by original path."""
    return {str(path): classify_file(path, rules=rules) for path in paths}
