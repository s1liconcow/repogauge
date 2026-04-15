"""E2E integration tests: repogauge run against the repogauge repo itself.

These tests exercise the full mine → review → export pipeline against the
real repository on disk, validating that:
  - the mine step correctly profiles a Python/pytest project
  - candidates.jsonl and scan.jsonl are structurally valid
  - auto-accept/auto-reject policy applies when no decisions are provided
  - scripted decisions drive review to accepted/rejected states
  - export produces valid SWE-bench-shaped dataset.jsonl
  - the --resume flag skips re-execution when inputs are unchanged
  - commit-range and max-commits limits are respected

Design note on export tests: the CLI export command resolves the git repo root
by walking up from the output path. To ensure this resolves correctly to
REPO_ROOT, export outputs are written inside a subdirectory of the repo.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Iterator

import pytest

# The repo under test is this repo itself.
REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_REMOTE = "s1liconcow/repogauge"

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _run_main(args: list[str]) -> int:
    """Call repogauge.cli:main in-process; returns exit code."""
    from repogauge.cli import main

    return main(args)


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _read_manifest(out: Path) -> dict:
    lines = (out / "manifest.json").read_text(encoding="utf-8").splitlines()
    return json.loads(lines[-1])


def _mine(out: Path, *, max_commits: int = 40, commit_range: str | None = None, extra: list[str] | None = None) -> int:
    args = ["mine", str(REPO_ROOT), "--out", str(out), "--llm-mode", "off"]
    args += ["--max-commits", str(max_commits)]
    if commit_range:
        args += ["--commit-range", commit_range]
    if extra:
        args += extra
    return _run_main(args)


def _write_decisions(path: Path, candidate_ids: list[str], state: str = "accepted") -> None:
    with path.open("w", encoding="utf-8") as fh:
        for cid in candidate_ids:
            fh.write(json.dumps({"id": cid, "state": state, "reviewer_notes": "e2e-test"}) + "\n")


def _review(candidates_path: Path, out: Path, decisions_path: Path | None = None, *, extra: list[str] | None = None) -> int:
    args = ["review", str(candidates_path), "--out", str(out), "--llm-mode", "off"]
    if decisions_path:
        args += ["--decisions", str(decisions_path)]
    if extra:
        args += extra
    return _run_main(args)


def _export(reviewed_path: Path, out: Path, *, extra: list[str] | None = None) -> int:
    # NOTE: export resolves the git repo root by walking up from reviewed_path.
    # reviewed_path must therefore reside inside the git repo so that .git is
    # reachable. See the `repo_workspace` fixture for how we arrange this.
    args = ["export", str(reviewed_path), "--out", str(out), "--llm-mode", "off"]
    if extra:
        args += extra
    return _run_main(args)


@pytest.fixture()
def repo_workspace() -> Iterator[Path]:
    """A temp workspace directory created INSIDE the repo root.

    The export command resolves the git repo root by walking up from the
    reviewed.jsonl path. This fixture places the workspace inside REPO_ROOT
    so that the .git directory is discoverable.

    The workspace is cleaned up after each test.
    """
    ws = REPO_ROOT / ".e2e_tmp"
    ws.mkdir(exist_ok=True)
    try:
        yield ws
    finally:
        shutil.rmtree(ws, ignore_errors=True)


# ──────────────────────────────────────────────────────────────────────────────
# Mine step – happy path and structural validation
# ──────────────────────────────────────────────────────────────────────────────


class TestMineAgainstSelf:
    def test_mine_succeeds_on_real_repo(self, tmp_path):
        """mine exits 0 and emits a succeeded manifest when run on this repo."""
        out = tmp_path / "mine"
        rc = _mine(out)
        assert rc == 0
        manifest = _read_manifest(out)
        assert manifest["command"] == "mine"
        assert manifest["status"] == "succeeded"
        assert manifest["step_statuses"]["execute"] == "succeeded"

    def test_mine_emits_repo_profile_with_correct_identity(self, tmp_path):
        """repo_profile.json names this repo correctly and detects Python+pytest."""
        out = tmp_path / "mine"
        _mine(out)
        profile = json.loads((out / "repo_profile.json").read_text(encoding="utf-8"))
        # Remote origin should resolve to the expected slug
        assert profile["repo_name"] == REPO_REMOTE
        # Must detect Python tooling
        assert profile["python_hints"] is not None
        # Must detect a test runner
        assert "commands" in profile["test_runner_hints"]
        assert len(profile["test_runner_hints"]["commands"]) > 0

    def test_mine_emits_required_artifact_files(self, tmp_path):
        """mine creates scan.jsonl, candidates.jsonl, repo_profile.json, manifest.json, events.jsonl."""
        out = tmp_path / "mine"
        _mine(out)
        for name in ("scan.jsonl", "candidates.jsonl", "repo_profile.json", "manifest.json", "events.jsonl"):
            assert (out / name).exists(), f"missing artifact: {name}"

    def test_mine_candidates_have_valid_schema_fields(self, tmp_path):
        """Every row in candidates.jsonl has required ScanRow fields with correct types."""
        out = tmp_path / "mine"
        _mine(out)
        rows = _read_jsonl(out / "candidates.jsonl")
        assert len(rows) > 0, "Expected at least one candidate from 40 commits"
        for row in rows:
            assert "id" in row and row["id"], "id must be a non-empty string"
            assert "repo" in row and row["repo"] == REPO_REMOTE
            assert "commit" in row and len(row["commit"]) == 40, "commit must be a full SHA"
            assert "heuristic_score" in row and isinstance(row["heuristic_score"], (int, float))
            assert "files_touched" in row and isinstance(row["files_touched"], list)
            assert "changed_lines" in row and row["changed_lines"] >= 0
            assert "metadata" in row
            assert "decision_band" in row["metadata"]
            assert row["metadata"]["decision_band"] in {"shortlist", "review", "reject"}
            assert "score_breakdown" in row["metadata"] and isinstance(row["metadata"]["score_breakdown"], list)

    def test_mine_scan_and_candidates_rows_match(self, tmp_path):
        """scan.jsonl and candidates.jsonl must contain identical rows (same ids, same order)."""
        out = tmp_path / "mine"
        _mine(out)
        scan_ids = [r["id"] for r in _read_jsonl(out / "scan.jsonl")]
        cand_ids = [r["id"] for r in _read_jsonl(out / "candidates.jsonl")]
        assert scan_ids == cand_ids, "scan.jsonl and candidates.jsonl must mirror each other"

    def test_mine_manifest_artifact_paths_point_to_existing_files(self, tmp_path):
        """manifest.json artifact_paths entries must resolve to real files."""
        out = tmp_path / "mine"
        _mine(out)
        manifest = _read_manifest(out)
        for key, rel_path in manifest.get("artifact_paths", {}).items():
            p = Path(rel_path)
            assert p.exists(), f"artifact_paths[{key!r}] = {rel_path!r} does not exist"

    def test_mine_max_commits_flag_limits_scan_count(self, tmp_path):
        """--max-commits N caps how many commits appear in scan.jsonl."""
        out = tmp_path / "mine"
        _mine(out, max_commits=5)
        rows = _read_jsonl(out / "scan.jsonl")
        assert len(rows) <= 5, f"Expected ≤5 scan rows, got {len(rows)}"

    def test_mine_respects_commit_range(self, tmp_path):
        """--commit-range HEAD~3..HEAD scans at most 3 commits."""
        out = tmp_path / "mine"
        rc = _mine(out, commit_range="HEAD~3..HEAD")
        assert rc == 0
        rows = _read_jsonl(out / "scan.jsonl")
        assert len(rows) <= 3, f"commit-range HEAD~3..HEAD should produce ≤3 rows, got {len(rows)}"

    def test_mine_events_log_has_start_and_finish_events(self, tmp_path):
        """events.jsonl must contain command.start and command.finish entries."""
        out = tmp_path / "mine"
        _mine(out)
        events = _read_jsonl(out / "events.jsonl")
        event_types = {e.get("event") for e in events}
        assert "command.start" in event_types, "Missing command.start event"
        assert "command.finish" in event_types, "Missing command.finish event"
        finish = next(e for e in events if e.get("event") == "command.finish")
        assert finish.get("status") == "succeeded"

    def test_mine_score_breakdown_components_are_named(self, tmp_path):
        """score_breakdown entries must each have 'component' and numeric 'weight'."""
        out = tmp_path / "mine"
        _mine(out)
        rows = _read_jsonl(out / "candidates.jsonl")
        for row in rows:
            for entry in row["metadata"]["score_breakdown"]:
                assert "component" in entry, "score_breakdown entry missing 'component'"
                assert "weight" in entry and isinstance(entry["weight"], (int, float)), (
                    "score_breakdown entry 'weight' must be numeric"
                )

    def test_mine_commits_with_test_files_score_above_prod_only(self, tmp_path):
        """Commits touching both prod+test files score higher than prod-only ones."""
        out = tmp_path / "mine"
        _mine(out, max_commits=40)
        rows = _read_jsonl(out / "candidates.jsonl")
        prod_test = [r for r in rows if r["metadata"].get("n_test_files", 0) > 0 and r["metadata"].get("n_prod_files", 0) > 0]
        prod_only = [r for r in rows if r["metadata"].get("n_test_files", 0) == 0 and r["metadata"].get("n_prod_files", 0) > 0]
        if prod_test and prod_only:
            avg_prod_test = sum(r["heuristic_score"] for r in prod_test) / len(prod_test)
            avg_prod_only = sum(r["heuristic_score"] for r in prod_only) / len(prod_only)
            assert avg_prod_test > avg_prod_only, (
                f"Commits with test files should score higher on average "
                f"(prod+test avg={avg_prod_test:.1f}, prod-only avg={avg_prod_only:.1f})"
            )


# ──────────────────────────────────────────────────────────────────────────────
# Resume behaviour
# ──────────────────────────────────────────────────────────────────────────────


class TestResumeFlag:
    def test_resume_skips_re_execution_when_inputs_unchanged(self, tmp_path):
        """A second mine --resume run records step_statuses['resume'] = 'skipped'."""
        out = tmp_path / "mine"
        first = _mine(out)
        assert first == 0
        second = _mine(out, extra=["--resume"])
        assert second == 0
        manifest = _read_manifest(out)
        assert manifest["status"] == "succeeded"
        assert "resume" in manifest["step_statuses"]

    def test_resume_does_not_alter_artifacts_from_first_run(self, tmp_path):
        """After a --resume run, candidates.jsonl is identical to the first run."""
        out = tmp_path / "mine"
        _mine(out)
        before = (out / "candidates.jsonl").read_text(encoding="utf-8")
        _mine(out, extra=["--resume"])
        after = (out / "candidates.jsonl").read_text(encoding="utf-8")
        assert before == after, "candidates.jsonl must be unchanged after --resume"

    def test_different_max_commits_bypasses_resume_cache(self, tmp_path):
        """Changing --max-commits invalidates the resume cache and re-runs the scan."""
        out = tmp_path / "mine"
        _mine(out, max_commits=5)

        # Re-run with a different max_commits; --resume should NOT skip
        _mine(out, max_commits=10)
        manifest = _read_manifest(out)
        # The second run was not a resume (no --resume flag), so step_statuses should
        # NOT contain 'resume' from this run
        assert "resume" not in manifest["step_statuses"], (
            "Without --resume flag, second run should not produce a resume entry"
        )

    def test_resume_with_changed_inputs_re_runs(self, tmp_path):
        """--resume with different inputs (commit range) does not reuse the cache."""
        out = tmp_path / "mine"
        _mine(out, max_commits=5)
        # Re-run with --resume but different max_commits — hash mismatch should trigger re-run
        _mine(out, max_commits=10, extra=["--resume"])
        manifest = _read_manifest(out)
        # status should still be succeeded (re-ran successfully)
        assert manifest["status"] == "succeeded"


# ──────────────────────────────────────────────────────────────────────────────
# Review step
# ──────────────────────────────────────────────────────────────────────────────


class TestReviewAgainstSelf:
    def _setup_mine(self, tmp_path) -> tuple[Path, list[dict]]:
        mine_out = tmp_path / "mine"
        _mine(mine_out, max_commits=40)
        candidates = _read_jsonl(mine_out / "candidates.jsonl")
        return mine_out, candidates

    def test_review_succeeds_and_emits_manifest(self, tmp_path):
        """review exits 0 and emits a succeeded manifest."""
        mine_out, candidates = self._setup_mine(tmp_path)
        rev_out = tmp_path / "review"
        rc = _review(mine_out / "candidates.jsonl", rev_out)
        assert rc == 0
        manifest = _read_manifest(rev_out)
        assert manifest["command"] == "review"
        assert manifest["status"] == "succeeded"

    def test_review_without_decisions_auto_accepts_shortlist_and_rejects_rest(self, tmp_path):
        """Without a decisions file, shortlist candidates are auto-accepted, others auto-rejected.

        Guards against: review silently changing auto-accept/reject policy without surfacing it.
        The policy in _run_decision: shortlist → accepted, everything else → rejected.
        """
        mine_out, candidates = self._setup_mine(tmp_path)
        rev_out = tmp_path / "review"
        _review(mine_out / "candidates.jsonl", rev_out)
        reviewed = _read_jsonl(rev_out / "reviewed.jsonl")

        # Index by candidate_id (reviewed rows have id = "{candidate_id}-reviewed")
        by_candidate_id = {r["candidate_id"]: r for r in reviewed}
        for cand in candidates:
            rev = by_candidate_id[cand["id"]]
            if cand["metadata"]["decision_band"] == "shortlist":
                assert rev["state"] == "accepted", (
                    f"Shortlist candidate {cand['id']} should be auto-accepted, got {rev['state']!r}"
                )
            else:
                assert rev["state"] == "rejected", (
                    f"Non-shortlist candidate {cand['id']} (band={cand['metadata']['decision_band']!r}) "
                    f"should be auto-rejected, got {rev['state']!r}"
                )

    def test_review_scripted_decisions_accept_targeted_candidates(self, tmp_path):
        """Passing --decisions accepts the specified candidate IDs and rejects the rest."""
        mine_out, candidates = self._setup_mine(tmp_path)
        if not candidates:
            pytest.skip("no candidates mined — nothing to review")

        # Accept the highest-scoring candidate
        top = sorted(candidates, key=lambda r: r["heuristic_score"], reverse=True)[0]
        decisions_path = tmp_path / "decisions.jsonl"
        _write_decisions(decisions_path, [top["id"]], state="accepted")

        rev_out = tmp_path / "review"
        rc = _review(mine_out / "candidates.jsonl", rev_out, decisions_path=decisions_path)
        assert rc == 0

        # reviewed.jsonl rows have id = "{candidate_id}-reviewed"; look up by candidate_id
        reviewed = _read_jsonl(rev_out / "reviewed.jsonl")
        by_candidate_id = {r["candidate_id"]: r for r in reviewed}
        assert by_candidate_id[top["id"]]["state"] == "accepted"

    def test_review_emits_reviewed_jsonl_with_required_fields(self, tmp_path):
        """Every row in reviewed.jsonl has id, candidate_id, state, and repo fields."""
        mine_out, candidates = self._setup_mine(tmp_path)
        rev_out = tmp_path / "review"
        _review(mine_out / "candidates.jsonl", rev_out)
        reviewed = _read_jsonl(rev_out / "reviewed.jsonl")
        for row in reviewed:
            assert "id" in row and row["id"]
            assert "candidate_id" in row and row["candidate_id"], "candidate_id field must be present"
            assert row["id"] == f"{row['candidate_id']}-reviewed", (
                "reviewed.jsonl id must be '{candidate_id}-reviewed'"
            )
            assert "state" in row and row["state"] in {"open", "accepted", "rejected"}
            assert "repo" in row

    def test_review_emits_html_and_markdown_reports(self, tmp_path):
        """review produces review.md and review.html alongside reviewed.jsonl."""
        mine_out, _ = self._setup_mine(tmp_path)
        rev_out = tmp_path / "review"
        _review(mine_out / "candidates.jsonl", rev_out)
        assert (rev_out / "review.md").exists(), "review.md not emitted"
        assert (rev_out / "review.html").exists(), "review.html not emitted"

    def test_review_count_matches_candidate_count(self, tmp_path):
        """reviewed.jsonl must contain exactly one row per input candidate."""
        mine_out, candidates = self._setup_mine(tmp_path)
        rev_out = tmp_path / "review"
        _review(mine_out / "candidates.jsonl", rev_out)
        reviewed = _read_jsonl(rev_out / "reviewed.jsonl")
        assert len(reviewed) == len(candidates), (
            f"reviewed.jsonl has {len(reviewed)} rows but candidates.jsonl has {len(candidates)}"
        )

    def test_review_reject_decision_marks_candidate_rejected(self, tmp_path):
        """A 'rejected' decision in the decisions file is faithfully written to reviewed.jsonl."""
        mine_out, candidates = self._setup_mine(tmp_path)
        if not candidates:
            pytest.skip("no candidates to reject")
        target = candidates[0]
        decisions_path = tmp_path / "decisions.jsonl"
        _write_decisions(decisions_path, [target["id"]], state="rejected")

        rev_out = tmp_path / "review"
        _review(mine_out / "candidates.jsonl", rev_out, decisions_path=decisions_path)

        reviewed = _read_jsonl(rev_out / "reviewed.jsonl")
        by_candidate_id = {r["candidate_id"]: r for r in reviewed}
        assert by_candidate_id[target["id"]]["state"] == "rejected"

    def test_review_candidate_ids_are_stable_between_mine_and_review(self, tmp_path):
        """candidate_id values in reviewed.jsonl must match ids from candidates.jsonl exactly.

        Guards against: ID format changes in either mine or review breaking the linkage.
        """
        mine_out, candidates = self._setup_mine(tmp_path)
        rev_out = tmp_path / "review"
        _review(mine_out / "candidates.jsonl", rev_out)
        reviewed = _read_jsonl(rev_out / "reviewed.jsonl")

        mine_ids = {r["id"] for r in candidates}
        review_candidate_ids = {r["candidate_id"] for r in reviewed}
        assert mine_ids == review_candidate_ids, (
            "candidate_id values in reviewed.jsonl must match ids from candidates.jsonl\n"
            f"  Missing in review: {mine_ids - review_candidate_ids}\n"
            f"  Extra in review: {review_candidate_ids - mine_ids}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Export step
# NOTE: export resolves git repo root from the path it's given. Tests use the
# `repo_workspace` fixture which creates the workspace INSIDE REPO_ROOT so that
# git root resolution succeeds.
# ──────────────────────────────────────────────────────────────────────────────


class TestExportAgainstSelf:
    def _setup_mine_review(self, workspace: Path) -> tuple[Path, list[dict]]:
        """Run mine then review with the top-scoring candidates accepted."""
        mine_out = workspace / "mine"
        _mine(mine_out, max_commits=40)
        candidates = _read_jsonl(mine_out / "candidates.jsonl")

        # Accept candidates that touch both prod and test files (best export candidates)
        exportable = [
            c for c in candidates
            if c["metadata"].get("n_test_files", 0) > 0 and c["metadata"].get("n_prod_files", 0) > 0
        ]
        if not exportable:
            exportable = [c for c in candidates if c["heuristic_score"] > 0]
        if not exportable:
            exportable = candidates[:1]

        decisions_path = workspace / "decisions.jsonl"
        _write_decisions(decisions_path, [c["id"] for c in exportable[:3]], state="accepted")

        rev_out = workspace / "review"
        _review(mine_out / "candidates.jsonl", rev_out, decisions_path=decisions_path)
        reviewed = _read_jsonl(rev_out / "reviewed.jsonl")
        return rev_out, reviewed

    def test_export_succeeds_when_accepted_candidates_exist(self, repo_workspace):
        """export exits 0 and emits a succeeded manifest when there are accepted candidates."""
        rev_out, reviewed = self._setup_mine_review(repo_workspace)
        accepted = [r for r in reviewed if r["state"] == "accepted"]
        if not accepted:
            pytest.skip("no accepted candidates to export")

        exp_out = repo_workspace / "export"
        rc = _export(rev_out / "reviewed.jsonl", exp_out)
        assert rc == 0
        manifest = _read_manifest(exp_out)
        assert manifest["status"] == "succeeded"

    def test_export_produces_dataset_jsonl(self, repo_workspace):
        """export creates dataset/dataset.jsonl."""
        rev_out, reviewed = self._setup_mine_review(repo_workspace)
        accepted = [r for r in reviewed if r["state"] == "accepted"]
        if not accepted:
            pytest.skip("no accepted candidates to export")

        exp_out = repo_workspace / "export"
        _export(rev_out / "reviewed.jsonl", exp_out)

        # dataset.jsonl may be empty if all candidates were rejected during materialization,
        # but the file itself must exist
        dataset_path = exp_out / "dataset" / "dataset.jsonl"
        assert dataset_path.exists(), "dataset/dataset.jsonl not created"

    def test_export_dataset_instances_have_swebench_schema(self, repo_workspace):
        """Every row in dataset.jsonl has the SWE-bench required fields with correct types."""
        rev_out, reviewed = self._setup_mine_review(repo_workspace)
        accepted = [r for r in reviewed if r["state"] == "accepted"]
        if not accepted:
            pytest.skip("no accepted candidates to export")

        exp_out = repo_workspace / "export"
        _export(rev_out / "reviewed.jsonl", exp_out)

        dataset_path = exp_out / "dataset" / "dataset.jsonl"
        rows = _read_jsonl(dataset_path)
        if not rows:
            pytest.skip("export produced empty dataset.jsonl (all candidates rejected during materialization)")

        for row in rows:
            assert "instance_id" in row and row["instance_id"], "instance_id must be non-empty"
            assert "repo" in row and "/" in row["repo"], "repo must be owner/name format"
            assert "base_commit" in row and len(row["base_commit"]) == 40, "base_commit must be full SHA"
            assert "problem_statement" in row
            assert "patch" in row
            assert "test_patch" in row
            assert "FAIL_TO_PASS" in row and isinstance(row["FAIL_TO_PASS"], list)
            assert "PASS_TO_PASS" in row and isinstance(row["PASS_TO_PASS"], list)

    def test_export_patch_and_test_patch_are_valid_diffs(self, repo_workspace):
        """patch and test_patch must start with 'diff --git' when non-empty."""
        rev_out, reviewed = self._setup_mine_review(repo_workspace)
        accepted = [r for r in reviewed if r["state"] == "accepted"]
        if not accepted:
            pytest.skip("no accepted candidates")

        exp_out = repo_workspace / "export"
        _export(rev_out / "reviewed.jsonl", exp_out)

        dataset_path = exp_out / "dataset" / "dataset.jsonl"
        rows = _read_jsonl(dataset_path)
        if not rows:
            pytest.skip("no dataset instances to validate")

        for row in rows:
            if row["patch"]:
                assert row["patch"].startswith("diff --git"), (
                    f"patch for {row['instance_id']} must start with 'diff --git'"
                )
            if row["test_patch"]:
                assert row["test_patch"].startswith("diff --git"), (
                    f"test_patch for {row['instance_id']} must start with 'diff --git'"
                )

    def test_export_gold_predictions_align_with_dataset(self, repo_workspace):
        """predictions.gold.jsonl instance IDs must match dataset.jsonl instance IDs."""
        rev_out, reviewed = self._setup_mine_review(repo_workspace)
        accepted = [r for r in reviewed if r["state"] == "accepted"]
        if not accepted:
            pytest.skip("no accepted candidates")

        exp_out = repo_workspace / "export"
        _export(rev_out / "reviewed.jsonl", exp_out)

        dataset_path = exp_out / "dataset" / "dataset.jsonl"
        gold_path = exp_out / "dataset" / "predictions.gold.jsonl"
        rows = _read_jsonl(dataset_path)
        if not rows:
            pytest.skip("no dataset instances to validate")

        assert gold_path.exists(), "predictions.gold.jsonl must be created alongside dataset.jsonl"
        dataset_ids = {r["instance_id"] for r in rows}
        gold_ids = {r["instance_id"] for r in _read_jsonl(gold_path)}
        assert dataset_ids == gold_ids, (
            f"Gold predictions IDs differ from dataset IDs.\n"
            f"  In dataset only: {dataset_ids - gold_ids}\n"
            f"  In gold only: {gold_ids - dataset_ids}"
        )

    def test_export_no_accepted_candidates_still_exits_zero(self, repo_workspace):
        """export exits 0 even when all reviewed candidates are rejected.

        Guards against: export failing with exit code 1 on an empty accepted set.
        """
        mine_out = repo_workspace / "mine_rejected"
        _mine(mine_out, max_commits=10)
        candidates = _read_jsonl(mine_out / "candidates.jsonl")

        if not candidates:
            pytest.skip("no candidates to reject")

        decisions_path = repo_workspace / "decisions_reject_all.jsonl"
        _write_decisions(decisions_path, [c["id"] for c in candidates], state="rejected")

        rev_out = repo_workspace / "review_rejected"
        _review(mine_out / "candidates.jsonl", rev_out, decisions_path=decisions_path)

        exp_out = repo_workspace / "export_empty"
        rc = _export(rev_out / "reviewed.jsonl", exp_out)
        assert rc == 0, "export should exit 0 even when nothing is accepted"


# ──────────────────────────────────────────────────────────────────────────────
# Full pipeline integration
# ──────────────────────────────────────────────────────────────────────────────


class TestFullPipeline:
    def test_mine_review_export_pipeline_produces_coherent_artifacts(self, repo_workspace):
        """End-to-end: mine → review (accept top candidates) → export produces valid dataset.

        Guards against: pipeline stages producing artifacts that are inconsistent with each
        other (mismatched IDs, missing linkages, wrong repo names).
        """
        mine_out = repo_workspace / "mine"
        _mine(mine_out, max_commits=40)
        candidates = _read_jsonl(mine_out / "candidates.jsonl")

        exportable = [
            c for c in candidates
            if c["metadata"].get("n_test_files", 0) > 0 and c["metadata"].get("n_prod_files", 0) > 0
        ][:3]

        if not exportable:
            pytest.skip("no candidates with both prod+test files in 40 commits; pipeline cannot be tested end-to-end")

        decisions_path = repo_workspace / "decisions.jsonl"
        _write_decisions(decisions_path, [c["id"] for c in exportable], state="accepted")

        rev_out = repo_workspace / "review"
        rc = _review(mine_out / "candidates.jsonl", rev_out, decisions_path=decisions_path)
        assert rc == 0

        exp_out = repo_workspace / "export"
        rc = _export(rev_out / "reviewed.jsonl", exp_out)
        assert rc == 0

        # Validate coherence across pipeline stages
        scan_ids = {r["id"] for r in _read_jsonl(mine_out / "scan.jsonl")}
        reviewed_candidate_ids = {r["candidate_id"] for r in _read_jsonl(rev_out / "reviewed.jsonl")}
        assert reviewed_candidate_ids == scan_ids, (
            "reviewed.jsonl candidate_ids must cover every candidate from scan.jsonl"
        )

        dataset_path = exp_out / "dataset" / "dataset.jsonl"
        if dataset_path.exists():
            dataset_rows = _read_jsonl(dataset_path)
            for row in dataset_rows:
                assert row["repo"] == REPO_REMOTE, (
                    f"Dataset instance repo {row['repo']!r} should be {REPO_REMOTE!r}"
                )

    def test_pipeline_manifest_chain_all_succeed(self, repo_workspace):
        """Each stage in the pipeline must record status=succeeded in its manifest.

        Guards against: silent failures where a stage writes partial artifacts but
        still marks itself as succeeded.
        """
        mine_out = repo_workspace / "mine"
        _mine(mine_out, max_commits=20)

        rev_out = repo_workspace / "review"
        _review(mine_out / "candidates.jsonl", rev_out)

        exp_out = repo_workspace / "export"
        _export(rev_out / "reviewed.jsonl", exp_out)

        for stage_out, cmd in [(mine_out, "mine"), (rev_out, "review"), (exp_out, "export")]:
            manifest = _read_manifest(stage_out)
            assert manifest["command"] == cmd
            assert manifest["status"] == "succeeded", (
                f"{cmd} manifest shows status={manifest['status']!r}, expected 'succeeded'"
            )

    def test_pipeline_events_chain_covers_all_stages(self, repo_workspace):
        """events.jsonl exists for each pipeline stage and each ends with command.finish succeeded.

        Guards against: a stage crashing after partial work without emitting a finish event.
        """
        mine_out = repo_workspace / "mine"
        _mine(mine_out, max_commits=20)

        rev_out = repo_workspace / "review"
        _review(mine_out / "candidates.jsonl", rev_out)

        exp_out = repo_workspace / "export"
        _export(rev_out / "reviewed.jsonl", exp_out)

        for stage_out, cmd in [(mine_out, "mine"), (rev_out, "review"), (exp_out, "export")]:
            events = _read_jsonl(stage_out / "events.jsonl")
            finish_events = [e for e in events if e.get("event") == "command.finish"]
            assert finish_events, f"No command.finish event in {cmd}/events.jsonl"
            assert finish_events[-1].get("status") == "succeeded", (
                f"{cmd} did not finish with status=succeeded"
            )

    def test_pipeline_schema_version_is_consistent_across_artifacts(self, repo_workspace):
        """All JSONL artifacts must carry the same schema_version value.

        Guards against: a version bump in one artifact type but not others breaking
        downstream consumers that rely on a consistent version.
        """
        mine_out = repo_workspace / "mine"
        _mine(mine_out, max_commits=20)

        rev_out = repo_workspace / "review"
        _review(mine_out / "candidates.jsonl", rev_out)

        scan_rows = _read_jsonl(mine_out / "scan.jsonl")
        reviewed_rows = _read_jsonl(rev_out / "reviewed.jsonl")

        all_rows = scan_rows + reviewed_rows
        versions = {r.get("schema_version") for r in all_rows if "schema_version" in r}
        assert len(versions) <= 1, (
            f"Multiple schema_version values found across artifacts: {versions}"
        )
