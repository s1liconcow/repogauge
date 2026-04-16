"""Solver attempt workspace preparation primitives."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from contextlib import contextmanager

from repogauge.utils.git import scoped_worktree


@dataclass(frozen=True)
class AttemptWorkspace:
    attempt_id: str
    instance_id: str
    solver_id: str
    base_commit: str
    workspace_path: Path
    attempt_root: Path
    instruction_pack_path: Path
    raw_output_path: Path
    normalized_patch_path: Path
    patch_stats_path: Path


def _coerce_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _coerce_str(value: Any) -> str:
    return str(value) if value is not None else ""


def _instance_value(instance_row: Mapping[str, Any], key: str) -> str:
    return _coerce_str(instance_row.get(key)).strip()


def _resolve_attempt_root(workspaces_root: Path, attempt_id: str) -> Path:
    safe_id = "".join(c if c.isalnum() or c in {"-", "_"} else "-" for c in attempt_id)
    if not safe_id:
        safe_id = "attempt"
    return workspaces_root / safe_id


def _write_instruction_pack(
    *,
    path: Path,
    attempt_id: str,
    instance_row: Mapping[str, Any],
    solver_id: str,
    base_commit: str,
    prompt_policy: Mapping[str, Any] | None,
    tool_policy: Mapping[str, Any] | None,
) -> None:
    payload = {
        "attempt_id": attempt_id,
        "instance_id": _instance_value(instance_row, "instance_id"),
        "repo": _instance_value(instance_row, "repo"),
        "base_commit": base_commit,
        "problem_statement": _instance_value(instance_row, "problem_statement"),
        "solver_id": solver_id,
        "prompt_policy": dict(_coerce_dict(prompt_policy)),
        "tool_policy": dict(_coerce_dict(tool_policy)),
        "expected_output": {
            "diff": "Return a unified diff rooted at repository root with diff --git headers.",
            "edits": 'Return JSON {"files": [{"path": str, "content": str}]}',
        },
    }
    path.write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )


@contextmanager
def prepare_attempt_workspace(
    *,
    repo_root: str | Path,
    instance_row: Mapping[str, Any],
    attempt_id: str,
    solver_id: str,
    workspaces_root: str | Path,
    prompt_policy: Mapping[str, Any] | None = None,
    tool_policy: Mapping[str, Any] | None = None,
) -> Iterator[AttemptWorkspace]:
    repo_root = Path(repo_root).resolve()
    workspaces_root = Path(workspaces_root).resolve()
    workspaces_root.mkdir(parents=True, exist_ok=True)

    instance_id = _instance_value(instance_row, "instance_id")
    base_commit = _instance_value(instance_row, "base_commit")
    if not base_commit:
        raise ValueError("instance_row.base_commit is required")
    if not instance_id:
        raise ValueError("instance_row.instance_id is required")

    attempt_root = _resolve_attempt_root(workspaces_root, attempt_id)
    if attempt_root.exists():
        shutil.rmtree(attempt_root)
    attempt_root.mkdir(parents=True)

    workspace_root = attempt_root / "workspace"
    instruction_pack_path = attempt_root / "instruction_pack.json"
    raw_output_path = attempt_root / "raw_output.txt"
    normalized_patch_path = attempt_root / "normalized.patch"
    patch_stats_path = attempt_root / "patch_stats.json"

    _write_instruction_pack(
        path=instruction_pack_path,
        attempt_id=attempt_id,
        instance_row=instance_row,
        solver_id=solver_id,
        base_commit=base_commit,
        prompt_policy=prompt_policy,
        tool_policy=tool_policy,
    )

    with scoped_worktree(
        repo_root, ref=base_commit, worktree_path=workspace_root
    ) as worktree:
        attempt = AttemptWorkspace(
            attempt_id=attempt_id,
            instance_id=instance_id,
            solver_id=solver_id,
            base_commit=base_commit,
            workspace_path=worktree,
            attempt_root=attempt_root,
            instruction_pack_path=instruction_pack_path,
            raw_output_path=raw_output_path,
            normalized_patch_path=normalized_patch_path,
            patch_stats_path=patch_stats_path,
        )
        yield attempt

    if workspace_root.exists():
        shutil.rmtree(workspace_root, ignore_errors=True)
