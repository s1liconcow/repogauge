"""Utility helpers used across RepoGauge subsystems."""

from .git import (
    CheckoutHandle,
    CommandPatchError,
    GitError,
    WorktreeHandle,
    apply_patch_text,
    create_checkout,
    extract_commit_diff,
    get_default_branch,
    get_repo_root,
    list_commit_parents,
    list_commits,
    remove_worktree,
    scoped_checkout,
    scoped_worktree,
)

__all__ = [
    "CheckoutHandle",
    "CommandPatchError",
    "GitError",
    "WorktreeHandle",
    "apply_patch_text",
    "create_checkout",
    "extract_commit_diff",
    "get_default_branch",
    "get_repo_root",
    "list_commit_parents",
    "list_commits",
    "remove_worktree",
    "scoped_checkout",
    "scoped_worktree",
]
