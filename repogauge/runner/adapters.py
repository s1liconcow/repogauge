"""Concrete solver adapters for benchmark execution.

The module provides:

- a mock adapter used by scaffolded/local tests
- concrete adapters for Anthropic, OpenAI/Responses, Codex CLI, OpenCode, and
  OpenAI-compatible endpoints
- a factory that wires matrix solver configs to adapter instances
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC
from dataclasses import dataclass
from typing import Any, Mapping

from repogauge.exec import run_command

from .scheduler import (
    SolverAdapter,
    SolverAdapterRequest,
    SolverAdapterResult,
    SolverAttemptState,
)
from .solvers import (
    SOLVER_ADAPTER_CLAUDE,
    SOLVER_ADAPTER_CODEX_CLI,
    SOLVER_ADAPTER_MOCK,
    SOLVER_ADAPTER_OPENAI_COMPATIBLE,
    SOLVER_ADAPTER_OPENAI_RESPONSES,
    SOLVER_ADAPTER_OPEN_CODEX_SERVER,
)
from .matrix import MatrixSolver


class SolverAdapterError(ValueError):
    """Raised for adapter construction, validation, or execution issues."""


def _coerce_mapping(
    value: Any, *, field_name: str, default: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    if value is None:
        return dict(default or {})
    if not isinstance(value, Mapping):
        raise SolverAdapterError(f"{field_name} must be a mapping")
    return dict(value)


def _coerce_text(value: Any, *, field_name: str, default: str | None = None) -> str:
    if value is None:
        if default is not None:
            return default
        raise SolverAdapterError(f"{field_name} is required")
    if not isinstance(value, str):
        raise SolverAdapterError(f"{field_name} must be a string")
    text = value.strip()
    if not text:
        if default is not None:
            return default
        raise SolverAdapterError(f"{field_name} cannot be empty")
    return text


def _coerce_int(value: Any, *, field_name: str, default: int | None = None) -> int:
    if value is None:
        if default is None:
            raise SolverAdapterError(f"{field_name} is required")
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and int(value) == value:
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError as exc:  # pragma: no cover - defensive
            raise SolverAdapterError(
                f"{field_name} must be an integer, got {value!r}"
            ) from exc
    raise SolverAdapterError(
        f"{field_name} must be an integer, got {type(value).__name__}"
    )


def _safe_parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return None


def _parse_json_lines(text: str) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        payload = _safe_parse_json(line)
        if isinstance(payload, dict):
            parsed.append(payload)
    return parsed


def _extract_text_candidates(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if not isinstance(value, Mapping):
        return []

    candidates: list[str] = []
    for key in ("text", "patch", "model_patch", "content", "output_text"):
        text = value.get(key)
        if isinstance(text, str) and text.strip():
            candidates.append(text.strip())

    output = value.get("output")
    if isinstance(output, list):
        for item in output:
            for part in _extract_text_candidates(item):
                candidates.append(part)

    return candidates


_CODEX_INFRA_TIMEOUT_MARKERS = (
    "exec_command failed",
    "failed to create unified exec process",
    "createprocess",
    'rejected("',
)


def _classify_codex_cli_timeout(
    *, stderr_output: str, telemetry_events: list[dict[str, Any]]
) -> str | None:
    haystack = "\n".join(
        part
        for part in (stderr_output, json.dumps(telemetry_events, sort_keys=True))
        if part
    ).lower()
    if any(marker in haystack for marker in _CODEX_INFRA_TIMEOUT_MARKERS):
        return "infra_tool_exec"
    return None


def _extract_patch_from_text(raw_output: str) -> str:
    text = raw_output.strip()
    if not text:
        return ""

    payload = _safe_parse_json(text)
    if isinstance(payload, Mapping):
        for key in ("model_patch", "patch"):
            candidate = payload.get(key)
            if isinstance(candidate, str) and "diff --git" in candidate:
                patch = candidate.strip()
                return patch if patch.endswith("\n") else patch + "\n"
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, Mapping):
                maybe_patch = _extract_patch_from_text(json.dumps(item))
                if maybe_patch:
                    return maybe_patch

    for line_no, line in enumerate(text.splitlines()):
        if line.startswith("diff --git "):
            lines = "\n".join(text.splitlines()[line_no:])
            normalized = lines.strip("\n")
            if normalized:
                return normalized if normalized.endswith("\n") else normalized + "\n"
            break

    for candidate in _extract_text_candidates(
        payload if isinstance(payload, Mapping) else {}
    ):
        if candidate and "diff --git" in candidate:
            normalized = candidate.strip("\n")
            return normalized if normalized.endswith("\n") else normalized + "\n"

    block_start = (
        text.lower().find("```diff"),
        text.lower().find("```patch"),
    )
    for start in block_start:
        if start != -1:
            chunk = text[start:]
            end = chunk.find("```", 6)
            if end != -1:
                code = chunk[6:end].strip()
                if "diff --git" in code:
                    normalized = code.strip("\n")
                    return (
                        normalized if normalized.endswith("\n") else normalized + "\n"
                    )

    return ""


def _coerce_usage(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _coerce_cost(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _required_instance_fields(instance_row: Mapping[str, Any] | None) -> dict[str, str]:
    if instance_row is None:
        raise SolverAdapterError("instance_row is required for solver execution")
    row = _coerce_mapping(instance_row, field_name="instance_row")
    required: dict[str, str] = {}
    for key in ("instance_id", "repo", "base_commit", "problem_statement"):
        value = str(row.get(key, "")).strip()
        if not value:
            raise SolverAdapterError(f"instance_row missing required field: {key}")
        required[key] = value
    return required


def _build_prompt(
    *, solver_id: str, model: str, attempt_index: int, instance_row: Mapping[str, Any]
) -> str:
    row = _coerce_mapping(instance_row, field_name="instance_row")
    prompt = [
        f"Solver {solver_id}, attempt {attempt_index}.",
        f"Repository: {row.get('repo', '')}".strip(),
        f"Instance: {row.get('instance_id', '')}".strip(),
        f"Base commit: {row.get('base_commit', '')}".strip(),
        f"Problem statement: {row.get('problem_statement', '')}".strip(),
        f"Model: {model}".strip(),
        "Return a unified diff rooted at repository root with diff --git headers.",
    ]
    if row.get("patch"):
        prompt.append(f"Reference patch:\n{row['patch']}")
    if row.get("test_patch"):
        prompt.append(f"Test patch:\n{row['test_patch']}")
    if row.get("FAIL_TO_PASS"):
        prompt.append(f"FAIL_TO_PASS: {row['FAIL_TO_PASS']}")
    if row.get("PASS_TO_PASS"):
        prompt.append(f"PASS_TO_PASS: {row['PASS_TO_PASS']}")
    return "\n".join(prompt) + "\n"


def _parse_usage_cost(
    payload: Mapping[str, Any] | list[Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(payload, Mapping):
        return {}, {}
    usage: Mapping[str, Any] = _coerce_usage(payload.get("usage"))
    cost: Mapping[str, Any] = _coerce_cost(payload.get("cost"))
    return dict(usage), dict(cost)


def _parse_usage_cost_with_source(
    payload: Mapping[str, Any] | list[Any] | None,
) -> tuple[dict[str, Any], str, dict[str, Any], str]:
    if not isinstance(payload, Mapping):
        return {}, "", {}, ""

    usage_source = ""
    usage = _coerce_usage(payload.get("usage")) if "usage" in payload else {}
    if usage or "usage" in payload:
        usage_source = "response.usage"

    cost_source = ""
    cost = _coerce_cost(payload.get("cost")) if "cost" in payload else {}
    if cost or "cost" in payload:
        cost_source = "response.cost"

    return dict(usage), usage_source, dict(cost), cost_source


def _post_json(
    *,
    url: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, str] | None = None,
    timeout_seconds: int = 120,
) -> Any:
    request_headers = {
        "Content-Type": "application/json",
        **(dict(headers) if headers else {}),
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(dict(payload), separators=(",", ":")).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8")
            if not raw.strip():
                return {}
            parsed = _safe_parse_json(raw)
            if parsed is None:
                raise SolverAdapterError(f"invalid JSON response from {url}")
            return parsed
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8")
        except Exception:
            raw = ""
        raise SolverAdapterError(
            f"request failed: {exc.code} {exc.reason} {raw}".strip()
        ) from exc
    except urllib.error.URLError as exc:
        raise SolverAdapterError(f"request failed: {exc}") from exc


class _BaseConcreteSolverAdapter(SolverAdapter, ABC):
    """Base implementation shared by HTTP/CLI-backed adapters."""

    def __init__(
        self,
        solver_id: str,
        provider_id: str,
        provider_config: Mapping[str, Any] | None,
        behavior: Mapping[str, Any] | None = None,
        model: str | None = None,
    ) -> None:
        self.solver_id = _coerce_text(solver_id, field_name="solver_id")
        self.provider_id = _coerce_text(provider_id, field_name="provider_id")
        self.provider_config = _coerce_mapping(
            provider_config, field_name="provider_config"
        )
        self.behavior = _coerce_mapping(behavior, field_name="solver.behavior")
        self.model = _coerce_text(
            model or self.behavior.get("model"),
            field_name="solver.model",
            default="",
        )
        self.timeout_seconds = _coerce_int(
            self.behavior.get("timeout_seconds"),
            field_name="solver.behavior.timeout_seconds",
            default=120,
        )
        if self.timeout_seconds <= 0:
            raise SolverAdapterError("solver.behavior.timeout_seconds must be > 0")

    def prepare_request(
        self,
        *,
        job: Any,
        attempt_id: str,
        attempt_index: int,
        instance_row: Mapping[str, Any] | None = None,
        workspace_path=None,
    ) -> SolverAdapterRequest:
        _required_instance_fields(instance_row)
        if not attempt_id:
            raise SolverAdapterError("attempt_id is required")
        if attempt_index < 1:
            raise SolverAdapterError("attempt_index must be >= 1")
        return SolverAdapterRequest(
            attempt_id=attempt_id,
            attempt_index=attempt_index,
            job=job,
            instance_row=instance_row,
            workspace_path=workspace_path,
        )

    def finalize_output(
        self, request: SolverAdapterRequest, result: SolverAdapterResult
    ) -> SolverAdapterResult:
        patch = result.model_patch or _extract_patch_from_text(result.raw_output)
        metadata = dict(result.metadata)
        metadata["solver_id"] = self.solver_id
        metadata["provider_id"] = self.provider_id
        metadata["attempt_index"] = request.attempt_index
        metadata["model"] = self.model

        if result.status == SolverAttemptState.SUCCEEDED:
            if not patch:
                return SolverAdapterResult(
                    attempt_id=result.attempt_id,
                    status=SolverAttemptState.INVALID_PATCH,
                    model_patch=None,
                    raw_output=result.raw_output,
                    exit_reason="invalid patch: no unified diff found in model output",
                    usage_source=result.usage_source,
                    cost_source=result.cost_source,
                    usage=result.usage,
                    cost=result.cost,
                    metadata=metadata,
                )
            return SolverAdapterResult(
                attempt_id=result.attempt_id,
                status=result.status,
                model_patch=patch,
                raw_output=result.raw_output,
                exit_reason=result.exit_reason,
                usage_source=result.usage_source,
                cost_source=result.cost_source,
                usage=_coerce_usage(result.usage),
                cost=_coerce_cost(result.cost),
                metadata=metadata,
            )

        return SolverAdapterResult(
            attempt_id=result.attempt_id,
            status=result.status,
            model_patch=result.model_patch,
            raw_output=result.raw_output,
            exit_reason=result.exit_reason,
            usage_source=result.usage_source,
            cost_source=result.cost_source,
            usage=_coerce_usage(result.usage),
            cost=_coerce_cost(result.cost),
            metadata=metadata,
        )


class MockSolverAdapter(_BaseConcreteSolverAdapter):
    """Deterministic scaffold adapter that emits pre-programmed statuses."""

    _VALID_STATUSES = {
        SolverAttemptState.SUCCEEDED,
        SolverAttemptState.FAILED,
        SolverAttemptState.TIMED_OUT,
        SolverAttemptState.BUDGET_EXCEEDED,
        SolverAttemptState.INVALID_PATCH,
    }

    def __init__(
        self,
        *,
        solver_id: str,
        provider_id: str,
        behavior: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(
            solver_id=solver_id,
            provider_id=provider_id,
            provider_config={},
            behavior=behavior,
            model="mock",
        )

        raw_statuses = self.behavior.get(
            "mock_statuses", [SolverAttemptState.SUCCEEDED]
        )
        if not isinstance(raw_statuses, list):
            raise SolverAdapterError("mock_statuses must be a list")
        if not raw_statuses:
            raise SolverAdapterError("mock_statuses cannot be empty")
        statuses = []
        for status in raw_statuses:
            normalized = _coerce_text(
                status, field_name="mock_status", default=""
            ).lower()
            if normalized not in self._VALID_STATUSES:
                raise SolverAdapterError(
                    f"unsupported mock status: {status}; expected one of {sorted(self._VALID_STATUSES)}"
                )
            statuses.append(normalized)
        self.mock_statuses = tuple(statuses)
        self.mock_patch = _coerce_text(
            self.behavior.get("mock_patch"),
            field_name="mock_patch",
            default="diff --git a/__stub__.txt b/__stub__.txt\n--- a/__stub__.txt\n+++ b/__stub__.txt\n@@\n+mock patch",
        )
        self.mock_exit_reason = _coerce_text(
            self.behavior.get("mock_exit_reason"),
            field_name="mock_exit_reason",
            default="mock status reached",
        )

    def execute_attempt(self, request: SolverAdapterRequest) -> SolverAdapterResult:
        status = self.mock_statuses[
            (request.attempt_index - 1) % len(self.mock_statuses)
        ]
        if status == SolverAttemptState.SUCCEEDED:
            return SolverAdapterResult(
                attempt_id=request.attempt_id,
                status=status,
                model_patch=self.mock_patch,
                raw_output=self.mock_patch,
                exit_reason="",
                usage_source="",
                cost_source="",
            )
        return SolverAdapterResult(
            attempt_id=request.attempt_id,
            status=status,
            model_patch=None,
            raw_output="",
            exit_reason=self.mock_exit_reason,
            usage_source="",
            cost_source="",
        )


class AnthropicAgentSDKAdapter(_BaseConcreteSolverAdapter):
    """Anthropic API path via Messages endpoint over HTTPS."""

    def __init__(
        self,
        *,
        solver_id: str,
        provider_id: str,
        provider_config: Mapping[str, Any] | None,
        behavior: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(
            solver_id=solver_id,
            provider_id=provider_id,
            provider_config=provider_config,
            behavior=behavior,
            model=None,
        )
        self.model = _coerce_text(
            self.model or self.behavior.get("model") or "claude-3-5-sonnet-latest",
            field_name="solver.model",
            default="claude-3-5-sonnet-latest",
        )
        self.api_key = _coerce_text(
            self.provider_config.get("api_key"),
            field_name="provider.api_key",
            default="",
        )
        self.base_url = _coerce_text(
            self.provider_config.get("base_url"),
            field_name="provider.base_url",
            default="https://api.anthropic.com",
        )
        self.api_version = _coerce_text(
            self.provider_config.get("api_version"),
            field_name="provider.api_version",
            default="2023-06-01",
        )

    def _coerce_output_text(self, payload: Mapping[str, Any]) -> str:
        content = _extract_text_candidates(payload)
        if content:
            return "\n".join(content)
        output_block = payload.get("output")
        if isinstance(output_block, str):
            return output_block
        return ""

    def execute_attempt(self, request: SolverAdapterRequest) -> SolverAdapterResult:
        instance_row = _required_instance_fields(request.instance_row)
        prompt = _build_prompt(
            solver_id=self.solver_id,
            model=self.model,
            attempt_index=request.attempt_index,
            instance_row=instance_row,
        )
        url = urllib.parse.urljoin(self.base_url.rstrip("/") + "/", "v1/messages")
        payload = {
            "model": self.model,
            "max_tokens": _coerce_int(
                self.behavior.get("max_tokens"),
                field_name="solver.behavior.max_tokens",
                default=4096,
            ),
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": self.api_version,
        }
        if not self.api_key:
            return SolverAdapterResult(
                attempt_id=request.attempt_id,
                status=SolverAttemptState.FAILED,
                model_patch=None,
                raw_output="",
                exit_reason="missing provider.api_key",
            )
        try:
            response = _post_json(
                url=url,
                payload=payload,
                headers=headers,
                timeout_seconds=self.timeout_seconds,
            )
        except SolverAdapterError as exc:
            return SolverAdapterResult(
                attempt_id=request.attempt_id,
                status=SolverAttemptState.FAILED,
                model_patch=None,
                raw_output="",
                exit_reason=str(exc),
            )

        text = self._coerce_output_text(
            _coerce_mapping(response, field_name="response")
        )
        usage, usage_source, cost, cost_source = _parse_usage_cost_with_source(
            _coerce_mapping(response, field_name="response")
        )
        return SolverAdapterResult(
            attempt_id=request.attempt_id,
            status=SolverAttemptState.SUCCEEDED,
            model_patch=text,
            raw_output=text,
            exit_reason="",
            usage_source=usage_source,
            cost_source=cost_source,
            usage=usage,
            cost=cost,
        )


class OpenAIResponsesAdapter(_BaseConcreteSolverAdapter):
    """OpenAI Responses API adapter."""

    def __init__(
        self,
        *,
        solver_id: str,
        provider_id: str,
        provider_config: Mapping[str, Any] | None,
        behavior: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(
            solver_id=solver_id,
            provider_id=provider_id,
            provider_config=provider_config,
            behavior=behavior,
            model=None,
        )
        self.model = _coerce_text(
            self.model or self.behavior.get("model") or "gpt-5.4",
            field_name="solver.model",
            default="gpt-5.4",
        )
        self.api_key = _coerce_text(
            self.provider_config.get("api_key"),
            field_name="provider.api_key",
            default="",
        )
        self.base_url = _coerce_text(
            self.provider_config.get("base_url"),
            field_name="provider.base_url",
            default="https://api.openai.com/v1",
        )

    def execute_attempt(self, request: SolverAdapterRequest) -> SolverAdapterResult:
        instance_row = _required_instance_fields(request.instance_row)
        prompt = _build_prompt(
            solver_id=self.solver_id,
            model=self.model,
            attempt_index=request.attempt_index,
            instance_row=instance_row,
        )
        url = urllib.parse.urljoin(self.base_url.rstrip("/") + "/", "responses")
        payload = {
            "model": self.model,
            "input": prompt,
            "max_output_tokens": _coerce_int(
                self.behavior.get("max_output_tokens"),
                field_name="solver.behavior.max_output_tokens",
                default=4096,
            ),
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if not self.api_key:
            return SolverAdapterResult(
                attempt_id=request.attempt_id,
                status=SolverAttemptState.FAILED,
                model_patch=None,
                raw_output="",
                exit_reason="missing provider.api_key",
            )
        try:
            response = _post_json(
                url=url,
                payload=payload,
                headers=headers,
                timeout_seconds=self.timeout_seconds,
            )
        except SolverAdapterError as exc:
            return SolverAdapterResult(
                attempt_id=request.attempt_id,
                status=SolverAttemptState.FAILED,
                model_patch=None,
                raw_output="",
                exit_reason=str(exc),
            )

        response_payload = _coerce_mapping(response, field_name="response")
        usage, usage_source, cost, cost_source = _parse_usage_cost_with_source(
            response_payload
        )
        text = response_payload.get("output_text")
        if not isinstance(text, str):
            text = ""
            for line in _parse_json_lines(json.dumps(response_payload)):
                text_candidates = _extract_text_candidates(line)
                if text_candidates:
                    text = "\n".join(text_candidates)
                    break
        return SolverAdapterResult(
            attempt_id=request.attempt_id,
            status=SolverAttemptState.SUCCEEDED,
            model_patch=text or "",
            raw_output=text or json.dumps(response_payload),
            exit_reason="",
            usage_source=usage_source,
            cost_source=cost_source,
            usage=usage,
            cost=cost,
        )


class OpenAICompatibleAdapter(_BaseConcreteSolverAdapter):
    """Generic OpenAI-compatible endpoint adapter (chat/completions style)."""

    def __init__(
        self,
        *,
        solver_id: str,
        provider_id: str,
        provider_config: Mapping[str, Any] | None,
        behavior: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(
            solver_id=solver_id,
            provider_id=provider_id,
            provider_config=provider_config,
            behavior=behavior,
            model=None,
        )
        self.model = _coerce_text(
            self.model or self.behavior.get("model") or "gpt-4o",
            field_name="solver.model",
            default="gpt-4o",
        )
        self.api_key = _coerce_text(
            self.provider_config.get("api_key"),
            field_name="provider.api_key",
            default="",
        )
        self.base_url = _coerce_text(
            self.provider_config.get("base_url"),
            field_name="provider.base_url",
            default="https://api.openai.com/v1",
        )

    def execute_attempt(self, request: SolverAdapterRequest) -> SolverAdapterResult:
        instance_row = _required_instance_fields(request.instance_row)
        prompt = _build_prompt(
            solver_id=self.solver_id,
            model=self.model,
            attempt_index=request.attempt_index,
            instance_row=instance_row,
        )
        url = urllib.parse.urljoin(
            self.base_url.rstrip("/") + "/",
            "chat/completions",
        )
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": _coerce_int(
                self.behavior.get("max_tokens"),
                field_name="solver.behavior.max_tokens",
                default=4096,
            ),
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if not self.api_key:
            return SolverAdapterResult(
                attempt_id=request.attempt_id,
                status=SolverAttemptState.FAILED,
                model_patch=None,
                raw_output="",
                exit_reason="missing provider.api_key",
            )
        try:
            response = _post_json(
                url=url,
                payload=payload,
                headers=headers,
                timeout_seconds=self.timeout_seconds,
            )
        except SolverAdapterError as exc:
            return SolverAdapterResult(
                attempt_id=request.attempt_id,
                status=SolverAttemptState.FAILED,
                model_patch=None,
                raw_output="",
                exit_reason=str(exc),
            )

        response_payload = _coerce_mapping(response, field_name="response")
        usage, usage_source, cost, cost_source = _parse_usage_cost_with_source(
            response_payload
        )
        text = ""
        choices = response_payload.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, Mapping):
                msg = _coerce_mapping(
                    first.get("message"), field_name="response.choices[0].message"
                )
                content = msg.get("content")
                if isinstance(content, str):
                    text = content
        if not text:
            for line in _extract_text_candidates(response_payload):
                if line:
                    text = line
                    break
        return SolverAdapterResult(
            attempt_id=request.attempt_id,
            status=SolverAttemptState.SUCCEEDED,
            model_patch=text or "",
            raw_output=text or json.dumps(response_payload),
            exit_reason="",
            usage_source=usage_source,
            cost_source=cost_source,
            usage=usage,
            cost=cost,
        )


class OpenCodeServerAdapter(OpenAICompatibleAdapter):
    """OpenCode server adapter, reusing OpenAI-compatible chat/completions contract."""

    def __init__(
        self,
        *,
        solver_id: str,
        provider_id: str,
        provider_config: Mapping[str, Any] | None,
        behavior: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(
            solver_id=solver_id,
            provider_id=provider_id,
            provider_config=provider_config,
            behavior=behavior,
        )


@dataclass(frozen=True)
class _CLIAdapterConfig:
    command: str
    cli_args: tuple[str, ...]


def _coerce_cli_command(config: Mapping[str, Any]) -> _CLIAdapterConfig:
    raw = config.get("command")
    if raw is None:
        raise SolverAdapterError("provider.command is required for codex_cli adapter")
    if isinstance(raw, str):
        if not raw.strip():
            raise SolverAdapterError("provider.command cannot be empty")
        return _CLIAdapterConfig(command=raw, cli_args=tuple())
    if isinstance(raw, (list, tuple)):
        if not raw:
            raise SolverAdapterError("provider.command cannot be empty")
        return _CLIAdapterConfig(
            command=str(raw[0]), cli_args=tuple(str(v) for v in raw[1:])
        )
    raise SolverAdapterError("provider.command must be a string or list")


class CodexCLIAdapter(_BaseConcreteSolverAdapter):
    """Codex CLI adapter driven via non-interactive JSON output."""

    _BATCH_CONFIG_OVERRIDES = (
        ("notify", "[]"),
        ("mcp_servers", "{}"),
    )

    def __init__(
        self,
        *,
        solver_id: str,
        provider_id: str,
        provider_config: Mapping[str, Any] | None,
        behavior: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(
            solver_id=solver_id,
            provider_id=provider_id,
            provider_config=provider_config,
            behavior=behavior,
            model=None,
        )
        self.model = _coerce_text(
            self.model or self.behavior.get("model") or "gpt-5.4",
            field_name="solver.model",
            default="gpt-5.4",
        )
        config = _coerce_mapping(provider_config, field_name="provider_config")
        resolved = _coerce_cli_command(config)
        self.command = [resolved.command, *resolved.cli_args]

    def requires_workspace(self) -> bool:
        return True

    def execute_attempt(self, request: SolverAdapterRequest) -> SolverAdapterResult:
        instance_row = _required_instance_fields(request.instance_row)
        prompt = _build_prompt(
            solver_id=self.solver_id,
            model=self.model,
            attempt_index=request.attempt_index,
            instance_row=instance_row,
        )
        command = list(self.command)
        command.extend(["--ask-for-approval", "never"])
        command.append("exec")
        for key, value in self._BATCH_CONFIG_OVERRIDES:
            command.extend(["-c", f"{key}={value}"])
        command.extend(
            [
                "--json",
                "--color",
                "never",
                "--sandbox",
                "danger-full-access",
                "--ephemeral",
            ]
        )
        if request.workspace_path is not None:
            command.extend(["--cd", str(request.workspace_path)])
        command.extend(["--model", self.model])
        command_env = None
        if request.workspace_path is not None:
            codex_home_root = request.workspace_path.parent / "codex-home"
            command_env = dict(os.environ)
            command_env["HOME"] = str(codex_home_root)
            command_env["XDG_CONFIG_HOME"] = str(codex_home_root / ".config")
            command_env["CODEX_HOME"] = str(codex_home_root / ".codex")
        command_result = run_command(
            command,
            cwd=str(request.workspace_path) if request.workspace_path else None,
            env=command_env,
            input_text=prompt,
            timeout_seconds=self.timeout_seconds,
        )
        usage = {}
        cost = {}
        usage_source = ""
        cost_source = ""
        output = command_result.stdout
        telemetry_events: list[dict[str, Any]] = []

        text = ""
        if output.strip():
            parsed = _parse_json_lines(output)
            text_parts = []
            for event in parsed:
                telemetry_events.append(event)
                if "usage" in event and isinstance(event["usage"], Mapping):
                    usage = dict(event["usage"])
                    usage_source = "codex_cli.event.usage"
                if "cost" in event and isinstance(event["cost"], Mapping):
                    cost = dict(event["cost"])
                    cost_source = "codex_cli.event.cost"
                if "message" in event and isinstance(event["message"], Mapping):
                    text = event["message"].get("content")
                    if isinstance(text, str):
                        text_parts.append(text)
                if "text" in event and isinstance(event["text"], str):
                    text_parts.append(event["text"])
            text = "".join(text_parts).strip()
            if not text and output.strip():
                text = output.strip()

        metadata = {"telemetry": telemetry_events}
        if command_result.timed_out:
            failure_code = _classify_codex_cli_timeout(
                stderr_output=command_result.stderr,
                telemetry_events=telemetry_events,
            )
            if failure_code:
                metadata["failure_code"] = failure_code
                metadata["timeout_classification"] = "infra"
            else:
                metadata["timeout_classification"] = "wall_clock"
            return SolverAdapterResult(
                attempt_id=request.attempt_id,
                status=(
                    SolverAttemptState.FAILED
                    if failure_code
                    else SolverAttemptState.TIMED_OUT
                ),
                model_patch=None,
                raw_output=output,
                stderr_output=command_result.stderr,
                exit_reason=f"command timeout: {command_result.stderr or 'timed out'}",
                usage_source=usage_source,
                cost_source=cost_source,
                usage=usage,
                cost=cost,
                metadata=metadata,
            )

        if not command_result.success:
            return SolverAdapterResult(
                attempt_id=request.attempt_id,
                status=SolverAttemptState.FAILED,
                model_patch=None,
                raw_output=output,
                stderr_output=command_result.stderr,
                exit_reason=(
                    command_result.stderr
                    or command_result.stdout
                    or "command execution failed"
                ),
                usage_source=usage_source,
                cost_source=cost_source,
                usage=usage,
                cost=cost,
                metadata=metadata,
            )

        return SolverAdapterResult(
            attempt_id=request.attempt_id,
            status=SolverAttemptState.SUCCEEDED,
            model_patch=text,
            raw_output=output,
            stderr_output=command_result.stderr,
            exit_reason="",
            usage_source=usage_source,
            cost_source=cost_source,
            usage=usage,
            cost=cost,
            metadata=metadata,
        )


def build_solver_adapters(
    *,
    solvers: tuple[MatrixSolver, ...],
    providers: tuple[Any, ...],
) -> dict[str, SolverAdapter]:
    if not solvers:
        return {}

    provider_map = {provider.provider_id: provider for provider in providers}
    adapters: dict[str, SolverAdapter] = {}

    for solver in solvers:
        provider = provider_map.get(solver.provider_id)
        if provider is None:
            raise SolverAdapterError(
                f"solver '{solver.solver_id}' references unknown provider '{solver.provider_id}'"
            )
        provider_config = _coerce_mapping(
            provider.config, field_name=f"provider '{solver.provider_id}'.config"
        )
        behavior = _coerce_mapping(
            solver.behavior, field_name=f"solver '{solver.solver_id}'.behavior"
        )
        adapter_name = solver.adapter
        if adapter_name == SOLVER_ADAPTER_MOCK:
            adapter: SolverAdapter = MockSolverAdapter(
                solver_id=solver.solver_id,
                provider_id=solver.provider_id,
                behavior=behavior,
            )
        elif adapter_name == SOLVER_ADAPTER_CLAUDE:
            adapter = AnthropicAgentSDKAdapter(
                solver_id=solver.solver_id,
                provider_id=solver.provider_id,
                provider_config=provider_config,
                behavior=behavior,
            )
        elif adapter_name == SOLVER_ADAPTER_OPENAI_RESPONSES:
            adapter = OpenAIResponsesAdapter(
                solver_id=solver.solver_id,
                provider_id=solver.provider_id,
                provider_config=provider_config,
                behavior=behavior,
            )
        elif adapter_name == SOLVER_ADAPTER_CODEX_CLI:
            adapter = CodexCLIAdapter(
                solver_id=solver.solver_id,
                provider_id=solver.provider_id,
                provider_config=provider_config,
                behavior=behavior,
            )
        elif adapter_name == SOLVER_ADAPTER_OPEN_CODEX_SERVER:
            adapter = OpenCodeServerAdapter(
                solver_id=solver.solver_id,
                provider_id=solver.provider_id,
                provider_config=provider_config,
                behavior=behavior,
            )
        elif adapter_name == SOLVER_ADAPTER_OPENAI_COMPATIBLE:
            adapter = OpenAICompatibleAdapter(
                solver_id=solver.solver_id,
                provider_id=solver.provider_id,
                provider_config=provider_config,
                behavior=behavior,
            )
        else:
            raise SolverAdapterError(f"unsupported solver adapter: {adapter_name}")
        adapters[solver.solver_id] = adapter

    return adapters


__all__ = [
    "SolverAdapterError",
    "MockSolverAdapter",
    "AnthropicAgentSDKAdapter",
    "CodexCLIAdapter",
    "OpenAIResponsesAdapter",
    "OpenCodeServerAdapter",
    "OpenAICompatibleAdapter",
    "build_solver_adapters",
]
