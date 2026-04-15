"""Utilities for splitting unified diffs into production and test patches."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from repogauge.mining.file_roles import classify_file


_HEADER_LINE_PREFIX = "diff --git "
_TEST_HELPER_FILES = {"conftest.py", "pytest.ini", "tox.ini"}


class PatchSplitError(ValueError):
    """Raised when a diff cannot be split with MVP-safe rules."""


@dataclass
class _FilePatchChunk:
    path: str
    raw_lines: List[str]
    role: str


def _strip_quote(value: str) -> str:
    if len(value) >= 2 and ((value[0] == '"' and value[-1] == '"') or (value[0] == "'" and value[-1] == "'")):
        return value[1:-1]
    return value


def _normalize_token(token: str) -> str:
    token = _strip_quote(token.strip())
    if token.startswith("a/"):
        token = token[2:]
    elif token.startswith("b/"):
        token = token[2:]
    return token


def _parse_diff_header(line: str) -> Optional[tuple[str, str]]:
    if not line.startswith(_HEADER_LINE_PREFIX):
        return None

    try:
        tokens = shlex.split(line)
    except ValueError:
        return None

    if len(tokens) < 4:
        return None

    return (_normalize_token(tokens[2]), _normalize_token(tokens[3]))


def _role_for_path(path: str) -> str:
    return classify_file(path).role


def _is_test_bucket(path: str, role: str) -> bool:
    name = Path(path).name
    return role == "test" or role == "test_support" or name in _TEST_HELPER_FILES


def _is_test_file_boundary_split(a_path: str, b_path: str) -> bool:
    a_role = _role_for_path(a_path)
    b_role = _role_for_path(b_path)
    return _is_test_bucket(a_path, a_role) != _is_test_bucket(b_path, b_role)


def _extract_rename_lines(lines: List[str]) -> tuple[Optional[str], Optional[str]]:
    rename_from: Optional[str] = None
    rename_to: Optional[str] = None
    for raw_line in lines:
        if raw_line.startswith("rename from "):
            rename_from = raw_line[len("rename from ") :].strip()
        elif raw_line.startswith("rename to "):
            rename_to = raw_line[len("rename to ") :].strip()
    return rename_from, rename_to


def _bucket_for_file(path: str, role: str, include_test_support: bool) -> str:
    if role == "test":
        return "test"
    if role == "test_support" and include_test_support:
        return "test"
    if Path(path).name in _TEST_HELPER_FILES and include_test_support:
        return "test"
    return "prod"


def _split_diff_files(diff: str) -> Tuple[List[_FilePatchChunk], List[str]]:
    chunks: List[_FilePatchChunk] = []
    touched_paths: List[str] = []
    current_lines: List[str] = []
    current_paths: Optional[tuple[str, str]] = None

    for raw_line in diff.splitlines(keepends=True):
        parsed = _parse_diff_header(raw_line)
        if parsed:
            if current_paths is not None:
                a_path, b_path = current_paths
                rename_from, rename_to = _extract_rename_lines(current_lines)
                from_path = _normalize_token(_strip_quote(rename_from or a_path))
                to_path = _normalize_token(_strip_quote(rename_to or b_path))
                if rename_from and rename_to and _is_test_file_boundary_split(from_path, to_path):
                    raise PatchSplitError("rename across production/test boundary is not supported in MVP")

                destination = to_path or b_path or a_path
                if destination == "/dev/null" and a_path != "/dev/null":
                    destination = a_path
                role = _role_for_path(destination)
                chunks.append(_FilePatchChunk(path=destination, raw_lines=current_lines, role=role))
                if destination not in touched_paths:
                    touched_paths.append(destination)

            current_lines = [raw_line]
            current_paths = parsed
            continue

        if current_lines:
            current_lines.append(raw_line)

    if current_paths is not None:
        a_path, b_path = current_paths
        rename_from, rename_to = _extract_rename_lines(current_lines)
        from_path = _normalize_token(_strip_quote(rename_from or a_path))
        to_path = _normalize_token(_strip_quote(rename_to or b_path))
        if rename_from and rename_to and _is_test_file_boundary_split(from_path, to_path):
            raise PatchSplitError("rename across production/test boundary is not supported in MVP")

        destination = to_path or b_path or a_path
        if destination == "/dev/null" and a_path != "/dev/null":
            destination = a_path
        role = _role_for_path(destination)
        chunks.append(_FilePatchChunk(path=destination, raw_lines=current_lines, role=role))
        if destination not in touched_paths:
            touched_paths.append(destination)

    return chunks, touched_paths


def split_prod_and_test(diff: str) -> Tuple[str, str, Dict[str, List[str]]]:
    file_chunks, touched_paths = _split_diff_files(diff)
    has_test_file = any(chunk.role == "test" for chunk in file_chunks)

    prod_lines: List[str] = []
    test_lines: List[str] = []
    touched: Dict[str, List[str]] = {"prod": [], "test": [], "test_support": []}

    for chunk in file_chunks:
        bucket = _bucket_for_file(chunk.path, chunk.role, include_test_support=has_test_file)
        if bucket == "test":
            test_lines.extend(chunk.raw_lines)
            if chunk.role == "test":
                touched["test"].append(chunk.path)
            else:
                touched["test_support"].append(chunk.path)
        else:
            prod_lines.extend(chunk.raw_lines)
            touched["prod"].append(chunk.path)

    return "".join(prod_lines), "".join(test_lines), {
        "prod_files": touched["prod"],
        "test_files": touched["test"] + touched["test_support"],
        "test_support_files": touched["test_support"],
        "all_touched_files": touched_paths,
    }
