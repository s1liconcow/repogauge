from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
import threading

from repogauge.runner.planner import PlannedRunJob
from repogauge.runner.scheduler import (
    SolverAdapter,
    SolverAdapterRequest,
    SolverAdapterResult,
    SolverAttemptState,
    SolverScheduler,
    SolverSchedulerConfig,
)


def _job(
    *,
    job_id: str,
    instance_id: str = "i-1",
    solver_id: str = "solver-a",
    provider_id: str = "mock",
) -> PlannedRunJob:
    return PlannedRunJob(
        run_id="run-1",
        job_id=job_id,
        instance_id=instance_id,
        solver_id=solver_id,
        provider_id=provider_id,
        seed=7,
        prompt_policy_hash="p",
        tool_policy_hash="t",
        solver_config_hash="s",
        dataset_path="/tmp/dataset.jsonl",
        matrix_path="/tmp/matrix.yaml",
        metadata={"provider": provider_id},
    )


class ReplayAdapter(SolverAdapter):
    """Adapter that returns a scripted list of attempt statuses."""

    def __init__(self, statuses: list[str], patch: str = "") -> None:
        self._statuses = list(statuses)
        self._patch = patch
        self.prepare_calls: list[str] = []
        self.execute_calls: list[str] = []

    def prepare_request(
        self,
        *,
        job: PlannedRunJob,
        attempt_id: str,
        attempt_index: int,
        instance_row=None,
    ) -> SolverAdapterRequest:
        self.prepare_calls.append(attempt_id)
        return SolverAdapterRequest(
            attempt_id=attempt_id,
            attempt_index=attempt_index,
            job=replace(job, metadata={"attempt_index": attempt_index}),
            instance_row=None,
        )

    def execute_attempt(self, request: SolverAdapterRequest) -> SolverAdapterResult:
        self.execute_calls.append(request.attempt_id)
        status = (
            self._statuses[min(request.attempt_index - 1, len(self._statuses) - 1)]
            if self._statuses
            else SolverAttemptState.FAILED
        )
        return SolverAdapterResult(
            attempt_id=request.attempt_id,
            status=status,
            model_patch=self._patch if status == SolverAttemptState.SUCCEEDED else None,
            raw_output=f"{status}-{request.attempt_id}",
        )

    def finalize_output(
        self, request: SolverAdapterRequest, result: SolverAdapterResult
    ) -> SolverAdapterResult:
        return result


class CaptureAdapter(SolverAdapter):
    """Adapter that records request inputs for assertions."""

    def __init__(
        self, statuses: list[str], patch: str = "", timed: float = 0.0
    ) -> None:
        self._statuses = list(statuses)
        self._patch = patch
        self._timed = timed
        self.prepare_requests: list[
            tuple[tuple[str, str], dict[str, object] | None]
        ] = []

    def prepare_request(
        self,
        *,
        job: PlannedRunJob,
        attempt_id: str,
        attempt_index: int,
        instance_row=None,
    ) -> SolverAdapterRequest:
        row = None if instance_row is None else dict(instance_row)
        self.prepare_requests.append(((attempt_id, job.job_id), row))
        return SolverAdapterRequest(
            attempt_id=attempt_id,
            attempt_index=attempt_index,
            job=replace(job, metadata={"attempt_index": attempt_index}),
            instance_row=instance_row,
        )

    def execute_attempt(self, request: SolverAdapterRequest) -> SolverAdapterResult:
        if self._timed:
            time.sleep(self._timed)
        status = (
            self._statuses[min(request.attempt_index - 1, len(self._statuses) - 1)]
            if self._statuses
            else SolverAttemptState.FAILED
        )
        return SolverAdapterResult(
            attempt_id=request.attempt_id,
            status=status,
            model_patch=self._patch if status == SolverAttemptState.SUCCEEDED else None,
            raw_output=f"{status}-{request.attempt_id}",
        )

    def finalize_output(
        self, request: SolverAdapterRequest, result: SolverAdapterResult
    ) -> SolverAdapterResult:
        return result


class SleepyAdapter(SolverAdapter):
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.lock = None

    def prepare_request(
        self,
        *,
        job: PlannedRunJob,
        attempt_id: str,
        attempt_index: int,
        instance_row=None,
    ) -> SolverAdapterRequest:
        return SolverAdapterRequest(
            attempt_id=attempt_id,
            attempt_index=attempt_index,
            job=job,
            instance_row=None,
        )

    def execute_attempt(self, request: SolverAdapterRequest) -> SolverAdapterResult:
        if self.lock is None:
            self.lock = threading.Lock()

        with self.lock:
            self.active += 1
            if self.active > self.max_active:
                self.max_active = self.active
        try:
            time.sleep(0.1)
        finally:
            with self.lock:
                self.active -= 1
        return SolverAdapterResult(
            attempt_id=request.attempt_id,
            status=SolverAttemptState.SUCCEEDED,
            raw_output="",
        )

    def finalize_output(
        self, request: SolverAdapterRequest, result: SolverAdapterResult
    ) -> SolverAdapterResult:
        return result


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_scheduler_records_job_attempt_state_transitions(tmp_path: Path) -> None:
    job = _job(job_id="run-1:i-1:solver-a:0")
    scheduler = SolverScheduler(
        config=SolverSchedulerConfig(
            default_solver_budget=3,
            persist_jobs_to=tmp_path / "jobs.jsonl",
            persist_attempts_to=tmp_path / "attempts.jsonl",
        )
    )
    adapter = ReplayAdapter(
        [
            SolverAttemptState.FAILED,
            SolverAttemptState.FAILED,
            SolverAttemptState.SUCCEEDED,
        ]
    )

    summary = scheduler.run([job], adapters={"solver-a": adapter})

    assert summary.jobs[0].final_status == SolverAttemptState.SUCCEEDED
    assert summary.jobs[0].attempts == 3

    job_rows = _read_jsonl(tmp_path / "jobs.jsonl")
    assert len(job_rows) >= 3
    assert job_rows[0]["status"] == SolverAttemptState.QUEUED
    assert job_rows[-1]["status"] == SolverAttemptState.SUCCEEDED
    assert job_rows[-1]["attempts"] == 3

    attempt_rows = _read_jsonl(tmp_path / "attempts.jsonl")
    assert len(attempt_rows) == 3
    assert attempt_rows[-1]["attempt_state"] == SolverAttemptState.SUCCEEDED
    assert attempt_rows[-1]["exit_reason"] == ""
    assert attempt_rows[0]["attempt_state"] == SolverAttemptState.FAILED


