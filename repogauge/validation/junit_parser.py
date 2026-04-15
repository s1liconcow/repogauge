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


def _classname_to_path(classname: str) -> str:
    """Convert ``tests.unit.test_foo`` → ``tests/unit/test_foo.py``."""
    parts = classname.split(".")
    # Heuristic: if any component starts with "test_" or equals "tests",
    # assume it is a file/directory component and convert dots to slashes.
    return "/".join(parts) + ".py"


def _canonical_id(classname: str, name: str) -> str:
    """Build a ``path::name`` or ``path::class::name`` test ID.

    pytest encodes parametrized cases as ``name[param]``; we preserve that as-is.
    """
    path_part = _classname_to_path(classname) if classname else ""
    if path_part:
        return f"{path_part}::{name}"
    return name


def _outcome_of(testcase: ET.Element) -> str:
    if testcase.find("skipped") is not None:
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
