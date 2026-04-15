from pathlib import Path

from repogauge.exec import run_command
from repogauge.mining.scan import scan_repository


def _init_repo(base: Path) -> Path:
    run_command(["git", "init", "-b", "main"], cwd=str(base))
    run_command(["git", "config", "user.name", "ci"], cwd=str(base))
    run_command(["git", "config", "user.email", "ci@example.com"], cwd=str(base))
    return base


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    return _init_repo(repo)


def test_scan_captures_commit_shape_metadata(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    (repo / "src").mkdir()
    (repo / "src" / "core.py").write_text("x = 1\n", encoding="utf-8")
    run_command(["git", "-C", str(repo), "add", "src/core.py"])
    run_command(["git", "-C", str(repo), "commit", "-m", "Add core module"])

    (repo / "src" / "core.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests" / "test_core.py").write_text("def test_core():\n    assert True\n", encoding="utf-8")
    run_command(["git", "-C", str(repo), "add", "src/core.py", "tests/test_core.py"])
    run_command(["git", "-C", str(repo), "commit", "-m", "Fix core behavior"])

    rows = scan_repository(repo, repo_name="owner/repo", max_count=10)
    assert len(rows) == 2
    newest = rows[0]
    assert newest.repo == "owner/repo"
    assert newest.state == "discovered"
    assert newest.metadata["n_prod_files"] >= 1
    assert newest.metadata["n_test_files"] >= 1
    assert newest.metadata["decision_band"] in {"shortlist", "review", "reject"}
    assert isinstance(newest.metadata["score_breakdown"], list)
    assert "total_changed_lines" in newest.metadata
    assert "heuristic_score" not in newest.__dict__ or newest.heuristic_score >= 0.0
    assert newest.changed_lines >= 0
    assert newest.metadata["n_hunks"] >= 1
    assert isinstance(newest.files_touched, list)


def test_scan_detects_rename_only_commits_and_merge_metadata(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    (repo / "src").mkdir()
    (repo / "src" / "core.py").write_text("x = 1\n", encoding="utf-8")
    run_command(["git", "-C", str(repo), "add", "src/core.py"])
    run_command(["git", "-C", str(repo), "commit", "-m", "Add core module"])

    run_command(["git", "-C", str(repo), "checkout", "-b", "feature"])
    run_command(["git", "-C", str(repo), "mv", "src/core.py", "src/core_renamed.py"])
    run_command(["git", "-C", str(repo), "add", "src/core_renamed.py"])
    run_command(["git", "-C", str(repo), "rm", "src/core.py"])
    run_command(["git", "-C", str(repo), "commit", "-m", "Refactor core path"])

    run_command(["git", "-C", str(repo), "checkout", "main"])
    (repo / "README.md").write_text("# Repo\n", encoding="utf-8")
    run_command(["git", "-C", str(repo), "add", "README.md"])
    run_command(["git", "-C", str(repo), "commit", "-m", "Update docs"])
    run_command(["git", "-C", str(repo), "merge", "--no-ff", "feature", "-m", "Merge rename feature"])

    rows = scan_repository(repo, repo_name="owner/repo", max_count=10)
    assert len(rows) == 3
    assert any(row.metadata.get("is_merge") for row in rows)
    rename_rows = [row for row in rows if row.metadata.get("has_rename_only")]
    assert rename_rows
    assert all(row.metadata["total_changed_lines"] == 0 for row in rename_rows)
