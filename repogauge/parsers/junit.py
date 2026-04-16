"""Harness-facing JUnit parser bridge for pytest outputs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping

from swebench.harness.log_parsers.python import parse_log_pytest_v2

from repogauge.validation.junit_parser import (
    parse_junit_xml,
    parse_junit_xml_content,
)


def _existing_path_from_text(report: str) -> Path | None:
    report = report.strip()
    if not report or "\n" in report or "\r" in report:
        return None
    try:
        path = Path(report)
        if path.exists():
            return path
    except OSError:
        return None
    return None


def _parse_string_payload(report: str, test_spec: Any | None) -> Dict[str, str]:
    text = report.strip()
    if not text:
        return {}

    if text.startswith("<"):
        return parse_junit_xml_content(text)

    normalized = "\n".join(line.strip() for line in report.splitlines())
    return parse_log_pytest_v2(normalized, test_spec)


def parse_repogauge_junit(
    report: object, test_spec: Any | None = None
) -> Dict[str, str]:
    """Parse pytest JUnit output into ``{test_id: outcome}``.

    The harness may pass either a path to a JUnit XML file or raw XML text. In
    order to be resilient to harness calling conventions, dictionary inputs are
    also accepted, using common key names for payload-like objects.

    Args:
        report: Path/string containing XML content or a payload mapping.
        test_spec: Optional harness-provided spec object. Accepted for
            compatibility with SWE-bench's parser callback signature and
            otherwise ignored.

    Returns:
        A canonicalized mapping of test IDs to outcomes.

    Raises:
        TypeError: if the input type is not parseable.
        JUnitParseError: if the XML payload cannot be parsed.
    """
    if isinstance(report, Path):
        return parse_junit_xml(report)

    if isinstance(report, (bytes, bytearray)):
        return _parse_string_payload(report.decode("utf-8"), test_spec)

    if isinstance(report, str):
        path = _existing_path_from_text(report)
        if path is not None:
            return parse_junit_xml(path)
        return _parse_string_payload(report, test_spec)

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
            try:
                return parse_repogauge_junit(value, test_spec)
            except TypeError:
                continue

    raise TypeError(
        f"unsupported report payload for parser: {type(report).__name__}; "
        f"expected file path, XML content, or mapping"
    )
