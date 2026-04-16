import json
from pathlib import Path

from repogauge.mining import enrich


def _cache_payload(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_parse_commit_references_recognizes_github_refs_and_numbers() -> None:
    issue_refs, pr_refs = enrich.parse_commit_references(
        "Fix issue #123 from https://github.com/org/repo/issues/456 and "
        "Merge pull request #789. "
        "Also handles PR #321 and GH-654."
    )
    assert issue_refs == ["456", "123", "654"]
    assert pr_refs == ["789", "321"]


def test_enrich_commit_metadata_populates_issue_and_pr_fields(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[str] = []

    def fake_fetch_json(url: str, token: str | None = None):
        calls.append(url)
        if "issues/123" in url:
            return {
                "title": "Issue title",
                "body": "Issue body",
                "html_url": "https://github.com/owner/repo/issues/123",
            }
        if "pulls/456" in url:
            return {
                "title": "PR title",
                "body": "PR body",
                "html_url": "https://github.com/owner/repo/pull/456",
            }
        return None

    monkeypatch.setattr(enrich, "_fetch_json", fake_fetch_json)

    metadata = enrich.enrich_commit_metadata(
        commit_subject="Fixes #123 and PR #456",
        commit_body="",
        repo_name="owner/repo",
        token="gh_token",
        cache_path=tmp_path / "cache.json",
    )

    assert metadata["issue_title"] == "Issue title"
    assert metadata["issue_body"] == "Issue body"
    assert metadata["pr_title"] == "PR title"
    assert metadata["pr_body"] == "PR body"
    assert metadata["issue_ref"] == "123"
    assert metadata["pr_ref"] == "456"
    assert metadata["provenance"] == {
        "issue_title": "from_issue",
        "issue_body": "from_issue",
        "issue_ref": "from_issue",
        "issue_url": "from_issue",
        "pr_title": "from_pr",
        "pr_body": "from_pr",
        "pr_ref": "from_pr",
        "pr_number": "from_pr",
        "pr_url": "from_pr",
    }
    assert calls

    payload = _cache_payload(tmp_path / "cache.json")
    assert "owner/repo:issue:123" in payload
    assert "owner/repo:pull:456" in payload


def test_enrich_commit_metadata_reuses_cached_payload_without_fetch(
    monkeypatch, tmp_path: Path
) -> None:
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "owner/repo:issue:42": {
                    "status": "ok",
                    "payload": {
                        "title": "Cached issue",
                        "body": "from cache",
                        "html_url": "https://github.com/owner/repo/issues/42",
                    },
                }
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    fetch_calls: list[str] = []

    def forbidden_fetch_json(url: str, token: str | None = None):
        fetch_calls.append(url)
        return {
            "title": "Should not be used",
            "body": "Should not be used",
        }

    monkeypatch.setattr(enrich, "_fetch_json", forbidden_fetch_json)

    metadata = enrich.enrich_commit_metadata(
        commit_subject="Fix #42",
        commit_body="",
        repo_name="owner/repo",
        token="gh_token",
        cache_path=cache_path,
    )

    assert metadata["issue_title"] == "Cached issue"
    assert metadata["issue_body"] == "from cache"
    assert not fetch_calls
