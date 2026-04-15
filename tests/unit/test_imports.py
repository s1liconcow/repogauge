"""Smoke tests for scaffold imports."""

from repogauge import __version__  # noqa: F401
from repogauge.cli import main
from repogauge.config import DatasetInstance
from repogauge.manifest import Manifest
from repogauge.export.specs import AdapterConfig
from repogauge.runner.telemetry import AttemptTelemetry


def test_cli_importable():
    assert callable(main)


def test_contract_imports():
    assert DatasetInstance(instance_id="i", repo="repo", base_commit="c", problem_statement="", version="", patch="", test_patch="")
    assert AdapterConfig()
    assert Manifest(command="x")
    assert AttemptTelemetry(attempt_id="a", provider="p")
