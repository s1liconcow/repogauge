"""Optional GitHub metadata enrichment for mined commits."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from json import JSONDecodeError
from pathlib import Path
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


_PR_URL_RE = re.compile(
    r"https?://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/(?:pull)/(?P<number>\d+)",
    re.IGNORECASE,
)
_ISSUE_URL_RE = re.compile(
    r"https?://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/(?:issues)/(?P<number>\d+)",
    re.IGNORECASE,
)
_MERGE_PR_RE = re.compile(
    r"\bmerge\s+pull\s+request\s+#?(?P<number>\d+)\b", re.IGNORECASE
)
_PR_KEYWORD_RE = re.compile(
    r"\b(?:pr|pull request)\s*#?\s*(?P<number>\d+)\b",
    re.IGNORECASE,
)
_ISSUE_REF_RE = re.compile(r"(?<![A-Za-z0-9])(?:#|gh-)(?P<number>\d+)\b", re.IGNORECASE)


def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text


def _coerce_repo_name(repo_name: Any) -> str:
    return _coerce_str(repo_name).strip().lower()


def parse_commit_references(text: str) -> tuple[list[str], list[str]]:
    """Return issue refs and PR refs discovered in commit text."""

    issue_refs: list[str] = []
    pr_refs: list[str] = []
    if not text:
        return issue_refs, pr_refs

    for match in _PR_URL_RE.finditer(text):
        number = _coerce_str(match.group("number"))
        if number and number not in pr_refs:
            pr_refs.append(number)

    for match in _ISSUE_URL_RE.finditer(text):
        number = _coerce_str(match.group("number"))
        if number and number not in issue_refs:
            issue_refs.append(number)

    for match in _MERGE_PR_RE.finditer(text):
        number = _coerce_str(match.group("number"))
        if number and number not in pr_refs:
            pr_refs.append(number)

    for match in _PR_KEYWORD_RE.finditer(text):
        number = _coerce_str(match.group("number"))
        if number and number not in pr_refs:
            pr_refs.append(number)

    for match in _ISSUE_REF_RE.finditer(text):
        number = _coerce_str(match.group("number"))
        if number and number not in issue_refs and number not in pr_refs:
            issue_refs.append(number)

    return issue_refs, pr_refs


def _make_cache_key(repo_name: str, ref_type: str, number: str) -> str:
    return f"{repo_name}:{ref_type}:{number}"


def _load_enrichment_cache(cache_path: Path | None) -> dict[str, Any]:
    if cache_path is None or not cache_path.exists():
        return {}
    raw = cache_path.read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _save_enrichment_cache(cache_path: Path, payload: dict[str, Any]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _build_request_headers(token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "RepoGauge-Optional-Enrichment",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _fetch_json(url: str, token: str | None = None) -> dict[str, Any] | None:
    request = Request(url=url, headers=_build_request_headers(token))
    try:
        with urlopen(request, timeout=5) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload) if payload else None
    except HTTPError as exc:
        if exc.code in {403, 429}:
            return None
        return None
    except (URLError, TimeoutError, OSError, ValueError):
        return None


def _fetch_cached_entry(
    cache: dict[str, Any],
    key: str,
) -> dict[str, Any] | None:
    raw = cache.get(key)
    if not isinstance(raw, dict):
        return None
    return raw


def _write_cache_entry(
    cache: dict[str, Any],
    key: str,
    status: str,
    payload: dict[str, Any] | None,
    *,
    repo_name: str,
    ref_type: str,
    ref_number: str,
) -> None:
    cache[key] = {
        "status": status,
        "repo": repo_name,
        "type": ref_type,
        "number": ref_number,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }


def _fetch_reference_payload(
    *,
    repo_name: str,
    ref_type: str,
    ref_number: str,
    token: str | None,
    cache: dict[str, Any],
    cache_path: Path | None = None,
) -> dict[str, Any] | None:
    key = _make_cache_key(repo_name, ref_type, ref_number)
    entry = _fetch_cached_entry(cache, key)
    if entry is not None:
        if entry.get("status") == "ok":
            payload = entry.get("payload")
            return payload if isinstance(payload, dict) else None
        return None

    if ref_type == "pull":
        endpoint = f"https://api.github.com/repos/{repo_name}/pulls/{ref_number}"
    else:
        endpoint = f"https://api.github.com/repos/{repo_name}/issues/{ref_number}"

    payload = _fetch_json(endpoint, token=token)
    if payload is None:
        _write_cache_entry(
            cache,
            key,
            "missing",
            None,
            repo_name=repo_name,
            ref_type=ref_type,
            ref_number=ref_number,
        )
        if cache_path is not None:
            _save_enrichment_cache(cache_path, cache)
        return None

    _write_cache_entry(
        cache,
        key,
        "ok",
        payload,
        repo_name=repo_name,
        ref_type=ref_type,
        ref_number=ref_number,
    )
    if cache_path is not None:
        _save_enrichment_cache(cache_path, cache)
    return payload


def _extract_title_body(payload: dict[str, Any]) -> tuple[str, str]:
    title = _coerce_str(payload.get("title"))
    body = _coerce_str(payload.get("body"))
    return title, body


def enrich_commit_metadata(
    *,
    commit_subject: str,
    commit_body: str,
    repo_name: str,
    token: str | None = None,
    cache_path: Path | None = None,
) -> dict[str, Any]:
    """Resolve related issue/PR metadata and return metadata fields."""

    normalized_repo = _coerce_repo_name(repo_name)
    if "/" not in normalized_repo:
        return {}

    combined_text = f"{commit_subject}\n{commit_body}"
    issue_refs, pr_refs = parse_commit_references(combined_text)
    if not issue_refs and not pr_refs:
        return {}

    cache = _load_enrichment_cache(cache_path)
    metadata: dict[str, Any] = {
        "issue_refs": issue_refs[:],
        "pull_request_refs": pr_refs[:],
        "issue_title": "",
        "issue_body": "",
        "pr_title": "",
        "pr_body": "",
    }
    provenance: dict[str, str] = {}

    for issue_number in issue_refs:
        payload = _fetch_reference_payload(
            repo_name=normalized_repo,
            ref_type="issue",
            ref_number=issue_number,
            token=token,
            cache=cache,
            cache_path=cache_path,
        )
        if not isinstance(payload, dict):
            continue
        title, body = _extract_title_body(payload)
        if title and not metadata["issue_title"]:
            metadata["issue_title"] = title
            metadata["issue_body"] = body
            metadata["issue_ref"] = issue_number
            metadata["issue_url"] = _coerce_str(payload.get("html_url"))
            provenance["issue_title"] = "from_issue"
            provenance["issue_body"] = "from_issue"
            provenance["issue_ref"] = "from_issue"
            provenance["issue_url"] = "from_issue"
        break

    for pr_number in pr_refs:
        payload = _fetch_reference_payload(
            repo_name=normalized_repo,
            ref_type="pull",
            ref_number=pr_number,
            token=token,
            cache=cache,
            cache_path=cache_path,
        )
        if not isinstance(payload, dict):
            continue
        title, body = _extract_title_body(payload)
        if title and not metadata["pr_title"]:
            metadata["pr_title"] = title
            metadata["pr_body"] = body
            metadata["pr_ref"] = pr_number
            metadata["pr_number"] = pr_number
            metadata["pr_url"] = _coerce_str(payload.get("html_url"))
            provenance["pr_title"] = "from_pr"
            provenance["pr_body"] = "from_pr"
            provenance["pr_ref"] = "from_pr"
            provenance["pr_number"] = "from_pr"
            provenance["pr_url"] = "from_pr"
        break

    if provenance:
        metadata["provenance"] = provenance
    if cache_path is not None:
        _save_enrichment_cache(cache_path, cache)

    for key in ("issue_title", "issue_body", "pr_title", "pr_body"):
        if not metadata[key]:
            metadata.pop(key, None)

    return metadata
