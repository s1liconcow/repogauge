from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from repogauge.exec import run_command
from repogauge.runner.normalize_patch import (
    PatchNormalizationError,
    normalize_solver_output,
)
from repogauge.runner.workspaces import prepare_attempt_workspace


def _create_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_command(["git", "init", "-b", "main"], cwd=str(repo))
    run_command(["git", "config", "user.name", "ci"], cwd=str(repo))
    run_command(["git", "config", "user.email", "ci@example.com"], cwd=str(repo))

    target = repo / "src.py"
    target.write_text("print('before')\n", encoding="utf-8")
    run_command(["git", "add", "src.py"], cwd=str(repo))
    run_command(["git", "commit", "-m", "base"], cwd=str(repo))

    commit = run_command(["git", "-C", str(repo), "rev-parse", "HEAD"]).stdout.strip()
    return repo, commit


def _attempt_row(commit: str) -> dict[str, str]:
    return {
        "instance_id": "repo__sample-1",
        "repo": "demo/repo",
        "base_commit": commit,
        "problem_statement": "Add a second log line.",
        "version": "1",
    }


def test_prepare_attempt_workspace_is_isolated_and_persists_artifacts(
    tmp_path: Path,
) -> None:
    repo, commit = _create_repo(tmp_path)
    row = _attempt_row(commit)

    workspace_root = tmp_path / "workspaces"
    with prepare_attempt_workspace(
        repo_root=repo,
        instance_row=row,
        attempt_id="att-1",
        solver_id="solver-a",
        workspaces_root=workspace_root,
        prompt_policy={"format": "patch"},
        tool_policy={"allowed": ["shell"]},
    ) as attempt:
        assert attempt.workspace_path.exists()
        assert (attempt.workspace_path / "src.py").read_text(
            encoding="utf-8"
        ).strip() == "print('before')"
        pack = json.loads(attempt.instruction_pack_path.read_text(encoding="utf-8"))
        assert pack["base_commit"] == commit
        assert pack["solver_id"] == "solver-a"
        assert pack["instance_id"] == row["instance_id"]
        agents_text = (attempt.workspace_path / "AGENTS.md").read_text(encoding="utf-8")
        assert "disposable benchmark attempt sandbox" in agents_text
        assert "Do not run repo triage workflows" in agents_text
        codex_config = (attempt.codex_home_root / ".codex" / "config.toml").read_text(
            encoding="utf-8"
        )
        assert codex_config == "notify = []\n"
        assert attempt.claude_home_root.exists()
        assert (attempt.claude_home_root / ".claude").is_dir()

    assert not (attempt.workspace_path).exists()
    assert attempt.attempt_root.exists()
    assert attempt.instruction_pack_path.exists()
    assert not attempt.codex_home_root.exists()
    assert not attempt.claude_home_root.exists()


def test_prepare_attempt_workspace_copies_claude_credentials_when_available(
    tmp_path: Path,
) -> None:
    repo, commit = _create_repo(tmp_path)
    row = _attempt_row(commit)

    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    (fake_home / ".claude" / ".credentials.json").write_text(
        '{"access_token":"token"}\n', encoding="utf-8"
    )

    original_home = os.environ.get("HOME")
    os.environ["HOME"] = str(fake_home)
    try:
        with prepare_attempt_workspace(
            repo_root=repo,
            instance_row=row,
            attempt_id="att-claude-creds",
            solver_id="solver-a",
            workspaces_root=tmp_path / "workspaces",
        ) as attempt:
            copied = attempt.claude_home_root / ".claude" / ".credentials.json"
            assert copied.read_text(encoding="utf-8") == '{"access_token":"token"}\n'
    finally:
        if original_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = original_home


def test_prepare_attempt_workspace_overrides_repo_agents_file(tmp_path: Path) -> None:
    repo, commit = _create_repo(tmp_path)
    (repo / "AGENTS.md").write_text("repo-specific instructions\n", encoding="utf-8")
    run_command(["git", "add", "AGENTS.md"], cwd=str(repo))
    run_command(["git", "commit", "-m", "add agents"], cwd=str(repo))
    commit = run_command(["git", "-C", str(repo), "rev-parse", "HEAD"]).stdout.strip()
    row = _attempt_row(commit)

    with prepare_attempt_workspace(
        repo_root=repo,
        instance_row=row,
        attempt_id="att-agents",
        solver_id="solver-a",
        workspaces_root=tmp_path / "workspaces",
    ) as attempt:
        agents_text = (attempt.workspace_path / "AGENTS.md").read_text(encoding="utf-8")
        assert agents_text != "repo-specific instructions\n"
        assert (
            "Return the patch/output requested by the benchmark harness." in agents_text
        )


def test_normalize_solver_output_from_diff(tmp_path: Path) -> None:
    repo, commit = _create_repo(tmp_path)
    row = _attempt_row(commit)

    with prepare_attempt_workspace(
        repo_root=repo,
        instance_row=row,
        attempt_id="att-2",
        solver_id="solver-a",
        workspaces_root=tmp_path / "workspaces",
    ) as attempt:
        raw = (
            "diff --git a/src.py b/src.py\n"
            "index 1111111..2222222 100644\n"
            "--- a/src.py\n"
            "+++ b/src.py\n"
            "@@ -1 +1 @@\n"
            "-print('before')\n"
            "+print('after')\n"
        )
        result = normalize_solver_output(raw, attempt=attempt)

        assert result.patch_stats.files_touched == 1
        assert result.patch_stats.insertions >= 1
        assert "print('after')" in result.patch
        assert '"insertions"' in attempt.patch_stats_path.read_text(encoding="utf-8")
        assert attempt.raw_output_path.read_text(encoding="utf-8").startswith(
            "diff --git"
        )


