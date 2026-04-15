import os
from pathlib import Path

from repogauge.exec import run_command
from repogauge.utils.git import (
    apply_patch_text,
    extract_commit_diff,
    get_default_branch,
    get_repo_root,
    list_commit_parents,
    list_commits,
    scoped_worktree,
)


def _init_git_repo(base: Path) -> Path:
    run_command(["git", "init", "-b", "main"], cwd=str(base))
    run_command(["git", "config", "user.name", "ci"], cwd=str(base))
    run_command(["git", "config", "user.email", "ci@example.com"], cwd=str(base))

    file = base / "hello.txt"
    file.write_text("first\n", encoding="utf-8")
    run_command(["git", "add", "hello.txt"], cwd=str(base))
    run_command(["git", "commit", "-m", "base"], cwd=str(base))

    file.write_text("second\n", encoding="utf-8")
    run_command(["git", "add", "hello.txt"], cwd=str(base))
    run_command(["git", "commit", "-m", "next"], cwd=str(base))
    return base


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    return _init_git_repo(repo)


def test_repo_root_resolution(tmp_path: Path):
    repo = _repo(tmp_path)
    nested = repo / "nested"
    nested.mkdir()
    assert get_repo_root(nested) == repo


def test_list_commits_and_parents(tmp_path: Path):
    repo = _repo(tmp_path)
    head, parent = list_commits(repo, max_count=2)[:2]
    assert head != parent
    parents = list_commit_parents(repo, head)
    assert parents == [parent]


def test_default_branch_is_main(tmp_path: Path):
    repo = _repo(tmp_path)
    assert get_default_branch(repo) == "main"


def test_commit_diff_and_patch_round_trip(tmp_path: Path):
    repo = _repo(tmp_path)
    head, parent = list_commits(repo, max_count=2)
    patch = extract_commit_diff(repo, left=parent, right=head)
    run_command(["git", "reset", "--hard", parent], cwd=str(repo))
    apply_patch_text(repo, patch, check=True)
    assert (repo / "hello.txt").read_text(encoding="utf-8") == "second\n"
    result = apply_patch_text(repo, patch, reverse=True, check=True)
    assert result.returncode == 0
    assert (repo / "hello.txt").read_text(encoding="utf-8") == "first\n"


def test_scoped_worktree_path_cleanup(tmp_path: Path):
    repo = _repo(tmp_path)
    with scoped_worktree(repo) as worktree:
        assert (worktree / "hello.txt").read_text(encoding="utf-8") in {"first\n", "second\n"}
        tracked = os.path.abspath(str(worktree))
    assert not os.path.exists(tracked)
