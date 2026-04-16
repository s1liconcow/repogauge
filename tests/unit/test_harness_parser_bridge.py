"""Tests for the exported harness parser bridge module."""

import textwrap
from pathlib import Path

import pytest

from repogauge.parsers.junit import parse_repogauge_junit
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


def test_parse_repogauge_junit_rejects_unknown_payload():
    with pytest.raises(TypeError, match="unsupported report payload"):
        parse_repogauge_junit(123)


def test_parse_repogauge_junit_propagates_parse_error_for_malformed_xml():
    with pytest.raises(JUnitParseError, match="malformed"):
        parse_repogauge_junit("<not valid xml")
