from pathlib import Path

from repogauge.mining import classify_file


def test_classify_prod_path():
    result = classify_file("repogauge/mining/inspect.py")
    assert result.role == "prod"
    assert "runtime source extension" in result.reason


def test_classify_test_file():
    result = classify_file("tests/unit/test_cli.py")
    assert result.role == "test"
    assert "test filename convention" in result.reason


def test_classify_test_support_fixture():
    result = classify_file("tests/fixtures/cli_input.json")
    assert result.role == "test_support"
    assert "test-support path under tests" in result.reason


def test_classify_config_build_file():
    result = classify_file(".github/workflows/test.yml")
    assert result.role == "config_build"
    assert "CI, package, or tooling configuration file" in result.reason


def test_classify_docs_file():
    result = classify_file("docs/notes/implementation.md")
    assert result.role == "docs"
    assert "documentation file or directory" in result.reason


def test_classify_generated_vendor_file():
    result = classify_file(".venv/lib/site-packages/pkg/__init__.py")
    assert result.role == "generated_vendor"
    assert "vendor or generated build cache directory" in result.reason


def test_classify_unknown_file():
    result = classify_file("random/binary.bin")
    assert result.role == "unknown"
    assert "No explicit role rule matched" in result.reason

