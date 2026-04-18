"""Smoke tests for scaffold imports."""

from repogauge import __version__  # noqa: F401
from repogauge.cli import main
from repogauge.config import DatasetInstance
from repogauge.config import RepoProfile
from repogauge.lang import DetectionResult
from repogauge.lang import FileRoleRules
from repogauge.lang import detect_language
from repogauge.lang import find_adapter
from repogauge.lang.javascript import JavaScriptAdapter
from repogauge.lang.go import GoAdapter
from repogauge.lang.rust import RustAdapter
from repogauge.validation import EnvPlan
from repogauge.manifest import Manifest
from repogauge.export.specs import AdapterConfig
from repogauge.runner.telemetry import AttemptTelemetry


def test_cli_importable():
    assert callable(main)


def test_contract_imports():
    assert DatasetInstance(
        instance_id="i",
        repo="repo",
        base_commit="c",
        problem_statement="",
        version="",
        patch="",
        test_patch="",
    )
    assert AdapterConfig()
    assert Manifest(command="x")
    telemetry = AttemptTelemetry(attempt_id="a", provider="p")
    assert telemetry.started_at.endswith("Z")
    assert telemetry.duration_ms == 0
    assert RepoProfile().updated_at.endswith("Z")
    assert EnvPlan(
        python_version="3.11",
        pre_install=[],
        install=[],
        build=[],
        test_cmd_base="pytest",
        strategy_name="poetry:pytest",
        confidence=1.0,
        provenance=[],
    )
    assert DetectionResult(language="unknown", confidence=0.0, signals=[])
    assert FileRoleRules(set(), [], set(), set(), set())
    assert callable(detect_language)
    assert isinstance(find_adapter("javascript"), JavaScriptAdapter)
    assert isinstance(find_adapter("go"), GoAdapter)
    assert isinstance(find_adapter("rust"), RustAdapter)
