import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from repogauge.cli import main
from repogauge.exec import run_command_checked
from repogauge.export import run_export, run_materialization


def _create_repo_with_commits(path: Path) -> tuple[str, str]:
    run_command_checked(["git", "init", "-b", "main"], cwd=str(path))
    run_command_checked(["git", "config", "user.name", "ci"], cwd=str(path))
    run_command_checked(["git", "config", "user.email", "ci@example.com"], cwd=str(path))

    (path / "src").mkdir()
    (path / "tests").mkdir()
    (path / "src" / "module.py").write_text("def add(a, b):\\n    return a + b\\n", encoding="utf-8")
    run_command_checked(["git", "add", "src/module.py"], cwd=str(path))
    run_command_checked(["git", "commit", "-m", "base"], cwd=str(path))
    base_commit = run_command_checked(["git", "rev-parse", "HEAD"], cwd=str(path)).stdout.strip()

    (path / "src" / "module.py").write_text("def add(a, b):\\n    return a + b\\n\\n", encoding="utf-8")
    (path / "tests" / "test_module.py").write_text("def test_add():\\n    assert add(1, 2) == 3\\n", encoding="utf-8")
    run_command_checked(["git", "add", "src/module.py", "tests/test_module.py"], cwd=str(path))
    run_command_checked(["git", "commit", "-m", "prod+tests"], cwd=str(path))
    prod_test_commit = run_command_checked(["git", "rev-parse", "HEAD"], cwd=str(path)).stdout.strip()

    (path / "src" / "module.py").write_text("def add(a, b):\\n    return a + b + 1\\n", encoding="utf-8")
    run_command_checked(["git", "add", "src/module.py"], cwd=str(path))
    run_command_checked(["git", "commit", "-m", "prod-only"], cwd=str(path))
    prod_only_commit = run_command_checked(["git", "rev-parse", "HEAD"], cwd=str(path)).stdout.strip()

    return base_commit, prod_test_commit, prod_only_commit


