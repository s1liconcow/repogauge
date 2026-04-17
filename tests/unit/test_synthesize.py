import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from repogauge.mining.synthesize import synthesize_problem_statement


class TestProblemStatementSynthesis(unittest.TestCase):
    def test_issue_title_and_body_take_precedence(self) -> None:
        statement, source, source_ref = synthesize_problem_statement(
            {
                "issue_title": "Crash when input is empty",
                "issue_body": "Empty input causes an exception",
                "issue_refs": ["1234"],
                "metadata": {
                    "source_subject": "Commit says something else",
                    "pr_title": "Unrelated PR title",
                },
            },
            patch="",
        )
        self.assertEqual(source, "linked_issue")
        self.assertEqual(source_ref, "1234")
        self.assertIn("Crash when input is empty", statement)

    def test_pull_request_title_is_used_when_issue_missing(self) -> None:
        statement, source, _ = synthesize_problem_statement(
            {
                "pr_title": "Use stable sort in helper",
                "pr_body": "Stable sort prevents flaky tests",
                "source_subject": "Commit subject",
                "source_body": "Commit body",
            },
            patch="",
        )
        self.assertEqual(source, "pull_request")
        self.assertIn("Use stable sort in helper", statement)

    def test_commit_message_is_the_fallback(self) -> None:
        statement, source, _ = synthesize_problem_statement(
            {
                "metadata": {
                    "source_subject": "Fix bug in parser",
                    "source_body": "Parser accepted invalid token.",
                    "file_roles": {
                        "prod": ["src/parser.py"],
                        "test": ["tests/test_parser.py"],
                    },
                    "total_changed_lines": 12,
                },
            },
            patch="@@ -1,2 +1,2 ...",
        )
        self.assertEqual(source, "commit")
        self.assertIn("Observed behavior", statement)
        self.assertIn("run tests impacted", statement)

    def test_llm_fallback_used_for_weak_commit_text(self) -> None:
        statement, source, source_ref = synthesize_problem_statement(
            {
                "source_subject": "Fix",
                "metadata": {
                    "llm_advisory": {
                        "problem_statement": "Refactor to handle edge-case input safely."
                    },
                    "llm_model": "model",
                },
            },
            patch="",
        )
        self.assertEqual(source, "llm_advisory")
        self.assertEqual(source_ref, "model")
        self.assertEqual(statement, "Refactor to handle edge-case input safely.")

    def test_commit_statement_includes_bead_and_all_issue_contexts(self) -> None:
        with TemporaryDirectory() as workspace:
            repo_root = Path(workspace)
            bead_dir = repo_root / ".beads"
            bead_dir.mkdir()
            (bead_dir / "issues.jsonl").write_text(
                (
                    '{"id":"oss_repogauge-rmy","title":"Deterministic env plan",'
                    '"description":"Keep environment planning stable across runs.",'
                    '"acceptance_criteria":"Plans are reproducible and test commands stay deterministic."}\n'
                ),
                encoding="utf-8",
            )

            statement, source, _ = synthesize_problem_statement(
                {
                    "source_subject": "Landing changes for bead oss_repogauge-rmy - fix parser",
                    "source_body": "Fixes #123 and gh-456",
                    "metadata": {
                        "file_roles": {
                            "prod": ["src/parser.py"],
                            "test": ["tests/test_parser.py"],
                        },
                        "issue_contexts": [
                            {
                                "ref": "123",
                                "title": "Parser crashes on empty input",
                                "body": "Empty input raises an unexpected exception.",
                            },
                            {"ref": "456", "title": "Cache path handling regressed"},
                        ],
                        "total_changed_lines": 18,
                    },
                },
                patch="@@ -1,2 +1,2 ...",
                repo_root=repo_root,
            )

            self.assertEqual(source, "commit")
            self.assertIn("Bead oss_repogauge-rmy: Deterministic env plan", statement)
            self.assertIn("Acceptance:", statement)
            self.assertIn(
                "Related GitHub issue #123: Parser crashes on empty input",
                statement,
            )
            self.assertIn(
                "Related GitHub issue #456: Cache path handling regressed",
                statement,
            )

    def test_primary_issue_statement_keeps_additional_issue_refs(self) -> None:
        statement, source, source_ref = synthesize_problem_statement(
            {
                "issue_title": "Primary regression",
                "issue_body": "The main regression still needs a fix.",
                "issue_refs": ["123", "456"],
                "metadata": {
                    "issue_contexts": [
                        {
                            "ref": "123",
                            "title": "Primary regression",
                            "body": "The main regression still needs a fix.",
                        },
                        {"ref": "456", "title": "Secondary timeout regression"},
                    ]
                },
            },
            patch="",
        )
        self.assertEqual(source, "linked_issue")
        self.assertEqual(source_ref, "123")
        self.assertIn("Primary regression", statement)
        self.assertIn("Related GitHub issue #456: Secondary timeout regression", statement)


if __name__ == "__main__":
    unittest.main()
