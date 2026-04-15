"""LLM abstraction and advisory triage schema utilities.

This module intentionally keeps LLM usage advisory only: malformed records are
silently dropped and do not alter deterministic review decisions.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

TRIAGE_SCHEMA_VERSION = "0.1.0"

ADVISORY_STATES = {"open", "accepted", "rejected"}
KNOWN_FILE_ROLES = set[str](
    (
        "prod",
        "test",
        "test_support",
        "config_build",
        "docs",
        "generated_vendor",
        "unknown",
    )
)


def _now_iso8601() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"


def _coerce_string(value: Any) -> str | None:
    if isinstance(value, str):
        candidate = value.strip()
        return candidate or None
    return None


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


@dataclass
class LlmModelSpec:
    """String-based model descriptor with optional usage/cost/cache references."""

    model_name: str
    provider: str
    prompt_version: str
    usage: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)
    request_ref: str | None = None
    response_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TriageSuggestion:
    candidate_id: str
    state: str | None = None
    reason: str | None = None
    reviewer_notes: str | None = None
    suggested_problem_statement: str | None = None
    suggested_file_roles: dict[str, list[str]] = field(default_factory=dict)
    confidence: float | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return {key: value for key, value in payload.items() if value is not None}


def _coerce_file_roles(value: Any) -> dict[str, list[str]] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, list[str]] = {}
    for role, files in value.items():
        if not isinstance(role, str) or role not in KNOWN_FILE_ROLES:
            return None
        if not isinstance(files, list):
            return None
        normalized_files = [str(path).strip() for path in files if str(path).strip()]
        result[role] = sorted(set(normalized_files))
    return result


def _coerce_state(value: Any) -> str | None:
    if value is None:
        return None
    candidate = _coerce_string(value)
    if candidate is None:
        return None
    lower = candidate.lower()
    if lower in ADVISORY_STATES:
        return lower
    return None


def parse_model_spec(
    payload: Any,
    *,
    default_name: str,
    default_provider: str,
    default_prompt_version: str,
) -> LlmModelSpec:
    if not isinstance(payload, Mapping):
        return LlmModelSpec(
            model_name=default_name,
            provider=default_provider,
            prompt_version=default_prompt_version,
        )

    model_name = _coerce_string(payload.get("model_name") or payload.get("model")) or default_name
    provider = _coerce_string(payload.get("provider")) or default_provider
    prompt_version = _coerce_string(payload.get("prompt_version")) or default_prompt_version
    usage = payload.get("usage")
    cost = payload.get("cost")
    if not isinstance(usage, dict):
        usage = {}
    if not isinstance(cost, dict):
        cost = {}

    request_ref = _coerce_string(payload.get("request_ref"))
    response_ref = _coerce_string(payload.get("response_ref"))

    return LlmModelSpec(
        model_name=model_name,
        provider=provider,
        prompt_version=prompt_version,
        usage=usage,
        cost=cost,
        request_ref=request_ref,
        response_ref=response_ref,
    )


def _coerce_suggestion(record: Mapping[str, Any]) -> TriageSuggestion | None:
    candidate_id = _coerce_string(record.get("candidate_id") or record.get("id"))
    if not candidate_id:
        return None

    state = _coerce_state(record.get("state") or record.get("recommended_state") or record.get("advisory_state"))
    reason = _coerce_string(record.get("reason"))
    reviewer_notes = _coerce_string(record.get("reviewer_notes"))
    suggested_problem_statement = _coerce_string(record.get("suggested_problem_statement"))
    raw_file_roles = record.get("suggested_file_roles", record.get("file_roles"))
    if raw_file_roles is not None:
        suggested_file_roles = _coerce_file_roles(raw_file_roles)
        if suggested_file_roles is None:
            return None
    else:
        suggested_file_roles = {}

    confidence = _coerce_float(record.get("confidence"))

    if (record.get("state") is not None and state is None) or (
        record.get("recommended_state") is not None and _coerce_state(record.get("recommended_state")) is None
    ):
        return None
    if (record.get("advisory_state") is not None and _coerce_state(record.get("advisory_state")) is None):
        return None

    return TriageSuggestion(
        candidate_id=candidate_id,
        state=state,
        reason=reason,
        reviewer_notes=reviewer_notes,
        suggested_problem_statement=suggested_problem_statement,
        suggested_file_roles=suggested_file_roles,
        confidence=confidence,
    )


def _coerce_records(payload: Any) -> tuple[LlmModelSpec, list[TriageSuggestion]]:
    default_prompt_version = "triage/default"
    if isinstance(payload, dict):
        model_payload = payload.get("model")
        model = parse_model_spec(
            model_payload,
            default_name=_coerce_string(payload.get("model_name")) or "local-default",
            default_provider=_coerce_string(payload.get("provider")) or "local",
            default_prompt_version=_coerce_string(payload.get("prompt_version")) or default_prompt_version,
        )
        candidates_payload = payload.get("candidates")
        if candidates_payload is None:
            if "candidate_id" in payload or "id" in payload:
                candidates_payload = [payload]
            else:
                candidate_records = []
                for maybe_id, maybe_value in payload.items():
                    if not isinstance(maybe_id, str) or not maybe_id or not isinstance(maybe_value, Mapping):
                        continue
                    if maybe_id in {"schema_version", "model", "generated_at", "model_name", "provider", "prompt_version"}:
                        continue
                    candidate_value = dict(maybe_value)
                    candidate_value["candidate_id"] = maybe_id
                    candidate_records.append(candidate_value)
                candidates_payload = candidate_records
        if not isinstance(candidates_payload, list):
            return model, []
        records = []
        for raw_record in candidates_payload:
            if not isinstance(raw_record, Mapping):
                continue
            parsed = _coerce_suggestion(raw_record)
            if parsed is not None:
                records.append(parsed)
        return model, records

    if isinstance(payload, list):
        model = parse_model_spec(
            None,
            default_name="local-default",
            default_provider="local",
            default_prompt_version=default_prompt_version,
        )
        records = []
        for raw_record in payload:
            if not isinstance(raw_record, Mapping):
                continue
            parsed = _coerce_suggestion(raw_record)
            if parsed is not None:
                records.append(parsed)
        return model, records

    model = parse_model_spec(
        None,
        default_name="local-default",
        default_provider="local",
        default_prompt_version=default_prompt_version,
    )
    return model, []


def parse_triage_payload(payload: Any, *, default_name: str, default_provider: str) -> tuple[LlmModelSpec, dict[str, TriageSuggestion]]:
    model, records = _coerce_records(payload)
    hints: dict[str, TriageSuggestion] = {}
    for suggestion in records:
        if suggestion.candidate_id not in hints:
            hints[suggestion.candidate_id] = suggestion
    if default_name and (not model.model_name or model.model_name == "local-default"):
        model.model_name = default_name
    if default_provider and (not model.provider or model.provider == "local"):
        model.provider = default_provider
    return model, hints


def _load_raw_triage_text(path: Path) -> str | None:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return None
    return text


def load_triage_payload(
    path: Path,
    *,
    default_name: str,
    default_provider: str,
) -> tuple[LlmModelSpec, dict[str, TriageSuggestion]]:
    raw = _load_raw_triage_text(path)
    if raw is None:
        return parse_model_spec(
            None,
            default_name=default_name,
            default_provider=default_provider,
            default_prompt_version="triage/default",
        ), {}

    payload: Any
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        records = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, Mapping):
                records.append(row)
        payload = {"candidates": records}
    model, hints = parse_triage_payload(payload, default_name=default_name, default_provider=default_provider)
    return model, hints


def _sorted_hints(hints: Iterable[TriageSuggestion]) -> list[dict[str, Any]]:
    return [hint.to_dict() for hint in sorted(hints, key=lambda item: item.candidate_id)]


def write_triage_payload(path: Path, model: LlmModelSpec, hints: Mapping[str, TriageSuggestion]) -> None:
    if not hints:
        return
    payload = {
        "schema_version": TRIAGE_SCHEMA_VERSION,
        "generated_at": _now_iso8601(),
        "model": model.to_dict(),
        "candidates": _sorted_hints(hints.values()),
    }
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
