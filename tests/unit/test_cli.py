import unittest

import json
import tempfile
from pathlib import Path

from repogauge.cli import _build_parser
from repogauge.cli import main


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


if __name__ == "__main__":
    unittest.main()
