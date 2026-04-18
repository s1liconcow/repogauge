"""Review workflow helpers for candidate curation."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from repogauge.config import ContractState, ReviewedCandidate
from repogauge.mining.file_roles import classify_files
from repogauge.llm import (
    LlmModelSpec,
    TriageSuggestion,
    load_triage_payload,
    write_triage_payload,
)

STATE_ACCEPTED = ContractState.ACCEPTED
STATE_REJECTED = ContractState.REJECTED
STATE_OPEN = ContractState.OPEN

LLM_OFF = "off"
LLM_LOCAL_ONLY = "local_only"
LLM_ALLOW_REMOTE = "allow_remote"

TRIAGE_CACHE_FILENAME = "triage_cache.json"
TRIAGE_DEFAULT_MODEL_NAME = "local-policy"
TRIAGE_DEFAULT_PROVIDER = "local"
LOCAL_ONLY_PROVIDERS = {
    "local",
    "local-only",
    "offline",
    "builtin",
    "built-in",
    "internal",
}


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
    return str(
        row.get("commit_subject", row.get("problem_statement", ""))
        or row.get("repo", "")
    ).strip()


def _candidate_body(row: dict[str, Any]) -> str:
    return str(row.get("commit_body", row.get("problem_statement", "")) or "").strip()


def _extract_decision_band(row: dict[str, Any]) -> str:
    return str(row.get("metadata", {}).get("decision_band", ""))


def _has_test_change(row: dict[str, Any]) -> bool:
    metadata = row.get("metadata", {})
    if isinstance(metadata, dict):
        try:
            return int(metadata.get("n_test_files", 0) or 0) > 0
        except (TypeError, ValueError):
            pass
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


def _coerce_llm_mode(value: Any) -> str:
    mode = str(value or LLM_OFF).lower()
    if mode not in {LLM_OFF, LLM_LOCAL_ONLY, LLM_ALLOW_REMOTE}:
        return LLM_OFF
    return mode


def _normalize_provider(value: Any) -> str:
    provider = str(value or TRIAGE_DEFAULT_PROVIDER).strip().lower()
    if not provider:
        return TRIAGE_DEFAULT_PROVIDER
    return provider.replace("_", "-")


def _provider_requires_remote_gate(mode: str, provider: str) -> bool:
    normalized = _normalize_provider(provider)
    return mode == LLM_LOCAL_ONLY and normalized not in LOCAL_ONLY_PROVIDERS


def _llm_policy_warning(mode: str, provider: str) -> str | None:
    normalized = _normalize_provider(provider)
    if mode != LLM_ALLOW_REMOTE:
        return None
    if normalized in LOCAL_ONLY_PROVIDERS:
        return None
    return (
        "Running with --llm-mode allow_remote enables advisory triage metadata that may"
        " include remote provider references in artifacts."
    )


def _validate_llm_policy(mode: str, model: LlmModelSpec) -> None:
    if _provider_requires_remote_gate(mode, model.provider):
        raise ValueError(
            f"remote provider '{model.provider}' requires --llm-mode allow_remote"
        )


def _merge_file_roles(
    files_by_role: dict[str, list[str]], suggested: dict[str, list[str]]
) -> dict[str, list[str]]:
    merged = {role: sorted(set(paths)) for role, paths in files_by_role.items()}
    for role, paths in suggested.items():
        existing = merged.setdefault(role, [])
        for path in paths:
            if path and path not in existing:
                existing.append(path)
        merged[role] = sorted(set(existing))
    return merged


def _default_triage_suggestion(
    row: dict[str, Any], candidate_id: str
) -> TriageSuggestion:
    files = [str(value) for value in row.get("files_touched", [])]
    file_roles = _file_roles(files)
    subject = _candidate_subject(row)
    return TriageSuggestion(
        candidate_id=candidate_id,
        state=None,
        reason="Local deterministic triage fallback",
        reviewer_notes="Generated offline from scan metadata.",
        suggested_problem_statement=subject,
        suggested_file_roles=file_roles,
        confidence=1.0,
    )


def _load_llm_artifacts(
    *,
    out_root: Path,
    llm_mode: str,
    triage_hints_path: Path | None,
    llm_model_name: str | None,
    llm_provider: str | None,
) -> tuple[dict[str, TriageSuggestion], dict[str, TriageSuggestion], LlmModelSpec]:
    model = LlmModelSpec(
        model_name=llm_model_name or TRIAGE_DEFAULT_MODEL_NAME,
        provider=llm_provider or TRIAGE_DEFAULT_PROVIDER,
        prompt_version="triage/v1",
    )
    source_hints: dict[str, TriageSuggestion] = {}
    cache_hints: dict[str, TriageSuggestion] = {}
    triage_model = model
    if llm_mode == LLM_OFF:
        return cache_hints, source_hints, model

    cache_path = out_root / TRIAGE_CACHE_FILENAME
    triage_model, cache_hints = load_triage_payload(
        cache_path,
        default_name=model.model_name,
        default_provider=model.provider,
    )

    if triage_hints_path is not None:
        source_model, source_hints = load_triage_payload(
            triage_hints_path,
            default_name=llm_model_name or triage_model.model_name,
            default_provider=llm_provider or triage_model.provider,
        )
        if source_model and source_model.model_name:
            triage_model = source_model
    return cache_hints, source_hints, triage_model


def _render_score_breakdown(
    score_breakdown: list[dict[str, Any]], score: float
) -> list[str]:
    from repogauge.mining.score import AUTO_SHORTLIST_THRESHOLD, REVIEW_THRESHOLD

    lines: list[str] = []
    if not score_breakdown:
        return lines
    band_label = (
        f"shortlist (≥{AUTO_SHORTLIST_THRESHOLD})"
        if score >= AUTO_SHORTLIST_THRESHOLD
        else f"review (≥{REVIEW_THRESHOLD})"
        if score >= REVIEW_THRESHOLD
        else f"reject (<{REVIEW_THRESHOLD})"
    )
    lines.append(f"- Score breakdown: **{score:.2f}** → {band_label}")
    for entry in score_breakdown:
        weight = entry.get("weight", 0)
        sign = "+" if weight >= 0 else ""
        lines.append(
            f"  - `{sign}{weight}` **{entry.get('component', '?')}**: {entry.get('reason', '')}"
        )
    return lines


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
        score_breakdown = (
            row.get("reviewed_record", {})
            .get("metadata", {})
            .get("score_breakdown", [])
        )
        lines.extend(_render_score_breakdown(score_breakdown, row["heuristic_score"]))
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
    from repogauge.mining.score import AUTO_SHORTLIST_THRESHOLD, REVIEW_THRESHOLD

    lines = [
        "<!doctype html><html><head>",
        "<meta charset='utf-8'/>",
        "<title>RepoGauge Review Artifact</title>",
        "<style>",
        "body{font-family:ui-sans-serif,system-ui,Segoe UI,Roboto,sans-serif;line-height:1.4;padding:1rem;}",
        "table{border-collapse:collapse;width:100%;margin-top:1rem;}",
        "th,td{border:1px solid #ddd;padding:0.5rem;vertical-align:top;text-align:left;}",
        "th{background:#f4f4f5;}",
        ".breakdown{font-size:0.85em;color:#555;margin-top:0.3rem;}",
        ".breakdown li{list-style:none;padding:0;}",
        ".pos{color:#2a7a2a;} .neg{color:#b03030;}",
        ".band-shortlist{color:#1a6b1a;font-weight:bold;}",
        ".band-review{color:#7a5a00;font-weight:bold;}",
        ".band-reject{color:#b03030;font-weight:bold;}",
        "</style></head><body>",
        "<h1>RepoGauge Review Artifact</h1>",
        f"<p>Generated: {datetime.now(timezone.utc).replace(tzinfo=None).isoformat()}Z</p>",
        f"<p>Candidates: {len(records)}</p>",
        "<table>",
        "<thead><tr><th>ID</th><th>Repo</th><th>Commit</th><th>Score / breakdown</th><th>Decision</th><th>Reason</th><th>Files</th></tr></thead>",
        "<tbody>",
    ]

    for row in records:
        file_rows = []
        for role, files in row["file_roles"].items():
            file_rows.append(f"<strong>{role}</strong>: {', '.join(files)}")

        score = row["heuristic_score"]
        if score >= AUTO_SHORTLIST_THRESHOLD:
            band_css, band_label = (
                "band-shortlist",
                f"shortlist (≥{AUTO_SHORTLIST_THRESHOLD})",
            )
        elif score >= REVIEW_THRESHOLD:
            band_css, band_label = "band-review", f"review (≥{REVIEW_THRESHOLD})"
        else:
            band_css, band_label = "band-reject", f"reject (&lt;{REVIEW_THRESHOLD})"

        score_breakdown = (
            row.get("reviewed_record", {})
            .get("metadata", {})
            .get("score_breakdown", [])
        )
        breakdown_html = ""
        if score_breakdown:
            items = []
            for entry in score_breakdown:
                w = entry.get("weight", 0)
                sign = "+" if w >= 0 else ""
                css = "pos" if w >= 0 else "neg"
                items.append(
                    f"<li><span class='{css}'>{sign}{w}</span> "
                    f"<strong>{entry.get('component', '?')}</strong>: "
                    f"{entry.get('reason', '')}</li>"
                )
            breakdown_html = f"<ul class='breakdown'>{''.join(items)}</ul>"

        lines.append(
            "<tr>"
            f"<td>{row['id']}</td>"
            f"<td>{row['repo']}</td>"
            f"<td><code>{row['commit']}</code></td>"
            f"<td><span class='{band_css}'>{score:.2f} → {band_label}</span>{breakdown_html}</td>"
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


def _run_decision(
    row: dict[str, Any], decision: Optional[dict[str, Any]]
) -> tuple[str, str | None, str]:
    source_state = str(row.get("review_state") or row.get("state") or STATE_OPEN.value)
    if decision:
        requested = _normalize_decision_state(decision.get("state"))
        if requested in {STATE_ACCEPTED.value, STATE_REJECTED.value}:
            return (
                requested,
                str(decision.get("reason") or ""),
                str(decision.get("reviewer_notes") or ""),
            )
        if requested == STATE_OPEN.value:
            return (
                STATE_OPEN.value,
                str(decision.get("reason") or ""),
                str(decision.get("reviewer_notes") or ""),
            )

    if source_state in {STATE_ACCEPTED.value, STATE_REJECTED.value, STATE_OPEN.value}:
        return source_state, None, ""

    if _extract_decision_band(row) == "shortlist":
        return (
            STATE_ACCEPTED.value,
            "Auto-accepted from shortlist",
            "Auto-generated acceptance",
        )

    return (
        STATE_REJECTED.value,
        "Auto-rejected (not in shortlist)",
        "No manual decision provided",
    )


def run_review(
    *,
    candidates_path: Path,
    out_root: Path,
    decisions_path: Path | None = None,
    llm_mode: str | None = None,
    triage_hints_path: Path | None = None,
    llm_model_name: str | None = None,
    llm_provider: str | None = None,
) -> dict[str, str | int]:
    rows = _read_jsonl(candidates_path)
    if not rows:
        raise ValueError("no candidate rows to review")

    decisions = _load_decisions(decisions_path)
    mode = _coerce_llm_mode(llm_mode)
    triage_cache, triage_source, triage_model = (
        {},
        {},
        LlmModelSpec(
            model_name=llm_model_name or TRIAGE_DEFAULT_MODEL_NAME,
            provider=llm_provider or TRIAGE_DEFAULT_PROVIDER,
            prompt_version="triage/v1",
        ),
    )
    if mode != LLM_OFF:
        triage_cache, triage_source, triage_model = _load_llm_artifacts(
            out_root=out_root,
            llm_mode=mode,
            triage_hints_path=triage_hints_path,
            llm_model_name=llm_model_name,
            llm_provider=llm_provider,
        )
    triage_model.provider = _normalize_provider(triage_model.provider)
    _validate_llm_policy(mode, triage_model)
    policy_warning = _llm_policy_warning(mode, triage_model.provider)
    triage_hints = {**triage_cache, **triage_source}

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

        triage_hint = triage_hints.get(candidate_id)
        if triage_hint is None and mode != LLM_OFF:
            triage_hint = _default_triage_suggestion(row, candidate_id)
            triage_hints[candidate_id] = triage_hint

        if triage_hint and triage_hint.suggested_problem_statement:
            subject = triage_hint.suggested_problem_statement
            if not body:
                body = triage_hint.suggested_problem_statement

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
        if triage_hint and triage_hint.suggested_file_roles:
            file_roles = _merge_file_roles(file_roles, triage_hint.suggested_file_roles)

        metadata = dict(row.get("metadata", {}))
        metadata.update(
            {
                "decision_source": "script" if decision else "default",
                "original_state": str(
                    row.get("review_state", row.get("state", STATE_OPEN.value))
                ),
                "heuristic_score": score,
                "decision_band": decision_band,
                "candidate_id": candidate_id,
                "force_include": force_include,
                "has_test_change": _has_test_change(row),
                "file_roles": file_roles,
                "source_commit": commit,
                "source_subject": subject,
                "source_body": body,
                "llm_advisory": {
                    "enabled": mode != LLM_OFF,
                    "model": triage_model.to_dict(),
                    "suggested_state": triage_hint.state if triage_hint else None,
                    "applied": triage_hint is not None,
                    "reason": triage_hint.reason if triage_hint else None,
                    "reviewer_notes": triage_hint.reviewer_notes
                    if triage_hint
                    else None,
                    "problem_statement": triage_hint.suggested_problem_statement
                    if triage_hint
                    else None,
                    "file_roles_hint": triage_hint.suggested_file_roles
                    if triage_hint
                    else None,
                    "confidence": triage_hint.confidence if triage_hint else None,
                },
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
    if mode != LLM_OFF:
        write_triage_payload(
            out_root / TRIAGE_CACHE_FILENAME, triage_model, triage_hints
        )

    reviewed_path = out_root / "reviewed.jsonl"
    reviewed_path.write_text(
        "".join(
            json.dumps(row["reviewed_record"], sort_keys=True) + "\n"
            for row in prepared_rows
        ),
        encoding="utf-8",
    )

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
        "warnings": [policy_warning] if policy_warning else [],
    }
