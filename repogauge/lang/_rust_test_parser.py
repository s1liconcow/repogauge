"""Rust test output parser helpers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from repogauge.parsers.junit import register_parser
from repogauge.validation.junit_parser import OUTCOME_FAIL, OUTCOME_PASS, OUTCOME_SKIP

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _report_text(report: object) -> str:
    if isinstance(report, Path):
        try:
            return report.read_text(encoding="utf-8")
        except OSError:
            return ""
    if isinstance(report, (bytes, bytearray)):
        return report.decode("utf-8")
    if isinstance(report, str):
        text = report.strip()
        if not text:
            return ""
        if "\n" not in text and "\r" not in text:
            candidate = Path(text)
            if candidate.exists():
                return _report_text(candidate)
        return report
    if isinstance(report, dict):
        for key in ("output", "log", "result", "stdout", "stderr", "raw"):
            value = report.get(key)
            if value is not None:
                return _report_text(value)
    raise TypeError(f"unsupported report payload for parser: {type(report).__name__}")


def _crate_name_from_runner(line: str) -> str:
    match = re.search(r"/([^/\s]+)-[0-9a-f]{4,}\)?$", line)
    if match:
        return match.group(1)
    return ""


def parse_cargo_human(report: object, test_spec: Any | None = None) -> dict[str, str]:
    del test_spec
    text = _ANSI_RE.sub("", _report_text(report))
    results: dict[str, str] = {}
    current_crate = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("Running unittests "):
            current_crate = _crate_name_from_runner(line)
            continue
        if line.startswith("Doc-tests "):
            current_crate = line.split("Doc-tests ", 1)[1].strip()
            continue

        match = re.match(r"test\s+(.+?)\s+\.\.\.\s+(ok|FAILED|ignored)$", line)
        if not match:
            continue
        test_name = match.group(1).strip()
        status = match.group(2)
        if test_name.startswith("src/") and "(line " in test_name:
            path_part, _, line_part = test_name.partition(" (line ")
            if " - " in path_part:
                path_part = path_part.split(" - ", 1)[0].strip()
            line_number = line_part.rstrip(")")
            test_name = f"{path_part}::doctest_{line_number}"
        test_id = f"{current_crate}::{test_name}" if current_crate else test_name
        if status == "ok":
            results[test_id] = OUTCOME_PASS
        elif status == "FAILED":
            results[test_id] = OUTCOME_FAIL
        elif status == "ignored":
            results[test_id] = OUTCOME_SKIP
    return results


register_parser("cargo_human", parse_cargo_human)


__all__ = ["parse_cargo_human"]
