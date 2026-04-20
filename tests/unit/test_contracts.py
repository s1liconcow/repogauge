from repogauge.config import AdapterSpec, DatasetInstance, PredictionRow, RepoProfile


def test_dataset_instance_round_trip():
    instance = DatasetInstance(
        instance_id="owner__repo-rg-abcd",
        repo="owner/repo",
        base_commit="abc123",
        problem_statement="Test regression",
        version="v1",
        patch="diff --git a/x.py b/x.py",
        test_patch="diff --git a/test_x.py b/test_x.py",
        immutable_paths=["tests/test.py"],
        FAIL_TO_PASS=["tests/test.py::test_regression"],
        PASS_TO_PASS=["tests/test.py::test_existing"],
    )
    payload = instance.to_dict()
    restored = DatasetInstance(**payload)
    assert restored == instance


def test_prediction_payload_has_schema_version():
    row = PredictionRow(
        instance_id="owner__repo-rg-abcd", model_name_or_path="gpt", model_patch="@@"
    )
    assert row.to_dict()["schema_version"] == "0.2.0"


def test_contracts_load_without_new_language_fields():
    repo_profile = RepoProfile.from_dict(
        {
            "repo": "owner/repo",
            "default_branch": "main",
            "source_path": "/tmp/repo",
            "python_version": "3.11",
            "package_manager": "uv",
            "install_cmds": ["uv sync"],
            "test_cmds": ["pytest"],
            "updated_at": "2026-04-18T00:00:00Z",
            "metadata": {},
        }
    )
    adapter_spec = AdapterSpec.from_dict(
        {
            "repo": "owner/repo",
            "version": "1.0.0",
            "docker_specs": {"python_version": "3.11"},
            "install_cmds": ["uv sync"],
            "test_cmds": ["pytest"],
            "module_name": "owner_repo",
            "metadata": {},
        }
    )

    assert repo_profile.language is None
    assert repo_profile.language_version is None
    assert repo_profile.python_version == "3.11"
    assert adapter_spec.language == "python"
    assert adapter_spec.runtime_version == ""
    assert adapter_spec.install_cmds == ["uv sync"]


def test_python_repo_profile_keeps_language_version_in_sync():
    profile = RepoProfile(
        repo="owner/repo",
        source_path="/tmp/repo",
        language="python",
        language_version="3.11",
        python_version="3.11",
    )

    assert profile.python_version == profile.language_version == "3.11"
