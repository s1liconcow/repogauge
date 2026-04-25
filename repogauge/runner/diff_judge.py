"""Advisory LLM judge for candidate diffs versus golden patches."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, TextIO

from repogauge.config import DiffJudgeRow
from repogauge.exec import run_command
from repogauge.llm import LlmModelSpec
from repogauge.runner.adapters import (
    SolverAdapterError,
    _codex_cli_env,
    _coerce_mapping,
    _extract_text_candidates,
    _parse_usage_cost_with_source,
    _parse_json_lines,
    _post_json,
)
from repogauge.runner.workspaces import _prepare_codex_home

JUDGE_PROMPT_VERSION = "diff_judge/v1"
JUDGE_ARTIFACT_FILENAME = "llm_judge.jsonl"
JUDGE_MAX_OUTPUT_TOKENS = 1600

JUDGE_DIMENSIONS: tuple[dict[str, Any], ...] = (
    {
        "name": "task_fit",
        "weight": 0.30,
        "description": "Does the candidate address the stated task as well as or better than the gold patch?",
    },
    {
        "name": "correctness_safety",
        "weight": 0.25,
        "description": "Semantic correctness, regression risk, and edge-case safety.",
    },
    {
        "name": "maintainability",
        "weight": 0.20,
        "description": "Clarity, cohesion, readability, and long-term maintainability.",
    },
    {
        "name": "test_quality",
        "weight": 0.15,
        "description": "Quality of tests or testing posture relative to the gold solution.",
    },
    {
        "name": "change_focus",
        "weight": 0.10,
        "description": "Scope discipline and absence of unrelated churn.",
    },
)

LOCAL_ONLY_PROVIDERS = {
    "local",
    "local-only",
    "offline",
    "builtin",
    "built-in",
    "internal",
    "codex",
    "codex-cli",
}
_OPENAI_RESPONSES_PROVIDERS = {"openai", "openai-responses"}
_OPENAI_COMPATIBLE_PROVIDERS = {
    "openai-compatible",
    "openai_compatible",
    "local",
    "local-only",
    "offline",
    "builtin",
    "built-in",
    "internal",
}
_ANTHROPIC_PROVIDERS = {"anthropic", "anthropic-api", "claude"}
_LABEL_BY_DELTA = {
    -2: "much_worse",
    -1: "worse",
    0: "same",
    1: "better",
    2: "much_better",
}
_DELTA_BY_LABEL = {value: key for key, value in _LABEL_BY_DELTA.items()}


@dataclass(frozen=True)
class DiffJudgeRunResult:
    rows_path: str
    rows: list[dict[str, Any]]
    report: dict[str, Any]
    model: dict[str, Any]


class _JudgeProgressReporter:
    def __init__(self, total: int, stream: TextIO | None) -> None:
        self._total = max(total, 0)
        self._stream = stream
        self._is_tty = bool(stream and hasattr(stream, "isatty") and stream.isatty())
        self._last_line_open = False

    def _render(self, completed: int, message: str) -> None:
        if self._stream is None or self._total <= 0:
            return
        bounded_completed = min(max(completed, 0), self._total)
        width = 20
        filled = int(width * bounded_completed / self._total)
        bar = "#" * filled + "-" * (width - filled)
        line = (
            f"repogauge analyze: llm judge [{bar}] "
            f"{bounded_completed}/{self._total} {message}"
        )
        if self._is_tty:
            print(f"\r{line}", end="", file=self._stream, flush=True)
            self._last_line_open = True
            return
        print(line, file=self._stream, flush=True)

    def start(self, *, completed: int, solver_id: str, instance_id: str) -> None:
        self._render(
            completed,
            f"calling judge for {solver_id} {instance_id}",
        )

    def finish(
        self,
        *,
        completed: int,
        solver_id: str,
        instance_id: str,
        status: str,
    ) -> None:
        self._render(
            completed,
            f"{status} {solver_id} {instance_id}",
        )

    def close(self) -> None:
        if self._stream is None or not self._last_line_open:
            return
        print(file=self._stream, flush=True)
        self._last_line_open = False


def _normalize_provider(value: Any) -> str:
    candidate = str(value or "codex").strip().lower().replace("_", "-")
    return candidate or "codex"


def _default_model_name(provider: str) -> str:
    if provider in {"codex", "codex-cli"}:
        return "gpt-5.5"
    if provider in _ANTHROPIC_PROVIDERS:
        return "opus-4.6"
    if provider in LOCAL_ONLY_PROVIDERS:
        return "local-judge"
    if provider in _OPENAI_COMPATIBLE_PROVIDERS:
        return "gpt-4o-mini"
    return "gpt-5.4-mini"


def _provider_family(provider: str) -> str:
    if provider in {"codex", "codex-cli"}:
        return "codex_cli"
    if provider in _ANTHROPIC_PROVIDERS:
        return "anthropic"
    if provider in _OPENAI_RESPONSES_PROVIDERS:
        return "openai_responses"
    return "openai_compatible"


def validate_llm_judge_policy(*, llm_mode: str | None, provider: str) -> None:
    mode = str(llm_mode or "off").strip().lower()
    if mode == "off":
        raise ValueError(
            "LLM Judge requires --llm-mode local_only or --llm-mode allow_remote"
        )
    if mode == "local_only" and provider not in LOCAL_ONLY_PROVIDERS:
        raise ValueError(
            f"remote provider '{provider}' requires --llm-mode allow_remote"
        )


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "resolved", "passed"}
    if isinstance(value, (int, float)):
        return value != 0
    return False


def _coerce_text(value: Any) -> str:
    return "" if value is None else str(value)


def _coerce_attempt_index(row: Mapping[str, Any]) -> int:
    attempt_id = _coerce_text(row.get("attempt_id"))
    match = re.search(r":attempt-(\d+)$", attempt_id)
    if match is not None:
        try:
            return int(match.group(1))
        except ValueError:
            return 0
    return 0


def _label_from_overall_delta(delta: float) -> str:
    if delta <= -1.25:
        return "much_worse"
    if delta <= -0.35:
        return "worse"
    if delta < 0.35:
        return "same"
    if delta < 1.25:
        return "better"
    return "much_better"


def _stable_json_dumps(payload: Mapping[str, Any]) -> str:
    return json.dumps(dict(payload), sort_keys=True, separators=(",", ":"))


def _cache_key(model: LlmModelSpec, prompt_payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        _stable_json_dumps(
            {
                "prompt_version": model.prompt_version,
                "provider": model.provider,
                "model_name": model.model_name,
                "prompt": dict(prompt_payload),
            }
        ).encode("utf-8")
    ).hexdigest()


def _safe_artifact_name(attempt_id: str, cache_key: str) -> str:
    stem = "".join(
        char if char.isalnum() or char in {"-", "_", ":"} else "-"
        for char in attempt_id
    ).strip("-")
    if not stem:
        stem = "judge"
    return f"{stem}-{cache_key[:12]}"


def _extract_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
    candidates = [stripped]
    fenced = re.findall(r"```(?:json)?\s*(.*?)```", stripped, flags=re.S | re.I)
    candidates.extend(candidate.strip() for candidate in fenced if candidate.strip())
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _parse_dimension(
    payload: Mapping[str, Any], *, spec: Mapping[str, Any]
) -> dict[str, Any]:
    name = _coerce_text(payload.get("name") or spec["name"]).strip()
    raw_delta = payload.get("delta")
    label = _coerce_text(payload.get("label")).strip().lower()
    if label in _DELTA_BY_LABEL:
        delta = _DELTA_BY_LABEL[label]
    else:
        try:
            delta = int(raw_delta)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"dimension '{name}' missing delta") from exc
    if delta not in _LABEL_BY_DELTA:
        raise ValueError(f"dimension '{name}' delta must be one of -2,-1,0,1,2")
    return {
        "name": spec["name"],
        "weight": float(spec["weight"]),
        "delta": delta,
        "label": _LABEL_BY_DELTA[delta],
        "rationale": _coerce_text(payload.get("rationale")).strip(),
    }


def _parse_dimensions(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise ValueError("judge response missing dimensions")
    by_name = {
        _coerce_text(item.get("name")).strip(): item
        for item in payload
        if isinstance(item, Mapping)
    }
    parsed: list[dict[str, Any]] = []
    for spec in JUDGE_DIMENSIONS:
        name = _coerce_text(spec["name"])
        candidate = by_name.get(name)
        if candidate is None:
            raise ValueError(f"judge response missing dimension '{name}'")
        parsed.append(_parse_dimension(candidate, spec=spec))
    return parsed


def _overall_delta(dimensions: Iterable[Mapping[str, Any]]) -> float:
    total = 0.0
    for dimension in dimensions:
        total += float(dimension.get("weight", 0.0)) * float(dimension.get("delta", 0))
    return round(total, 6)


def _coerce_summary(payload: Mapping[str, Any]) -> str:
    for key in ("summary", "overall_summary", "rationale"):
        text = _coerce_text(payload.get(key)).strip()
        if text:
            return text
    return "Judge output did not include a summary."


def _resolve_model(provider: str, model_name: str | None) -> LlmModelSpec:
    return LlmModelSpec(
        model_name=(model_name or _default_model_name(provider)).strip()
        or _default_model_name(provider),
        provider=provider,
        prompt_version=JUDGE_PROMPT_VERSION,
    )


def _judge_request_payload(
    *,
    row: Mapping[str, Any],
    dataset_row: Mapping[str, Any],
    model: LlmModelSpec,
) -> dict[str, Any]:
    return {
        "prompt_version": model.prompt_version,
        "instance_id": _coerce_text(row.get("instance_id")),
        "solver_id": _coerce_text(row.get("solver_id")),
        "problem_statement": _coerce_text(
            dataset_row.get("problem_statement") or row.get("problem_statement")
        ),
        "harness_outcome": _coerce_text(row.get("harness_outcome")),
        "resolved": _safe_bool(row.get("resolved")),
        "attempt_state": _coerce_text(row.get("attempt_state")),
        "gold_prod_patch": _coerce_text(dataset_row.get("patch")),
        "gold_test_patch": _coerce_text(
            dataset_row.get("test_patch")
            or dataset_row.get("PASS_TO_PASS")
            or dataset_row.get("FAIL_TO_PASS")
        ),
        "candidate_patch": _coerce_text(row.get("model_patch")),
        "rubric": list(JUDGE_DIMENSIONS),
    }


def _judge_prompt(prompt_payload: Mapping[str, Any]) -> str:
    rubric = "\n".join(
        f"- {item['name']} ({item['weight']:.2f}): {item['description']}"
        for item in JUDGE_DIMENSIONS
    )
    return (
        "You are an expert senior engineer reviewing a candidate code diff against a"
        " repository-specific golden fix.\n"
        "Return JSON only, with no markdown fence and no extra prose.\n"
        "Score each rubric dimension relative to the gold patch using delta values"
        " -2,-1,0,1,2 where negative means worse than gold and positive means better"
        " than gold.\n"
        "Gold is the reference implementation, not a ceiling. If the candidate is"
        " genuinely cleaner, safer, or better tested, mark it better.\n"
        "Favor task fit and correctness over style.\n\n"
        "Required JSON shape:\n"
        "{"
        '"summary":"short text",'
        '"confidence":0.0,'
        '"dimensions":['
        '{"name":"task_fit","delta":0,"label":"same","rationale":"..."},'
        '{"name":"correctness_safety","delta":0,"label":"same","rationale":"..."},'
        '{"name":"maintainability","delta":0,"label":"same","rationale":"..."},'
        '{"name":"test_quality","delta":0,"label":"same","rationale":"..."},'
        '{"name":"change_focus","delta":0,"label":"same","rationale":"..."}'
        "]}\n\n"
        f"Rubric:\n{rubric}\n\n"
        f"Task:\n{_coerce_text(prompt_payload.get('problem_statement'))}\n\n"
        f"Harness outcome: {_coerce_text(prompt_payload.get('harness_outcome'))}\n"
        f"Resolved: {_coerce_text(prompt_payload.get('resolved'))}\n"
        f"Attempt state: {_coerce_text(prompt_payload.get('attempt_state'))}\n\n"
        "Golden production patch:\n"
        f"{_coerce_text(prompt_payload.get('gold_prod_patch'))}\n\n"
        "Golden test patch:\n"
        f"{_coerce_text(prompt_payload.get('gold_test_patch'))}\n\n"
        "Candidate patch:\n"
        f"{_coerce_text(prompt_payload.get('candidate_patch'))}\n"
    )


def _extract_response_text(provider: str, payload: Mapping[str, Any]) -> str:
    family = _provider_family(provider)
    if family == "codex_cli":
        output_text = _coerce_text(payload.get("output_text")).strip()
        if output_text:
            return output_text
    if family == "anthropic":
        text_candidates = _extract_text_candidates(payload)
        return "\n".join(text_candidates).strip()
    if family == "openai_responses":
        output_text = payload.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()
    if family == "openai_compatible":
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, Mapping):
                message = first.get("message")
                if isinstance(message, Mapping):
                    content = message.get("content")
                    if isinstance(content, str) and content.strip():
                        return content.strip()
    text_candidates = _extract_text_candidates(payload)
    return "\n".join(text_candidates).strip()


def _codex_cli_output_text(raw_output: str) -> str:
    parsed = _parse_json_lines(raw_output)
    for event in reversed(parsed):
        for candidate in reversed(_extract_text_candidates(event)):
            if _extract_json_object(candidate) is not None:
                return candidate.strip()
    return raw_output.strip()


def _prepare_codex_cli_home(home_root: Path) -> dict[str, str]:
    _prepare_codex_home(home_root)
    (home_root / ".config").mkdir(parents=True, exist_ok=True)
    return _codex_cli_env(home_root)


def _invoke_model(
    *,
    model: LlmModelSpec,
    prompt: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str, dict[str, Any], str]:
    provider = _normalize_provider(model.provider)
    family = _provider_family(provider)
    request_payload: dict[str, Any]
    if family == "anthropic":
        base_url = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise SolverAdapterError("missing ANTHROPIC_API_KEY")
        request_payload = {
            "model": model.model_name,
            "max_tokens": JUDGE_MAX_OUTPUT_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        }
        response_payload = _coerce_mapping(
            _post_json(
                url=urllib.parse.urljoin(base_url.rstrip("/") + "/", "v1/messages"),
                payload=request_payload,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
                timeout_seconds=120,
            ),
            field_name="response",
        )
    elif family == "codex_cli":
        command = [
            "codex",
            "--ask-for-approval",
            "never",
            "exec",
            "-c",
            "notify=[]",
            "-c",
            "mcp_servers={}",
            "--json",
            "--color",
            "never",
            "--sandbox",
            "danger-full-access",
            "--ephemeral",
            "--skip-git-repo-check",
            "--model",
            model.model_name,
        ]
        request_payload = {
            "command": command,
            "provider_family": family,
            "model": model.model_name,
        }
        home_root = Path.cwd() / ".repogauge" / "judge-codex-home"
        command_env = _prepare_codex_cli_home(home_root)
        command_result = run_command(
            command,
            cwd=str(Path.cwd()),
            env=command_env,
            input_text=prompt,
            timeout_seconds=120,
        )
        if command_result.timed_out:
            raise SolverAdapterError(
                f"codex exec timed out: {command_result.stderr or 'timed out'}"
            )
        if not command_result.success:
            raise SolverAdapterError(
                command_result.stderr or command_result.stdout or "codex exec failed"
            )
        raw_output = command_result.stdout or ""
        response_payload = {
            "raw_output": raw_output,
            "output_text": _codex_cli_output_text(raw_output),
        }
    elif family == "openai_responses":
        base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise SolverAdapterError("missing OPENAI_API_KEY")
        request_payload = {
            "model": model.model_name,
            "input": prompt,
            "max_output_tokens": JUDGE_MAX_OUTPUT_TOKENS,
        }
        response_payload = _coerce_mapping(
            _post_json(
                url=urllib.parse.urljoin(base_url.rstrip("/") + "/", "responses"),
                payload=request_payload,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout_seconds=120,
            ),
            field_name="response",
        )
    else:
        base_url = os.getenv(
            "REPOGAUGE_LLM_JUDGE_BASE_URL",
            os.getenv("OPENAI_BASE_URL", "http://localhost:11434/v1"),
        )
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        request_payload = {
            "model": model.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": JUDGE_MAX_OUTPUT_TOKENS,
        }
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        response_payload = _coerce_mapping(
            _post_json(
                url=urllib.parse.urljoin(
                    base_url.rstrip("/") + "/",
                    "chat/completions",
                ),
                payload=request_payload,
                headers=headers,
                timeout_seconds=120,
            ),
            field_name="response",
        )

    usage, usage_source, cost, cost_source = _parse_usage_cost_with_source(
        response_payload
    )
    return request_payload, response_payload, usage, usage_source, cost, cost_source


def _judge_row_from_response(
    *,
    row: Mapping[str, Any],
    model: LlmModelSpec,
    response_text: str,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    parsed = _extract_json_object(response_text)
    if parsed is None:
        raise ValueError("judge returned non-JSON text")
    dimensions = _parse_dimensions(parsed.get("dimensions"))
    overall_delta = _overall_delta(dimensions)
    return DiffJudgeRow(
        attempt_id=_coerce_text(row.get("attempt_id")),
        job_id=_coerce_text(row.get("job_id")),
        instance_id=_coerce_text(row.get("instance_id")),
        solver_id=_coerce_text(row.get("solver_id")),
        resolved=_safe_bool(row.get("resolved")),
        harness_outcome=_coerce_text(row.get("harness_outcome") or "unknown"),
        attempt_state=_coerce_text(row.get("attempt_state") or "unknown"),
        overall_delta=overall_delta,
        overall_label=_label_from_overall_delta(overall_delta),
        confidence=max(0.0, min(1.0, _safe_float(parsed.get("confidence"), 0.0))),
        summary=_coerce_summary(parsed),
        dimensions=dimensions,
        metadata=dict(
            metadata,
            judge_status="scored",
            model=model.to_dict(),
        ),
    ).to_dict()


def _fallback_row(
    *,
    row: Mapping[str, Any],
    model: LlmModelSpec,
    metadata: Mapping[str, Any],
    error: str,
) -> dict[str, Any]:
    return DiffJudgeRow(
        attempt_id=_coerce_text(row.get("attempt_id")),
        job_id=_coerce_text(row.get("job_id")),
        instance_id=_coerce_text(row.get("instance_id")),
        solver_id=_coerce_text(row.get("solver_id")),
        resolved=_safe_bool(row.get("resolved")),
        harness_outcome=_coerce_text(row.get("harness_outcome") or "unknown"),
        attempt_state=_coerce_text(row.get("attempt_state") or "unknown"),
        overall_delta=0.0,
        overall_label="same",
        confidence=0.0,
        summary="Judge output unavailable for this attempt.",
        dimensions=[],
        metadata=dict(
            metadata,
            judge_status="error",
            error=error,
            model=model.to_dict(),
        ),
    ).to_dict()


def _load_existing_rows(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    if not path.exists():
        return {}
    loaded: dict[tuple[str, str], dict[str, Any]] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        attempt_id = _coerce_text(row.get("attempt_id"))
        cache_key = _coerce_text(row.get("metadata", {}).get("cache_key"))
        judge_status = _coerce_text(row.get("metadata", {}).get("judge_status"))
        if attempt_id and cache_key and judge_status == "scored":
            loaded[(attempt_id, cache_key)] = row
    return loaded


def _write_rows(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(
        rows,
        key=lambda row: (
            _coerce_text(row.get("instance_id")),
            _coerce_text(row.get("solver_id")),
            _coerce_text(row.get("attempt_id")),
        ),
    )
    path.write_text(
        "".join(json.dumps(dict(row), sort_keys=True) + "\n" for row in ordered),
        encoding="utf-8",
    )


def _latest_rows_by_job(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for row in rows:
        job_id = _coerce_text(row.get("job_id")) or (
            f"{_coerce_text(row.get('solver_id'))}:{_coerce_text(row.get('instance_id'))}"
        )
        candidate = dict(row)
        existing = selected.get(job_id)
        if existing is None:
            selected[job_id] = candidate
            continue
        candidate_key = (
            _coerce_attempt_index(candidate),
            _coerce_text(candidate.get("attempt_id")),
        )
        existing_key = (
            _coerce_attempt_index(existing),
            _coerce_text(existing.get("attempt_id")),
        )
        if candidate_key >= existing_key:
            selected[job_id] = candidate
    return sorted(
        selected.values(),
        key=lambda row: (
            _coerce_text(row.get("instance_id")),
            _coerce_text(row.get("solver_id")),
            _coerce_text(row.get("attempt_id")),
        ),
    )


def _sample_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "attempt_id": _coerce_text(row.get("attempt_id")),
        "instance_id": _coerce_text(row.get("instance_id")),
        "solver_id": _coerce_text(row.get("solver_id")),
        "resolved": _safe_bool(row.get("resolved")),
        "harness_outcome": _coerce_text(row.get("harness_outcome")),
        "attempt_state": _coerce_text(row.get("attempt_state")),
        "overall_delta": _safe_float(row.get("overall_delta")),
        "overall_label": _coerce_text(row.get("overall_label")),
        "confidence": _safe_float(row.get("confidence")),
        "summary": _coerce_text(row.get("summary")),
    }


def build_diff_judge_report(
    rows: list[dict[str, Any]],
    *,
    model: Mapping[str, Any],
) -> dict[str, Any]:
    latest_rows = _latest_rows_by_job(rows)
    scored_rows = [
        row
        for row in latest_rows
        if _coerce_text(row.get("metadata", {}).get("judge_status")) == "scored"
    ]
    error_rows = [
        row
        for row in latest_rows
        if _coerce_text(row.get("metadata", {}).get("judge_status")) != "scored"
    ]
    if not latest_rows:
        return {
            "enabled": True,
            "model": dict(model),
            "top_line": {
                "judged_attempt_count": 0,
                "judged_job_count": 0,
                "scored_job_count": 0,
                "error_job_count": 0,
                "avg_overall_delta": 0.0,
            },
            "solver_rows": [],
            "dimension_rows": [],
            "resolved_but_worse_than_gold": [],
            "unresolved_but_promising": [],
            "best_samples": [],
            "worst_samples": [],
        }

    better_count = sum(
        1
        for row in scored_rows
        if _coerce_text(row.get("overall_label")) in {"better", "much_better"}
    )
    worse_count = sum(
        1
        for row in scored_rows
        if _coerce_text(row.get("overall_label")) in {"worse", "much_worse"}
    )
    cache_hit_count = sum(
        1 for row in rows if _safe_bool(row.get("metadata", {}).get("cache_hit"))
    )

    solver_buckets: dict[str, list[dict[str, Any]]] = {}
    for row in scored_rows:
        solver_buckets.setdefault(_coerce_text(row.get("solver_id")), []).append(row)

    solver_rows: list[dict[str, Any]] = []
    for solver_id, solver_items in sorted(solver_buckets.items()):
        avg_delta = sum(
            _safe_float(item.get("overall_delta")) for item in solver_items
        ) / len(solver_items)
        resolved_worse = sum(
            1
            for item in solver_items
            if _safe_bool(item.get("resolved"))
            and _safe_float(item.get("overall_delta")) <= -0.35
        )
        unresolved_promising = sum(
            1
            for item in solver_items
            if not _safe_bool(item.get("resolved"))
            and _safe_float(item.get("overall_delta")) >= 0.35
        )
        solver_rows.append(
            {
                "solver_id": solver_id,
                "judged_job_count": len(solver_items),
                "avg_overall_delta": round(avg_delta, 6),
                "better_share": (
                    sum(
                        1
                        for item in solver_items
                        if _coerce_text(item.get("overall_label"))
                        in {"better", "much_better"}
                    )
                    / len(solver_items)
                ),
                "worse_share": (
                    sum(
                        1
                        for item in solver_items
                        if _coerce_text(item.get("overall_label"))
                        in {"worse", "much_worse"}
                    )
                    / len(solver_items)
                ),
                "resolved_but_worse_count": resolved_worse,
                "unresolved_but_promising_count": unresolved_promising,
            }
        )
    solver_rows.sort(
        key=lambda row: (
            -_safe_float(row.get("avg_overall_delta")),
            -_safe_float(row.get("better_share")),
            _coerce_text(row.get("solver_id")),
        )
    )

    dimension_buckets: dict[str, list[dict[str, Any]]] = {
        _coerce_text(spec["name"]): [] for spec in JUDGE_DIMENSIONS
    }
    for row in scored_rows:
        for dimension in row.get("dimensions", []):
            if isinstance(dimension, Mapping):
                name = _coerce_text(dimension.get("name"))
                if name in dimension_buckets:
                    dimension_buckets[name].append(dict(dimension))
    dimension_rows: list[dict[str, Any]] = []
    for spec in JUDGE_DIMENSIONS:
        name = _coerce_text(spec["name"])
        items = dimension_buckets.get(name, [])
        avg_delta = (
            sum(_safe_float(item.get("delta")) for item in items) / len(items)
            if items
            else 0.0
        )
        better_share = (
            sum(1 for item in items if _safe_float(item.get("delta")) > 0) / len(items)
            if items
            else 0.0
        )
        worse_share = (
            sum(1 for item in items if _safe_float(item.get("delta")) < 0) / len(items)
            if items
            else 0.0
        )
        dimension_rows.append(
            {
                "name": name,
                "weight": spec["weight"],
                "avg_delta": round(avg_delta, 6),
                "better_share": better_share,
                "worse_share": worse_share,
            }
        )

    resolved_but_worse = [
        _sample_row(row)
        for row in scored_rows
        if _safe_bool(row.get("resolved"))
        and _safe_float(row.get("overall_delta")) <= -0.35
    ]
    resolved_but_worse.sort(
        key=lambda row: (row["overall_delta"], row["instance_id"], row["solver_id"])
    )

    unresolved_promising = [
        _sample_row(row)
        for row in scored_rows
        if not _safe_bool(row.get("resolved"))
        and _safe_float(row.get("overall_delta")) >= 0.35
    ]
    unresolved_promising.sort(
        key=lambda row: (-row["overall_delta"], row["instance_id"], row["solver_id"])
    )

    best_samples = sorted(
        (_sample_row(row) for row in scored_rows),
        key=lambda row: (-row["overall_delta"], row["instance_id"], row["solver_id"]),
    )[:10]
    worst_samples = sorted(
        (_sample_row(row) for row in scored_rows),
        key=lambda row: (row["overall_delta"], row["instance_id"], row["solver_id"]),
    )[:10]

    avg_delta = (
        sum(_safe_float(row.get("overall_delta")) for row in scored_rows)
        / len(scored_rows)
        if scored_rows
        else 0.0
    )
    best_solver_id = (
        _coerce_text(solver_rows[0].get("solver_id")) if solver_rows else ""
    )
    return {
        "enabled": True,
        "model": dict(model),
        "top_line": {
            "judged_attempt_count": len(rows),
            "judged_job_count": len(latest_rows),
            "scored_job_count": len(scored_rows),
            "error_job_count": len(error_rows),
            "cache_hit_count": cache_hit_count,
            "avg_overall_delta": round(avg_delta, 6),
            "better_share": better_count / len(scored_rows) if scored_rows else 0.0,
            "worse_share": worse_count / len(scored_rows) if scored_rows else 0.0,
            "best_solver_id": best_solver_id,
        },
        "solver_rows": solver_rows,
        "dimension_rows": dimension_rows,
        "resolved_but_worse_than_gold": resolved_but_worse[:10],
        "unresolved_but_promising": unresolved_promising[:10],
        "best_samples": best_samples,
        "worst_samples": worst_samples,
    }


def run_diff_judge(
    *,
    joined_rows: list[dict[str, Any]],
    dataset_rows: Mapping[str, Mapping[str, Any]],
    out_root: Path,
    llm_mode: str | None,
    model_name: str | None,
    provider: str | None,
    progress_stream: TextIO | None = None,
) -> DiffJudgeRunResult:
    normalized_provider = _normalize_provider(provider)
    validate_llm_judge_policy(llm_mode=llm_mode, provider=normalized_provider)
    model = _resolve_model(normalized_provider, model_name)
    if progress_stream is None:
        progress_stream = sys.stderr

    judge_root = out_root / "judge"
    requests_root = judge_root / "requests"
    responses_root = judge_root / "responses"
    rows_path = judge_root / JUDGE_ARTIFACT_FILENAME
    requests_root.mkdir(parents=True, exist_ok=True)
    responses_root.mkdir(parents=True, exist_ok=True)
    existing_rows = _load_existing_rows(rows_path)

    candidate_rows = [
        row for row in joined_rows if _coerce_text(row.get("model_patch")).strip()
    ]
    progress = _JudgeProgressReporter(len(candidate_rows), progress_stream)

    judged_rows: list[dict[str, Any]] = []
    counter = 0
    for row in candidate_rows:
        instance_id = _coerce_text(row.get("instance_id"))
        dataset_row = dataset_rows.get(instance_id)
        if dataset_row is None:
            raise RuntimeError(
                f"dataset row missing for judged instance: {instance_id}"
            )

        attempt_id = _coerce_text(row.get("attempt_id")) or (
            f"{_coerce_text(row.get('solver_id'))}:{instance_id}:{counter}"
        )
        job_id = _coerce_text(row.get("job_id")) or (
            f"{_coerce_text(row.get('solver_id'))}:{instance_id}"
        )
        prompt_payload = _judge_request_payload(
            row=dict(row, attempt_id=attempt_id, job_id=job_id),
            dataset_row=dataset_row,
            model=model,
        )
        cache_key = _cache_key(model, prompt_payload)
        existing = existing_rows.get((attempt_id, cache_key))
        if existing is not None:
            cached = dict(existing)
            metadata = dict(cached.get("metadata", {}))
            metadata["cache_hit"] = True
            cached["metadata"] = metadata
            judged_rows.append(cached)
            progress.finish(
                completed=counter + 1,
                solver_id=_coerce_text(row.get("solver_id")),
                instance_id=instance_id,
                status="cache-hit",
            )
            counter += 1
            continue

        prompt = _judge_prompt(prompt_payload)
        artifact_name = _safe_artifact_name(attempt_id, cache_key)
        request_ref = requests_root / f"{artifact_name}.json"
        response_ref = responses_root / f"{artifact_name}.json"
        base_metadata = {
            "cache_key": cache_key,
            "cache_hit": False,
            "request_ref": str(request_ref),
            "response_ref": str(response_ref),
            "prompt_version": model.prompt_version,
        }
        progress.start(
            completed=counter,
            solver_id=_coerce_text(row.get("solver_id")),
            instance_id=instance_id,
        )
        request_ref.write_text(
            json.dumps(
                {
                    "model": model.to_dict(),
                    "prompt_payload": prompt_payload,
                },
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        try:
            (
                request_payload,
                response_payload,
                usage,
                usage_source,
                cost,
                cost_source,
            ) = _invoke_model(model=model, prompt=prompt)
            request_ref.write_text(
                json.dumps(
                    {
                        "model": model.to_dict(),
                        "prompt_payload": prompt_payload,
                        "request_payload": request_payload,
                    },
                    sort_keys=True,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            response_ref.write_text(
                json.dumps(response_payload, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            response_text = _extract_response_text(
                normalized_provider, response_payload
            )
            judged_rows.append(
                _judge_row_from_response(
                    row=dict(row, attempt_id=attempt_id, job_id=job_id),
                    model=model,
                    response_text=response_text,
                    metadata=dict(
                        base_metadata,
                        usage=usage,
                        usage_source=usage_source,
                        cost=cost,
                        cost_source=cost_source,
                    ),
                )
            )
            progress.finish(
                completed=counter + 1,
                solver_id=_coerce_text(row.get("solver_id")),
                instance_id=instance_id,
                status="scored",
            )
        except Exception as exc:
            response_ref.write_text(
                json.dumps({"error": str(exc)}, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            judged_rows.append(
                _fallback_row(
                    row=dict(row, attempt_id=attempt_id, job_id=job_id),
                    model=model,
                    metadata=base_metadata,
                    error=str(exc),
                )
            )
            progress.finish(
                completed=counter + 1,
                solver_id=_coerce_text(row.get("solver_id")),
                instance_id=instance_id,
                status="error",
            )
        counter += 1

    _write_rows(rows_path, judged_rows)
    report = build_diff_judge_report(judged_rows, model=model.to_dict())
    progress.close()
    return DiffJudgeRunResult(
        rows_path=str(rows_path),
        rows=judged_rows,
        report=report,
        model=model.to_dict(),
    )


__all__ = [
    "DiffJudgeRunResult",
    "JUDGE_ARTIFACT_FILENAME",
    "JUDGE_PROMPT_VERSION",
    "build_diff_judge_report",
    "run_diff_judge",
    "validate_llm_judge_policy",
]
