"""Go test output parser helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from repogauge.parsers.junit import register_parser
from repogauge.validation.junit_parser import OUTCOME_FAIL, OUTCOME_PASS, OUTCOME_SKIP


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


def parse_go_test_json(report: object, test_spec: Any | None = None) -> dict[str, str]:
    del test_spec
    text = _report_text(report)
    results: dict[str, str] = {}
    for line in text.splitlines():
        value = line.strip()
        if not value:
            continue
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        test_name = payload.get("Test")
        package_name = payload.get("Package")
        action = str(payload.get("Action") or "").strip().lower()
        if not isinstance(test_name, str) or not test_name.strip():
            continue
        if not isinstance(package_name, str) or not package_name.strip():
            continue
        if action == "pass":
            results[f"{package_name}::{test_name.strip()}"] = OUTCOME_PASS
        elif action == "fail":
            results[f"{package_name}::{test_name.strip()}"] = OUTCOME_FAIL
        elif action == "skip":
            results[f"{package_name}::{test_name.strip()}"] = OUTCOME_SKIP
    return results


register_parser("go_json", parse_go_test_json)


__all__ = ["parse_go_test_json"]