class TestMaterialize(unittest.TestCase):
    def test_run_materialization_splits_prod_and_test_diffs(self) -> None:
        with TemporaryDirectory() as workspace:
            root = Path(workspace) / "repo"
            root.mkdir()
            base_commit, prod_test_commit, prod_only_commit = _create_repo_with_commits(root)

            reviewed = Path(workspace) / "reviewed.jsonl"
            reviewed.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "id": "owner__repo-rg-11a",
                                "repo": "owner/repo",
                                "commit": prod_test_commit,
                                "state": "accepted",
                                "parent_commit": base_commit,
                            }
                        ),
                        json.dumps(
                            {
                                "id": "owner__repo-rg-22b",
                                "repo": "owner/repo",
                                "commit": prod_only_commit,
                                "state": "accepted",
                                "parent_commit": prod_test_commit,
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            summary = run_materialization(
                reviewed_path=reviewed,
                out_root=Path(workspace) / "out",
                repo_root=root,
            )
            ready_path = Path(summary["materialized_path"])
            rejected_path = Path(summary["rejected_path"])
            ready_rows = [json.loads(line) for line in ready_path.read_text(encoding="utf-8").splitlines()]
            rejected_rows = [json.loads(line) for line in rejected_path.read_text(encoding="utf-8").splitlines()]

            self.assertEqual(summary["ready_count"], 1)
            self.assertEqual(summary["rejected_count"], 1)
            self.assertEqual(summary["total_count"], 2)
            self.assertEqual(len(ready_rows), 1)
            self.assertEqual(len(rejected_rows), 1)
            self.assertEqual(ready_rows[0]["candidate_id"], "owner__repo-rg-11a")
            self.assertIn("diff --git", ready_rows[0]["patch"])
            self.assertIn("src/module.py", ready_rows[0]["prod_patch"])
            self.assertIn("tests/test_module.py", ready_rows[0]["test_patch"])
            self.assertEqual(rejected_rows[0]["reason"], "empty_test_patch_after_split")

    def test_run_materialization_rejects_missing_commit(self) -> None:
        with TemporaryDirectory() as workspace:
            root = Path(workspace)
            reviewed = root / "reviewed.jsonl"
            reviewed.write_text(
                json.dumps(
                    {
                        "id": "owner__repo-rg-miss",
                        "repo": "owner/repo",
                        "state": "accepted",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            (root / "repo").mkdir()
            run_command_checked(["git", "init", "-b", "main"], cwd=str(root / "repo"))
            run_command_checked(["git", "config", "user.name", "ci"], cwd=str(root / "repo"))
            run_command_checked(["git", "config", "user.email", "ci@example.com"], cwd=str(root / "repo"))

            summary = run_materialization(
                reviewed_path=reviewed,
                out_root=root / "out",
                repo_root=root / "repo",
            )
            rejected_rows = [json.loads(line) for line in Path(summary["rejected_path"]).read_text(encoding="utf-8").splitlines()]

            self.assertEqual(summary["ready_count"], 0)
            self.assertEqual(summary["rejected_count"], 1)
            self.assertEqual(rejected_rows[0]["reason"], "missing_commit")


class TestExportCommand(unittest.TestCase):
    def test_export_command_writes_materialization_and_dataset_artifacts(self) -> None:
        with TemporaryDirectory() as workspace:
            root = Path(workspace) / "repo"
            root.mkdir()
            _, prod_test_commit, _ = _create_repo_with_commits(root)

            reviewed = root / "reviewed.jsonl"
            reviewed.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "id": "owner__repo-rg-11a",
                                "repo": "owner/repo",
                                "commit": prod_test_commit,
                                "state": "accepted",
                                "parent_commit": "",
                            }
                        )
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            out_root = Path(workspace) / "export_out"
            exit_code = main(["export", str(reviewed), "--out", str(out_root)])
            manifest_path = out_root / "manifest.json"
            materialized_path = out_root / "materialized.jsonl"
            dataset_path = out_root / "dataset" / "dataset.jsonl"
            predictions_path = out_root / "dataset" / "predictions.gold.jsonl"

            self.assertEqual(exit_code, 0)
            self.assertTrue(manifest_path.exists())
            self.assertTrue(materialized_path.exists())
            self.assertTrue(dataset_path.exists())
            self.assertTrue(predictions_path.exists())

            materialized_rows = [json.loads(line) for line in materialized_path.read_text(encoding="utf-8").splitlines()]
            dataset_rows = [json.loads(line) for line in dataset_path.read_text(encoding="utf-8").splitlines()]
            prediction_rows = [json.loads(line) for line in predictions_path.read_text(encoding="utf-8").splitlines()]

            manifest = json.loads(manifest_path.read_text(encoding="utf-8").strip().splitlines()[-1])
            self.assertEqual(manifest["status"], "succeeded")
            self.assertEqual(manifest["step_statuses"]["execute"], "succeeded")
            self.assertEqual(manifest["artifact_paths"]["dataset"], str(dataset_path))
            self.assertEqual(manifest["artifact_paths"]["predictions"], str(predictions_path))
            self.assertEqual(manifest["metadata"]["export"]["dataset_count"], 1)
            self.assertEqual(manifest["metadata"]["export"]["prediction_count"], 1)
            self.assertEqual(len(materialized_rows), 1)
            self.assertEqual(len(dataset_rows), 1)
            self.assertEqual(len(prediction_rows), 1)
            self.assertEqual(dataset_rows[0]["instance_id"], materialized_rows[0]["candidate_id"])
            self.assertEqual(prediction_rows[0]["instance_id"], materialized_rows[0]["candidate_id"])
            self.assertEqual(prediction_rows[0]["model_patch"], materialized_rows[0]["patch"])
            self.assertEqual(prediction_rows[0]["model_name_or_path"], "gold")

    def test_run_export_converts_materialized_rows(self) -> None:
        with TemporaryDirectory() as workspace:
            out_root = Path(workspace) / "out"
            out_root.mkdir(parents=True)
            materialized = out_root / "materialized.jsonl"
            materialized.write_text(
                json.dumps(
                    {
                        "candidate_id": "owner__repo-rg-11a",
                        "repo": "owner/repo",
                        "base_commit": "base123",
                        "commit": "longcommit",
                        "problem_statement": "Problem details",
                        "patch": "diff --git a/x.py b/x.py\n+print('x')",
                        "test_patch": "diff --git a/tests/test_x.py b/tests/test_x.py\n+print('t')",
                        "metadata": {"FAIL_TO_PASS": ["t1"], "PASS_TO_PASS": ["t2"]},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            summary = run_export(materialized_path=materialized, out_root=out_root)
            dataset_rows = [json.loads(line) for line in Path(summary["dataset_path"]).read_text(encoding="utf-8").splitlines()]
            prediction_rows = [
                json.loads(line) for line in Path(summary["predictions_path"]).read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(summary["dataset_count"], 1)
            self.assertEqual(summary["prediction_count"], 1)
            self.assertEqual(dataset_rows[0]["instance_id"], "owner__repo-rg-11a")
            self.assertEqual(dataset_rows[0]["repo"], "owner/repo")
            self.assertEqual(dataset_rows[0]["version"], "0.0.0")
            self.assertEqual(dataset_rows[0]["FAIL_TO_PASS"], ["t1"])
            self.assertEqual(prediction_rows[0]["model_patch"], dataset_rows[0]["patch"])
            self.assertEqual(prediction_rows[0]["model_name_or_path"], "gold")
