"""JUnit XML parser bridge for pytest output (bead 3wi).

Parses the --junit-xml output produced by pytest and returns a mapping of
canonical test IDs to outcomes.  Only the pytest+JUnit contract is supported;
other test runners are out of scope for v1.

Canonical ID format mirrors pytest's own ``::``-separated node IDs:
    tests/unit/test_foo.py::TestClass::test_method
    tests/unit/test_bar.py::test_function

The classname attribute in JUnit XML encodes the path + class (dots instead of
slashes), and the name attribute holds the function.  This module converts back
to the ``::`` form so IDs match the strings in ``FAIL_TO_PASS`` / ``PASS_TO_PASS``.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict


OUTCOME_PASS = "pass"
OUTCOME_FAIL = "fail"
OUTCOME_ERROR = "error"
OUTCOME_SKIP = "skip"


class JUnitParseError(ValueError):
    """Raised when the JUnit XML cannot be parsed."""


def _split_classname(classname: str) -> tuple[str, str]:
    """Split ``tests.unit.test_foo.TestClass`` into (path, class_chain).

    pytest's JUnit classname encodes both the module path and the class
    hierarchy.  Module components are snake_case; class components are
    PascalCase (first letter uppercase).  Split at the first uppercase
    component so the file path and the class name are correctly separated.

    Examples::

        "tests.unit.test_foo"              → ("tests/unit/test_foo.py", "")
        "tests.unit.test_foo.TestBar"      → ("tests/unit/test_foo.py", "TestBar")
        "tests.unit.test_foo.TestBar.Inner"→ ("tests/unit/test_foo.py", "TestBar.Inner")
    """
    parts = classname.split(".")
    class_start = len(parts)
    for i, part in enumerate(parts):
        if part and part[0].isupper():
            class_start = i
            break
    path = "/".join(parts[:class_start]) + ".py" if parts[:class_start] else ""
    class_chain = ".".join(parts[class_start:])
    return path, class_chain


def _canonical_id(classname: str, name: str) -> str:
    """Build a canonical pytest node ID from JUnit classname + test name.

    Returns one of::

        path/to/test_file.py::test_name
        path/to/test_file.py::ClassName::test_name

    pytest encodes parametrized cases as ``name[param]``; we preserve that as-is.
    """
    if not classname:
        return name
    path, class_chain = _split_classname(classname)
    if not path:
        return f"{class_chain}::{name}" if class_chain else name
    if class_chain:
        return f"{path}::{class_chain}::{name}"
    return f"{path}::{name}"


def _outcome_of(testcase: ET.Element) -> str:
    skipped = testcase.find("skipped")
    if skipped is not None:
        return OUTCOME_SKIP
    if testcase.find("xpass") is not None:
        # Pytest may emit explicit xpass signals for xfails that unexpectedly passed.
        return OUTCOME_SKIP
    if testcase.find("xfail") is not None:
        return OUTCOME_SKIP
    if testcase.find("error") is not None:
        return OUTCOME_ERROR
    if testcase.find("failure") is not None:
        return OUTCOME_FAIL
    return OUTCOME_PASS


def parse_junit_xml(xml_path: Path) -> Dict[str, str]:
    """Parse a pytest JUnit XML file and return ``{test_id: outcome}``.

    Outcomes are one of: ``"pass"``, ``"fail"``, ``"error"``, ``"skip"``.

    Raises:
        JUnitParseError: if the file is absent, empty, or malformed.
    """
    if not xml_path.exists():
        raise JUnitParseError(f"JUnit XML not found: {xml_path}")

    text = xml_path.read_text(encoding="utf-8").strip()
    if not text:
        raise JUnitParseError(f"JUnit XML is empty: {xml_path}")

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise JUnitParseError(f"malformed JUnit XML at {xml_path}: {exc}") from exc

    # Support both <testsuites><testsuite>… and bare <testsuite>…
    suites = root.findall(".//testsuite") or ([root] if root.tag == "testsuite" else [])
    if not suites:
        raise JUnitParseError(f"no <testsuite> elements found in {xml_path}")

    results: Dict[str, str] = {}
    for suite in suites:
        for tc in suite.findall("testcase"):
            classname = (tc.get("classname") or "").strip()
            name = (tc.get("name") or "").strip()
            if not name:
                continue
            test_id = _canonical_id(classname, name)
            results[test_id] = _outcome_of(tc)

    return results
