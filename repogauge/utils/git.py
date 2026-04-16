"""Git and worktree primitives for RepoGauge."""

from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from repogauge.exec import CommandResult, run_command


class GitError(RuntimeError):
    """Raised when a git operation fails."""


class CommandPatchError(RuntimeError):
    """Raised when patch application or reversal fails."""


def get_repo_root(path: str | Path) -> Path:
    """Return the repository root for ``path``."""
    result = run_command(["git", "-C", str(path), "rev-parse", "--show-toplevel"])
    if not result.success:
        raise GitError(f"not a git repository: {path}")
    return Path(result.stdout.strip())


def get_default_branch(path: str | Path) -> str:
    """Return repository default branch name."""
    root = get_repo_root(path)
    head_result = run_command(
        ["git", "-C", str(root), "symbolic-ref", "refs/remotes/origin/HEAD"]
    )
    if head_result.success and head_result.stdout.strip():
        return head_result.stdout.strip().rsplit("/", 1)[-1]

    branch_result = run_command(["git", "-C", str(root), "branch", "--show-current"])
    if branch_result.success and branch_result.stdout.strip():
        return branch_result.stdout.strip()

    return "main"


def list_commits(
    path: str | Path,
    *,
    max_count: int = 100,
    branch: Optional[str] = None,
    include_merges: bool = True,
) -> list[str]:
    """List recent commits from a repo."""
    root = get_repo_root(path)
    cmd = ["git", "-C", str(root), "log", f"--max-count={max_count}", "--pretty=%H"]
    if branch is not None:
        cmd.append(branch)
    if not include_merges:
        cmd.insert(3, "--no-merges")
    result = run_command(cmd)
    if not result.success:
        raise GitError(
            f"failed listing commits: {result.stderr.strip() or result.stdout}"
        )
    output = result.stdout.strip()
    return output.splitlines() if output else []


def list_commit_parents(path: str | Path, commit: str) -> list[str]:
    """Return direct parent SHAs for commit."""
    root = get_repo_root(path)
    result = run_command(
        ["git", "-C", str(root), "rev-list", "--parents", "-n", "1", commit]
    )
    if not result.success:
        raise GitError(
            f"failed listing parents for {commit}: {result.stderr.strip() or result.stdout}"
        )
    fields = result.stdout.strip().split()
    return fields[1:]


def extract_commit_diff(
    path: str | Path,
    *,
    left: str,
    right: str,
) -> str:
    """Return unified diff text between two commits."""
    root = get_repo_root(path)
    result = run_command(["git", "-C", str(root), "diff", "--no-color", left, right])
    if not result.success:
        raise GitError(
            f"failed extracting diff between {left} and {right}: {result.stderr.strip() or result.stdout}"
        )
    return result.stdout


def apply_patch_text(
    path: str | Path,
    patch: str,
    *,
    reverse: bool = False,
    check: bool = True,
) -> CommandResult:
    """Apply unified diff text in-place in repo working tree."""
    root = get_repo_root(path)
    args = ["git", "-C", str(root), "apply"]
    if reverse:
        args.append("-R")
    args.append("-")
    result = run_command(args, input_text=patch)
    if check and not result.success:
        raise CommandPatchError(result.stderr.strip() or result.stdout)
    return result


@dataclass
class WorktreeHandle:
    """RAII-style handle for ephemeral worktrees."""

    repo: Path
    path: Path

    def remove(self) -> None:
        remove_worktree(self.repo, self.path)


def create_worktree(
    path: str | Path,
    *,
    ref: str = "HEAD",
    worktree_path: Optional[str | Path] = None,
) -> WorktreeHandle:
    """Create an isolated git worktree and return a removable handle."""
    repo = get_repo_root(path)
    if worktree_path is None:
        temp_path = Path(tempfile.mkdtemp(prefix="repogauge-wt-"))
        temp_path.rmdir()
    else:
        temp_path = Path(worktree_path)

    result = run_command(
        ["git", "-C", str(repo), "worktree", "add", "--detach", str(temp_path), ref]
    )
    if not result.success:
        if worktree_path is None and temp_path.exists():
            shutil.rmtree(temp_path, ignore_errors=True)
        raise GitError(
            f"failed to create worktree: {result.stderr.strip() or result.stdout}"
        )
    return WorktreeHandle(repo=repo, path=temp_path)


def remove_worktree(path: str | Path, worktree_path: str | Path) -> None:
    """Remove a worktree and best-effort clean directory."""
    root = get_repo_root(path)
    wt_path = Path(worktree_path)
    run_command(["git", "-C", str(root), "worktree", "remove", "--force", str(wt_path)])
    if wt_path.exists():
        shutil.rmtree(wt_path, ignore_errors=True)


@contextmanager
def scoped_worktree(
    path: str | Path, *, ref: str = "HEAD", worktree_path: Optional[str | Path] = None
) -> Iterator[Path]:
    """Context manager yielding a temporary worktree path."""
    handle = create_worktree(path, ref=ref, worktree_path=worktree_path)
    try:
        yield handle.path
    finally:
        handle.remove()