def test_scheduler_retries_are_budgeted_and_exhausted(tmp_path: Path) -> None:
    job = _job(job_id="run-1:i-1:solver-b:0", solver_id="solver-b")
    scheduler = SolverScheduler(
        config=SolverSchedulerConfig(
            default_solver_budget=2,
            persist_jobs_to=tmp_path / "jobs.jsonl",
            persist_attempts_to=tmp_path / "attempts.jsonl",
        )
    )
    adapter = ReplayAdapter(
        [SolverAttemptState.TIMED_OUT, SolverAttemptState.TIMED_OUT]
    )

    summary = scheduler.run([job], adapters={"solver-b": adapter})

    assert summary.jobs[0].final_status == "budget_exceeded"
    assert summary.jobs[0].attempts == 2
    assert len(_read_jsonl(tmp_path / "attempts.jsonl")) == 2


def test_provider_parallelism_is_enforced(tmp_path: Path) -> None:
    jobs = [_job(job_id=f"run-1:i-{i}:solver-a:0") for i in range(2)]
    adapter = SleepyAdapter()
    scheduler = SolverScheduler(
        config=SolverSchedulerConfig(
            max_parallel_jobs=2,
            provider_parallelism={"mock": 1},
            default_solver_budget=1,
        )
    )

    start = time.perf_counter()
    summary = scheduler.run(jobs, adapters={"solver-a": adapter})
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert summary.jobs[0].final_status == SolverAttemptState.SUCCEEDED
    assert summary.jobs[1].final_status == SolverAttemptState.SUCCEEDED
    assert adapter.max_active == 1
    assert elapsed_ms >= 180


def test_scheduler_preserves_dataset_row_in_prepare_request(tmp_path: Path) -> None:
    job = _job(job_id="run-1:i-1:solver-a:0")
    scheduler = SolverScheduler(config=SolverSchedulerConfig(default_solver_budget=1))
    adapter = CaptureAdapter([SolverAttemptState.SUCCEEDED])
    dataset_rows = {
        "i-1": {
            "instance_id": "i-1",
            "repo": "sample/repo",
            "problem_statement": "fix me",
        }
    }

    summary = scheduler.run(
        [job],
        adapters={"solver-a": adapter},
        dataset_rows=dataset_rows,
    )

    assert summary.jobs[0].final_status == SolverAttemptState.SUCCEEDED
    assert adapter.prepare_requests == [
        (
            ("run-1:i-1:solver-a:0:attempt-1", "run-1:i-1:solver-a:0"),
            dataset_rows["i-1"],
        )
    ]


def test_scheduler_applies_solver_budget_and_marks_terminal_state(
    tmp_path: Path,
) -> None:
    job = _job(job_id="run-1:i-1:solver-b:0", solver_id="solver-b")
    scheduler = SolverScheduler(config=SolverSchedulerConfig(default_solver_budget=1))
    adapter = CaptureAdapter([SolverAttemptState.INVALID_PATCH])

    summary = scheduler.run([job], adapters={"solver-b": adapter})
    assert summary.jobs[0].final_status == SolverAttemptState.INVALID_PATCH
    assert summary.jobs[0].attempts == 1


def test_scheduler_rate_limit_is_respected(tmp_path: Path) -> None:
    jobs = [_job(job_id=f"run-1:i-{i}:solver-a:0") for i in range(2)]
    adapter = CaptureAdapter([SolverAttemptState.SUCCEEDED], timed=0.0)
    config = SolverSchedulerConfig(
        max_parallel_jobs=2,
        provider_rate_limit_per_minute={"mock": 1},
        default_solver_budget=1,
    )
    scheduler = SolverScheduler(config=config)
    scheduler._rate_limiters["mock"].window_seconds = 0.12

    start = time.perf_counter()
    summary = scheduler.run(jobs, adapters={"solver-a": adapter})
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert summary.jobs[0].final_status == SolverAttemptState.SUCCEEDED
    assert summary.jobs[1].final_status == SolverAttemptState.SUCCEEDED
    assert elapsed_ms >= 100
