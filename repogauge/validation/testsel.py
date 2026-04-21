"""Targeted test selection helpers for validation runs.

Target selection is intentionally conservative at dataset/export time.
We only emit stable file- or directory-level pytest inputs here; concrete
node IDs are resolved later from the actual checkout under test.
"""

from __future__ import annotations

import shlex
from typing import List, Tuple

_PYTEST_CMD_PREFIX = "python -m pytest"
_JUNIT_XML_PLACEHOLDER = "{junit_xml}"
_JUNIT_XML_FLAGS = ("--junit-xml", "--junitxml")


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


def extract_patch_paths(test_patch: str) -> List[str]:
    """Extract candidate file paths from unified-diff headers."""
    paths: List[str] = []
    for line in test_patch.splitlines():
        if line.startswith("diff --git "):
            try:
                tokens = shlex.split(line)
            except ValueError:
                tokens = []
            if len(tokens) >= 4:
                candidate = tokens[3]
                if candidate.startswith("b/"):
                    candidate = candidate[2:]
                if (
                    candidate
                    and candidate != "/dev/null"
                    and not candidate.endswith(".rej")
                ):
                    paths.append(candidate)
            continue
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
    return any(part == flag or part.startswith(f"{flag}=") for part in parts)


def _command_has_junit_flag(parts: List[str]) -> bool:
    return any(_command_has_flag(parts, flag) for flag in _JUNIT_XML_FLAGS)


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
    if not _command_has_junit_flag(parts):
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


def build_targeted_test_inputs(test_patch: str) -> List[str]:
    """Build conservative pytest inputs from changed files in `test_patch`.

    The returned values are stable file- or directory-level inputs. Runtime
    validation can refine these to concrete node IDs after checking out the
    exact tree for each validation pass.
    """
    changed = extract_patch_paths(test_patch)
    if not changed:
        return []

    test_files = [
        path
        for path in changed
        if _is_test_path(path) and not _is_test_support_path(path)
    ]
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
    "extract_patch_paths",
    "build_targeted_test_inputs",
    "build_targeted_test_plan",
]
