from repogauge.config import DatasetInstance, PredictionRow


def test_dataset_instance_round_trip():
    instance = DatasetInstance(
        instance_id="owner__repo-rg-abcd",
        repo="owner/repo",
        base_commit="abc123",
        problem_statement="Test regression",
        version="v1",
        patch="diff --git a/x.py b/x.py",
        test_patch="diff --git a/test_x.py b/test_x.py",
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
    assert row.to_dict()["schema_version"] == "0.1.0"
