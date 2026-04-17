"""Normalization utilities for solver outputs into unified patches."""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from repogauge.exec import run_command
from repogauge.utils.git import CommandPatchError, apply_patch_text

from .workspaces import AttemptWorkspace


class PatchNormalizationError(RuntimeError):
    """Raised when a solver output cannot be normalized safely."""


@dataclass(frozen=True)
class PatchStats:
    files_touched: int
    files_added: int
    files_modified: int
    files_removed: int
    insertions: int
    deletions: int


@dataclass(frozen=True)
class PatchNormalizationResult:
    attempt_id: str
    normalized_patch_path: str
    patch_stats_path: str
    raw_output_path: str
    patch: str
    patch_stats: PatchStats


_DIFF_HEADER_PREFIX = "diff --git "


def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _normalize_token(token: str) -> str:
    token = token.strip()
    if token.startswith("a/"):
        token = token[2:]
    elif token.startswith("b/"):
        token = token[2:]
    if token.startswith('"') and token.endswith('"'):
        token = token[1:-1]
    if token.startswith("'") and token.endswith("'"):
        token = token[1:-1]
    return token


def _parse_diff_header(line: str) -> tuple[str, str] | None:
    if not line.startswith(_DIFF_HEADER_PREFIX):
        return None

    try:
        tokens = shlex.split(line)
    except ValueError:
        return None

    if len(tokens) < 4:
        return None

    return _normalize_token(tokens[2]), _normalize_token(tokens[3])


def _extract_unified_patch(raw: str) -> str:
    normalized = _coerce_str(raw).strip()
    if not normalized:
        return ""

    lines = normalized.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("diff --git"):
            patch_lines = "\n".join(lines[index:]).strip("\n")
            if patch_lines:
                return patch_lines if patch_lines.endswith("\n") else patch_lines + "\n"

    m = re.search(r"```(?:diff|patch)\n(.*?)```", normalized, flags=re.S | re.I)
    if m:
        code = m.group(1).strip()
        if "diff --git" in code:
            return code if code.endswith("\n") else code + "\n"

    if normalized.startswith("{") or normalized.startswith("["):
        return ""

    return ""


def _extract_edit_plan(raw: str) -> list[tuple[str, str]]:
    text = _coerce_str(raw).strip()
    if not text:
        return []

    payload: Any
    try:
        payload = json.loads(text)
    except Exception:
        m = re.search(r"```json\n(.*?)```", text, flags=re.S | re.I)
        if not m:
            return []
        payload = json.loads(m.group(1).strip())

    files: list[tuple[str, str]] = []

    if isinstance(payload, Mapping):
        if (
            isinstance(payload.get("model_patch"), str)
            and "diff --git" in payload["model_patch"]
        ):
            return []
        if isinstance(payload.get("patch"), str) and "diff --git" in payload["patch"]:
            return []

        mapping = payload.get("files")
        if isinstance(mapping, Mapping):
            for path, content in mapping.items():
                if isinstance(path, str) and isinstance(
                    content, (str, int, float, bool, type(None))
                ):
                    files.append((path, _coerce_str(content)))
        elif isinstance(mapping, list):
            for entry in mapping:
                if not isinstance(entry, Mapping):
                    continue
                path = entry.get("path")
                content = entry.get("content")
                if isinstance(path, str) and isinstance(
                    content, (str, int, float, bool, type(None))
                ):
                    files.append((path, _coerce_str(content)))

        edits = payload.get("edits")
        if isinstance(edits, list):
            for entry in edits:
                if not isinstance(entry, Mapping):
                    continue
                path = entry.get("path")
                content = entry.get("content")
                if isinstance(path, str) and isinstance(
                    content, (str, int, float, bool, type(None))
                ):
                    files.append((path, _coerce_str(content)))

    if isinstance(payload, list):
        for entry in payload:
            if not isinstance(entry, Mapping):
                continue
            path = entry.get("path")
            content = entry.get("content")
            if isinstance(path, str) and isinstance(
                content, (str, int, float, bool, type(None))
            ):
                files.append((path, _coerce_str(content)))

    return files


def _safe_path_for_workspace(workspace: Path, path: str) -> Path:
    candidate = (workspace / path).resolve()
    workspace_resolved = workspace.resolve()
    if not candidate.is_relative_to(workspace_resolved):
        raise PatchNormalizationError(f"path escapes workspace: {path}")
    return candidate


def _validate_patch_paths(patch: str, workspace: Path) -> None:
    for line in patch.splitlines():
        parsed = _parse_diff_header(line)
        if not parsed:
            continue
        a_path, b_path = parsed
        for raw_path in (a_path, b_path):
            if raw_path == "/dev/null":
                continue
            target = Path(raw_path)
            if target.is_absolute():
                raise PatchNormalizationError(
                    f"patch path must be inside repo root: {raw_path}"
                )
            _safe_path_for_workspace(workspace, str(target))


def _is_binary_payload(content: str) -> bool:
    return "\x00" in content


