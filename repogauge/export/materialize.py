"""Materialization helpers for converting reviewed candidates into work items."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from repogauge.mining.synthesize import synthesize_problem_statement
from repogauge.export.split_patch import PatchSplitError, split_prod_and_test
from repogauge.utils.git import extract_commit_diff, get_repo_root, list_commit_parents


class MaterializationError(RuntimeError):
    """Raised when a reviewed candidate cannot be materialized."""


@dataclass
class MaterializedItem:
    candidate_id: str
    repo: str
    commit: str
    base_commit: str
    problem_statement: str = ""
    patch: str = ""
    test_patch: str = ""
    prod_patch: str = ""
    status: str = "ready"
    reason: Optional[str] = None
    metadata: Dict[str, Any] | None = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "repo": self.repo,
            "commit": self.commit,
            "base_commit": self.base_commit,
            "problem_statement": self.problem_statement,
            "status": self.status,
            "reason": self.reason,
            "patch": self.patch,
            "test_patch": self.test_patch,
            "prod_patch": self.prod_patch,
            "metadata": self.metadata or {},
        }


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value:
            continue
        rows.append(json.loads(value))
    return rows


def _coerce_accepted_state(value: Any) -> bool:
    if hasattr(value, "value") and isinstance(getattr(value, "value"), str):
        value = getattr(value, "value")
    normalized = str(value).strip().lower()
    if normalized.startswith("contractstate."):
        normalized = normalized.split(".", 1)[-1]
    return normalized == "accepted"


def _parent_count(row: Dict[str, Any]) -> int:
    metadata = row.get("metadata", {})
    value = metadata.get("parent_count")
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _resolve_base_commit(repo_root: Path, commit: str, row: Dict[str, Any]) -> str:
    metadata = row.get("metadata", {})
    candidate_parent = metadata.get("parent_commit")
    if isinstance(candidate_parent, str) and candidate_parent:
        return candidate_parent
    parents = list_commit_parents(repo_root, commit)
    if not parents:
        return ""
    return parents[0]


def _extract_candidate_metadata(row: Dict[str, Any], patch: str, base_commit: str) -> Dict[str, Any]:
    metadata = dict(row.get("metadata", {}))
    split_prod, split_test, split_meta = split_prod_and_test(patch)
    metadata.update(
        {
            "materialization": {
                "split_meta": split_meta,
                "base_commit": base_commit,
                "patch_lines": len(patch.splitlines()),
                "prod_patch_lines": len(split_prod.splitlines()),
                "test_patch_lines": len(split_test.splitlines()),
            },
        }
    )
    return metadata


def _materialize_candidate(
    repo_root: Path,
    row: Dict[str, Any],
) -> Tuple[Optional[MaterializedItem], Optional[MaterializedItem]]:
    candidate_id = str(row.get("id") or row.get("candidate_id") or "")
    state = row.get("state")
    commit = str(
        row.get("commit")
        or row.get("source_commit")
        or row.get("metadata", {}).get("source_commit")
        or ""
    ).strip()
    repo = str(row.get("repo") or "unknown")

    if not candidate_id:
        return None, MaterializedItem(
            candidate_id="unknown",
            repo=repo,
            commit=commit,
            base_commit="",
            patch="",
            test_patch="",
            prod_patch="",
            status="rejected",
            reason="missing_candidate_id",
            metadata={"reason": "candidate row did not include id"},
        )

    if not _coerce_accepted_state(state):
        return None, None

    if commit == "":
        return None, MaterializedItem(
            candidate_id=candidate_id,
            repo=repo,
            commit=commit,
            base_commit="",
            patch="",
            test_patch="",
            prod_patch="",
            status="rejected",
            reason="missing_commit",
            metadata={"reason": "candidate row did not include commit"},
        )

    metadata_parent_count = _parent_count(row)
    if metadata_parent_count != 1:
        try:
            actual_parent_count = len(list_commit_parents(repo_root, commit))
        except Exception:
            actual_parent_count = metadata_parent_count
        if actual_parent_count != 1:
            return None, MaterializedItem(
                candidate_id=candidate_id,
                repo=repo,
                commit=commit,
                base_commit="",
                patch="",
                test_patch="",
                prod_patch="",
                status="rejected",
                reason="non_single_parent",
                metadata={"reason": f"commit has {actual_parent_count} parent(s), expected 1"},
            )

    base_commit = _resolve_base_commit(repo_root, commit, row)
    if not base_commit:
        return None, MaterializedItem(
            candidate_id=candidate_id,
            repo=repo,
            commit=commit,
            base_commit="",
            patch="",
            test_patch="",
            prod_patch="",
            status="rejected",
            reason="patch_extraction_failed",
            metadata={"reason": "commit parent could not be resolved"},
        )

    try:
        patch = extract_commit_diff(repo_root, left=base_commit, right=commit)
    except Exception as exc:
        return None, MaterializedItem(
            candidate_id=candidate_id,
            repo=repo,
            commit=commit,
            base_commit=base_commit,
            patch="",
            test_patch="",
            prod_patch="",
            status="rejected",
            reason="patch_extraction_failed",
            metadata={"reason": str(exc)},
        )

    if not patch.strip():
        return None, MaterializedItem(
            candidate_id=candidate_id,
            repo=repo,
            commit=commit,
            base_commit=base_commit,
            patch="",
            test_patch="",
            prod_patch="",
            status="rejected",
            reason="patch_extraction_failed",
            metadata={"reason": "patch extraction returned no content"},
        )

    try:
        prod_patch, test_patch, split_meta = split_prod_and_test(patch)
    except PatchSplitError as exc:
        return None, MaterializedItem(
            candidate_id=candidate_id,
            repo=repo,
            commit=commit,
            base_commit=base_commit,
            patch=patch,
            test_patch="",
            prod_patch="",
            status="rejected",
            reason="unsupported_rename_split",
            metadata={"reason": str(exc), "split_error": type(exc).__name__},
        )
    if not prod_patch.strip():
        return None, MaterializedItem(
            candidate_id=candidate_id,
            repo=repo,
            commit=commit,
            base_commit=base_commit,
            patch=patch,
            test_patch=test_patch,
            prod_patch=prod_patch,
            status="rejected",
            reason="empty_prod_patch_after_split",
            metadata={"reason": "prod split is empty", "split_meta": split_meta},
        )

    if not test_patch.strip():
        return None, MaterializedItem(
            candidate_id=candidate_id,
            repo=repo,
            commit=commit,
            base_commit=base_commit,
            patch=patch,
            test_patch=test_patch,
            prod_patch=prod_patch,
            status="rejected",
            reason="empty_test_patch_after_split",
            metadata={"reason": "test split is empty", "split_meta": split_meta},
        )

    materialized_metadata = _extract_candidate_metadata(row, patch, base_commit)
    problem_statement, statement_source, statement_ref = synthesize_problem_statement(
        row=row,
        patch=patch,
    )
    materialized_metadata.update(
        {
            "problem_statement_source": statement_source,
            "problem_statement_source_ref": statement_ref,
        }
    )
    item = MaterializedItem(
        candidate_id=candidate_id,
        repo=repo,
        commit=commit,
        base_commit=base_commit,
        problem_statement=problem_statement,
        patch=patch,
        test_patch=test_patch,
        prod_patch=prod_patch,
        status="ready",
        reason=None,
        metadata=materialized_metadata,
    )
    return item, None


def _iter_repo_candidates(seed: Path) -> list[Path]:
    start = seed.resolve()
    if start.is_file():
        start = start.parent
    candidates: list[Path] = []
    seen: set[str] = set()

    current = start
    while True:
        if str(current) not in seen:
            candidates.append(current)
            seen.add(str(current))
        try:
            for child in current.iterdir():
                if not child.is_dir():
                    continue
                if str(child) in seen:
                    continue
                candidates.append(child)
                seen.add(str(child))
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            pass

        if current.parent == current:
            break
        current = current.parent
        if str(current) in seen:
            continue

    return candidates


def _candidate_repo_matches_commit(repo_root: Path, commit: str | None) -> bool:
    if not commit:
        return True
    try:
        list_commit_parents(repo_root, commit)
    except Exception:
        return False
    return True


def _normalize_repo_root(
    path: Path,
    *,
    explicit_repo_root: str | None = None,
    commit_hint: str | None = None,
) -> Path:
    if explicit_repo_root:
        return get_repo_root(explicit_repo_root)

    for candidate in _iter_repo_candidates(path):
        if (candidate / ".git").exists():
            if _candidate_repo_matches_commit(candidate, commit_hint):
                return candidate
            continue
        try:
            repo_root = get_repo_root(candidate)
        except Exception:
            continue
        if _candidate_repo_matches_commit(repo_root, commit_hint):
            return repo_root
    raise MaterializationError(f"could not resolve repository root for {path}")


def run_materialization(
    *,
    reviewed_path: str | Path,
    out_root: str | Path,
    repo_root: str | Path | None = None,
) -> Dict[str, Any]:
    reviewed = Path(reviewed_path)
    source_rows = _read_jsonl(reviewed)
    if not source_rows:
        raise MaterializationError(f"no reviewed candidates found in {reviewed}")

    commit_hint = None
    for row in source_rows:
        commit_hint = str(row.get("commit") or row.get("source_commit") or "").strip()
        if commit_hint:
            break

    resolved_repo_root = _normalize_repo_root(
        reviewed,
        explicit_repo_root=str(repo_root) if repo_root else None,
        commit_hint=commit_hint,
    )
    out_root_path = Path(out_root)
    out_root_path.mkdir(parents=True, exist_ok=True)

    ready_path = out_root_path / "materialized.jsonl"
    rejected_path = out_root_path / "materialization_rejections.jsonl"

    ready_rows: list[dict[str, Any]] = []
    rejected_rows: list[dict[str, Any]] = []

    for row in source_rows:
        materialized, rejected = _materialize_candidate(resolved_repo_root, row)
        if materialized is not None:
            ready_rows.append(materialized.to_dict())
        elif rejected is not None:
            rejected_rows.append(rejected.to_dict())

    ready_path.write_text("".join(json.dumps(item, sort_keys=True) + "\n" for item in ready_rows), encoding="utf-8")
    rejected_path.write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in rejected_rows),
        encoding="utf-8",
    )

    return {
        "materialized_path": str(ready_path),
        "rejected_path": str(rejected_path),
        "ready_count": len(ready_rows),
        "rejected_count": len(rejected_rows),
        "total_count": len(source_rows),
    }
