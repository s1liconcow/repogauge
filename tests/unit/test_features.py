from __future__ import annotations

from repogauge.runner.features import TASK_FEATURE_VERSION, build_task_feature_bundle


def test_task_features_ignore_leak_prone_patch_fields() -> None:
    base_row = {
        "instance_id": "owner__repo-rg-abc123",
        "repo": "owner/repo",
        "base_commit": "deadbeef",
        "version": "1.2.3",
        "problem_statement": "Fix the parser when the traceback includes a file path.",
    }
    leaked_row = {
        **base_row,
        "patch": "diff --git a/x b/x",
        "test_patch": "diff --git a/tests/test_x.py b/tests/test_x.py",
        "FAIL_TO_PASS": ["test_parser_handles_traceback"],
        "PASS_TO_PASS": ["test_parser_smoke"],
        "metadata": {"changed_file_count": 99, "gold_changed_lines": 2048},
    }

    first = build_task_feature_bundle(base_row)
    second = build_task_feature_bundle(leaked_row)

    assert first.feature_version == TASK_FEATURE_VERSION
    assert first.feature_hash == second.feature_hash
    assert first.cluster_label == second.cluster_label
    assert first.features == second.features
    assert first.to_metadata()["task_cluster"] == first.cluster_label


def test_task_features_bucket_problem_signal_deterministically() -> None:
    row = {
        "repo": "owner/repo",
        "version": "py311",
        "problem_statement": "Traceback when loading cached settings from disk.",
    }

    bundle = build_task_feature_bundle(row)

    assert bundle.feature_version == TASK_FEATURE_VERSION
    assert bundle.features["problem_statement_signal"] == "stacktrace"
    assert bundle.features["problem_statement_length_bucket"] == "short"
    assert bundle.features["version_bucket"] == "python-tagged"
    assert bundle.cluster_label == "len=short|signal=stacktrace|version=python-tagged"
