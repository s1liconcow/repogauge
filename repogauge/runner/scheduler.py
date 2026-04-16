"""Solver-side queue scheduler and adapter interface."""

from __future__ import annotations

import json
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from repogauge.config import AttemptRow, JobRow

from .planner import PlannedRunJob
from .features import build_task_feature_bundle

try:
    from tqdm import tqdm
except Exception:  # pragma: no cover - fallback when tqdm is unavailable
    tqdm = None


class SolverAttemptState:
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    BUDGET_EXCEEDED = "budget_exceeded"
    INVALID_PATCH = "invalid_patch"


class SolverSchedulerError(RuntimeError):
    """Raised when the solver scheduler cannot execute jobs."""


@dataclass(frozen=True)
class SolverSchedulerConfig:
    max_parallel_jobs: int = 4
    provider_parallelism: dict[str, int] = field(default_factory=dict)
    # Maximum attempts per minute per provider_id. Omitted providers are unthrottled.
    provider_rate_limit_per_minute: dict[str, int] = field(default_factory=dict)
    solver_budget: dict[str, int] = field(default_factory=dict)
    default_solver_budget: int = 1
    retriable_states: tuple[str, ...] = field(
        default_factory=lambda: (
            SolverAttemptState.FAILED,
            SolverAttemptState.TIMED_OUT,
        )
    )
    persist_jobs_to: Path | None = None
    persist_attempts_to: Path | None = None
    persist_attempts_parquet: Path | None = None
    persist_attempt_logs_root: Path | None = None


@dataclass(frozen=True)
class SolverAdapterRequest:
    attempt_id: str
    attempt_index: int
    job: PlannedRunJob
    instance_row: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class SolverAdapterResult:
    attempt_id: str
    status: str
    model_patch: str | None = None
    raw_output: str = ""
    stderr_output: str = ""
    exit_reason: str = ""
    usage_source: str = ""
    cost_source: str = ""
    usage: Mapping[str, Any] = field(default_factory=dict)
    cost: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SolverJobProgress:
    job_id: str
    final_status: str
    attempts: int
    attempt_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SolverScheduleResult:
    jobs: tuple[SolverJobProgress, ...]
    completed_at: str


class SolverAdapter(ABC):
    """Base interface for concrete solver implementations."""

    @abstractmethod
    def prepare_request(
        self,
        *,
        job: PlannedRunJob,
        attempt_id: str,
        attempt_index: int,
        instance_row: Mapping[str, Any] | None = None,
    ) -> SolverAdapterRequest:
        """Return a request object for one scheduler attempt."""

    @abstractmethod
    def execute_attempt(self, request: SolverAdapterRequest) -> SolverAdapterResult:
        """Execute one solver attempt and return attempt output."""

    def stream_telemetry(self, attempt_id: str) -> Iterable[Mapping[str, Any]]:
        """Optional streaming hook for live telemetry events."""
        if False:
            yield {  # pragma: no cover - placeholder for static analyzers
                "attempt_id": attempt_id
            }

    def collect_telemetry(self, attempt_id: str) -> tuple[dict[str, Any], ...]:
        """Collect telemetry for a completed attempt."""
        return tuple(self.stream_telemetry(attempt_id))

    @abstractmethod
    def finalize_output(
        self, request: SolverAdapterRequest, result: SolverAdapterResult
    ) -> SolverAdapterResult:
        """Normalize and return the final adapter result."""


@dataclass
class _SimpleRateLimiter:
    calls_per_minute: int
    window_seconds: float = 60.0
    timestamps: deque[float] = field(default_factory=deque)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def acquire(self) -> None:
        if self.calls_per_minute <= 0:
            return

        while True:
            now = time.monotonic()
            with self.lock:
                while (
                    self.timestamps and now - self.timestamps[0] > self.window_seconds
                ):
                    self.timestamps.popleft()

                if len(self.timestamps) < self.calls_per_minute:
                    self.timestamps.append(now)
                    return

                wait_seconds = self.window_seconds - (now - self.timestamps[0])
                self.timestamps.popleft()
            if wait_seconds > 0:
                time.sleep(wait_seconds)


