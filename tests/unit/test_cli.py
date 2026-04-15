import unittest

import json
import tempfile
from pathlib import Path

from repogauge.cli import _build_parser
from repogauge.cli import main
from repogauge.exec import run_command


class TestCliSurface(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = _build_parser()

    def test_commands_are_registered(self) -> None:
        for cmd in ("mine", "review", "export", "eval", "run", "analyze", "train-router"):
            namespace = self.parser.parse_args([cmd, "./input"])
            self.assertEqual(namespace.command, cmd)

    def test_stable_flags_exist(self) -> None:
        namespace = self.parser.parse_args(["mine", "./repo", "--config", "cfg.json", "--out", "./out", "--dry-run", "--resume", "--llm-mode", "off", "--verbose"])
        self.assertEqual(namespace.config, "cfg.json")
        self.assertEqual(namespace.out, "./out")
        self.assertTrue(namespace.dry_run)
        self.assertTrue(namespace.resume)
        self.assertTrue(namespace.verbose)
        self.assertEqual(namespace.llm_mode, "off")
        namespace = self.parser.parse_args(["mine", "./repo", "--commit-range", "HEAD~5..HEAD", "--max-commits", "10", "--exclude-merges"])
        self.assertEqual(namespace.commit_range, "HEAD~5..HEAD")
        self.assertEqual(namespace.max_commits, 10)
        self.assertTrue(namespace.exclude_merges)
        namespace = self.parser.parse_args(["review", "./candidates.jsonl", "--decisions", "./decisions.jsonl"])
        self.assertEqual(namespace.decisions, "./decisions.jsonl")
        namespace = self.parser.parse_args(["review", "./candidates.jsonl", "--triage-hints", "./triage.jsonl", "--llm-model", "local-unit", "--llm-provider", "local"])
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

            payload = json.loads(manifest_path.read_text(encoding="utf-8").strip().splitlines()[-1])
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
            payload = json.loads(manifest_path.read_text(encoding="utf-8").strip().splitlines()[-1])
            self.assertEqual(payload["status"], "succeeded")
            self.assertIn("resume", payload["step_statuses"])

    def test_mine_writes_repo_profile(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            repo = Path(workspace) / "repo"
            repo.mkdir()
            run_command(["git", "init", "-b", "main"], cwd=str(repo))
            run_command(["git", "config", "user.name", "ci"], cwd=str(repo))
            run_command(["git", "config", "user.email", "ci@example.com"], cwd=str(repo))
            run_command(["git", "remote", "add", "origin", "git@github.com:example/demo.git"], cwd=str(repo))
            (repo / "pyproject.toml").write_text("[tool.poetry]\\nname = 'demo'\\nversion='0.1'\\n", encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
