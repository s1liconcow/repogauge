"""Commit scanning and diff extraction helpers for deterministic mining."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from repogauge.config import ScanRow
from repogauge.exec import run_command
from repogauge.mining.score import score_scan_commit
from repogauge.mining.file_roles import classify_files
from repogauge.utils.git import get_default_branch, get_repo_root, extract_commit_diff, list_commit_parents


_RECORD_SEPARATOR = "\x1f"
_HUNK_RE = re.compile(r"^@@\s")


def _run_git_command(repo_root: Path, command: list[str]) -> str:
    result = run_command(["git", "-C", str(repo_root), *command])
    if not result.success:
        raise RuntimeError(result.stderr.strip() or result.stdout)
    return result.stdout


def _list_commit_shas(
    repo_root: Path,
    *,
    max_count: int,
    commit_range: str | None,
) -> list[str]:
    if max_count <= 0:
        return []

    if commit_range:
        command = ["log", f"--max-count={max_count}", "--pretty=%H", commit_range]
    else:
        default_branch = get_default_branch(repo_root)
        command = ["log", f"--max-count={max_count}", "--pretty=%H", default_branch]

    try:
        output = _run_git_command(repo_root, command)
    except RuntimeError as exc:
        if commit_range:
            raise RuntimeError(f"failed scanning commit range {commit_range}: {exc}") from exc

        # Empty repos have no branch tip commit object; keep behavior permissive.
        return []

    return [line.strip() for line in output.splitlines() if line.strip()]


def _read_commit_metadata(repo_root: Path, commit: str) -> tuple[str, str, str]:
    format_string = "%H\x1f%aI\x1f%s\x1f%B"
    output = _run_git_command(
        repo_root,
        ["show", "--no-patch", f"--format={format_string}", "--no-color", commit],
    )
    parts = output.split(_RECORD_SEPARATOR, maxsplit=3)
    if len(parts) < 4:
        raise RuntimeError(f"failed parsing commit metadata for {commit}")
    _, authored_at, subject, body = parts
    return subject.strip(), body.strip(), authored_at.strip()


def _extract_changed_paths_and_hunks(
    repo_root: Path,
    parent: str | None,
    commit: str,
) -> tuple[list[str], int, bool, int]:
    if parent is None:
        name_status_cmd = ["diff-tree", "--no-commit-id", "--name-status", "-r", "--find-renames", "--root", commit]
        numstat_cmd = ["diff", "--no-color", "--numstat", "--find-renames", "--find-copies", "--root", commit]
        patch_cmd = ["diff", "--no-color", "--find-renames", "--find-copies", "--root", commit]
    else:
        name_status_cmd = [
            "diff-tree",
            "--no-commit-id",
            "--name-status",
            "-r",
            "--find-renames",
            "--find-copies",
            str(parent),
            str(commit),
        ]
        numstat_cmd = [
            "diff",
            "--no-color",
            "--numstat",
            "--find-renames",
            "--find-copies",
            str(parent),
            str(commit),
        ]
        patch_cmd = [
            "diff",
            "--no-color",
            "--find-renames",
            "--find-copies",
            str(parent),
            str(commit),
        ]

    name_status = _run_git_command(repo_root, name_status_cmd).splitlines()
    numstat = _run_git_command(repo_root, numstat_cmd).splitlines()

    files_touched: list[str] = []
    statuses: list[str] = []

    for line in name_status:
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]
        path = parts[-1]
        statuses.append(status)
        if status.startswith(("R", "C")):
            pass
        files_touched.append(path)

    changed_lines = 0
    for line in numstat:
        if not line:
            continue
        pieces = line.split("\t")
        if len(pieces) < 3:
            continue
        insertions, deletions = pieces[0], pieces[1]
        try:
            changed_lines += int(insertions) + int(deletions)
        except ValueError:
            # Binary or unknown counters; treat as unchanged until downstream analysis.
            pass

    patch = _run_git_command(repo_root, patch_cmd)
    n_hunks = len([line for line in patch.splitlines() if _HUNK_RE.match(line)])
    has_rename_only = bool(statuses) and all(status.startswith(("R", "C")) for status in statuses) and changed_lines == 0
    return files_touched, n_hunks, has_rename_only, changed_lines


def _classify_file_counts(files_touched: list[str]) -> dict[str, int]:
    roles = classify_files(files_touched)
    counts = Counter()
    for classification in roles.values():
        counts[classification.role] += 1
    return {
        "prod": counts["prod"],
        "test": counts["test"],
        "config_build": counts["config_build"],
        "test_support": counts["test_support"],
        "docs": counts["docs"],
        "generated_vendor": counts["generated_vendor"],
        "unknown": counts["unknown"],
    }


def _build_scan_row(
    repo: str,
    repo_root: Path,
    commit: str,
) -> ScanRow:
    parents = list_commit_parents(repo_root, commit)
    subject, body, author_date = _read_commit_metadata(repo_root, commit)
    is_revert = "revert" in subject.lower() or "revert" in body.lower()
    files_touched, n_hunks, has_rename_only, changed_lines = _extract_changed_paths_and_hunks(
        repo_root,
        parent=str(parents[0]) if parents else None,
        commit=commit,
    )
    files_touched = sorted(set(files_touched))
    file_counts = _classify_file_counts(files_touched)
    diff = extract_commit_diff(repo_root, left=str(parents[0]) if parents else commit, right=commit) if parents else extract_commit_diff(repo_root, left=commit, right=commit)
    if not parents:
        root_diff = _run_git_command(
            repo_root,
            ["diff", "--no-color", "--root", commit],
        )
        if root_diff:
            diff = root_diff
    metadata = {
        "commit_subject": subject,
        "commit_body": body,
        "author_date": author_date,
        "parent_count": len(parents),
        "is_merge": len(parents) > 1,
        "is_revert": is_revert,
        "has_rename_only": has_rename_only,
        "has_bead_changes": any(f.startswith(".beads/") for f in files_touched),
        "n_prod_files": file_counts["prod"],
        "n_test_files": file_counts["test"],
        "n_config_build_files": file_counts["config_build"],
        "n_test_support_files": file_counts["test_support"],
        "n_docs_files": file_counts["docs"],
        "n_generated_vendor_files": file_counts["generated_vendor"],
        "n_unknown_files": file_counts["unknown"],
        "n_hunks": n_hunks,
        "total_changed_lines": changed_lines,
    }
    scoring = score_scan_commit(
        commit_subject=subject,
        commit_body=body,
        diff=diff,
        metadata=metadata,
    )
    state = "discovered"
    if scoring.decision_band == "shortlist":
        state = "shortlist"
    elif scoring.decision_band == "reject":
        state = "rejected"

    metadata.update(
        {
            "score_breakdown": scoring.score_breakdown,
            "decision_band": scoring.decision_band,
        }
    )

    return ScanRow(
        id=f"{repo.replace('/', '__')}-rg-{commit[:12]}",
        repo=repo,
        commit=commit,
        parent_commit=parents[0] if parents else None,
        diff=diff,
        files_touched=files_touched,
        changed_lines=changed_lines,
        heuristic_score=scoring.score,
        state=state,
        metadata=metadata,
    )


def scan_repository(
    path: str | Path,
    *,
    repo_name: str,
    max_count: int = 100,
    commit_range: str | None = None,
    include_merges: bool = True,
) -> list[ScanRow]:
    """Scan commit history and emit deterministic `ScanRow` entries."""
    try:
        repo_root = get_repo_root(path)
    except RuntimeError:
        return []

    shas = _list_commit_shas(repo_root, max_count=max_count, commit_range=commit_range)
    rows: list[ScanRow] = []

    for commit in shas:
        parents = list_commit_parents(repo_root, commit)
        if not include_merges and len(parents) > 1:
            continue
        rows.append(_build_scan_row(repo_name, repo_root, commit))
    return rows
