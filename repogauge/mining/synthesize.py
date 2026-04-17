"""Deterministic problem-statement synthesis for materialized dataset rows."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable

from repogauge.mining.enrich import parse_commit_references

_BEAD_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_]+-[A-Za-z0-9]+\b")


def _coerce_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.strip().split())


def _coerce_block_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""

    normalized_lines: list[str] = []
    pending_blank = False
    for raw_line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = " ".join(raw_line.strip().split())
        if line:
            if pending_blank and normalized_lines:
                normalized_lines.append("")
            normalized_lines.append(line)
            pending_blank = False
        elif normalized_lines:
            pending_blank = True
    return "\n".join(normalized_lines).strip()


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


def _unique_preserve(values: Iterable[str]) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _coerce_text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        items.append(text)
    return items


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


def _pick_block_text(row: Dict[str, Any], keys: Iterable[str]) -> str:
    metadata = _coerce_mapping(row.get("metadata"))
    for key in keys:
        value = _coerce_block_text(row.get(key))
        if value:
            return value
        value = _coerce_block_text(metadata.get(key))
        if value:
            return value
    return ""


def _commit_text(row: Dict[str, Any]) -> str:
    subject = _pick_text(
        row,
        ("source_subject", "commit_subject", "subject"),
    )
    body = _pick_text(
        row,
        ("source_body", "commit_body"),
    )
    return "\n".join(part for part in (subject, body) if part)


def _extract_issue_refs(row: Dict[str, Any]) -> list[str]:
    metadata = _coerce_mapping(row.get("metadata"))
    refs: list[str] = []
    for value in (row.get("issue_refs"), metadata.get("issue_refs")):
        if not value:
            continue
        if isinstance(value, (list, tuple, set)):
            refs.extend(_coerce_text(item) for item in value if _coerce_text(item))
            continue
        single = _coerce_text(value)
        if single:
            refs.append(single)

    direct_ref = _pick_ref(row, ("issue_ref", "linked_issue_ref"))
    if direct_ref:
        refs.append(direct_ref)

    commit_text = _commit_text(row)
    if commit_text:
        commit_issue_refs, _ = parse_commit_references(commit_text)
        refs.extend(commit_issue_refs)

    return _unique_preserve(refs)


def _issue_contexts(row: Dict[str, Any]) -> list[Dict[str, str]]:
    metadata = _coerce_mapping(row.get("metadata"))
    merged: dict[str, Dict[str, str]] = {}
    ordered_keys: list[str] = []

    def add_context(candidate: Any) -> None:
        context = _coerce_mapping(candidate)
        if not context:
            return
        ref = _coerce_text(context.get("ref") or context.get("issue_ref"))
        title = _coerce_text(context.get("title") or context.get("issue_title"))
        body = _coerce_block_text(context.get("body") or context.get("issue_body"))
        url = _coerce_text(context.get("url") or context.get("issue_url"))
        key = ref or title or body or url
        if not key:
            return
        if key not in merged:
            merged[key] = {}
            ordered_keys.append(key)
        target = merged[key]
        if ref and not target.get("ref"):
            target["ref"] = ref
        if title and not target.get("title"):
            target["title"] = title
        if body and not target.get("body"):
            target["body"] = body
        if url and not target.get("url"):
            target["url"] = url

    for value in (row.get("issue_contexts"), metadata.get("issue_contexts")):
        if not isinstance(value, (list, tuple)):
            continue
        for item in value:
            add_context(item)

    add_context(
        {
            "ref": _pick_ref(row, ("issue_ref", "linked_issue_ref")),
            "title": _pick_text(
                row,
                ("issue_title", "linked_issue_title", "source_issue_title"),
            ),
            "body": _pick_block_text(
                row,
                ("issue_body", "linked_issue_body", "source_issue_body"),
            ),
            "url": _pick_ref(row, ("issue_url", "linked_issue_url")),
        }
    )

    for ref in _extract_issue_refs(row):
        add_context({"ref": ref})

    return [merged[key] for key in ordered_keys]


@lru_cache(maxsize=16)
def _load_bead_contexts(repo_root: str) -> dict[str, Dict[str, str]]:
    issues_path = Path(repo_root) / ".beads" / "issues.jsonl"
    if not issues_path.exists():
        return {}

    contexts: dict[str, Dict[str, str]] = {}
    try:
        lines = issues_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    for line in lines:
        value = line.strip()
        if not value:
            continue
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        bead_id = _coerce_text(payload.get("id"))
        if not bead_id:
            continue
        contexts[bead_id] = {
            "id": bead_id,
            "title": _coerce_text(payload.get("title")),
            "description": _coerce_block_text(payload.get("description")),
            "acceptance_criteria": _coerce_block_text(
                payload.get("acceptance_criteria")
            ),
            "design": _coerce_block_text(payload.get("design")),
        }
    return contexts


def _bead_contexts(
    row: Dict[str, Any], repo_root: str | Path | None
) -> list[Dict[str, str]]:
    if repo_root is None:
        return []

    context_map = _load_bead_contexts(str(Path(repo_root).resolve()))
    if not context_map:
        return []

    metadata = _coerce_mapping(row.get("metadata"))
    refs = _unique_preserve(
        [
            *_coerce_list(row.get("bead_refs")),
            *_coerce_list(metadata.get("bead_refs")),
            _coerce_text(row.get("bead_id")),
            _coerce_text(metadata.get("bead_id")),
        ]
    )
    if not refs:
        commit_tokens = _BEAD_TOKEN_RE.findall(_commit_text(row))
        refs = [token for token in commit_tokens if token in context_map]

    return [context_map[ref] for ref in refs if ref in context_map]


def _render_file_summary(file_roles: Dict[str, list[str]]) -> str:
    prod_files = _coerce_list(file_roles.get("prod"))
    test_files = _coerce_list(file_roles.get("test"))
    details = []
    if prod_files:
        details.append(f"Production changes: {', '.join(prod_files)}.")
    if test_files:
        details.append(f"Test changes: {', '.join(test_files)}.")
    return " ".join(details)


def _indented_block(label: str, text: str, *, indent: str = "  ") -> list[str]:
    block = _coerce_block_text(text)
    if not block:
        return []
    lines = block.splitlines()
    if len(lines) == 1:
        return [f"{indent}{label}: {lines[0]}"]
    return [f"{indent}{label}:"] + [f"{indent}{line}" for line in lines]


def _bullet_labeled_block(label: str, text: str) -> list[str]:
    block = _coerce_block_text(text)
    if not block:
        return []
    lines = block.splitlines()
    if len(lines) == 1:
        return [f"- {label}: {lines[0]}"]
    return [f"- {label}:"] + [f"  {line}" for line in lines]


def _render_issue_context_lines(
    row: Dict[str, Any], *, skip_refs: set[str] | None = None
) -> list[str]:
    lines: list[str] = []
    for context in _issue_contexts(row):
        ref = _coerce_text(context.get("ref"))
        if ref and skip_refs and ref in skip_refs:
            continue
        prefix = f"Related GitHub issue #{ref}" if ref else "Related GitHub issue"
        title = _coerce_text(context.get("title"))
        body = _coerce_block_text(context.get("body"))
        if title and body and body != title:
            lines.append(f"- {prefix}: {title}")
            lines.extend(_indented_block("Context", body))
            continue
        if title:
            lines.append(f"- {prefix}: {title}")
            continue
        if body:
            lines.append(f"- {prefix}.")
            lines.extend(_indented_block("Context", body))
            continue
        lines.append(f"- {prefix}.")
    return lines


def _render_bead_context_lines(
    row: Dict[str, Any], repo_root: str | Path | None
) -> list[str]:
    lines: list[str] = []
    for context in _bead_contexts(row, repo_root):
        bead_id = _coerce_text(context.get("id"))
        title = _coerce_text(context.get("title"))
        description = _coerce_block_text(context.get("description"))
        acceptance = _coerce_block_text(context.get("acceptance_criteria"))
        detail = title or description or acceptance or "Referenced bead context."
        lines.append(f"- Bead {bead_id}: {detail}")
        if description and description != detail:
            lines.extend(_indented_block("Context", description))
        if acceptance:
            lines.extend(_indented_block("Acceptance", acceptance))
    return lines


def _supporting_context_lines(
    row: Dict[str, Any],
    *,
    repo_root: str | Path | None = None,
    skip_issue_refs: set[str] | None = None,
) -> list[str]:
    lines: list[str] = []
    lines.extend(_render_bead_context_lines(row, repo_root))
    lines.extend(_render_issue_context_lines(row, skip_refs=skip_issue_refs))
    return lines


def _issue_style_statement(
    title: str,
    body: str | None,
    reference: str,
    kind: str,
    *,
    supporting_lines: Iterable[str] = (),
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
        parts.extend(_bullet_labeled_block("Context", body_text))
    parts.extend(line for line in supporting_lines if line)
    parts.extend(
        [
            "- Reproduction: run the project's tests around this change.",
            "- Expected behavior: the regression described by the source should be fixed.",
        ]
    )
    return "\n".join(parts), "linked_issue" if "issue" in kind else "pull_request"


def _commit_style_statement(
    row: Dict[str, Any],
    patch: str,
    *,
    supporting_lines: Iterable[str] = (),
) -> tuple[str, str]:
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
    parts.extend(line for line in supporting_lines if line)
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


def _append_supporting_lines(statement: str, supporting_lines: Iterable[str]) -> str:
    lines = [line for line in supporting_lines if line]
    if not lines:
        return statement
    return f"{statement.rstrip()}\n" + "\n".join(lines)


def synthesize_problem_statement(
    row: Dict[str, Any], patch: str = "", *, repo_root: str | Path | None = None
) -> tuple[str, str, str | None]:
    """Return a deterministic issue-style statement, a source label, and provenance string."""

    issue_title = _pick_text(
        row,
        ("issue_title", "linked_issue_title", "source_issue_title"),
    )
    issue_body = _pick_block_text(
        row,
        ("issue_body", "linked_issue_body", "source_issue_body"),
    )
    if issue_title or issue_body:
        refs = _extract_issue_refs(row)
        ref = refs[0] if refs else _pick_ref(row, ("issue_ref", "linked_issue_ref"))
        statement, source = _issue_style_statement(
            issue_title,
            issue_body,
            ref,
            "linked_issue",
            supporting_lines=_supporting_context_lines(
                row,
                repo_root=repo_root,
                skip_issue_refs={ref} if ref else None,
            ),
        )
        return statement, source, ref or None

    pr_title = _pick_text(
        row,
        ("pr_title", "pull_request_title", "source_pr_title"),
    )
    pr_body = _pick_block_text(
        row,
        ("pr_body", "pull_request_body", "source_pr_body"),
    )
    if pr_title or pr_body:
        statement, source = _issue_style_statement(
            pr_title,
            pr_body,
            _pick_ref(row, ("pr_ref", "pull_request_number")),
            "pull_request",
            supporting_lines=_supporting_context_lines(
                row,
                repo_root=repo_root,
            ),
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
    supporting_lines = _supporting_context_lines(row, repo_root=repo_root)
    commit_statement, commit_source = _commit_style_statement(
        row,
        patch,
        supporting_lines=supporting_lines,
    )
    llm_text = _llm_candidate(row)
    if llm_text and _is_weak_source(commit_subject, commit_body):
        ref = _pick_ref(row, ("llm_advisory_model", "llm_model")) or _pick_text(
            row, ("llm_reference",)
        )
        return (
            _append_supporting_lines(llm_text, supporting_lines),
            "llm_advisory",
            ref or None,
        )

    return commit_statement, commit_source, _pick_text(row, ("commit", "source_commit"))
