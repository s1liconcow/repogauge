"""Harness-facing JUnit parser bridge for pytest outputs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping

from repogauge.validation.junit_parser import (
    parse_junit_xml,
    parse_junit_xml_content,
)


def parse_repogauge_junit(report: object) -> Dict[str, str]:
    """Parse pytest JUnit output into ``{test_id: outcome}``.

    The harness may pass either a path to a JUnit XML file or raw XML text. In
    order to be resilient to harness calling conventions, dictionary inputs are
    also accepted, using common key names for payload-like objects.

    Args:
        report: Path/string containing XML content or a payload mapping.

    Returns:
        A canonicalized mapping of test IDs to outcomes.

    Raises:
        TypeError: if the input type is not parseable.
        JUnitParseError: if the XML payload cannot be parsed.
    """
    if isinstance(report, Path):
        return parse_junit_xml(report)

    if isinstance(report, (bytes, bytearray)):
        return parse_junit_xml_content(report.decode("utf-8"))

    if isinstance(report, str):
        path = Path(report)
        if path.exists():
            return parse_junit_xml(path)
        return parse_junit_xml_content(report)

    if isinstance(report, Mapping):
        candidate_keys = (
            "junit_xml",
            "junit_xml_path",
            "junit_xml_file",
            "output",
            "log",
            "result",
            "stdout",
            "stderr",
            "raw",
        )
        for key in candidate_keys:
            value = report.get(key)
            if value is None:
                continue
            return parse_repogauge_junit(value)

    raise TypeError(
        f"unsupported report payload for parser: {type(report).__name__}; "
        f"expected file path, XML content, or mapping"
    )
