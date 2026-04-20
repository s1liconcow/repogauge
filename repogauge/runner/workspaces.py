"""Solver attempt workspace preparation primitives."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from contextlib import contextmanager

from repogauge.utils.git import scoped_checkout
from repogauge.validation.testsel import extract_patch_paths


_BENCHMARK_AGENTS_FILENAME = "AGENTS.md"
_CODEX_HOME_DIRNAME = "codex-home"
_CODEX_CONFIG_DIRNAME = ".codex"
_CODEX_MINIMAL_CONFIG = """notify = []
"""
_BENCHMARK_AGENTS_TEXT = """# RepoGauge Benchmark Workspace

This workspace is a disposable benchmark attempt sandbox.

Rules:
- Focus only on producing the requested patch for the benchmark task.
- Do not run repo triage workflows such as `bv`, `br`, issue management, or planning tools.
- Do not browse the web unless the prompt explicitly requires it.
- Do not push, commit, branch, or modify git metadata.
- Do not perform session wrap-up steps such as smoke tests, release checks, or handoff docs.
- Prefer the smallest useful set of reads, one patch, and only the most relevant validation command.
- Treat files supplied through `test_patch` as benchmark-owned immutable inputs.
- Do not modify held-out regression test files in your patch.
- Return the patch/output requested by the benchmark harness.
"""


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
    benchmark_agents_path: Path
    codex_home_root: Path
    claude_home_root: Path


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


def _immutable_paths(instance_row: Mapping[str, Any]) -> list[str]:
    raw = instance_row.get("immutable_paths")
    if isinstance(raw, list):
        return [str(value).strip() for value in raw if str(value).strip()]
    return extract_patch_paths(_instance_value(instance_row, "test_patch"))


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
    immutable_paths = _immutable_paths(instance_row)
    payload = {
        "attempt_id": attempt_id,
        "instance_id": _instance_value(instance_row, "instance_id"),
        "repo": _instance_value(instance_row, "repo"),
        "base_commit": base_commit,
        "problem_statement": _instance_value(instance_row, "problem_statement"),
        "solver_id": solver_id,
        "immutable_paths": immutable_paths,
        "benchmark_contract": {
            "test_patch_is_benchmark_owned": bool(immutable_paths),
            "do_not_modify_immutable_paths": immutable_paths,
        },
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


def _write_benchmark_agents(workspace_root: Path) -> None:
    # Override repo-local agent instructions inside disposable solver worktrees so
    # benchmark attempts follow the task prompt instead of full repo session policy.
    (workspace_root / _BENCHMARK_AGENTS_FILENAME).write_text(
        _BENCHMARK_AGENTS_TEXT,
        encoding="utf-8",
    )


def _prepare_codex_home(codex_home_root: Path) -> None:
    config_root = codex_home_root / _CODEX_CONFIG_DIRNAME
    config_root.mkdir(parents=True, exist_ok=True)
    (config_root / "config.toml").write_text(_CODEX_MINIMAL_CONFIG, encoding="utf-8")

    source_root = Path.home() / _CODEX_CONFIG_DIRNAME
    for name in ("auth.json", "installation_id"):
        source = source_root / name
        if source.exists():
            shutil.copy2(source, config_root / name)


def _prepare_claude_home(claude_home_root: Path) -> None:
    config_root = claude_home_root / ".claude"
    config_root.mkdir(parents=True, exist_ok=True)

    source = Path.home() / ".claude" / ".credentials.json"
    if source.exists():
        shutil.copy2(source, config_root / ".credentials.json")


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
    benchmark_agents_path = workspace_root / _BENCHMARK_AGENTS_FILENAME
    codex_home_root = attempt_root / _CODEX_HOME_DIRNAME
    claude_home_root = attempt_root / "claude-home"

    _write_instruction_pack(
        path=instruction_pack_path,
        attempt_id=attempt_id,
        instance_row=instance_row,
        solver_id=solver_id,
        base_commit=base_commit,
        prompt_policy=prompt_policy,
        tool_policy=tool_policy,
    )

    with scoped_checkout(
        repo_root, ref=base_commit, checkout_path=workspace_root
    ) as worktree:
        _write_benchmark_agents(worktree)
        _prepare_codex_home(codex_home_root)
        _prepare_claude_home(claude_home_root)
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
            benchmark_agents_path=benchmark_agents_path,
            codex_home_root=codex_home_root,
            claude_home_root=claude_home_root,
        )
        yield attempt

    if codex_home_root.exists():
        shutil.rmtree(codex_home_root, ignore_errors=True)
    if claude_home_root.exists():
        shutil.rmtree(claude_home_root, ignore_errors=True)
    if workspace_root.exists():
        shutil.rmtree(workspace_root, ignore_errors=True)