def _collect_changed_file_paths(workspace: Path) -> list[tuple[str, str]]:
    staged = run_command(
        ["git", "-C", str(workspace), "diff", "--cached", "--name-status"]
    )
    if not staged.success:
        raise PatchNormalizationError(
            f"failed to inspect staged changes: {staged.stderr or staged.stdout}"
        )

    changed: list[tuple[str, str]] = []
    for raw_line in staged.stdout.splitlines():
        if not raw_line.strip():
            continue
        parts = raw_line.split("\t")
        if not parts:
            continue
        status = parts[0]
        if status.startswith("R") and len(parts) >= 3:
            raw_path = parts[2]
        elif len(parts) >= 2:
            raw_path = parts[1]
        else:
            continue
        changed.append((status, raw_path))

        candidate = Path(raw_path)
        if candidate.is_absolute():
            raise PatchNormalizationError(
                f"tracked change escaped repository root: {raw_path}"
            )
        _safe_path_for_workspace(workspace, str(candidate))

    return changed


def _assert_not_binary(workspace: Path, status: str, path: str) -> None:
    if status.startswith("D"):
        return

    resolved = (workspace / path).resolve()
    if not resolved.exists() or not resolved.is_file():
        return

    if resolved.read_bytes().find(b"\x00") != -1:
        raise PatchNormalizationError(f"binary file change not allowed: {path}")


def _collect_patch_stats(workspace: Path) -> PatchStats:
    diff = run_command(["git", "-C", str(workspace), "diff", "--cached", "--numstat"])
    if not diff.success:
        raise PatchNormalizationError(
            f"failed to collect diff statistics: {diff.stderr or diff.stdout}"
        )

    files_added = files_modified = files_removed = 0
    additions = deletions = 0

    by_status = _collect_changed_file_paths(workspace)
    for status, path in by_status:
        if status.startswith("A"):
            files_added += 1
        elif status.startswith("D"):
            files_removed += 1
        elif status.startswith("R"):
            files_added += 1
        else:
            files_modified += 1

    for line in diff.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        ins, dele = parts[0].strip(), parts[1].strip()
        if ins == "-" or dele == "-":
            path = parts[-1] if len(parts) >= 3 else "<unknown>"
            raise PatchNormalizationError(f"binary changes are not supported: {path}")
        additions += int(ins)
        deletions += int(dele)

    return PatchStats(
        files_touched=len(by_status),
        files_added=files_added,
        files_modified=files_modified,
        files_removed=files_removed,
        insertions=additions,
        deletions=deletions,
    )


def _normalize_from_file_edits(
    workspace: Path, edits: Iterable[tuple[str, str]]
) -> None:
    for raw_path, content in edits:
        safe_path = _safe_path_for_workspace(workspace, raw_path)
        safe_path.parent.mkdir(parents=True, exist_ok=True)
        if _is_binary_payload(content):
            raise PatchNormalizationError(
                f"binary content not allowed for file edit output: {raw_path}"
            )
        safe_path.write_text(content, encoding="utf-8")


def _normalize_from_patch(workspace: Path, patch: str) -> None:
    _validate_patch_paths(patch, workspace)
    try:
        apply_patch_text(workspace, patch, check=True)
    except CommandPatchError as exc:
        raise PatchNormalizationError(f"failed to apply solver patch: {exc}") from exc


def _collect_normalized_patch(workspace: Path) -> str:
    run_command(["git", "-C", str(workspace), "add", "-A"])
    diff = run_command(["git", "-C", str(workspace), "diff", "--cached", "--no-color"])
    if not diff.success:
        raise PatchNormalizationError(
            f"failed to collect normalized patch: {diff.stderr or diff.stdout}"
        )
    return diff.stdout


def normalize_solver_output(
    raw_output: str, attempt: AttemptWorkspace
) -> PatchNormalizationResult:
    attempt.raw_output_path.write_text(_coerce_str(raw_output), encoding="utf-8")

    patch = _extract_unified_patch(raw_output)
    if patch:
        _normalize_from_patch(attempt.workspace_path, patch)
    else:
        edits = _extract_edit_plan(raw_output)
        if not edits:
            raise PatchNormalizationError("unrecognized solver output format")
        _normalize_from_file_edits(attempt.workspace_path, edits)

    if attempt.benchmark_agents_path.exists():
        attempt.benchmark_agents_path.unlink()

    stage_result = run_command(["git", "-C", str(attempt.workspace_path), "add", "-A"])
    if not stage_result.success:
        raise PatchNormalizationError(
            f"failed to stage workspace changes: {stage_result.stderr or stage_result.stdout}"
        )

    staged_changes = _collect_changed_file_paths(attempt.workspace_path)
    if not staged_changes:
        raise PatchNormalizationError("solver output produced no repository changes")

    for status, raw_path in staged_changes:
        _assert_not_binary(attempt.workspace_path, status, raw_path)

    patch_stats = _collect_patch_stats(attempt.workspace_path)
    normalized_patch = _collect_normalized_patch(attempt.workspace_path)

    attempt.normalized_patch_path.write_text(normalized_patch, encoding="utf-8")
    attempt.patch_stats_path.write_text(
        json.dumps(asdict(patch_stats), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    return PatchNormalizationResult(
        attempt_id=attempt.attempt_id,
        normalized_patch_path=str(attempt.normalized_patch_path),
        patch_stats_path=str(attempt.patch_stats_path),
        raw_output_path=str(attempt.raw_output_path),
        patch=normalized_patch,
        patch_stats=patch_stats,
    )


__all__ = [
    "PatchNormalizationError",
    "PatchStats",
    "PatchNormalizationResult",
    "normalize_solver_output",
]
