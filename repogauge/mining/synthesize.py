"""Deterministic problem-statement synthesis for materialized dataset rows."""

from __future__ import annotations

from typing import Any, Dict, Iterable


def _coerce_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.strip().split())


def _coerce_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value:
        text = _coerce_text(item)
        if text:
            result.append(text)
    return result


def _coerce_mapping(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _pick_text(row: Dict[str, Any], keys: Iterable[str]) -> str:
    metadata = _coerce_mapping(row.get("metadata"))
    for key in keys:
        value = _coerce_text(row.get(key))
        if value:
            return value
        value = _coerce_text(metadata.get(key))
        if value:
            return value
    return ""


def _pick_ref(row: Dict[str, Any], keys: Iterable[str]) -> str:
    metadata = _coerce_mapping(row.get("metadata"))
    for key in keys:
        value = _coerce_text(row.get(key))
        if value:
            return value
        value = _coerce_text(metadata.get(key))
        if value:
            return value
    return ""


def _extract_issue_refs(row: Dict[str, Any]) -> list[str]:
    metadata = _coerce_mapping(row.get("metadata"))
    for value in (row.get("issue_refs"), metadata.get("issue_refs")):
        if not value:
            continue
        if isinstance(value, (list, tuple, set)):
            return sorted({_coerce_text(item) for item in value if _coerce_text(item)})
        single = _coerce_text(value)
        if single:
            return [single]
    return []


def _render_file_summary(file_roles: Dict[str, list[str]]) -> str:
    prod_files = _coerce_list(file_roles.get("prod"))
    test_files = _coerce_list(file_roles.get("test"))
    details = []
    if prod_files:
        details.append(f"Production changes: {', '.join(prod_files)}.")
    if test_files:
        details.append(f"Test changes: {', '.join(test_files)}.")
    return " ".join(details)


def _issue_style_statement(
    title: str, body: str | None, reference: str, kind: str
) -> tuple[str, str]:
    title_text = (
        title
        or (body or "").strip()
        or f"{kind.replace('_', ' ').title()}-linked change"
    )
    body_text = body.strip() if isinstance(body, str) else ""
    parts = [
        f"Observed behavior\n- {title_text}",
    ]
    if reference:
        parts.append(f"- Source reference: {reference}")
    if body_text:
        parts.append(f"- Context: {body_text}")
    parts.extend(
        [
            "- Reproduction: run the project's tests around this change.",
            "- Expected behavior: the regression described by the source should be fixed.",
        ]
    )
    return "\n".join(parts), "linked_issue" if "issue" in kind else "pull_request"


def _commit_style_statement(row: Dict[str, Any], patch: str) -> tuple[str, str]:
    metadata = _coerce_mapping(row.get("metadata"))
    subject = _pick_text(
        row,
        ("source_subject", "commit_subject", "subject"),
    )
    body = _pick_text(
        row,
        ("source_body", "commit_body"),
    )
    file_roles = _coerce_mapping(metadata.get("file_roles"))
    file_summary = _render_file_summary(file_roles)
    test_path = _pick_text(metadata, ("n_test_files", "changed_test_files"))
    if not test_path:
        changed_lines = metadata.get("total_changed_lines")
        if isinstance(changed_lines, int) and changed_lines > 0:
            test_path = f"{changed_lines} changed lines"
    if not test_path:
        test_path = f"{len(patch.splitlines())} patch lines"

    title = subject or "Commit-based change."
    detail = body or "A code update without a full issue description."
    parts = [
        "Observed behavior",
        f"- {title}",
        f"- Details: {detail}",
    ]
    if file_summary:
        parts.append(f"- {file_summary}")
    parts.extend(
        [
            f"- Reproduction: run tests impacted by this change ({test_path}).",
            "- Expected behavior: the update should make the corresponding regression test pass.",
        ]
    )
    return "\n".join(parts), "commit"


def _is_weak_source(subject: str, body: str) -> bool:
    combined = _coerce_text(f"{subject} {body}")
    words = [value for value in combined.replace("\n", " ").split() if value]
    return len(words) <= 6


def _llm_candidate(row: Dict[str, Any]) -> str:
    metadata = _coerce_mapping(row.get("metadata"))
    advisory = _coerce_mapping(metadata.get("llm_advisory"))
    return _coerce_text(advisory.get("problem_statement"))


def synthesize_problem_statement(
    row: Dict[str, Any], patch: str = ""
) -> tuple[str, str, str | None]:
    """Return a deterministic issue-style statement, a source label, and provenance string."""

    issue_title = _pick_text(
        row,
        ("issue_title", "linked_issue_title", "source_issue_title"),
    )
    issue_body = _pick_text(
        row,
        ("issue_body", "linked_issue_body", "source_issue_body"),
    )
    if issue_title or issue_body:
        refs = _extract_issue_refs(row)
        ref = refs[0] if refs else _pick_ref(row, ("issue_ref", "linked_issue_ref"))
        statement, source = _issue_style_statement(
            issue_title, issue_body, ref, "linked_issue"
        )
        return statement, source, ref or None

    pr_title = _pick_text(
        row,
        ("pr_title", "pull_request_title", "source_pr_title"),
    )
    pr_body = _pick_text(
        row,
        ("pr_body", "pull_request_body", "source_pr_body"),
    )
    if pr_title or pr_body:
        statement, source = _issue_style_statement(
            pr_title,
            pr_body,
            _pick_ref(row, ("pr_ref", "pull_request_number")),
            "pull_request",
        )
        return (
            statement,
            source,
            _pick_ref(row, ("pr_ref", "pull_request_number")) or None,
        )

    commit_subject = _pick_text(
        row,
        ("source_subject", "commit_subject", "subject"),
    )
    commit_body = _pick_text(
        row,
        ("source_body", "commit_body"),
    )
    commit_statement, commit_source = _commit_style_statement(row, patch)
    llm_text = _llm_candidate(row)
    if llm_text and _is_weak_source(commit_subject, commit_body):
        ref = _pick_ref(row, ("llm_advisory_model", "llm_model")) or _pick_text(
            row, ("llm_reference",)
        )
        return llm_text, "llm_advisory", ref or None

    return commit_statement, commit_source, _pick_text(row, ("commit", "source_commit"))
