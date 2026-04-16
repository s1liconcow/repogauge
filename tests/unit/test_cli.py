import unittest

import json
from unittest.mock import patch
import tempfile
from pathlib import Path
import yaml
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
        namespace = self.parser.parse_args(
            [
                "train-router",
                "./router_train.parquet",
                "--seed",
                "11",
                "--train-fraction",
                "0.7",
                "--validation-fraction",
                "0.2",
                "--max-depth",
                "4",
            ]
        )
        self.assertEqual(namespace.seed, 11)
        self.assertEqual(namespace.train_fraction, 0.7)
        self.assertEqual(namespace.validation_fraction, 0.2)
        self.assertEqual(namespace.max_depth, 4)

    def test_llm_mode_help_notes_review_only_behavior(self) -> None:
        subparsers_action = next(
            action
            for action in self.parser._actions
            if getattr(action, "choices", None)
        )
        help_text = subparsers_action.choices["run"].format_help()
        self.assertIn("Currently only affects", help_text)
        self.assertIn("review command.", help_text)

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

    def test_eval_prefers_dataset_directory_files_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            dataset_dir = Path(workspace) / "dataset"
            dataset_dir.mkdir()
            dataset_path = dataset_dir / "dataset.jsonl"
            predictions_path = dataset_dir / "predictions.gold.jsonl"
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
            predictions_path.write_text(
                json.dumps(
                    {
                        "instance_id": "repo__sample-1",
                        "model_name_or_path": "gold",
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
                    ["eval", str(dataset_dir), "--gold", "--out", str(out_root)]
                )

            self.assertEqual(result, 0)
            mock_eval.assert_called_once()
            self.assertEqual(
                Path(mock_eval.call_args.kwargs["dataset_path"]),
                dataset_path,
            )
            self.assertEqual(
                Path(mock_eval.call_args.kwargs["predictions_path"]),
                predictions_path,
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
            self.assertEqual(mock_eval.call_args.kwargs["workers"], 4)
            judge_config = mock_eval.call_args.kwargs["judge_config"]
            self.assertEqual(judge_config.batch_size, 32)
            self.assertEqual(judge_config.max_parallel_batches, 1)
            self.assertEqual(judge_config.workers_per_batch, 1)
            self.assertEqual(mock_eval.call_args.kwargs["container_runtime"], "podman")
            self.assertIsNone(mock_eval.call_args.kwargs["container_host"])

    def test_eval_parallelism_flags_override_defaults(self) -> None:
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
                        "--workers",
                        "6",
                        "--batch-size",
                        "8",
                        "--max-parallel-batches",
                        "3",
                        "--workers-per-batch",
                        "2",
                    ]
                )

            self.assertEqual(result, 0)
            mock_eval.assert_called_once()
            self.assertEqual(mock_eval.call_args.kwargs["workers"], 6)
            judge_config = mock_eval.call_args.kwargs["judge_config"]
            self.assertEqual(judge_config.batch_size, 8)
            self.assertEqual(judge_config.max_parallel_batches, 3)
            self.assertEqual(judge_config.workers_per_batch, 2)
            self.assertEqual(mock_eval.call_args.kwargs["container_runtime"], "podman")
            self.assertIsNone(mock_eval.call_args.kwargs["container_host"])

    def test_eval_container_runtime_flags_override_defaults(self) -> None:
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
                        "--container-runtime",
                        "podman",
                        "--container-host",
                        "unix:///tmp/podman.sock",
                    ]
                )

            self.assertEqual(result, 0)
            mock_eval.assert_called_once()
            self.assertEqual(mock_eval.call_args.kwargs["container_runtime"], "podman")
            self.assertEqual(
                mock_eval.call_args.kwargs["container_host"],
                "unix:///tmp/podman.sock",
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
    api_key: super-secret
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
            self.assertEqual(
                run_manifest["providers"][0]["config"]["api_key"], "<redacted>"
            )
            self.assertEqual(len(run_manifest["solvers"]), 1)
            self.assertEqual(run_manifest["solvers"][0]["solver_id"], "solver-a")

            matrix_snapshot = yaml.safe_load(matrix_copy.read_text(encoding="utf-8"))
            self.assertEqual(
                matrix_snapshot["providers"]["mock"]["api_key"], "<redacted>"
            )
            self.assertNotIn("super-secret", matrix_copy.read_text(encoding="utf-8"))

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
            self.assertTrue(Path(paths["router_train"]).exists())
            self.assertEqual(manifest["step_statuses"]["execute"], "succeeded")

            summary = json.loads(
                Path(paths["analyze_summary"]).read_text(encoding="utf-8")
            )
            self.assertEqual(summary["metadata"]["attempt_rows"], 2)
            self.assertEqual(summary["metadata"]["instance_result_rows"], 2)
            self.assertEqual(len(summary["summary"]), 1)
            self.assertEqual(summary["metadata"]["router_training_rows"], 2)

    def test_train_router_writes_report_from_router_training_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            run_root = Path(workspace) / "unit-run"
            run_root.mkdir()
            router_train_path = run_root / "router_train.parquet"
            from repogauge.runner.router import (
                build_router_training_rows,
                write_router_training_rows,
            )

            attempts = [
                {
                    "attempt_id": "a-1",
                    "attempt_index": 1,
                    "instance_id": "inst-1",
                    "solver_id": "solver-cheap",
                    "duration_ms": 10,
                    "cost": {"total_cost": 1.0},
                    "attempt_state": "succeeded",
                    "resolved": True,
                    "harness_outcome": "resolved",
                    "repo": "owner/repo",
                    "base_commit": "abc123",
                    "version": "1.0.0",
                    "problem_statement": "Fix inst-1",
                    "task_feature_version": "task-features-v1",
                    "task_feature_hash": "hash-inst-1",
                    "task_cluster": "len=short|signal=neutral|version=semantic",
                    "task_features": {"repo": "owner/repo"},
                    "prompt_policy_hash": "prompt-cheap",
                    "tool_policy_hash": "tool-cheap",
                    "solver_config_hash": "config-cheap",
                },
                {
                    "attempt_id": "b-1",
                    "attempt_index": 1,
                    "instance_id": "inst-1",
                    "solver_id": "solver-expensive",
                    "duration_ms": 12,
                    "cost": {"total_cost": 12.0},
                    "attempt_state": "succeeded",
                    "resolved": True,
                    "harness_outcome": "resolved",
                    "repo": "owner/repo",
                    "base_commit": "abc123",
                    "version": "1.0.0",
                    "problem_statement": "Fix inst-1",
                    "task_feature_version": "task-features-v1",
                    "task_feature_hash": "hash-inst-1",
                    "task_cluster": "len=short|signal=neutral|version=semantic",
                    "task_features": {"repo": "owner/repo"},
                    "prompt_policy_hash": "prompt-expensive",
                    "tool_policy_hash": "tool-expensive",
                    "solver_config_hash": "config-expensive",
                },
            ]
            instance_results = [
                {
                    "instance_id": "inst-1",
                    "solver_id": "solver-cheap",
                    "harness_outcome": "resolved",
                    "resolved": True,
                },
                {
                    "instance_id": "inst-1",
                    "solver_id": "solver-expensive",
                    "harness_outcome": "resolved",
                    "resolved": True,
                },
            ]
            write_router_training_rows(
                router_train_path,
                build_router_training_rows(attempts, instance_results),
            )

            report_out = Path(workspace) / "router_report_out"
            result = main(
                [
                    "train-router",
                    str(router_train_path),
                    "--out",
                    str(report_out),
                ]
            )
            self.assertEqual(result, 0)
            report_path = report_out / "router_report.json"
            self.assertTrue(report_path.exists())
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["instance_count"], 1)
            self.assertEqual(report["cheap_solver_id"], "solver-cheap")
            self.assertEqual(len(report["policies"]), 4)
            self.assertEqual(report["policies"][0]["policy"], "always_cheap")
            self.assertIn("learned_router", report)
            self.assertEqual(report["learned_router"]["policy"], "learned_router")
            self.assertIn("model", report)
            self.assertEqual(report["model"]["model_version"], "router-model-v1")
            self.assertTrue((report_out / "router_model.json").exists())

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
