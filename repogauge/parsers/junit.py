"""Harness-facing test-output parser bridge."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Dict, Mapping

from repogauge.validation.junit_parser import (
    parse_junit_xml,
    parse_junit_xml_content,
)

ParserFn = Callable[[object, Any | None], Dict[str, str]]

_PARSER_REGISTRY: dict[str, ParserFn] = {}


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


def _parse_pytest_log_payload(report: str, test_spec: Any | None) -> Dict[str, str]:
    parse_log_pytest_v2 = import_module(
        "swebench.harness.log_parsers.python"
    ).parse_log_pytest_v2
    text = report.strip()
    if not text:
        return {}

    if text.startswith("<"):
        return parse_junit_xml_content(text)

    normalized = "\n".join(line.strip() for line in report.splitlines())
    return parse_log_pytest_v2(normalized, test_spec)


def _normalize_parser_name(parser_name: str) -> str:
    normalized = parser_name.strip().lower()
    if not normalized:
        raise KeyError("unknown test parser: ''")
    return normalized


def register_parser(name: str, parser: ParserFn) -> None:
    normalized = _normalize_parser_name(name)
    if normalized in _PARSER_REGISTRY:
        raise ValueError(f"test parser already registered: {normalized}")
    _PARSER_REGISTRY[normalized] = parser


def get_parser(name: str) -> ParserFn:
    normalized = _normalize_parser_name(name)
    try:
        return _PARSER_REGISTRY[normalized]
    except KeyError as exc:
        raise KeyError(f"unknown test parser: {name!r}") from exc


def _parse_test_output_for_name(
    report: object, test_spec: Any | None, parser_name: str
) -> Dict[str, str]:
    parser = get_parser(parser_name)
    return parser(report, test_spec)


def _parse_junit_output(report: object, test_spec: Any | None) -> Dict[str, str]:
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
        return _parse_pytest_log_payload(report.decode("utf-8"), test_spec)

    if isinstance(report, str):
        path = _existing_path_from_text(report)
        if path is not None:
            return parse_junit_xml(path)
        return _parse_pytest_log_payload(report, test_spec)

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
                return _parse_junit_output(value, test_spec)
            except TypeError:
                continue

    raise TypeError(
        f"unsupported report payload for parser: {type(report).__name__}; "
        f"expected file path, XML content, or mapping"
    )


def parse_repogauge_test_output(
    report: object,
    test_spec: Any | None = None,
    *,
    parser_name: str = "junit",
) -> Dict[str, str]:
    """Dispatch test-output parsing by parser name."""
    return _parse_test_output_for_name(report, test_spec, parser_name)


def parse_repogauge_junit(
    report: object, test_spec: Any | None = None
) -> Dict[str, str]:
    """Parse pytest JUnit output into ``{test_id: outcome}``."""
    return parse_repogauge_test_output(report, test_spec, parser_name="junit")


register_parser("junit", _parse_junit_output)
