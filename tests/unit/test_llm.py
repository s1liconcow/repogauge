from pathlib import Path
from tempfile import TemporaryDirectory

from repogauge.llm import LlmModelSpec, TriageSuggestion, load_triage_payload, parse_triage_payload, write_triage_payload


def _sample_model() -> tuple[LlmModelSpec, dict[str, TriageSuggestion]]:
    payload = {
        "model": {
            "model_name": "advisor-unit",
            "provider": "local",
            "prompt_version": "review/v1",
        },
        "candidates": [
            {
                "candidate_id": "owner__repo-rg-good",
                "state": "accepted",
                "reason": "Looks good",
                "suggested_problem_statement": "Fixes regression in parser.",
                "suggested_file_roles": {"prod": ["src/parser.py"], "test": ["tests/test_parser.py"]},
                "confidence": 0.9,
            },
            {
                "candidate_id": "owner__repo-rg-bad-state",
                "state": "invalid",
                "reason": "Bad state should fail",
            },
            {
                "candidate_id": "owner__repo-rg-bad-roles",
                "state": "accepted",
                "suggested_file_roles": {"prod": "src/parser.py"},
            },
        ],
    }
    return parse_triage_payload(payload, default_name="fallback", default_provider="fallback")


def test_parse_triage_payload_strips_invalid_hints_and_keeps_valid():
    model, hints = _sample_model()
    assert model.model_name == "advisor-unit"
    assert model.provider == "local"
    assert "owner__repo-rg-good" in hints
    assert "owner__repo-rg-bad-state" not in hints
    assert "owner__repo-rg-bad-roles" not in hints


def test_triage_cache_round_trip():
    with TemporaryDirectory() as workspace:
        root = Path(workspace)
        model, hints = _sample_model()
        cache_path = root / "triage_cache.json"
        write_triage_payload(cache_path, model, hints)

        reloaded_model, reloaded_hints = load_triage_payload(
            cache_path,
            default_name="fallback",
            default_provider="fallback",
        )
        assert reloaded_model.model_name == "advisor-unit"
        assert reloaded_model.provider == "local"
        assert reloaded_hints.keys() == hints.keys()

