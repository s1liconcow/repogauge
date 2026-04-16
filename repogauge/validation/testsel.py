"""Targeted test selection helpers for validation runs."""

from __future__ import annotations

import re
import shlex
from typing import List, Tuple


_TEST_NODE_RE = re.compile(
    r"^\+\s*(?:async\s+)?def\s+(test_[A-Za-z_][A-Za-z0-9_]*)\s*\("
)
_TEST_CLASS_RE = re.compile(r"^\+\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\s*[:(]")
_PYTEST_CMD_PREFIX = "python -m pytest"
_JUNIT_XML_PLACEHOLDER = "{junit_xml}"


def _dedupe(values: List[str]) -> List[str]:
    """Return deterministic unique ordering while preserving first-seen order."""
    seen = set()
    out: List[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _extract_patch_paths(test_patch: str) -> List[str]:
    """Extract candidate file paths from unified-diff headers."""
    paths: List[str] = []
    for line in test_patch.splitlines():
        if not line.startswith("+++ b/"):
            continue
        path = line[6:].strip()
        if not path or path == "/dev/null":
            continue
        if path.endswith(".rej"):
            continue
        paths.append(path)
    return _dedupe(paths)


def _is_pytest_cmd(test_cmd_base: str) -> bool:
    candidate = (test_cmd_base or "").strip()
    if not candidate:
        return True
    return candidate.startswith("pytest") or candidate.startswith(_PYTEST_CMD_PREFIX)


def _command_has_flag(parts: List[str], flag: str) -> bool:
    if not parts:
        return False
    if flag.endswith("="):
        return any(part.startswith(flag) for part in parts)
    return flag in parts


def _build_pytest_targeted_cmd(test_cmd_base: str) -> str:
    candidate = test_cmd_base.strip() or _PYTEST_CMD_PREFIX
    try:
        parts = shlex.split(candidate)
    except ValueError:
        parts = [candidate]

    if not parts:
        parts = [_PYTEST_CMD_PREFIX]

    if not _command_has_flag(parts, "--tb=no"):
        parts.append("--tb=no")
    if not _command_has_flag(parts, "-q"):
        parts.append("-q")
    if not _command_has_flag(parts, "--junit-xml="):
        parts.append(f"--junit-xml={_JUNIT_XML_PLACEHOLDER}")

    return " ".join(parts)


def _is_test_path(path: str) -> bool:
    lower = path.lower()
    base = lower.rsplit("/", 1)[-1]
    return lower.endswith(".py") and (
        lower.startswith("test/")
        or lower.startswith("tests/")
        or lower.startswith("spec/")
        or lower.startswith("testsuites/")
        or base.startswith("test_")
        or base.endswith("_test.py")
        or "_test_" in base
        or "spec" in base
    )


def _is_test_support_path(path: str) -> bool:
    lower = path.lower()
    return lower.startswith("tests/") and (
        lower.endswith("conftest.py")
        or lower.endswith("pytest.ini")
        or lower.endswith("tox.ini")
        or "fixtures" in lower
        or lower.endswith("testdata")
    )


def _extract_test_node_ids(test_patch: str) -> List[str]:
    """Infer pytest node IDs from added test definitions in the patch."""
    test_nodes: List[str] = []
    current_test_file: str | None = None
    current_class: str | None = None

    for line in test_patch.splitlines():
        if line.startswith("+++ b/"):
            current_test_file = line[6:].strip()
            current_class = None
            continue
        if line.startswith("--- a/") or line.startswith("@@ "):
            continue
        if not current_test_file:
            continue
        if not _is_test_path(current_test_file):
            continue
        if line.startswith("-"):
            continue
        class_match = _TEST_CLASS_RE.match(line)
        if class_match:
            current_class = class_match.group(1)
            continue
        func_match = _TEST_NODE_RE.match(line)
        if not func_match:
            continue
        func = func_match.group(1)
        if current_class:
            test_nodes.append(f"{current_test_file}::{current_class}::{func}")
        else:
            test_nodes.append(f"{current_test_file}::{func}")

    return _dedupe(test_nodes)


def build_targeted_test_inputs(test_patch: str) -> List[str]:
    """Build conservative pytest inputs from changed files in `test_patch`."""
    changed = _extract_patch_paths(test_patch)
    if not changed:
        return []

    test_files = [
        path
        for path in changed
        if _is_test_path(path) and not _is_test_support_path(path)
    ]
    test_nodes = _extract_test_node_ids(test_patch)
    if test_nodes:
        return test_nodes
    if test_files:
        return _dedupe(test_files)

    if any(_is_test_support_path(path) for path in changed):
        return ["tests"]

    return []


def build_targeted_test_plan(
    test_cmd_base: str, test_patch: str
) -> Tuple[str, List[str]]:
    """Return `(targeted_test_cmd, targeted_test_inputs)` for a dataset row."""
    if not _is_pytest_cmd(test_cmd_base):
        return test_cmd_base, []

    targeted_test_inputs = build_targeted_test_inputs(test_patch)
    targeted_test_cmd = _build_pytest_targeted_cmd(test_cmd_base)
    return targeted_test_cmd, targeted_test_inputs


__all__ = [
    "build_targeted_test_inputs",
    "build_targeted_test_plan",
]
