import os
from pathlib import Path
from unittest import mock

from repogauge.exec import CommandResult, run_command
from repogauge.utils.git import (
    create_checkout,
    apply_patch_text,
    create_worktree,
    extract_commit_diff,
    get_default_branch,
    get_repo_root,
    list_commit_parents,
    list_commits,
    remove_worktree,
    scoped_checkout,
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
        assert (worktree / "hello.txt").read_text(encoding="utf-8") in {
            "first\n",
            "second\n",
        }
        tracked = os.path.abspath(str(worktree))
    assert not os.path.exists(tracked)


def test_scoped_checkout_path_cleanup(tmp_path: Path):
    repo = _repo(tmp_path)
    with scoped_checkout(repo) as checkout:
        assert (checkout / "hello.txt").read_text(encoding="utf-8") in {
            "first\n",
            "second\n",
        }
        tracked = os.path.abspath(str(checkout))
    assert not os.path.exists(tracked)


def test_create_checkout_is_self_contained_without_origin_and_ignores_filemode(
    tmp_path: Path,
):
    repo = _repo(tmp_path)
    head = list_commits(repo, max_count=1)[0]

    handle = create_checkout(repo, ref=head, checkout_path=tmp_path / "checkout")
    try:
        git_dir = run_command(
            ["git", "-C", str(handle.path), "rev-parse", "--git-dir"]
        ).stdout.strip()
        git_common_dir = run_command(
            ["git", "-C", str(handle.path), "rev-parse", "--git-common-dir"]
        ).stdout.strip()
        remotes = run_command(["git", "-C", str(handle.path), "remote"]).stdout.strip()
        filemode = run_command(
            ["git", "-C", str(handle.path), "config", "--get", "core.fileMode"]
        ).stdout.strip()

        assert git_dir == ".git"
        assert git_common_dir == ".git"
        assert (handle.path / ".git").is_dir()
        assert remotes == ""
        assert filemode == "false"
    finally:
        handle.remove()


def test_create_worktree_prunes_and_retries_stale_registration(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    worktree_path = tmp_path / "retry-worktree"

    with (
        mock.patch("repogauge.utils.git.get_repo_root", return_value=repo),
        mock.patch("repogauge.utils.git.run_command") as mock_run_command,
    ):
        mock_run_command.side_effect = [
            CommandResult(command=["git"], returncode=0, stdout="", stderr=""),
            CommandResult(
                command=["git"],
                returncode=1,
                stdout="",
                stderr=(
                    "fatal: '/tmp/retry-worktree' is a missing but already "
                    "registered worktree; use 'add -f' to override"
                ),
            ),
            CommandResult(command=["git"], returncode=0, stdout="", stderr=""),
            CommandResult(command=["git"], returncode=0, stdout="", stderr=""),
            CommandResult(command=["git"], returncode=0, stdout="", stderr=""),
        ]

        handle = create_worktree(repo, worktree_path=worktree_path)

    assert handle.path == worktree_path
    calls = [call.args[0] for call in mock_run_command.call_args_list]
    assert calls[0][-2:] == ["worktree", "prune"]
    assert calls[1][4:7] == ["add", "--detach", str(worktree_path)]
    assert calls[2][4:7] == ["remove", "--force", str(worktree_path)]
    assert calls[3][-2:] == ["worktree", "prune"]
    assert calls[4][4:7] == ["add", "--detach", str(worktree_path)]


def test_remove_worktree_prunes_after_cleanup(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    worktree_path = tmp_path / "wt"
    worktree_path.mkdir()
    (worktree_path / "hello.txt").write_text("x\n", encoding="utf-8")

    with (
        mock.patch("repogauge.utils.git.get_repo_root", return_value=repo),
        mock.patch("repogauge.utils.git.run_command") as mock_run_command,
    ):
        remove_worktree(repo, worktree_path)

    calls = [call.args[0] for call in mock_run_command.call_args_list]
    assert calls[0][4:7] == ["remove", "--force", str(worktree_path)]
    assert calls[1][-2:] == ["worktree", "prune"]
    assert not worktree_path.exists()
