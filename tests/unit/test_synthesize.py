import unittest

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


if __name__ == "__main__":
    unittest.main()
