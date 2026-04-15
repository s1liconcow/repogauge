import unittest

from repogauge.mining import score_scan_commit


def _metadata(**kwargs):
    base = {
        "n_prod_files": 0,
        "n_test_files": 0,
        "n_test_support_files": 0,
        "n_config_build_files": 0,
        "n_docs_files": 0,
        "n_generated_vendor_files": 0,
        "n_unknown_files": 0,
        "n_hunks": 0,
        "total_changed_lines": 0,
        "parent_count": 1,
        "is_merge": False,
        "is_revert": False,
        "has_rename_only": False,
        "author_date": "2026-01-01T00:00:00Z",
    }
    base.update(kwargs)
    return base


class TestScoring(unittest.TestCase):
    def test_merge_commits_are_hard_rejected(self) -> None:
        scored = score_scan_commit(
            commit_subject="Merge branch feature",
            commit_body="",
            diff="",
            metadata=_metadata(is_merge=True, n_hunks=4, total_changed_lines=40),
        )

        self.assertEqual(scored.score, 0.0)
        self.assertEqual(scored.decision_band, "reject")
        self.assertEqual(scored.score_breakdown[0]["reason"], "merge commit")

    def test_high_quality_bugfix_is_shortlisted(self) -> None:
        scored = score_scan_commit(
            commit_subject="Fix regression in parser",
            commit_body="This fixes issue #123",
            diff="\n".join(
                [
                    "diff --git a/src/parser.py b/src/parser.py",
                    "@@ -1,3 +1,3 @@",
                    "+import logging",
                    "+def test_parsed_item():",
                    "     return True",
                    "+    assert True",
                ]
            ),
            metadata=_metadata(
                n_prod_files=1,
                n_test_files=1,
                n_hunks=2,
                total_changed_lines=45,
            ),
        )

        self.assertGreaterEqual(scored.score, 8.0)
        self.assertEqual(scored.decision_band, "shortlist")
        self.assertTrue(any(item["component"] == "prod_and_tests" for item in scored.score_breakdown))

    def test_issue_reference_in_subject_is_detected(self) -> None:
        scored = score_scan_commit(
            commit_subject="Fix failing case; refs GH-77 and closes #55",
            commit_body="",
            diff="",
            metadata=_metadata(
                n_prod_files=1,
                n_test_files=1,
                n_hunks=3,
                total_changed_lines=45,
            ),
        )

        self.assertTrue(any(item["component"] == "issue_link" for item in scored.score_breakdown))

    def test_mid_score_stays_in_review_band(self) -> None:
        scored = score_scan_commit(
            commit_subject="Adjust output handling",
            commit_body="",
            diff="",
            metadata=_metadata(
                n_prod_files=1,
                n_test_files=1,
                n_hunks=3,
                total_changed_lines=45,
            ),
        )

        self.assertEqual(scored.decision_band, "review")
        self.assertGreaterEqual(scored.score, 5.0)
        self.assertLess(scored.score, 8.0)

    def test_bead_file_change_gets_context_bonus(self) -> None:
        scored = score_scan_commit(
            commit_subject="Landing changes for bead oss_repogauge-rmy - deterministic env plan",
            commit_body="",
            diff="",
            metadata=_metadata(n_prod_files=1, n_test_files=1, n_hunks=3, total_changed_lines=50, has_bead_changes=True),
        )
        self.assertTrue(any(item["component"] == "bead_context" for item in scored.score_breakdown))
        bead_item = next(i for i in scored.score_breakdown if i["component"] == "bead_context")
        self.assertEqual(bead_item["weight"], 2)

    def test_no_bead_file_change_gets_no_context_bonus(self) -> None:
        scored = score_scan_commit(
            commit_subject="Landing changes for bead oss_repogauge-rmy - deterministic env plan",
            commit_body="",
            diff="",
            metadata=_metadata(n_prod_files=1, n_test_files=1, n_hunks=3, total_changed_lines=50),
        )
        self.assertFalse(any(item["component"] == "bead_context" for item in scored.score_breakdown))

    def test_dependency_only_change_is_rejected(self) -> None:
        scored = score_scan_commit(
            commit_subject="chore: bump deps",
            commit_body="",
            diff="diff --git a/requirements.txt b/requirements.txt\n",
            metadata=_metadata(
                n_config_build_files=2,
                n_hunks=1,
                total_changed_lines=3,
                n_unknown_files=0,
                n_docs_files=0,
            ),
        )

        self.assertEqual(scored.decision_band, "reject")
        self.assertTrue(any(item["component"] == "hard_reject" for item in scored.score_breakdown))


if __name__ == "__main__":
    unittest.main()