class _ThreadSafeWriter:
    def __init__(self) -> None:
        self.lock = threading.Lock()

    def append_jsonl(self, path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(payload, sort_keys=True) + "\n")


def _now_ts() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"


def _coerce_attempt_status(status: str) -> str:
    if status in (
        SolverAttemptState.QUEUED,
        SolverAttemptState.RUNNING,
        SolverAttemptState.SUCCEEDED,
        SolverAttemptState.FAILED,
        SolverAttemptState.TIMED_OUT,
        SolverAttemptState.BUDGET_EXCEEDED,
        SolverAttemptState.INVALID_PATCH,
    ):
        return status
    return SolverAttemptState.FAILED


class _ProgressReporter:
    def update(self, status: str) -> None:
        """Record one completed job."""

    def close(self) -> None:
        """Flush and close the progress reporter."""


class _NullProgressReporter(_ProgressReporter):
    pass


class _TqdmProgressReporter(_ProgressReporter):
    def __init__(self, total: int) -> None:
        self._counts: dict[str, int] = {
            SolverAttemptState.SUCCEEDED: 0,
            SolverAttemptState.INVALID_PATCH: 0,
            SolverAttemptState.FAILED: 0,
            SolverAttemptState.TIMED_OUT: 0,
            SolverAttemptState.BUDGET_EXCEEDED: 0,
        }
        self._bar = tqdm(
            total=total,
            desc="Run",
            unit="job",
            dynamic_ncols=True,
            file=sys.stderr,
            disable=not sys.stderr.isatty(),
        )
        self._refresh_postfix()

    def _refresh_postfix(self) -> None:
        self._bar.set_postfix(
            {
                "ok": self._counts[SolverAttemptState.SUCCEEDED],
                "invalid": self._counts[SolverAttemptState.INVALID_PATCH],
                "failed": self._counts[SolverAttemptState.FAILED],
                "timed_out": self._counts[SolverAttemptState.TIMED_OUT],
                "budget": self._counts[SolverAttemptState.BUDGET_EXCEEDED],
            }
        )

    def update(self, status: str) -> None:
        normalized = _coerce_attempt_status(status)
        if normalized in self._counts:
            self._counts[normalized] += 1
        else:
            self._counts[SolverAttemptState.FAILED] += 1
        self._bar.update(1)
        self._refresh_postfix()

    def close(self) -> None:
        self._bar.close()


def _create_progress_reporter(total: int) -> _ProgressReporter:
    if total < 1 or tqdm is None:
        return _NullProgressReporter()
    return _TqdmProgressReporter(total)


def _serialize_job_row(
    *,
    job: PlannedRunJob,
    status: str,
    attempts: int,
    started_at: str | None,
    ended_at: str | None,
) -> dict[str, Any]:
    row = JobRow(
        job_id=job.job_id,
        instance_id=job.instance_id,
        solver_id=job.solver_id,
        status=status,
        started_at=started_at,
        ended_at=ended_at,
        attempts=attempts,
        metadata={"provider_id": job.provider_id},
    )
    return row.to_dict()


