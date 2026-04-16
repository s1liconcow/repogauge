"""Solver adapter implementations and adapter construction helpers."""

from __future__ import annotations

from typing import Any, Mapping

from .matrix import MatrixProvider, MatrixSolver
from .scheduler import (
    SolverAdapter,
    SolverAttemptState,
    SolverAdapterRequest,
    SolverAdapterResult,
)
from .solvers import (
    SOLVER_ADAPTER_ALIAS,
    SOLVER_ADAPTER_MOCK,
    SOLVER_ADAPTER_OPENAI_COMPATIBLE,
    SOLVER_ADAPTER_OPENAI_RESPONSES,
    SOLVER_ADAPTER_CODEX_CLI,
    SOLVER_ADAPTER_CLAUDE,
    SOLVER_ADAPTER_OPEN_CODEX_SERVER,
)

SOLVER_ATTEMPT_STATES = (
    SolverAttemptState.QUEUED,
    SolverAttemptState.RUNNING,
    SolverAttemptState.SUCCEEDED,
    SolverAttemptState.FAILED,
    SolverAttemptState.TIMED_OUT,
    SolverAttemptState.BUDGET_EXCEEDED,
    SolverAttemptState.INVALID_PATCH,
)


class SolverAdapterError(ValueError):
    """Raised when adapter construction or execution cannot proceed."""


class MockSolverAdapter(SolverAdapter):
    """Deterministic mock adapter for local dry-run and scaffolded execution."""

    def __init__(
        self,
        *,
        solver_id: str,
        provider_id: str,
        behavior: Mapping[str, Any],
    ) -> None:
        self.solver_id = solver_id
        self.provider_id = provider_id
        self.behavior = dict(behavior)
        self._status_cycle = self._coerce_status_cycle(
            self.behavior.get("mock_statuses", ["succeeded"])
        )
        self._model_patch = self.behavior.get("model_patch", "")

    @staticmethod
    def _coerce_status_cycle(values: Any) -> tuple[str, ...]:
        if values is None:
            return ("succeeded",)

        if isinstance(values, str):
            values = [values]

        if not isinstance(values, (list, tuple)):
            raise SolverAdapterError("mock_statuses must be a string or sequence")

        normalized = tuple(
            str(item).strip().lower() for item in values if str(item).strip()
        )
        if not normalized:
            return ("succeeded",)

        for state in normalized:
            if state not in SOLVER_ATTEMPT_STATES:
                raise SolverAdapterError(f"unsupported mock status: {state}")

        return normalized

    def prepare_request(
        self,
        *,
        job: Any,
        attempt_id: str,
        attempt_index: int,
        instance_row: Mapping[str, Any] | None = None,
    ) -> SolverAdapterRequest:
        _ = (job, instance_row)
        if attempt_index < 1:
            raise SolverAdapterError("attempt_index must be >= 1")
        return SolverAdapterRequest(
            attempt_id=attempt_id,
            attempt_index=attempt_index,
            job=job,
            instance_row=instance_row,
        )

    def execute_attempt(self, request: SolverAdapterRequest) -> SolverAdapterResult:
        status = self._status_cycle[
            min(request.attempt_index - 1, len(self._status_cycle) - 1)
        ]
        return SolverAdapterResult(
            attempt_id=request.attempt_id,
            status=status,
            model_patch=(
                self._model_patch
                if status == SolverAttemptState.SUCCEEDED and self._model_patch
                else None
            ),
            raw_output=f"mock:{self.solver_id}:{status}:{request.attempt_id}",
            usage={"source": "mock", "provider_id": self.provider_id},
            cost={"source": "mock"},
            metadata={"solver_id": self.solver_id},
        )

    def finalize_output(
        self,
        request: SolverAdapterRequest,
        result: SolverAdapterResult,
    ) -> SolverAdapterResult:
        return result


def _unsupported_adapters() -> set[str]:
    return {
        SOLVER_ADAPTER_OPENAI_RESPONSES,
        SOLVER_ADAPTER_CODEX_CLI,
        SOLVER_ADAPTER_OPENAI_COMPATIBLE,
        SOLVER_ADAPTER_OPEN_CODEX_SERVER,
        SOLVER_ADAPTER_CLAUDE,
    } | set(SOLVER_ADAPTER_ALIAS.values())


def build_solver_adapters(
    *,
    solvers: tuple[MatrixSolver, ...],
    providers: tuple[MatrixProvider, ...],
) -> dict[str, SolverAdapter]:
    provider_by_id: dict[str, MatrixProvider] = {
        provider.provider_id: provider for provider in providers
    }
    adapters: dict[str, SolverAdapter] = {}

    for solver in solvers:
        provider = provider_by_id.get(solver.provider_id)
        if provider is None:
            raise SolverAdapterError(
                f"solver '{solver.solver_id}' references unknown provider "
                f"'{solver.provider_id}'"
            )
        if solver.adapter == SOLVER_ADAPTER_MOCK:
            adapters[solver.solver_id] = MockSolverAdapter(
                solver_id=solver.solver_id,
                provider_id=provider.provider_id,
                behavior=solver.behavior,
            )
            continue

        unsupported = _unsupported_adapters()
        if solver.adapter in unsupported:
            raise SolverAdapterError(
                f"solver '{solver.solver_id}' adapter '{solver.adapter}' is not implemented in this release"
            )
        raise SolverAdapterError(
            f"solver '{solver.solver_id}' adapter '{solver.adapter}' is unknown"
        )

    return adapters


__all__ = [
    "SolverAdapterError",
    "MockSolverAdapter",
    "build_solver_adapters",
]
