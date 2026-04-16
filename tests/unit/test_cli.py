import unittest

import json
from unittest.mock import patch
import tempfile
from pathlib import Path
from repogauge.runner.judge import HarnessRunSummary

from repogauge.cli import _build_parser
from repogauge.cli import main
from repogauge.exec import run_command


class TestCliSurface(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = _build_parser()

    def test_commands_are_registered(self) -> None:
        for cmd in (
            "mine",
            "review",
            "export",
            "eval",
            "run",
            "analyze",
            "train-router",
        ):
            namespace = self.parser.parse_args([cmd, "./input"])
            self.assertEqual(namespace.command, cmd)

    def test_stable_flags_exist(self) -> None:
        namespace = self.parser.parse_args(
            [
                "mine",
                "./repo",
                "--config",
                "cfg.json",
                "--out",
                "./out",
                "--dry-run",
                "--resume",
                "--llm-mode",
                "off",
                "--verbose",
            ]
        )
        self.assertEqual(namespace.config, "cfg.json")
        self.assertEqual(namespace.out, "./out")
        self.assertTrue(namespace.dry_run)
        self.assertTrue(namespace.resume)
        self.assertTrue(namespace.verbose)
        self.assertEqual(namespace.llm_mode, "off")
        namespace = self.parser.parse_args(
            [
                "mine",
                "./repo",
                "--enrich-github",
                "--github-token",
                "ghp_test",
                "--github-enrichment-cache",
                "./cache/github.json",
                "--commit-range",
                "HEAD~5..HEAD",
                "--max-commits",
                "10",
                "--exclude-merges",
            ]
        )
        self.assertEqual(namespace.commit_range, "HEAD~5..HEAD")
        self.assertEqual(namespace.max_commits, 10)
        self.assertTrue(namespace.exclude_merges)
        self.assertTrue(namespace.enrich_github)
        self.assertEqual(namespace.github_token, "ghp_test")
        self.assertEqual(namespace.github_enrichment_cache, "./cache/github.json")
        namespace = self.parser.parse_args(
            ["review", "./candidates.jsonl", "--decisions", "./decisions.jsonl"]
        )
        self.assertEqual(namespace.decisions, "./decisions.jsonl")
        namespace = self.parser.parse_args(
            [
                "review",
                "./candidates.jsonl",
                "--triage-hints",
                "./triage.jsonl",
                "--llm-model",
                "local-unit",
                "--llm-provider",
                "local",
            ]
        )
        self.assertEqual(namespace.triage_hints, "./triage.jsonl")
        self.assertEqual(namespace.llm_model, "local-unit")
        self.assertEqual(namespace.llm_provider, "local")

    def test_command_emits_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            out = Path(workspace) / "mine_out"
            result = main(["mine", "./repo", "--out", str(out)])
            self.assertEqual(result, 0)
            manifest_path = out / "manifest.json"
            events_path = out / "events.jsonl"
            self.assertTrue(manifest_path.exists())
            self.assertTrue(events_path.exists())

            payload = json.loads(
                manifest_path.read_text(encoding="utf-8").strip().splitlines()[-1]
            )
            self.assertEqual(payload["command"], "mine")
            self.assertEqual(payload["status"], "succeeded")
            self.assertEqual(payload["step_statuses"]["execute"], "succeeded")
            self.assertEqual(payload["step_statuses"]["finish"], "succeeded")

    def test_resume_skips_execution_when_manifest_matches(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            out = Path(workspace) / "mine_out"
            first = main(["mine", "./repo", "--out", str(out)])
            self.assertEqual(first, 0)
            second = main(["mine", "./repo", "--out", str(out), "--resume"])
            self.assertEqual(second, 0)

            manifest_path = out / "manifest.json"
            payload = json.loads(
                manifest_path.read_text(encoding="utf-8").strip().splitlines()[-1]
            )
            self.assertEqual(payload["status"], "succeeded")
            self.assertIn("resume", payload["step_statuses"])

    def test_mine_writes_repo_profile(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            repo = Path(workspace) / "repo"
            repo.mkdir()
            run_command(["git", "init", "-b", "main"], cwd=str(repo))
            run_command(["git", "config", "user.name", "ci"], cwd=str(repo))
            run_command(
                ["git", "config", "user.email", "ci@example.com"], cwd=str(repo)
            )
            run_command(
                ["git", "remote", "add", "origin", "git@github.com:example/demo.git"],
                cwd=str(repo),
            )
            (repo / "pyproject.toml").write_text(
                "[tool.poetry]\\nname = 'demo'\\nversion='0.1'\\n", encoding="utf-8"
            )
            (repo / "pytest.ini").write_text("[pytest]\\n", encoding="utf-8")

            out = Path(workspace) / "mine_out"
            result = main(["mine", str(repo), "--out", str(out)])
            self.assertEqual(result, 0)

            profile_path = out / "repo_profile.json"
            payload = json.loads(profile_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["repo_name"], "example/demo")
            self.assertEqual(payload["python_hints"]["package_style"], "unknown")
            self.assertIn("commands", payload["test_runner_hints"])
            scan_path = out / "scan.jsonl"
            self.assertTrue(scan_path.exists())
            candidates_path = out / "candidates.jsonl"
            self.assertTrue(candidates_path.exists())
            scan_payloads = [
                json.loads(line)
                for line in scan_path.read_text(encoding="utf-8").splitlines()
            ]
            candidates_payloads = [
                json.loads(line)
                for line in candidates_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(scan_payloads), 0)
            self.assertEqual(len(candidates_payloads), 0)

    def test_mine_forwards_github_enrichment_options_to_scan(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            repo = Path(workspace) / "repo"
            repo.mkdir()
            run_command(["git", "init", "-b", "main"], cwd=str(repo))
            run_command(["git", "config", "user.name", "ci"], cwd=str(repo))
            run_command(
                ["git", "config", "user.email", "ci@example.com"], cwd=str(repo)
            )
            run_command(
                ["git", "remote", "add", "origin", "git@github.com:example/demo.git"],
                cwd=str(repo),
            )
            out = Path(workspace) / "mine_out"
            cache_path = out / "custom_github_cache.json"
            with patch("repogauge.cli.scan_repository") as mock_scan:
                mock_scan.return_value = []
                result = main(
                    [
                        "mine",
                        str(repo),
                        "--out",
                        str(out),
                        "--enrich-github",
                        "--github-token",
                        "ghp_token_for_tests",
                        "--github-enrichment-cache",
                        str(cache_path),
                        "--max-commits",
                        "3",
                    ]
                )
            self.assertEqual(result, 0)
            mock_scan.assert_called_once()
            scan_kwargs = mock_scan.call_args.kwargs
            self.assertTrue(scan_kwargs["enrich_github"])
            self.assertEqual(scan_kwargs["enrichment_cache_path"], cache_path)
            self.assertEqual(scan_kwargs["github_token"], "ghp_token_for_tests")

    def test_mine_artifact_contract_is_recorded_in_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            repo = Path(workspace) / "repo"
            repo.mkdir()
            run_command(["git", "init", "-b", "main"], cwd=str(repo))
            run_command(["git", "config", "user.name", "ci"], cwd=str(repo))
            run_command(
                ["git", "config", "user.email", "ci@example.com"], cwd=str(repo)
            )
            (repo / "pyproject.toml").write_text(
                "[project]\nname = 'demo'\n", encoding="utf-8"
            )

            out = Path(workspace) / "mine_out"
            result = main(["mine", str(repo), "--out", str(out)])
            self.assertEqual(result, 0)

            manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            paths = manifest["artifact_paths"]

            self.assertEqual(paths["manifest"], str(out / "manifest.json"))
            self.assertEqual(paths["events"], str(out / "events.jsonl"))
            self.assertEqual(paths["repo_profile"], str(out / "repo_profile.json"))
            self.assertEqual(paths["scan"], str(out / "scan.jsonl"))
            self.assertEqual(paths["candidates"], str(out / "candidates.jsonl"))

            for key in ("manifest", "events", "repo_profile", "scan", "candidates"):
                self.assertTrue(
                    Path(paths[key]).exists(), f"missing artifact for {key}"
                )

    def test_eval_with_missing_gold_file_allows_generation(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            dataset_path = Path(workspace) / "dataset.jsonl"
            dataset_path.write_text(
                json.dumps(
                    {
                        "instance_id": "repo__sample-1",
                        "patch": "diff --git a/x b/x\n+print('ok')",
                        "repo": "repo",
                        "version": "1",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            out_root = Path(workspace) / "out"
            with patch("repogauge.runner.judge.run_harness_evaluation") as mock_eval:
                mock_eval.return_value = HarnessRunSummary(
                    validation_path=str(out_root / "validation.jsonl"),
                    total=1,
                    resolved=1,
                    not_resolved=0,
                    error=0,
                    skipped=0,
                    resolve_rate=1.0,
                    harness_output="official_swebench",
                )
                result = main(
                    ["eval", str(dataset_path), "--gold", "--out", str(out_root)]
                )

            self.assertEqual(result, 0)
            mock_eval.assert_called_once()
            self.assertTrue(
                mock_eval.call_args.kwargs["gold_if_missing"],
                "gold flag should enable missing-predictions generation",
            )

    def test_eval_requires_predictions_or_gold(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            dataset_path = Path(workspace) / "dataset.jsonl"
            dataset_path.write_text(
                json.dumps(
                    {
                        "instance_id": "repo__sample-1",
                        "patch": "diff --git a/x b/x\n+print('ok')",
                        "repo": "repo",
                        "version": "1",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            out_root = Path(workspace) / "out"
            result = main(["eval", str(dataset_path), "--out", str(out_root)])
            self.assertEqual(result, 1)

    def test_eval_with_predictions_calls_harness_runner(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            dataset_path = Path(workspace) / "dataset.jsonl"
            dataset_path.write_text(
                json.dumps(
                    {
                        "instance_id": "repo__sample-1",
                        "patch": "diff --git a/x b/x\n+print('ok')",
                        "repo": "repo",
                        "version": "1",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            predictions_path = Path(workspace) / "predictions.jsonl"
            predictions_path.write_text(
                json.dumps(
                    {
                        "instance_id": "repo__sample-1",
                        "model_name_or_path": "agent",
                        "model_patch": "diff",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            out_root = Path(workspace) / "out"
            with patch("repogauge.runner.judge.run_harness_evaluation") as mock_eval:
                mock_eval.return_value = HarnessRunSummary(
                    validation_path=str(out_root / "validation.jsonl"),
                    total=1,
                    resolved=1,
                    not_resolved=0,
                    error=0,
                    skipped=0,
                    resolve_rate=1.0,
                    harness_output="official_swebench",
                )
                result = main(
                    [
                        "eval",
                        str(dataset_path),
                        "--predictions",
                        str(predictions_path),
                        "--out",
                        str(out_root),
                    ]
                )

            self.assertEqual(result, 0)
            mock_eval.assert_called_once()
            self.assertFalse(
                mock_eval.call_args.kwargs["gold_if_missing"],
                "explicit predictions should disable gold generation",
            )

    def test_eval_manifest_records_batched_artifact_paths(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            dataset_path = Path(workspace) / "dataset.jsonl"
            dataset_path.write_text(
                json.dumps(
                    {
                        "instance_id": "repo__sample-1",
                        "patch": "diff --git a/x b/x\n+print('ok')",
                        "repo": "repo",
                        "version": "1",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            predictions_path = Path(workspace) / "predictions.jsonl"
            predictions_path.write_text(
                json.dumps(
                    {
                        "instance_id": "repo__sample-1",
                        "model_name_or_path": "agent",
                        "model_patch": "diff",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            out_root = Path(workspace) / "out"
            results_path = out_root / "results.json"
            instance_results_path = out_root / "instance_results.jsonl"
            with patch("repogauge.runner.judge.run_harness_evaluation") as mock_eval:
                mock_eval.return_value = HarnessRunSummary(
                    validation_path=str(out_root / "validation.jsonl"),
                    total=1,
                    resolved=1,
                    not_resolved=0,
                    error=0,
                    skipped=0,
                    resolve_rate=1.0,
                    harness_output="official_swebench",
                    results_path=str(results_path),
                    instance_results_path=str(instance_results_path),
                )
                result = main(
                    [
                        "eval",
                        str(dataset_path),
                        "--predictions",
                        str(predictions_path),
                        "--out",
                        str(out_root),
                    ]
                )

            self.assertEqual(result, 0)
            manifest = json.loads(
                (out_root / "manifest.json").read_text(encoding="utf-8")
            )
            paths = manifest["artifact_paths"]
            self.assertEqual(paths["results"], str(results_path))
            self.assertEqual(paths["instance_results"], str(instance_results_path))

    def test_run_command_builds_run_root_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            dataset_root = root / "artifact"
            dataset_root.mkdir()
            dataset_path = dataset_root / "dataset.jsonl"
            dataset_path.write_text(
                json.dumps(
                    {
                        "instance_id": "repo__sample-1",
                        "repo": "repo",
                        "base_commit": "abc",
                        "problem_statement": "fix foo",
                        "version": "1",
                        "patch": "diff --git a/x b/x\n+print('ok')",
                        "test_patch": "",
                        "FAIL_TO_PASS": [],
                        "PASS_TO_PASS": [],
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "instance_id": "repo__sample-2",
                        "repo": "repo",
                        "base_commit": "abc",
                        "problem_statement": "fix bar",
                        "version": "1",
                        "patch": "diff --git a/y b/y\n+print('ok')",
                        "test_patch": "",
                        "FAIL_TO_PASS": [],
                        "PASS_TO_PASS": [],
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            matrix_path = root / "matrix.yaml"
            matrix_path.write_text(
                """
run_id: unit-run
dataset:
  path: artifact/dataset.jsonl
providers:
  mock:
    kind: local
execution:
  repeats: 2
  seed: 7
  shuffle: false
solvers:
  - id: solver-a
    provider: mock
    prompt_policy:
      template: concise
    tool_policy:
      safe: true
""".strip()
                + "\n",
                encoding="utf-8",
            )

            out = root / "out"
            result = main(["run", str(matrix_path), "--out", str(out)])
            self.assertEqual(result, 0)

            run_root = out / "unit-run"
            matrix_copy = run_root / "matrix.yaml"
            jobs_path = run_root / "jobs.jsonl"
            run_manifest_path = run_root / "manifest.json"

            self.assertTrue(matrix_copy.exists())
            self.assertTrue(jobs_path.exists())
            self.assertTrue(run_manifest_path.exists())
            self.assertTrue((run_root / "run_jobs.jsonl").exists())
            self.assertTrue((run_root / "attempts.jsonl").exists())
            self.assertTrue((run_root / "attempts.parquet").exists())
            self.assertTrue((run_root / "run_summary.json").exists())

            rows = [
                json.loads(line)
                for line in jobs_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(rows), 4)
            self.assertEqual(rows[0]["run_id"], "unit-run")

            run_manifest = json.loads(run_manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(run_manifest["command"], "run")
            self.assertEqual(run_manifest["run_id"], "unit-run")
            self.assertEqual(run_manifest["job_count"], 4)
            self.assertEqual(run_manifest["run_root"], str(run_root))
            self.assertEqual(run_manifest["dataset_path"], str(dataset_path.resolve()))
            self.assertEqual(run_manifest["solver_count"], 1)
            self.assertEqual(run_manifest["provider_count"], 1)
            self.assertEqual(len(run_manifest["providers"]), 1)
            self.assertEqual(run_manifest["providers"][0]["provider_id"], "mock")
            self.assertEqual(len(run_manifest["solvers"]), 1)
            self.assertEqual(run_manifest["solvers"][0]["solver_id"], "solver-a")

            manifest_payload = json.loads(
                (out / "manifest.json").read_text(encoding="utf-8")
            )
            artifact_paths = manifest_payload["artifact_paths"]
            self.assertEqual(
                artifact_paths["attempts_parquet"],
                str(run_root / "attempts.parquet"),
            )

            run_summary = json.loads(
                (run_root / "run_summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(run_summary["job_count"], 4)
            self.assertEqual(run_summary["solved"], 4)

            run_manifest_payload = json.loads(
                (out / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                run_manifest_payload["step_statuses"]["execute"], "succeeded"
            )
            self.assertEqual(
                run_manifest_payload["step_statuses"]["inspect"], "succeeded"
            )

            attempt_rows = [
                json.loads(line)
                for line in (run_root / "attempts.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            self.assertEqual(len(attempt_rows), 4)
            self.assertTrue(
                all(row["attempt_state"] == "succeeded" for row in attempt_rows)
            )

    def test_run_rejects_unknown_solver_provider(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            dataset_path = root / "dataset.jsonl"
            dataset_path.write_text(
                json.dumps(
                    {
                        "instance_id": "repo__sample-1",
                        "repo": "repo",
                        "base_commit": "abc",
                        "problem_statement": "fix foo",
                        "version": "1",
                        "patch": "diff --git a/x b/x\n+print('ok')",
                        "test_patch": "",
                        "FAIL_TO_PASS": [],
                        "PASS_TO_PASS": [],
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            matrix_path = root / "matrix.yaml"
            matrix_path.write_text(
                """
dataset:
  path: dataset.jsonl
providers:
  mock:
    kind: local
solvers:
  - id: solver-a
    provider: missing
""".strip()
                + "\n",
                encoding="utf-8",
            )

            out = root / "out"
            result = main(["run", str(matrix_path), "--out", str(out)])
            self.assertEqual(result, 1)
            self.assertFalse((out / "matrix").exists())

    def test_analyze_generates_reports_and_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            run_root = Path(workspace) / "unit-run"
            run_root.mkdir()
            (run_root / "attempts.jsonl").write_text(
                "".join(
                    json.dumps(
                        {
                            "run_id": "unit-run",
                            "solver_id": "solver-a",
                            "instance_id": "inst-1",
                            "duration_ms": 100,
                            "cost": {"total_cost": 2.0},
                            "attempt_state": "succeeded",
                        }
                    )
                    + "\n"
                    + json.dumps(
                        {
                            "run_id": "unit-run",
                            "solver_id": "solver-a",
                            "instance_id": "inst-2",
                            "duration_ms": 120,
                            "cost": {"total_cost": 6.0},
                            "attempt_state": "succeeded",
                        }
                    )
                    + "\n"
                ),
                encoding="utf-8",
            )
            (run_root / "validation.jsonl").write_text(
                "".join(
                    json.dumps(
                        {
                            "instance_id": "inst-1",
                            "solver_id": "solver-a",
                            "status": "resolved",
                            "resolved": True,
                            "harness_outcome": "resolved",
                        }
                    )
                    + "\n"
                    + json.dumps(
                        {
                            "instance_id": "inst-2",
                            "solver_id": "solver-a",
                            "status": "not_resolved",
                            "resolved": False,
                            "harness_outcome": "not_resolved",
                        }
                    )
                    + "\n"
                ),
                encoding="utf-8",
            )

            result = main(["analyze", str(run_root)])
            self.assertEqual(result, 0)

            manifest_path = run_root / "analyze" / "manifest.json"
            self.assertTrue(manifest_path.exists())
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            paths = manifest["artifact_paths"]
            self.assertTrue(Path(paths["analyze_summary"]).exists())
            self.assertTrue(Path(paths["analyze_report_csv"]).exists())
            self.assertTrue(Path(paths["analyze_report_parquet"]).exists())
            self.assertTrue(Path(paths["analyze_report_html"]).exists())
            self.assertEqual(manifest["step_statuses"]["execute"], "succeeded")

            summary = json.loads(
                Path(paths["analyze_summary"]).read_text(encoding="utf-8")
            )
            self.assertEqual(summary["metadata"]["attempt_rows"], 2)
            self.assertEqual(summary["metadata"]["instance_result_rows"], 2)
            self.assertEqual(len(summary["summary"]), 1)

    def test_analyze_fails_when_attempts_missing(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            run_root = Path(workspace) / "empty-run"
            run_root.mkdir()
            (run_root / "validation.jsonl").write_text(
                "".join(
                    json.dumps(
                        {
                            "instance_id": "inst-1",
                            "solver_id": "solver-a",
                            "status": "resolved",
                            "resolved": True,
                            "harness_outcome": "resolved",
                        }
                    )
                    + "\n"
                ),
                encoding="utf-8",
            )

            result = main(["analyze", str(run_root)])
            self.assertEqual(result, 1)

            manifest = json.loads(
                (run_root / "analyze" / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["step_statuses"]["inspect"], "failed")
            self.assertEqual(manifest["step_statuses"]["execute"], "skipped")


if __name__ == "__main__":
    unittest.main()
