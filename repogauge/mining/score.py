from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


AUTO_SHORTLIST_THRESHOLD = 8.0
REVIEW_THRESHOLD = 5.0


_BUGFIX_KEYWORDS = (
    "fix",
    "bug",
    "regression",
    "error",
    "handle",
    "crash",
    "fails",
    "incorrect",
)


_ISSUE_REFERENCE_RE = re.compile(
    r"(?:\b(?:fix|close|close[s]?|resolve|resolve[s]?|refs)\b[^\n]*?(?:#\d+))"
    r"|\b(?:https?://github\.com/[^/]+/[^/]+/(?:issues|pull)/\d+)"
    r"|\b(?:PR\s*#?\d+)\b",
    re.IGNORECASE,
)


_TEST_FUNCTION_RE = re.compile(r"^\\+[ \\t]*(?:async\\s+)?def\\s+test_", re.IGNORECASE)
_ASSERTION_RE = re.compile(r"^\\+[ \\t]*assert\\s+")


@dataclass(frozen=True)
class ScoredCommit:
    score: float
    decision_band: str
    score_breakdown: list[dict[str, Any]]


def score_scan_commit(
    *,
    commit_subject: str,
    commit_body: str,
    diff: str,
    metadata: dict[str, Any],
) -> ScoredCommit:
    """Compute deterministic heuristic score and explain why each component fired."""

    if _is_hard_reject(metadata, diff):
        return ScoredCommit(
            score=0.0,
            decision_band="reject",
            score_breakdown=[{
                "component": "hard_reject",
                "weight": 0,
                "reason": _hard_reject_reason(metadata, diff),
            }],
        )

    score = 0.0
    score_breakdown: list[dict[str, Any]] = []

    score, score_breakdown = _apply_positional_scores(commit_subject, commit_body, diff, metadata, score, score_breakdown)
    score, score_breakdown = _apply_penalties(diff, metadata, score, score_breakdown)

    band = _decision_band(score)
    return ScoredCommit(score=score, decision_band=band, score_breakdown=score_breakdown)


def _decision_band(score: float) -> str:
    if score >= AUTO_SHORTLIST_THRESHOLD:
        return "shortlist"
    if score >= REVIEW_THRESHOLD:
        return "review"
    return "reject"


