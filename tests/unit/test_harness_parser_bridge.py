"""Tests for the exported harness parser bridge module."""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from repogauge.parsers.junit import (
    parse_repogauge_junit,
    parse_repogauge_test_output,
)
from repogauge.validation.junit_parser import (
    JUnitParseError,
    OUTCOME_FAIL,
    OUTCOME_PASS,
)


XML = """\
<?xml version="1.0" encoding="utf-8"?>
<testsuite>
  <testcase classname="tests.unit.test_foo" name="test_ok"/>
  <testcase classname="tests.unit.test_foo" name="test_fail">
    <failure message="boom"/>
  </testcase>
</testsuite>
"""


def test_parse_repogauge_junit_accepts_path_and_payload_variants(
    tmp_path: Path,
) -> None:
    xml_path = tmp_path / "results.xml"
    xml_path.write_text(textwrap.dedent(XML), encoding="utf-8")

    by_path = parse_repogauge_junit(xml_path)
    by_string = parse_repogauge_junit(textwrap.dedent(XML))
    by_dict = parse_repogauge_junit({"junit_xml": xml_path})
    by_bytes = parse_repogauge_junit(textwrap.dedent(XML).encode("utf-8"))

    assert by_path == by_string == by_dict == by_bytes
    assert by_path == {
        "tests/unit/test_foo.py::test_ok": OUTCOME_PASS,
        "tests/unit/test_foo.py::test_fail": OUTCOME_FAIL,
    }


def test_parse_repogauge_junit_accepts_optional_test_spec_argument(
    tmp_path: Path,
) -> None:
    xml_path = tmp_path / "results.xml"
    xml_path.write_text(textwrap.dedent(XML), encoding="utf-8")

    parsed = parse_repogauge_junit(xml_path, object())

    assert parsed == {
        "tests/unit/test_foo.py::test_ok": OUTCOME_PASS,
        "tests/unit/test_foo.py::test_fail": OUTCOME_FAIL,
    }


def test_parse_repogauge_junit_accepts_leading_trailing_whitespace_path(
    tmp_path: Path,
) -> None:
    xml_path = tmp_path / "results.xml"
    xml_path.write_text(textwrap.dedent(XML), encoding="utf-8")

    parsed = parse_repogauge_junit(f" {xml_path} \n")

    assert parsed == {
        "tests/unit/test_foo.py::test_ok": OUTCOME_PASS,
        "tests/unit/test_foo.py::test_fail": OUTCOME_FAIL,
    }


def test_parse_repogauge_junit_handles_nested_mapping_payloads(
    tmp_path: Path,
) -> None:
    xml_path = tmp_path / "results.xml"
    xml_path.write_text(textwrap.dedent(XML), encoding="utf-8")

    parsed = parse_repogauge_junit({"output": {"junit_xml": xml_path}})

    assert parsed == {
        "tests/unit/test_foo.py::test_ok": OUTCOME_PASS,
        "tests/unit/test_foo.py::test_fail": OUTCOME_FAIL,
    }


def test_parse_repogauge_junit_falls_back_to_next_candidate_if_previous_is_unsupported(
    tmp_path: Path,
) -> None:
    xml_path = tmp_path / "results.xml"
    xml_path.write_text(textwrap.dedent(XML), encoding="utf-8")

    parsed = parse_repogauge_junit({"output": 123, "junit_xml": xml_path})

    assert parsed == {
        "tests/unit/test_foo.py::test_ok": OUTCOME_PASS,
        "tests/unit/test_foo.py::test_fail": OUTCOME_FAIL,
    }


def test_parse_repogauge_junit_falls_back_to_pytest_log_parsing() -> None:
    parsed = parse_repogauge_junit(
        """
        + python -m pytest tests/unit/test_example.py
        FAILED tests/unit/test_example.py::test_fails - AssertionError: boom
        PASSED tests/unit/test_example.py::test_passes
        """
    )

    assert parsed == {
        "tests/unit/test_example.py::test_fails": "FAILED",
        "tests/unit/test_example.py::test_passes": "PASSED",
    }


def test_parse_repogauge_junit_does_not_treat_multiline_logs_as_paths() -> None:
    parsed = parse_repogauge_junit(
        """
        + python -m pytest tests/unit/test_adapter.py
        ============================= test session starts ==============================
        collected 1 item
        tests/unit/test_adapter.py .                                            [100%]
        ============================== 1 passed in 0.07s ===============================
        """
    )

    assert parsed == {}


def test_parse_repogauge_junit_rejects_unknown_payload():
    with pytest.raises(TypeError, match="unsupported report payload"):
        parse_repogauge_junit(123)


def test_parse_repogauge_junit_propagates_parse_error_for_malformed_xml():
    with pytest.raises(JUnitParseError, match="malformed"):
        parse_repogauge_junit("<not valid xml")


def test_parse_repogauge_test_output_dispatches_by_parser_name(tmp_path: Path) -> None:
    xml_path = tmp_path / "results.xml"
    xml_path.write_text(textwrap.dedent(XML), encoding="utf-8")

    parsed = parse_repogauge_test_output(xml_path, parser_name="junit")

    assert parsed == {
        "tests/unit/test_foo.py::test_ok": OUTCOME_PASS,
        "tests/unit/test_foo.py::test_fail": OUTCOME_FAIL,
    }


def test_parse_repogauge_test_output_rejects_unknown_parser_name() -> None:
    with pytest.raises(KeyError, match="unknown test parser"):
        parse_repogauge_test_output("ignored", parser_name="unknown")


def test_importing_bridge_module_does_not_eagerly_import_swebench_parser() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import repogauge.parsers.junit; "
                "print('swebench.harness.log_parsers.python' in sys.modules)"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.strip() == "False"
