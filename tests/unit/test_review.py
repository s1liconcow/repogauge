import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from repogauge.cli import main
from repogauge.config import ScanRow


def _write_candidates(path: Path) -> list[str]:
    rows = [
        ScanRow(
            id="owner__repo-rg-a111",
            repo="owner/repo",
            commit="a1111111111111111111111111111111111111111",
            parent_commit="0000000000000000000000000000000000000000",
            diff="diff --git a/src/x.py b/src/x.py\n@@ -1 +1\n+print('x')\n",
            files_touched=["src/x.py", "tests/test_x.py"],
            changed_lines=10,
            heuristic_score=9.2,
            state="shortlist",
            metadata={"decision_band": "shortlist", "score_breakdown": [{"component": "tests", "points": 4.2}]},
        ).to_dict(),
        ScanRow(
            id="owner__repo-rg-b222",
            repo="owner/repo",
            commit="b2222222222222222222222222222222222222",
            parent_commit="a1111111111111111111111111111111111111111",
            diff="diff --git a/src/y.py b/src/y.py\n@@ -1 +1\n+print('y')\n",
            files_touched=["src/y.py"],
            changed_lines=8,
            heuristic_score=4.0,
            state="discovered",
            metadata={"decision_band": "review", "score_breakdown": [{"component": "low_confidence", "points": 2.2}]},
        ).to_dict(),
    ]
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    return ["owner__repo-rg-a111", "owner__repo-rg-b222"]


def _write_triage_hints(path: Path, candidate_id: str, payload: dict) -> None:
    triage_payload = {
        "model": {
            "model_name": "advisor-unit-1",
            "provider": "local",
            "prompt_version": "review/v1",
        },
        "candidates": [
            {
                "candidate_id": candidate_id,
                **payload,
            }
        ],
    }
    path.write_text(json.dumps(triage_payload), encoding="utf-8")


class TestReviewCommand(unittest.TestCase):
    def test_review_command_generates_artifacts_and_defaults(self) -> None:
        with TemporaryDirectory() as workspace:
            root = Path(workspace)
            candidates_path = root / "candidates.jsonl"
            candidate_ids = _write_candidates(candidates_path)

            exit_code = main(["review", str(candidates_path)])
            self.assertEqual(exit_code, 0)
            reviewed = root / "reviewed.jsonl"
            self.assertTrue(reviewed.exists())
            markdown = root / "review.md"
            html = root / "review.html"
            self.assertTrue(markdown.exists())
            self.assertTrue(html.exists())

            reviewed_rows = [json.loads(line) for line in reviewed.read_text(encoding="utf-8").splitlines() if line]
            self.assertEqual(len(reviewed_rows), 2)
            by_id = {row["candidate_id"]: row for row in reviewed_rows}
            self.assertEqual(by_id[candidate_ids[0]]["state"], "accepted")
            self.assertEqual(by_id[candidate_ids[1]]["state"], "rejected")

    def test_review_command_respects_scripted_decisions(self) -> None:
        with TemporaryDirectory() as workspace:
            root = Path(workspace)
            candidates_path = root / "candidates.jsonl"
            _write_candidates(candidates_path)

            decisions_path = root / "decisions.jsonl"
            decisions_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "id": "owner__repo-rg-b222",
                                "state": "accepted",
                                "reason": "manual include",
                                "reviewer_notes": "Keep for export",
                                "force_include": True,
                            }
                        )
                    ]
                ),
                encoding="utf-8",
            )

            exit_code = main(["review", str(candidates_path), "--decisions", str(decisions_path)])
            self.assertEqual(exit_code, 0)

            reviewed = root / "reviewed.jsonl"
            reviewed_rows = [json.loads(line) for line in reviewed.read_text(encoding="utf-8").splitlines() if line]
            by_id = {row["candidate_id"]: row for row in reviewed_rows}
            accepted = by_id["owner__repo-rg-b222"]
            self.assertEqual(accepted["state"], "accepted")
            self.assertEqual(accepted["reason"], "manual include")
            self.assertTrue(accepted["metadata"]["force_include"])

    def test_review_command_accepts_advisory_triage_without_overriding_state(self) -> None:
        with TemporaryDirectory() as workspace:
            root = Path(workspace)
            candidates_path = root / "candidates.jsonl"
            candidate_ids = _write_candidates(candidates_path)
            triage_path = root / "triage.json"
            _write_triage_hints(
                triage_path,
                candidate_ids[1],
                {
                    "state": "accepted",
                    "reason": "Model recommends include",
                    "reviewer_notes": "Looks like a real fix",
                    "suggested_problem_statement": "Manual statement for review",
                },
            )

            exit_code = main(
                [
                    "review",
                    str(candidates_path),
                    "--llm-mode",
                    "local_only",
                    "--triage-hints",
                    str(triage_path),
                    "--llm-model",
                    "advisor-unit-1",
                    "--llm-provider",
                    "local",
                ]
            )
            self.assertEqual(exit_code, 0)

            reviewed = root / "reviewed.jsonl"
            reviewed_rows = [json.loads(line) for line in reviewed.read_text(encoding="utf-8").splitlines() if line]
            by_id = {row["candidate_id"]: row for row in reviewed_rows}
            self.assertEqual(by_id[candidate_ids[1]]["state"], "rejected")
            advisory = by_id[candidate_ids[1]]["metadata"]["llm_advisory"]
            self.assertTrue(advisory["enabled"])
            self.assertEqual(advisory["suggested_state"], "accepted")
            self.assertEqual(by_id[candidate_ids[1]]["metadata"]["source_subject"], "Manual statement for review")
            self.assertEqual(advisory["problem_statement"], "Manual statement for review")
            self.assertTrue((root / "triage_cache.json").exists())


if __name__ == "__main__":
    unittest.main()
