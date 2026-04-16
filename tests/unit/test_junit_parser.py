"""Tests for the JUnit XML parser bridge (bead 3wi)."""

import textwrap
from pathlib import Path

import pytest

from repogauge.validation.junit_parser import (
    JUnitParseError,
    OUTCOME_ERROR,
    OUTCOME_FAIL,
    OUTCOME_PASS,
    OUTCOME_SKIP,
    parse_junit_xml,
)


def _write_xml(path: Path, content: str) -> Path:
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


class TestParseJunitXml:
    def test_passes_failures_and_errors_are_classified(self, tmp_path):
        xml = _write_xml(
            tmp_path / "results.xml",
            """\
            <?xml version="1.0" encoding="utf-8"?>
            <testsuites>
              <testsuite name="pytest">
                <testcase classname="tests.unit.test_foo" name="test_pass"/>
                <testcase classname="tests.unit.test_foo" name="test_fail">
                  <failure message="AssertionError">assert False</failure>
                </testcase>
                <testcase classname="tests.unit.test_foo" name="test_error">
                  <error message="RuntimeError">boom</error>
                </testcase>
                <testcase classname="tests.unit.test_foo" name="test_skip">
                  <skipped/>
                </testcase>
                <testcase classname="tests.unit.test_foo" name="test_xfail">
                  <skipped type="pytest.xfail" message="expected failure"/>
                </testcase>
                <testcase classname="tests.unit.test_foo" name="test_xpass">
                  <xpass/>
                </testcase>
              </testsuite>
            </testsuites>
        """,
        )
        results = parse_junit_xml(xml)
        assert results["tests/unit/test_foo.py::test_pass"] == OUTCOME_PASS
        assert results["tests/unit/test_foo.py::test_fail"] == OUTCOME_FAIL
        assert results["tests/unit/test_foo.py::test_error"] == OUTCOME_ERROR
        assert results["tests/unit/test_foo.py::test_skip"] == OUTCOME_SKIP
        assert results["tests/unit/test_foo.py::test_xfail"] == OUTCOME_SKIP
        assert results["tests/unit/test_foo.py::test_xpass"] == OUTCOME_SKIP

    def test_bare_testsuite_root(self, tmp_path):
        xml = _write_xml(
            tmp_path / "results.xml",
            """\
            <?xml version="1.0" encoding="utf-8"?>
            <testsuite name="pytest">
              <testcase classname="tests.test_bar" name="test_something"/>
            </testsuite>
        """,
        )
        results = parse_junit_xml(xml)
        assert "tests/test_bar.py::test_something" in results
        assert results["tests/test_bar.py::test_something"] == OUTCOME_PASS

    def test_parametrized_test_ids_are_preserved(self, tmp_path):
        xml = _write_xml(
            tmp_path / "results.xml",
            """\
            <?xml version="1.0" encoding="utf-8"?>
            <testsuite>
              <testcase classname="tests.test_params" name="test_add[1-2-3]"/>
              <testcase classname="tests.test_params" name="test_add[0-0-0]"/>
            </testsuite>
        """,
        )
        results = parse_junit_xml(xml)
        assert "tests/test_params.py::test_add[1-2-3]" in results
        assert "tests/test_params.py::test_add[0-0-0]" in results

    def test_classname_with_class_produces_three_part_id(self, tmp_path):
        # pytest encodes class-based tests as module.path.ClassName in classname
        xml = _write_xml(
            tmp_path / "results.xml",
            """\
            <?xml version="1.0" encoding="utf-8"?>
            <testsuite>
              <testcase classname="tests.unit.test_foo.TestSuite" name="test_method"/>
              <testcase classname="tests.unit.test_foo" name="test_standalone"/>
            </testsuite>
        """,
        )
        results = parse_junit_xml(xml)
        assert "tests/unit/test_foo.py::TestSuite::test_method" in results
        assert "tests/unit/test_foo.py::test_standalone" in results

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(JUnitParseError, match="not found"):
            parse_junit_xml(tmp_path / "nonexistent.xml")

    def test_empty_file_raises(self, tmp_path):
        f = tmp_path / "empty.xml"
        f.write_text("", encoding="utf-8")
        with pytest.raises(JUnitParseError, match="empty"):
            parse_junit_xml(f)

    def test_malformed_xml_raises(self, tmp_path):
        f = tmp_path / "bad.xml"
        f.write_text("<not valid xml<<<", encoding="utf-8")
        with pytest.raises(JUnitParseError, match="malformed"):
            parse_junit_xml(f)

    def test_no_testsuite_element_raises(self, tmp_path):
        f = tmp_path / "nosuite.xml"
        f.write_text("<root><something/></root>", encoding="utf-8")
        with pytest.raises(JUnitParseError, match="testsuite"):
            parse_junit_xml(f)

    def test_testcase_without_name_is_skipped(self, tmp_path):
        xml = _write_xml(
            tmp_path / "results.xml",
            """\
            <?xml version="1.0" encoding="utf-8"?>
            <testsuite>
              <testcase classname="tests.test_foo" name=""/>
              <testcase classname="tests.test_foo" name="test_real"/>
            </testsuite>
        """,
        )
        results = parse_junit_xml(xml)
        assert len(results) == 1
        assert "tests/test_foo.py::test_real" in results

    def test_multiple_suites_are_merged(self, tmp_path):
        xml = _write_xml(
            tmp_path / "results.xml",
            """\
            <?xml version="1.0" encoding="utf-8"?>
            <testsuites>
              <testsuite name="suite1">
                <testcase classname="tests.a" name="test_x"/>
              </testsuite>
              <testsuite name="suite2">
                <testcase classname="tests.b" name="test_y">
                  <failure/>
                </testcase>
              </testsuite>
            </testsuites>
        """,
        )
        results = parse_junit_xml(xml)
        assert results["tests/a.py::test_x"] == OUTCOME_PASS
        assert results["tests/b.py::test_y"] == OUTCOME_FAIL

    def test_file_attribute_without_classname_still_resolves_to_test_path(
        self, tmp_path
    ):
        xml = _write_xml(
            tmp_path / "results.xml",
            """\
            <?xml version="1.0" encoding="utf-8"?>
            <testsuite>
              <testcase file="tests/unit/test_file_only.py" name="test_file_variant"/>
            </testsuite>
        """,
        )
        results = parse_junit_xml(xml)
        assert (
            results["tests/unit/test_file_only.py::test_file_variant"] == OUTCOME_PASS
        )
