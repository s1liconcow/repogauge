"""Utility helpers used across RepoGauge subsystems."""

from .git import (
    CommandPatchError,
    GitError,
    WorktreeHandle,
    apply_patch_text,
    extract_commit_diff,
    get_default_branch,
    get_repo_root,
    list_commit_parents,
    list_commits,
    remove_worktree,
    scoped_worktree,
)

__all__ = [
    "CommandPatchError",
    "GitError",
    "WorktreeHandle",
    "apply_patch_text",
    "extract_commit_diff",
    "get_default_branch",
    "get_repo_root",
    "list_commit_parents",
    "list_commits",
    "remove_worktree",
    "scoped_worktree",
]