def _row_metadata_int(metadata: dict[str, Any], key: str) -> int:
    value = metadata.get(key, 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _is_hard_reject(metadata: dict[str, Any], diff: str) -> bool:
    if bool(metadata.get("is_merge")):
        return True
    if bool(metadata.get("is_revert")):
        return True
    if bool(metadata.get("has_rename_only")):
        return True
    if _is_docs_only(metadata):
        return True
    if _is_dependency_only(metadata):
        return True
    if _is_generated_vendor_only(metadata):
        return True
    if _is_formatting_only(diff):
        return True
    return False


def _hard_reject_reason(metadata: dict[str, Any], diff: str) -> str:
    if bool(metadata.get("is_merge")):
        return "merge commit"
    if bool(metadata.get("is_revert")):
        return "revert commit"
    if bool(metadata.get("has_rename_only")):
        return "rename-only commit"
    if _is_docs_only(metadata):
        return "docs-only changes"
    if _is_dependency_only(metadata):
        return "dependency-only changes"
    if _is_generated_vendor_only(metadata):
        return "generated/vendor-only changes"
    if _is_formatting_only(diff):
        return "formatting-only changes"
    return "unknown hard reject condition"


def _is_docs_only(metadata: dict[str, Any]) -> bool:
    has_prod = _row_metadata_int(metadata, "n_prod_files") > 0
    has_test = _row_metadata_int(metadata, "n_test_files") > 0
    has_test_support = _row_metadata_int(metadata, "n_test_support_files") > 0
    has_config = _row_metadata_int(metadata, "n_config_build_files") > 0
    has_unknown = _row_metadata_int(metadata, "n_unknown_files") > 0
    has_docs = _row_metadata_int(metadata, "n_docs_files") > 0

    return not (has_prod or has_test) and (has_docs or has_config or has_test_support or has_unknown)


def _is_dependency_only(metadata: dict[str, Any]) -> bool:
    has_prod = _row_metadata_int(metadata, "n_prod_files") > 0
    has_test = _row_metadata_int(metadata, "n_test_files") > 0
    has_config = _row_metadata_int(metadata, "n_config_build_files") > 0
    return bool(has_config and not (has_prod or has_test))


def _is_generated_vendor_only(metadata: dict[str, Any]) -> bool:
    has_prod = _row_metadata_int(metadata, "n_prod_files") > 0
    has_test = _row_metadata_int(metadata, "n_test_files") > 0
    has_vendor = _row_metadata_int(metadata, "n_generated_vendor_files") > 0
    if not has_vendor:
        return False
    return not (has_prod or has_test or _row_metadata_int(metadata, "n_unknown_files") > 0)


def _is_formatting_only(diff: str) -> bool:
    # Conservative heuristic: formatting-only commits frequently have mostly
    # whitespace-only changes with very weak syntax signal.
    added_removed = [
        line[1:]
        for line in diff.splitlines()
        if len(line) > 1 and line[0] in {"+", "-"} and not line.startswith(("+++", "---"))
    ]
    if not added_removed:
        return False

    if len(added_removed) > 80:
        return False

    for line in added_removed:
        stripped = line.strip()
        if not stripped:
            continue
        if any(token in stripped for token in ("def ", "class ", "return ", "assert ", "if ", "for ", "while ", "with ", "except ", "raise ", "import ", "from ")):
            return False
        if re.search(r"[A-Za-z0-9_]{3,}", stripped):
            return False
    return True


def _apply_positional_scores(
    commit_subject: str,
    commit_body: str,
    diff: str,
    metadata: dict[str, Any],
    score: float,
    score_breakdown: list[dict[str, Any]],
) -> tuple[float, list[dict[str, Any]]]:
    message = f"{commit_subject}\n{commit_body}".strip().lower()

    if _row_metadata_int(metadata, "n_prod_files") > 0 and _row_metadata_int(metadata, "n_test_files") > 0:
        score += 4
        score_breakdown.append({
            "component": "prod_and_tests",
            "weight": 4,
            "reason": "Commit touches both production and test files.",
        })

    changed_lines = _row_metadata_int(metadata, "total_changed_lines")
    if changed_lines <= 350 and changed_lines > 0:
        score += 3
        score_breakdown.append({
            "component": "patch_size",
            "weight": 3,
            "reason": "Patch is small/medium.",
        })
    elif changed_lines > 900:
        score += -3
        score_breakdown.append({
            "component": "patch_size",
            "weight": -3,
            "reason": "Patch is very large and likely high-effort.",
        })

    if any(token in message for token in _BUGFIX_KEYWORDS):
        score += 3
        score_breakdown.append({
            "component": "message",
            "weight": 3,
            "reason": "Commit message contains bugfix-like signal.",
        })

    if _ISSUE_REFERENCE_RE.search(message):
        score += 2
        score_breakdown.append({
            "component": "issue_link",
            "weight": 2,
            "reason": "Commit references an issue or PR.",
        })

    if _TEST_FUNCTION_RE.search(diff) or _ASSERTION_RE.search(diff):
        score += 2
        score_breakdown.append({
            "component": "new_tests",
            "weight": 2,
            "reason": "Diff appears to add test-like content.",
        })

    return score, score_breakdown


def _apply_penalties(
    diff: str,
    metadata: dict[str, Any],
    score: float,
    score_breakdown: list[dict[str, Any]],
) -> tuple[float, list[dict[str, Any]]]:
    if _row_metadata_int(metadata, "n_test_files") == 0:
        score += -4
        score_breakdown.append({
            "component": "missing_test_files",
            "weight": -4,
            "reason": "No test file changes were detected.",
        })

    if _row_metadata_int(metadata, "n_hunks") > 14 or _row_metadata_int(metadata, "total_changed_lines") > 900:
        score += -3
        score_breakdown.append({
            "component": "large_refactor",
            "weight": -3,
            "reason": "Large refactor-like patch shape.",
        })

    return score, score_breakdown