def test_normalize_solver_output_from_file_edits(tmp_path: Path) -> None:
    repo, commit = _create_repo(tmp_path)
    row = _attempt_row(commit)

    edit_plan = {
        "files": [
            {
                "path": "src.py",
                "content": "print('edited')\n",
            },
            {
                "path": "nested/extra.txt",
                "content": "extra\n",
            },
        ]
    }

    with prepare_attempt_workspace(
        repo_root=repo,
        instance_row=row,
        attempt_id="att-3",
        solver_id="solver-a",
        workspaces_root=tmp_path / "workspaces",
    ) as attempt:
        result = normalize_solver_output(json.dumps(edit_plan), attempt=attempt)
        assert result.patch_stats.files_touched == 2
        assert "nested/extra.txt" in result.patch
        assert attempt.normalized_patch_path.exists()
        assert "print('edited')" in (
            attempt.normalized_patch_path.read_text(encoding="utf-8")
        )


def test_normalize_solver_output_resets_dirty_workspace_before_applying_diff(
    tmp_path: Path,
) -> None:
    repo, commit = _create_repo(tmp_path)
    row = _attempt_row(commit)

    with prepare_attempt_workspace(
        repo_root=repo,
        instance_row=row,
        attempt_id="att-dirty-diff",
        solver_id="solver-a",
        workspaces_root=tmp_path / "workspaces",
    ) as attempt:
        (attempt.workspace_path / "src.py").write_text(
            "print('after')\n", encoding="utf-8"
        )
        (attempt.workspace_path / "noise.txt").write_text(
            "solver scratch\n", encoding="utf-8"
        )

        raw = (
            "diff --git a/src.py b/src.py\n"
            "index 1111111..2222222 100644\n"
            "--- a/src.py\n"
            "+++ b/src.py\n"
            "@@ -1 +1 @@\n"
            "-print('before')\n"
            "+print('after')\n"
        )
        result = normalize_solver_output(raw, attempt=attempt)

        assert "print('after')" in result.patch
        assert "noise.txt" not in result.patch


def test_normalize_solver_output_resets_dirty_workspace_before_file_edits(
    tmp_path: Path,
) -> None:
    repo, commit = _create_repo(tmp_path)
    row = _attempt_row(commit)

    edit_plan = {"files": [{"path": "src.py", "content": "print('edited')\n"}]}

    with prepare_attempt_workspace(
        repo_root=repo,
        instance_row=row,
        attempt_id="att-dirty-edits",
        solver_id="solver-a",
        workspaces_root=tmp_path / "workspaces",
    ) as attempt:
        (attempt.workspace_path / "src.py").write_text(
            "print('solver-side')\n", encoding="utf-8"
        )
        (attempt.workspace_path / "noise.txt").write_text(
            "solver scratch\n", encoding="utf-8"
        )

        result = normalize_solver_output(json.dumps(edit_plan), attempt=attempt)

        assert "print('edited')" in result.patch
        assert "solver-side" not in result.patch
        assert "noise.txt" not in result.patch


def test_normalize_solver_output_preserves_tracked_agents_file(tmp_path: Path) -> None:
    repo, commit = _create_repo(tmp_path)
    (repo / "AGENTS.md").write_text("repo instructions\n", encoding="utf-8")
    run_command(["git", "add", "AGENTS.md"], cwd=str(repo))
    run_command(["git", "commit", "-m", "add agents"], cwd=str(repo))
    commit = run_command(["git", "-C", str(repo), "rev-parse", "HEAD"]).stdout.strip()
    row = _attempt_row(commit)

    with prepare_attempt_workspace(
        repo_root=repo,
        instance_row=row,
        attempt_id="att-agents-diff",
        solver_id="solver-a",
        workspaces_root=tmp_path / "workspaces",
    ) as attempt:
        raw = (
            "diff --git a/src.py b/src.py\n"
            "index 1111111..2222222 100644\n"
            "--- a/src.py\n"
            "+++ b/src.py\n"
            "@@ -1 +1 @@\n"
            "-print('before')\n"
            "+print('after')\n"
        )

        result = normalize_solver_output(raw, attempt=attempt)

        assert "AGENTS.md" not in result.patch


def test_normalize_rejects_escape_path(tmp_path: Path) -> None:
    repo, commit = _create_repo(tmp_path)
    row = _attempt_row(commit)

    with prepare_attempt_workspace(
        repo_root=repo,
        instance_row=row,
        attempt_id="att-4",
        solver_id="solver-a",
        workspaces_root=tmp_path / "workspaces",
    ) as attempt:
        raw = "diff --git a/../outside b/../outside\n@@ -1 +1 @@\n-bad\n+bad2\n"
        with pytest.raises(PatchNormalizationError, match="outside"):
            normalize_solver_output(raw, attempt=attempt)


def test_normalize_rejects_binary_edit_plan(tmp_path: Path) -> None:
    repo, commit = _create_repo(tmp_path)
    row = _attempt_row(commit)

    with prepare_attempt_workspace(
        repo_root=repo,
        instance_row=row,
        attempt_id="att-5",
        solver_id="solver-a",
        workspaces_root=tmp_path / "workspaces",
    ) as attempt:
        raw = json.dumps({"files": {"src.py": "good\u0000bad"}})

        with pytest.raises(PatchNormalizationError, match="binary"):
            normalize_solver_output(raw, attempt=attempt)
