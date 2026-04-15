import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from repogauge.exec import run_command
from repogauge.export.materialize import MaterializationError, run_materialization


def _init_repo(base: Path) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    run_command(["git", "init", "-b", "main"], cwd=str(base))
    run_command(["git", "config", "user.name", "ci"], cwd=str(base))
    run_command(["git", "config", "user.email", "ci@example.com"], cwd=str(base))
    return base


def _commit_file(repo: Path, message: str, files: dict[str, str]) -> str:
    for relative_path, content in files.items():
        target = repo / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    run_command(["git", "-C", str(repo), "add", *files.keys()])
    run_command(["git", "-C", str(repo), "commit", "-m", message])
    return run_command(["git", "-C", str(repo), "rev-parse", "HEAD"]).stdout.strip()


def _write_reviewed(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows), encoding="utf-8")
    return path


def _load_records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestMaterialize(unittest.TestCase):
    def test_materialize_accepted_candidate_becomes_ready_item(self) -> None:
        with TemporaryDirectory() as workspace:
            tmp_root = Path(workspace)
            repo = _init_repo(tmp_root / "repo")
            commit_root = _commit_file(
                repo=repo,
                message="Add prod and test",
                files={
                    "src/core.py": "def value():\n    return 1\n",
                    "tests/test_core.py": "def test_value():\n    assert value() == 1\n",
                },
            )
            commit_prod = _commit_file(
                repo=repo,
                message="Fix behavior",
                files={
                    "src/core.py": "def value():\n    return 2\n",
                    "tests/test_core.py": "def test_value():\n    assert value() == 2\n",
                },
            )

            reviewed = _write_reviewed(
                tmp_root / "reviewed.jsonl",
                [
                    {
                        "id": "owner__repo-rg-a111",
                        "repo": "owner/repo",
                        "commit": commit_prod,
                        "state": "accepted",
                        "metadata": {"parent_count": 1},
                    }
                ],
            )
            summary = run_materialization(reviewed_path=reviewed, out_root=tmp_root / "out", repo_root=repo)

            self.assertEqual(summary["ready_count"], 1)
            self.assertEqual(summary["rejected_count"], 0)
            materialized = _load_records(tmp_root / "out" / "materialized.jsonl")
            item = materialized[0]
            self.assertEqual(item["candidate_id"], "owner__repo-rg-a111")
            self.assertEqual(item["base_commit"], commit_root)
            self.assertEqual(item["status"], "ready")
            self.assertIsNone(item["reason"])
            self.assertTrue(item["patch"])
            self.assertTrue(item["prod_patch"])
            self.assertTrue(item["test_patch"])
            self.assertIn("problem_statement", item)
            self.assertTrue(item["problem_statement"])
            self.assertEqual(item["metadata"]["materialization"]["base_commit"], commit_root)
            self.assertEqual(item["metadata"]["problem_statement_source"], "commit")

    def test_materialize_uses_issue_text_for_problem_statement(self) -> None:
        with TemporaryDirectory() as workspace:
            tmp_root = Path(workspace)
            repo = _init_repo(tmp_root / "repo")
            _commit_file(
                repo=repo,
                message="Base",
                files={
                    "src/core.py": "def value():\n    return 1\n",
                    "tests/test_core.py": "def test_value():\n    assert value() == 1\n",
                },
            )
            commit_prod = _commit_file(
                repo=repo,
                message="Fix issue",
                files={
                    "src/core.py": "def value():\n    return 2\n",
                    "tests/test_core.py": "def test_value():\n    assert value() == 2\n",
                },
            )

            reviewed = _write_reviewed(
                tmp_root / "reviewed.jsonl",
                [
                    {
                        "id": "owner__repo-rg-issue",
                        "repo": "owner/repo",
                        "commit": commit_prod,
                        "state": "accepted",
                        "metadata": {
                            "parent_count": 1,
                            "issue_title": "Regression in value",
                            "issue_body": "value() returns wrong result for odd inputs",
                            "issue_refs": ["1234"],
                        },
                    }
                ],
            )
            summary = run_materialization(reviewed_path=reviewed, out_root=tmp_root / "out", repo_root=repo)

            self.assertEqual(summary["ready_count"], 1)
            item = _load_records(tmp_root / "out" / "materialized.jsonl")[0]
            self.assertEqual(item["metadata"]["problem_statement_source"], "linked_issue")
            self.assertIn("Regression in value", item["problem_statement"])

    def test_materialize_rejects_non_single_parent_candidate(self) -> None:
        with TemporaryDirectory() as workspace:
            tmp_root = Path(workspace)
            repo = _init_repo(tmp_root / "repo")
            _ = _commit_file(
                repo=repo,
                message="Base commit",
                files={"src/core.py": "def value():\n    return 1\n"},
            )
            run_command(["git", "-C", str(repo), "checkout", "-b", "feature"])
            _ = _commit_file(
                repo=repo,
                message="Feature path",
                files={"src/feature.py": "def feature():\n    return 1\n"},
            )
            run_command(["git", "-C", str(repo), "checkout", "main"])
            _commit_file(
                repo=repo,
                message="Main path",
                files={"src/main.py": "def main_path():\n    return 1\n"},
            )
            run_command(["git", "-C", str(repo), "merge", "--no-ff", "feature", "-m", "Merge feature branch"])
            merge_commit = run_command(["git", "-C", str(repo), "rev-parse", "HEAD"]).stdout.strip()

            reviewed = _write_reviewed(
                tmp_root / "reviewed.jsonl",
                [
                    {
                        "id": "owner__repo-rg-b222",
                        "repo": "owner/repo",
                        "commit": merge_commit,
                        "state": "accepted",
                        "metadata": {"parent_count": 2},
                    }
                ],
            )

            summary = run_materialization(reviewed_path=reviewed, out_root=tmp_root / "out", repo_root=repo)
            self.assertEqual(summary["ready_count"], 0)
            self.assertEqual(summary["rejected_count"], 1)

            rejected = _load_records(tmp_root / "out" / "materialization_rejections.jsonl")
            self.assertEqual(rejected[0]["reason"], "non_single_parent")
            self.assertIn("expected 1", rejected[0]["metadata"]["reason"])

    def test_materialize_rejects_empty_test_patch(self) -> None:
        with TemporaryDirectory() as workspace:
            tmp_root = Path(workspace)
            repo = _init_repo(tmp_root / "repo")
            _ = _commit_file(
                repo=repo,
                message="Add prod",
                files={"src/core.py": "def value():\n    return 1\n"},
            )
            commit_prod = _commit_file(
                repo=repo,
                message="Refactor prod only",
                files={"src/core.py": "def value():\n    return 42\n"},
            )

            reviewed = _write_reviewed(
                tmp_root / "reviewed.jsonl",
                [
                    {
                        "id": "owner__repo-rg-c333",
                        "repo": "owner/repo",
                        "commit": commit_prod,
                        "state": "accepted",
                        "metadata": {"parent_count": 1},
                    }
                ],
            )
            summary = run_materialization(reviewed_path=reviewed, out_root=tmp_root / "out", repo_root=repo)
            self.assertEqual(summary["ready_count"], 0)
            self.assertEqual(summary["rejected_count"], 1)

            rejected = _load_records(tmp_root / "out" / "materialization_rejections.jsonl")
            self.assertEqual(rejected[0]["reason"], "empty_test_patch_after_split")

    def test_materialization_is_deterministic(self) -> None:
        with TemporaryDirectory() as workspace:
            tmp_root = Path(workspace)
            repo = _init_repo(tmp_root / "repo")
            _commit_file(
                repo=repo,
                message="Initial",
                files={
                    "src/core.py": "def value():\n    return 1\n",
                    "tests/test_core.py": "def test_value():\n    assert value() == 1\n",
                },
            )
            commit = _commit_file(
                repo=repo,
                message="Second",
                files={
                    "src/core.py": "def value():\n    return 2\n",
                    "tests/test_core.py": "def test_value():\n    assert value() == 2\n",
                },
            )

            reviewed = _write_reviewed(
                tmp_root / "reviewed.jsonl",
                [
                    {
                        "id": "owner__repo-rg-d444",
                        "repo": "owner/repo",
                        "commit": commit,
                        "state": "accepted",
                        "metadata": {"parent_count": 1},
                    }
                ],
            )

            first = run_materialization(reviewed_path=reviewed, out_root=tmp_root / "out_a", repo_root=repo)
            second = run_materialization(reviewed_path=reviewed, out_root=tmp_root / "out_b", repo_root=repo)
            self.assertEqual(first["ready_count"], second["ready_count"])
            self.assertEqual(
                _load_records(tmp_root / "out_a" / "materialized.jsonl"),
                _load_records(tmp_root / "out_b" / "materialized.jsonl"),
            )

    def test_materialize_missing_candidates_raises(self) -> None:
        with TemporaryDirectory() as workspace:
            tmp_root = Path(workspace)
            (tmp_root / "reviewed.jsonl").write_text("", encoding="utf-8")
            with self.assertRaises(MaterializationError):
                run_materialization(reviewed_path=tmp_root / "reviewed.jsonl", out_root=tmp_root / "out")


if __name__ == "__main__":
    unittest.main()
