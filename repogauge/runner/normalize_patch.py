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

    def _extract_patch_fragment(text: str) -> str:
        m = re.search(r"```(?:diff|patch)\r?\n(.*?)```", text, flags=re.S | re.I)
        if m:
            code = m.group(1).strip()
            if "diff --git" in code:
                return code if code.endswith("\n") else code + "\n"

        lines = text.splitlines()
        for index, line in enumerate(lines):
            if line.startswith("diff --git"):
                patch_body: list[str] = []
                for patch_line in lines[index:]:
                    if patch_line.strip() == "```":
                        break
                    patch_body.append(patch_line)
                patch_lines = "\n".join(patch_body).strip("\n")
                if patch_lines:
                    return (
                        patch_lines
                        if patch_lines.endswith("\n")
                        else patch_lines + "\n"
                    )
        return ""

    patch = _extract_patch_fragment(normalized)
    if patch:
        return patch

    for raw_line in normalized.splitlines():
        line = raw_line.strip()
        if line[:1] not in "{[":
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, (Mapping, list)):
            for candidate in _extract_text_candidates(payload):
                patch = _extract_patch_fragment(candidate)
                if patch:
                    return patch

    payload: Any = None
    if normalized.startswith("{") or normalized.startswith("["):
        try:
            payload = json.loads(normalized)
        except Exception:
            payload = None
    if isinstance(payload, (Mapping, list)):
        for candidate in _extract_text_candidates(payload):
            patch = _extract_patch_fragment(candidate)
            if patch:
                return patch

    return ""


def _extract_edit_plan(raw: str) -> list[tuple[str, str]]:
    text = _coerce_str(raw).strip()
    if not text:
        return []

    def _extract_from_payload(payload: Any) -> list[tuple[str, str]]:
        files: list[tuple[str, str]] = []
        if isinstance(payload, Mapping):
            if (
                isinstance(payload.get("model_patch"), str)
                and "diff --git" in payload["model_patch"]
            ):
                return []
            if (
                isinstance(payload.get("patch"), str)
                and "diff --git" in payload["patch"]
            ):
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

            if files:
                return files

            for nested in payload.values():
                if isinstance(nested, (Mapping, list)):
                    nested_files = _extract_from_payload(nested)
                    if nested_files:
                        return nested_files

            for candidate in _extract_text_candidates(payload):
                if candidate == text:
                    continue
                try:
                    nested_payload = json.loads(candidate)
                except Exception:
                    continue
                nested_files = _extract_from_payload(nested_payload)
                if nested_files:
                    return nested_files

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
            if files:
                return files

            for nested in payload:
                if isinstance(nested, (Mapping, list)):
                    nested_files = _extract_from_payload(nested)
                    if nested_files:
                        return nested_files
        return []

    payload: Any
    try:
        payload = json.loads(text)
    except Exception:
        m = re.search(r"```json\n(.*?)```", text, flags=re.S | re.I)
        if m:
            payload = json.loads(m.group(1).strip())
            files = _extract_from_payload(payload)
            if files:
                return files
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line[:1] not in "{[":
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue
            files = _extract_from_payload(payload)
            if files:
                return files
        return []

    return _extract_from_payload(payload)


def _extract_text_candidates(value: Any) -> list[str]:
    candidates: list[str] = []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list):
        for item in value:
            candidates.extend(_extract_text_candidates(item))
        return candidates
    if not isinstance(value, Mapping):
        return candidates

    for key in (
        "text",
        "patch",
        "model_patch",
        "content",
        "output_text",
        "aggregated_output",
    ):
        if key in value:
            candidates.extend(_extract_text_candidates(value.get(key)))

    for key, nested in value.items():
        if key in {
            "text",
            "patch",
            "model_patch",
            "content",
            "output_text",
            "aggregated_output",
        }:
            continue
        if isinstance(nested, (Mapping, list)):
            candidates.extend(_extract_text_candidates(nested))

    return candidates


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


def _run_git_workspace_command(workspace: Path, args: list[str]):
    return run_command(
        [
            "git",
            "-c",
            f"safe.directory={workspace}",
            "-C",
            str(workspace),
            *args,
        ]
    )


def _collect_changed_file_paths(workspace: Path) -> list[tuple[str, str]]:
    staged = _run_git_workspace_command(workspace, ["diff", "--cached", "--name-status"])
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
    diff = _run_git_workspace_command(workspace, ["diff", "--cached", "--numstat"])
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


def _restore_attempt_workspace(attempt: AttemptWorkspace) -> None:
    reset = _run_git_workspace_command(
        attempt.workspace_path,
        ["reset", "--hard", attempt.base_commit],
    )
    if not reset.success:
        raise PatchNormalizationError(
            f"failed to reset attempt workspace: {reset.stderr or reset.stdout}"
        )

    clean = _run_git_workspace_command(attempt.workspace_path, ["clean", "-fd"])
    if not clean.success:
        raise PatchNormalizationError(
            f"failed to clean attempt workspace: {clean.stderr or clean.stdout}"
        )


def _collect_normalized_patch(workspace: Path) -> str:
    _run_git_workspace_command(workspace, ["add", "-A"])
    diff = _run_git_workspace_command(workspace, ["diff", "--cached", "--no-color"])
    if not diff.success:
        raise PatchNormalizationError(
            f"failed to collect normalized patch: {diff.stderr or diff.stdout}"
        )
    return diff.stdout


def _discard_benchmark_agents_override(attempt: AttemptWorkspace) -> None:
    agents_path = attempt.benchmark_agents_path
    if not agents_path.exists():
        return

    tracked = _run_git_workspace_command(
        attempt.workspace_path,
        ["ls-files", "--error-unmatch", agents_path.name],
    )
    if tracked.success:
        _run_git_workspace_command(
            attempt.workspace_path,
            ["checkout", "--", agents_path.name],
        )
        return
    agents_path.unlink(missing_ok=True)


def _normalize_from_workspace_state(attempt: AttemptWorkspace) -> PatchNormalizationResult:
    _discard_benchmark_agents_override(attempt)
    stage_result = _run_git_workspace_command(attempt.workspace_path, ["add", "-A"])
    if not stage_result.success:
        raise PatchNormalizationError(
            "failed to stage workspace changes: "
            f"{stage_result.stderr or stage_result.stdout}"
        )

    staged_changes = _collect_changed_file_paths(attempt.workspace_path)
    if not staged_changes:
        raise PatchNormalizationError("unrecognized solver output format")

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


def normalize_solver_output(
    raw_output: str, attempt: AttemptWorkspace
) -> PatchNormalizationResult:
    attempt.raw_output_path.write_text(_coerce_str(raw_output), encoding="utf-8")
    workspace_fallback: PatchNormalizationResult | None = None
    try:
        workspace_fallback = _normalize_from_workspace_state(attempt)
    except PatchNormalizationError:
        workspace_fallback = None

    patch = _extract_unified_patch(raw_output)
    if patch:
        _restore_attempt_workspace(attempt)
        try:
            _normalize_from_patch(attempt.workspace_path, patch)
        except PatchNormalizationError:
            if workspace_fallback is not None:
                return workspace_fallback
            raise
    else:
        edits = _extract_edit_plan(raw_output)
        if not edits:
            if workspace_fallback is not None:
                return workspace_fallback
            raise PatchNormalizationError("unrecognized solver output format")
        _restore_attempt_workspace(attempt)
        _normalize_from_file_edits(attempt.workspace_path, edits)

    return _normalize_from_workspace_state(attempt)


__all__ = [
    "PatchNormalizationError",
    "PatchStats",
    "PatchNormalizationResult",
    "normalize_solver_output",
]
