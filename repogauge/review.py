"""Review workflow helpers for candidate curation."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from repogauge.config import ContractState, ReviewedCandidate
from repogauge.mining.file_roles import classify_files

STATE_ACCEPTED = ContractState.ACCEPTED
STATE_REJECTED = ContractState.REJECTED
STATE_OPEN = ContractState.OPEN


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    payloads: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        payloads.append(json.loads(line))
    return payloads


def _normalize_decision_state(value: Any) -> str:
    if not isinstance(value, str):
        return str(STATE_OPEN.value)
    normalized = value.strip().lower()
    if normalized in {STATE_OPEN.value, STATE_ACCEPTED.value, STATE_REJECTED.value}:
        return normalized
    return STATE_OPEN.value


def _load_decisions(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return {}

    records: Iterable[dict[str, Any]]
    if raw.startswith("["):
        loaded = json.loads(raw)
        if not isinstance(loaded, list):
            raise ValueError("decisions file must contain a JSON array or jsonl lines")
        records = loaded
    else:
        records = [_parse_jsonl_line(line) for line in raw.splitlines() if line.strip()]
    mapping: dict[str, dict[str, Any]] = {}
    for item in records:
        if not isinstance(item, dict):
            continue
        if "id" not in item or "state" not in item:
            continue
        item = dict(item)
        item["state"] = _normalize_decision_state(item.get("state"))
        mapping[str(item["id"])] = item
    return mapping


def _parse_jsonl_line(line: str) -> dict[str, Any]:
    return json.loads(line)


def _extract_candidate_id(row: dict[str, Any]) -> str:
    if row.get("id"):
        return str(row["id"])
    if row.get("source_scan"):
        return str(row["source_scan"])
    raise ValueError("candidate row has no identifier")


def _candidate_subject(row: dict[str, Any]) -> str:
    return str(row.get("commit_subject", row.get("problem_statement", "")) or row.get("repo", "")).strip()


def _candidate_body(row: dict[str, Any]) -> str:
    return str(row.get("commit_body", row.get("problem_statement", "")) or "").strip()


def _extract_decision_band(row: dict[str, Any]) -> str:
    return str(row.get("metadata", {}).get("decision_band", ""))


def _has_test_change(row: dict[str, Any]) -> bool:
    files = row.get("files_touched")
    if isinstance(files, list):
        roles = classify_files([str(value) for value in files])
        return any(item.role == "test" for item in roles.values())
    return row.get("n_test_files", 0) > 0


def _file_roles(files: list[str]) -> dict[str, list[str]]:
    roles = classify_files(files)
    grouped: dict[str, list[str]] = {}
    for file_path, classification in sorted(roles.items()):
        grouped.setdefault(classification.role, []).append(file_path)
    return {key: sorted(values) for key, values in grouped.items()}


def _extract_issue_refs(text: str) -> list[str]:
    if not text:
        return []
    return sorted(set(re.findall(r"(?:#|GH-)(\d+)", text, flags=re.IGNORECASE)))


def _render_markdown(records: list[dict[str, Any]]) -> str:
    lines: list[str] = [
        "# RepoGauge Review Artifact",
        "",
        f"Generated: {datetime.now(timezone.utc).replace(tzinfo=None).isoformat()}Z",
        "",
        f"Candidates: {len(records)}",
        "",
    ]

    for row in records:
        candidate_id = row["id"]
        lines.extend(
            [
                f"## {candidate_id}",
                f"- Repo: {row['repo']}",
                f"- Commit: `{row['commit']}`",
                f"- Review state: {row['state']}",
                f"- Original state: {row['original_state']}",
                f"- Heuristic score: {row['heuristic_score']:.2f}",
                f"- Decision band: {row['decision_band'] or 'unknown'}",
                f"- Force include despite no test change: {row['force_include']}",
            ]
        )
        notes = row["notes"] or "—"
        lines.append(f"- Reviewer notes: {notes}")
        issue_refs = row["issue_refs"]
        if issue_refs:
            lines.append(f"- Linked references: {', '.join(issue_refs)}")
        lines.append("- Files by role:")
        for role, files in row["file_roles"].items():
            if not files:
                continue
            lines.append(f"  - **{role}**")
            for file_path in files:
                lines.append(f"    - `{file_path}`")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_html(records: list[dict[str, Any]]) -> str:
    lines = [
        "<!doctype html><html><head>",
        "<meta charset='utf-8'/>",
        "<title>RepoGauge Review Artifact</title>",
        "<style>",
        "body{font-family:ui-sans-serif,system-ui,Segoe UI,Roboto,sans-serif;line-height:1.4;padding:1rem;}",
        "table{border-collapse:collapse;width:100%;margin-top:1rem;}",
        "th,td{border:1px solid #ddd;padding:0.5rem;vertical-align:top;text-align:left;}",
        "th{background:#f4f4f5;}",
        "</style></head><body>",
        "<h1>RepoGauge Review Artifact</h1>",
        f"<p>Generated: {datetime.now(timezone.utc).replace(tzinfo=None).isoformat()}Z</p>",
        f"<p>Candidates: {len(records)}</p>",
        "<table>",
        "<thead><tr><th>ID</th><th>Repo</th><th>Commit</th><th>Heuristic score</th><th>Decision</th><th>Reason</th><th>Files</th></tr></thead>",
        "<tbody>",
    ]

    for row in records:
        file_rows = []
        for role, files in row["file_roles"].items():
            file_rows.append(f"<strong>{role}</strong>: {', '.join(files)}")
        lines.append(
            "<tr>"
            f"<td>{row['id']}</td>"
            f"<td>{row['repo']}</td>"
            f"<td><code>{row['commit']}</code></td>"
            f"<td>{row['heuristic_score']:.2f}</td>"
            f"<td>{row['state']}</td>"
            f"<td>{row['reason'] or '&mdash;'}</td>"
            f"<td>{'<br/>'.join(file_rows) if file_rows else '&mdash;'}</td>"
            "</tr>"
        )
    lines.extend(["</tbody></table>", "</body></html>"])
    return "".join(lines)


def _coerce_score(row: dict[str, Any]) -> float:
    value = row.get("heuristic_score", 0.0)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _run_decision(row: dict[str, Any], decision: Optional[dict[str, Any]]) -> tuple[str, str | None, str]:
    source_state = str(row.get("review_state") or row.get("state") or STATE_OPEN.value)
    if decision:
        requested = _normalize_decision_state(decision.get("state"))
        if requested in {STATE_ACCEPTED.value, STATE_REJECTED.value}:
            return requested, str(decision.get("reason") or ""), str(decision.get("reviewer_notes") or "")
        if requested == STATE_OPEN.value:
            return STATE_OPEN.value, str(decision.get("reason") or ""), str(decision.get("reviewer_notes") or "")

    if source_state in {STATE_ACCEPTED.value, STATE_REJECTED.value, STATE_OPEN.value}:
        return source_state, None, ""

    if _extract_decision_band(row) == "shortlist":
        return STATE_ACCEPTED.value, "Auto-accepted from shortlist", "Auto-generated acceptance"

    return STATE_REJECTED.value, "Auto-rejected (not in shortlist)", "No manual decision provided"


def run_review(*, candidates_path: Path, out_root: Path, decisions_path: Path | None = None) -> dict[str, str | int]:
    rows = _read_jsonl(candidates_path)
    if not rows:
        raise ValueError("no candidate rows to review")

    decisions = _load_decisions(decisions_path)
    prepared_rows = []
    accepted = rejected = open_count = 0

    for row in rows:
        candidate_id = _extract_candidate_id(row)
        repo = str(row.get("repo", "unknown"))
        commit = str(row.get("commit", ""))
        subject = _candidate_subject(row)
        body = _candidate_body(row)
        files = [str(value) for value in row.get("files_touched", [])]
        decision = decisions.get(candidate_id)
        force_include = bool(decision.get("force_include")) if decision else False
        state, reason, notes = _run_decision(row, decision)
        if state == STATE_OPEN.value:
            open_count += 1
        elif state == STATE_ACCEPTED.value:
            accepted += 1
        else:
            rejected += 1

        score = _coerce_score(row)
        decision_band = _extract_decision_band(row)
        file_roles = _file_roles(files)
        metadata = dict(row.get("metadata", {}))
        metadata.update(
            {
                "decision_source": "script" if decision else "default",
                "original_state": str(row.get("review_state", row.get("state", STATE_OPEN.value))),
                "heuristic_score": score,
                "decision_band": decision_band,
                "candidate_id": candidate_id,
                "force_include": force_include,
                "has_test_change": _has_test_change(row),
                "file_roles": file_roles,
                "source_commit": commit,
                "source_subject": subject,
                "source_body": body,
            }
        )
        prepared_row = {
            "id": candidate_id,
            "repo": repo,
            "commit": commit,
            "state": state,
            "reason": reason,
            "notes": notes,
            "subject": subject,
            "issue_refs": _extract_issue_refs(f"{subject} {body}"),
            "heuristic_score": score,
            "decision_band": decision_band,
            "original_state": str(row.get("state", "")),
            "file_roles": file_roles,
            "force_include": force_include,
            "reviewed_record": ReviewedCandidate(
                id=f"{candidate_id}-reviewed",
                candidate_id=candidate_id,
                repo=repo,
                reviewer_notes=notes,
                state=state,
                reason=reason,
                metadata=metadata,
            ).to_dict(),
        }
        prepared_rows.append(prepared_row)

    out_root.mkdir(parents=True, exist_ok=True)
    reviewed_path = out_root / "reviewed.jsonl"
    reviewed_path.write_text("".join(json.dumps(row["reviewed_record"], sort_keys=True) + "\n" for row in prepared_rows), encoding="utf-8")

    md_path = out_root / "review.md"
    html_path = out_root / "review.html"
    md_path.write_text(_render_markdown(prepared_rows), encoding="utf-8")
    html_path.write_text(_render_html(prepared_rows), encoding="utf-8")

    return {
        "candidates_path": str(candidates_path),
        "reviewed_path": str(reviewed_path),
        "markdown_path": str(md_path),
        "html_path": str(html_path),
        "total": len(prepared_rows),
        "accepted": accepted,
        "rejected": rejected,
        "open": open_count,
    }