def _serialize_attempt_row(
    *,
    attempt_id: str,
    job: PlannedRunJob,
    attempt_state: str,
    elapsed_ms: int,
    patch: str | None,
    raw_output: str,
    usage: Mapping[str, Any],
    cost: Mapping[str, Any],
    usage_source: str,
    cost_source: str,
    exit_reason: str,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    row = AttemptRow(
        attempt_id=attempt_id,
        job_id=job.job_id,
        instance_id=job.instance_id,
        solver_id=job.solver_id,
        duration_ms=elapsed_ms,
        exit_reason=exit_reason,
        model_patch=patch,
        usage=dict(usage),
        usage_source=usage_source,
        cost=dict(cost),
        cost_source=cost_source,
        metadata=dict(metadata, attempt_state=attempt_state),
    )
    payload = row.to_dict()
    payload["raw_output"] = raw_output
    payload["attempt_state"] = attempt_state
    return payload


def _safe_attempt_log_dir(root: Path, attempt_id: str) -> Path:
    safe_id = "".join(
        c if c.isalnum() or c in {"-", "_", ":"} else "-" for c in attempt_id
    )
    if not safe_id:
        safe_id = "attempt"
    return root / safe_id


def _coerce_attempt_index(attempt_id: str) -> int:
    parts = attempt_id.rsplit(":attempt-", 1)
    if len(parts) != 2:
        return 1
    try:
        return int(parts[-1])
    except ValueError:
        return 1


def _normalize_attempt_row(
    *,
    attempt_id: str,
    job: PlannedRunJob,
    row: dict[str, Any],
    dataset_row: Mapping[str, Any] | None,
    attempt_started_at: str,
    attempt_ended_at: str,
    attempt_state: str,
) -> dict[str, Any]:
    normalized = dict(row)
    normalized.update(
        {
            "attempt_index": _coerce_attempt_index(attempt_id),
            "attempt_started_at": attempt_started_at,
            "attempt_ended_at": attempt_ended_at,
            "attempt_state": attempt_state,
            "run_id": job.run_id,
            "provider_id": job.provider_id,
            "patch_length": len(normalized.get("model_patch") or ""),
            "prompt_policy_hash": job.prompt_policy_hash,
            "tool_policy_hash": job.tool_policy_hash,
            "solver_config_hash": job.solver_config_hash,
            "dataset_path": job.dataset_path,
            "matrix_path": job.matrix_path,
            "instance_repo": "",
            "instance_base_commit": "",
            "instance_version": "",
            "exit_reason": normalized.get("exit_reason", ""),
        }
    )

    if dataset_row:
        normalized["instance_repo"] = str(dataset_row.get("repo", ""))
        normalized["instance_base_commit"] = str(dataset_row.get("base_commit", ""))
        normalized["instance_version"] = str(dataset_row.get("version", ""))
        normalized["problem_statement"] = dataset_row.get("problem_statement")

    task_features = build_task_feature_bundle(normalized)
    normalized["task_feature_version"] = task_features.feature_version
    normalized["task_feature_hash"] = task_features.feature_hash
    normalized["task_cluster"] = task_features.cluster_label
    normalized["task_features"] = task_features.features

    existing_metadata = normalized.get("metadata", {})
    metadata = dict(existing_metadata) if isinstance(existing_metadata, Mapping) else {}
    metadata.update(task_features.to_metadata())
    normalized["metadata"] = metadata
    return normalized


class SolverScheduler:
    """Run solver jobs through an adapter with bounded retries and scheduler controls."""

    def __init__(
        self,
        config: SolverSchedulerConfig | None = None,
    ) -> None:
        config = config or SolverSchedulerConfig()
        if config.max_parallel_jobs < 1:
            raise SolverSchedulerError("max_parallel_jobs must be >= 1")
        if config.default_solver_budget < 1:
            raise SolverSchedulerError("default_solver_budget must be >= 1")

        self.config = config
        self._global_sema = threading.Semaphore(config.max_parallel_jobs)
        self._provider_sema = {
            provider_id: threading.Semaphore(value)
            for provider_id, value in config.provider_parallelism.items()
        }
        self._fallback_provider_sema = threading.Semaphore(config.max_parallel_jobs)
        self._rate_limiters: dict[str, _SimpleRateLimiter] = {
            provider_id: _SimpleRateLimiter(calls_per_minute=calls_per_minute)
            for provider_id, calls_per_minute in (
                config.provider_rate_limit_per_minute.items()
            )
        }
        self._writer = _ThreadSafeWriter()
        self._attempt_rows: list[dict[str, Any]] = []
        self._attempt_rows_lock = threading.Lock()

    def _flush_attempts_parquet(self) -> None:
        if self.config.persist_attempts_parquet is None:
            return

        with self._attempt_rows_lock:
            rows = tuple(self._attempt_rows)
            self._attempt_rows.clear()

        if not rows:
            return

        try:
            import pyarrow as pa
            import pyarrow.parquet as pq  # type: ignore

            table = pa.Table.from_pylist(list(rows))
            self.config.persist_attempts_parquet.parent.mkdir(
                parents=True, exist_ok=True
            )
            pq.write_table(table, str(self.config.persist_attempts_parquet))
            return
        except Exception:
            for row in rows:
                self._writer.append_jsonl(self.config.persist_attempts_parquet, row)
            return

    def run(
        self,
        jobs: Iterable[PlannedRunJob],
        *,
        adapters: Mapping[str, SolverAdapter],
        dataset_rows: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> SolverScheduleResult:
        """Execute all jobs and return a compact completion summary."""
        jobs_list = list(jobs)
        if not jobs_list:
            return SolverScheduleResult(jobs=(), completed_at=_now_ts())

        job_futures = []
        results: list[SolverJobProgress] = []
        dataset_rows = dict(dataset_rows or {})
        progress = _create_progress_reporter(len(jobs_list))

        try:
            with ThreadPoolExecutor(max_workers=self.config.max_parallel_jobs) as pool:
                for job in jobs_list:
                    adapter = adapters.get(job.solver_id)
                    if adapter is None:
                        raise SolverSchedulerError(
                            f"no adapter for solver '{job.solver_id}'"
                        )

                    job_futures.append(
                        pool.submit(
                            self._execute_job,
                            job=job,
                            adapter=adapter,
                            dataset_row=dataset_rows.get(job.instance_id),
                        )
                    )

                for future in as_completed(job_futures):
                    job_result = future.result()
                    results.append(job_result)
                    progress.update(job_result.final_status)
                self._flush_attempts_parquet()
        finally:
            progress.close()

        return SolverScheduleResult(
            jobs=tuple(results),
            completed_at=_now_ts(),
        )

    def _get_provider_semaphore(self, provider_id: str) -> threading.Semaphore:
        if provider_id in self._provider_sema:
            return self._provider_sema[provider_id]
        return self._fallback_provider_sema

    def _get_rate_limiter(self, provider_id: str) -> _SimpleRateLimiter | None:
        return self._rate_limiters.get(provider_id)

    def _attempt_budget(self, solver_id: str) -> int:
        budget = self.config.solver_budget.get(
            solver_id, self.config.default_solver_budget
        )
        if budget < 1:
            raise SolverSchedulerError(
                f"solver budget for '{solver_id}' must be >= 1, got {budget}"
            )
        return budget

    @contextmanager
    def _resource_guards(self, provider_id: str) -> Iterable[None]:
        provider_sema = self._get_provider_semaphore(provider_id)
        limiter = self._get_rate_limiter(provider_id)

        self._global_sema.acquire()
        provider_sema.acquire()
        try:
            if limiter:
                limiter.acquire()
            yield
        finally:
            provider_sema.release()
            self._global_sema.release()

    def _persist_job(
        self,
        *,
        job: PlannedRunJob,
        status: str,
        attempts: int,
        started_at: str | None,
        ended_at: str | None,
    ) -> None:
        if self.config.persist_jobs_to is None:
            return
        payload = _serialize_job_row(
            job=job,
            status=_coerce_attempt_status(status),
            attempts=attempts,
            started_at=started_at,
            ended_at=ended_at,
        )
        self._writer.append_jsonl(self.config.persist_jobs_to, payload)

    def _persist_attempt(
        self,
        *,
        attempt_id: str,
        job: PlannedRunJob,
        attempt_state: str,
        elapsed_ms: int,
        result: SolverAdapterResult,
        raw_output: str,
        dataset_row: Mapping[str, Any] | None,
        started_at: str,
        ended_at: str,
    ) -> None:
        if (
            self.config.persist_attempts_to is None
            and self.config.persist_attempts_parquet is None
            and self.config.persist_attempt_logs_root is None
        ):
            return
        payload = _serialize_attempt_row(
            attempt_id=attempt_id,
            job=job,
            attempt_state=attempt_state,
            elapsed_ms=elapsed_ms,
            patch=result.model_patch,
            raw_output=raw_output,
            usage=result.usage,
            usage_source=result.usage_source,
            cost=result.cost,
            cost_source=result.cost_source,
            exit_reason=result.exit_reason,
            metadata=result.metadata,
        )
        normalized_payload = _normalize_attempt_row(
            attempt_id=attempt_id,
            job=job,
            row=payload,
            dataset_row=dataset_row,
            attempt_started_at=started_at,
            attempt_ended_at=ended_at,
            attempt_state=attempt_state,
        )
        if self.config.persist_attempt_logs_root is not None:
            logs_root = self.config.persist_attempt_logs_root
            logs_root.mkdir(parents=True, exist_ok=True)
            attempt_log_dir = _safe_attempt_log_dir(logs_root, attempt_id)
            attempt_log_dir.mkdir(parents=True, exist_ok=True)
            stdout_log_path = attempt_log_dir / "stdout.log"
            stderr_log_path = attempt_log_dir / "stderr.log"
            stdout_log_path.write_text(raw_output or "", encoding="utf-8")
            stderr_log_path.write_text(
                result.stderr_output or result.exit_reason or "",
                encoding="utf-8",
            )
            normalized_payload["stdout_log_path"] = str(stdout_log_path)
            normalized_payload["stderr_log_path"] = str(stderr_log_path)
        if self.config.persist_attempts_to is not None:
            self._writer.append_jsonl(
                self.config.persist_attempts_to, normalized_payload
            )
        if self.config.persist_attempts_parquet is not None:
            with self._attempt_rows_lock:
                self._attempt_rows.append(normalized_payload)

    def _execute_job(
        self,
        job: PlannedRunJob,
        adapter: SolverAdapter,
        dataset_row: Mapping[str, Any] | None = None,
    ) -> SolverJobProgress:
        attempt_ids: list[str] = []
        budget = self._attempt_budget(job.solver_id)
        attempts = 0
        started_at: str | None = None
        ended_at: str | None = None
        job_status = SolverAttemptState.QUEUED
        self._persist_job(
            job=job,
            status=job_status,
            attempts=attempts,
            started_at=started_at,
            ended_at=ended_at,
        )

        while attempts < budget:
            attempts += 1
            attempt_id = f"{job.job_id}:attempt-{attempts}"
            attempt_ids.append(attempt_id)

            job_status = SolverAttemptState.RUNNING
            if started_at is None:
                started_at = _now_ts()
            self._persist_job(
                job=job,
                status=job_status,
                attempts=attempts,
                started_at=started_at,
                ended_at=None,
            )

            result: SolverAdapterResult | None = None
            attempt_started = datetime.now(timezone.utc).timestamp()
            telemetry = ()
            request = SolverAdapterRequest(
                attempt_id=attempt_id,
                attempt_index=attempts,
                job=job,
                instance_row=dataset_row,
            )

            with self._resource_guards(job.provider_id):
                try:
                    request = adapter.prepare_request(
                        job=job,
                        attempt_id=attempt_id,
                        attempt_index=attempts,
                        instance_row=dataset_row,
                    )
                except Exception as exc:
                    result = SolverAdapterResult(
                        attempt_id=attempt_id,
                        status=SolverAttemptState.FAILED,
                        stderr_output=str(exc),
                        usage_source="",
                        cost_source="",
                        exit_reason=f"adapter_prepare_error: {exc}",
                        raw_output="",
                    )

                if result is None:
                    try:
                        result = adapter.execute_attempt(request)
                    except Exception as exc:
                        result = SolverAdapterResult(
                            attempt_id=attempt_id,
                            status=SolverAttemptState.FAILED,
                            stderr_output=str(exc),
                            usage_source="",
                            cost_source="",
                            exit_reason=f"adapter_execution_error: {exc}",
                            raw_output="",
                        )

                    try:
                        telemetry = adapter.collect_telemetry(attempt_id)
                    except Exception as exc:
                        telemetry = (({"error": f"telemetry_error: {exc}"}),)

                    metadata = dict(result.metadata)
                    metadata["telemetry"] = list(telemetry)
                    result = SolverAdapterResult(
                        attempt_id=result.attempt_id,
                        status=result.status,
                        stderr_output=result.stderr_output,
                        usage_source=result.usage_source,
                        cost_source=result.cost_source,
                        model_patch=result.model_patch,
                        raw_output=result.raw_output,
                        exit_reason=result.exit_reason,
                        usage=result.usage,
                        cost=result.cost,
                        metadata=metadata,
                    )

                    try:
                        result = adapter.finalize_output(request=request, result=result)
                    except Exception as exc:
                        result = SolverAdapterResult(
                            attempt_id=result.attempt_id,
                            status=SolverAttemptState.FAILED,
                            stderr_output=str(exc),
                            usage_source=result.usage_source,
                            cost_source=result.cost_source,
                            model_patch=result.model_patch,
                            raw_output=result.raw_output,
                            exit_reason=f"adapter_finalize_error: {exc}",
                            usage=result.usage,
                            cost=result.cost,
                            metadata=result.metadata,
                        )

            attempt_started_at = datetime.fromtimestamp(
                attempt_started, tz=timezone.utc
            ).replace(tzinfo=None)
            attempt_ended_at = datetime.now(timezone.utc).replace(tzinfo=None)
            attempt_elapsed_ms = int(
                (attempt_ended_at - attempt_started_at).total_seconds() * 1000
            )
            attempt_started_iso = attempt_started_at.isoformat() + "Z"
            attempt_ended_iso = attempt_ended_at.isoformat() + "Z"
            attempt_state = _coerce_attempt_status(result.status)
            self._persist_attempt(
                attempt_id=attempt_id,
                job=job,
                attempt_state=attempt_state,
                elapsed_ms=attempt_elapsed_ms,
                result=result,
                raw_output=result.raw_output,
                dataset_row=dataset_row,
                started_at=attempt_started_iso,
                ended_at=attempt_ended_iso,
            )

            if attempt_state == SolverAttemptState.SUCCEEDED:
                job_status = attempt_state
                ended_at = _now_ts()
                break

            if attempt_state in (
                SolverAttemptState.INVALID_PATCH,
                SolverAttemptState.BUDGET_EXCEEDED,
            ):
                ended_at = _now_ts()
                job_status = attempt_state
                break

            if attempt_state == SolverAttemptState.TIMED_OUT:
                if attempts < budget and attempt_state in self.config.retriable_states:
                    job_status = SolverAttemptState.QUEUED
                    self._persist_job(
                        job=job,
                        status=job_status,
                        attempts=attempts,
                        started_at=started_at,
                        ended_at=None,
                    )
                    continue
                job_status = SolverAttemptState.BUDGET_EXCEEDED
                ended_at = _now_ts()
                break

            if (
                attempt_state == SolverAttemptState.FAILED
                and attempt_state in self.config.retriable_states
            ):
                if attempts < budget:
                    job_status = SolverAttemptState.QUEUED
                    self._persist_job(
                        job=job,
                        status=job_status,
                        attempts=attempts,
                        started_at=started_at,
                        ended_at=None,
                    )
                    continue

                job_status = SolverAttemptState.BUDGET_EXCEEDED
                ended_at = _now_ts()
                break

            job_status = attempt_state
            ended_at = _now_ts()
            break

        self._persist_job(
            job=job,
            status=job_status,
            attempts=attempts,
            started_at=started_at,
            ended_at=ended_at,
        )
        return SolverJobProgress(
            job_id=job.job_id,
            final_status=job_status,
            attempts=attempts,
            attempt_ids=tuple(attempt_ids),
        )
