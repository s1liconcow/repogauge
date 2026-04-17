from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
import threading

from repogauge.exec import run_command
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
        workspace_path=None,
    ) -> SolverAdapterRequest:
        self.prepare_calls.append(attempt_id)
        return SolverAdapterRequest(
            attempt_id=attempt_id,
            attempt_index=attempt_index,
            job=replace(job, metadata={"attempt_index": attempt_index}),
            instance_row=None,
            workspace_path=workspace_path,
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
        workspace_path=None,
    ) -> SolverAdapterRequest:
        row = None if instance_row is None else dict(instance_row)
        self.prepare_requests.append(((attempt_id, job.job_id), row))
        return SolverAdapterRequest(
            attempt_id=attempt_id,
            attempt_index=attempt_index,
            job=replace(job, metadata={"attempt_index": attempt_index}),
            instance_row=instance_row,
            workspace_path=workspace_path,
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


class CanonicalPatchAdapter(SolverAdapter):
    """Adapter that returns a valid model patch with non-diff raw output."""

    def __init__(self, *, patch: str, raw_output: str) -> None:
        self._patch = patch
        self._raw_output = raw_output

    def requires_workspace(self) -> bool:
        return True

    def prepare_request(
        self,
        *,
        job: PlannedRunJob,
        attempt_id: str,
        attempt_index: int,
        instance_row=None,
        workspace_path=None,
    ) -> SolverAdapterRequest:
        return SolverAdapterRequest(
            attempt_id=attempt_id,
            attempt_index=attempt_index,
            job=job,
            instance_row=instance_row,
            workspace_path=workspace_path,
        )

    def execute_attempt(self, request: SolverAdapterRequest) -> SolverAdapterResult:
        return SolverAdapterResult(
            attempt_id=request.attempt_id,
            status=SolverAttemptState.SUCCEEDED,
            model_patch=self._patch,
            raw_output=self._raw_output,
        )

    def finalize_output(
        self, request: SolverAdapterRequest, result: SolverAdapterResult
    ) -> SolverAdapterResult:
        return result


class SourceAwareAdapter(SolverAdapter):
    """Adapter that returns fixed usage/cost metadata with provenance."""

    def __init__(
        self,
        *,
        status: str,
        usage: dict[str, object],
        cost: dict[str, object],
        usage_source: str,
        cost_source: str,
    ) -> None:
        self._status = status
        self._usage = usage
        self._cost = cost
        self._usage_source = usage_source
        self._cost_source = cost_source
        self.prepare_request_calls: list[str] = []
        self.execute_calls: list[str] = []

    def prepare_request(
        self,
        *,
        job: PlannedRunJob,
        attempt_id: str,
        attempt_index: int,
        instance_row=None,
        workspace_path=None,
    ) -> SolverAdapterRequest:
        self.prepare_request_calls.append(attempt_id)
        return SolverAdapterRequest(
            attempt_id=attempt_id,
            attempt_index=attempt_index,
            job=job,
            instance_row=instance_row,
            workspace_path=workspace_path,
        )

    def execute_attempt(self, request: SolverAdapterRequest) -> SolverAdapterResult:
        self.execute_calls.append(request.attempt_id)
        return SolverAdapterResult(
            attempt_id=request.attempt_id,
            status=self._status,
            model_patch="diff --git a/x b/x\n+ok",
            raw_output="ok",
            usage_source=self._usage_source,
            cost_source=self._cost_source,
            usage=self._usage,
            cost=self._cost,
        )

    def finalize_output(
        self, request: SolverAdapterRequest, result: SolverAdapterResult
    ) -> SolverAdapterResult:
        return result


class StdoutStderrAdapter(SolverAdapter):
    def prepare_request(
        self,
        *,
        job: PlannedRunJob,
        attempt_id: str,
        attempt_index: int,
        instance_row=None,
        workspace_path=None,
    ) -> SolverAdapterRequest:
        return SolverAdapterRequest(
            attempt_id=attempt_id,
            attempt_index=attempt_index,
            job=job,
            instance_row=instance_row,
            workspace_path=workspace_path,
        )

    def execute_attempt(self, request: SolverAdapterRequest) -> SolverAdapterResult:
        return SolverAdapterResult(
            attempt_id=request.attempt_id,
            status=SolverAttemptState.FAILED,
            model_patch=None,
            raw_output="stdout payload\nsecond line\n",
            stderr_output="stderr payload\n",
            exit_reason="command failed",
        )

    def finalize_output(
        self, request: SolverAdapterRequest, result: SolverAdapterResult
    ) -> SolverAdapterResult:
        return result


class PrepareErrorAdapter(ReplayAdapter):
    def __init__(self, statuses: list[str], fail_prepare_attempts: set[int]) -> None:
        super().__init__(statuses)
        self.fail_prepare_attempts = fail_prepare_attempts

    def prepare_request(
        self,
        *,
        job: PlannedRunJob,
        attempt_id: str,
        attempt_index: int,
        instance_row=None,
        workspace_path=None,
    ) -> SolverAdapterRequest:
        if attempt_index in self.fail_prepare_attempts:
            raise RuntimeError("prepare failed")
        return super().prepare_request(
            job=job,
            attempt_id=attempt_id,
            attempt_index=attempt_index,
            instance_row=instance_row,
            workspace_path=workspace_path,
        )


class MetadataAwareAdapter(SolverAdapter):
    """Adapter that returns fixed metadata from execute attempts."""

    def __init__(self, statuses: list[str], telemetry: list[dict[str, object]]) -> None:
        self._statuses = list(statuses)
        self._telemetry = telemetry

    def prepare_request(
        self,
        *,
        job: PlannedRunJob,
        attempt_id: str,
        attempt_index: int,
        instance_row=None,
        workspace_path=None,
    ) -> SolverAdapterRequest:
        return SolverAdapterRequest(
            attempt_id=attempt_id,
            attempt_index=attempt_index,
            job=job,
            instance_row=instance_row,
            workspace_path=workspace_path,
        )

    def execute_attempt(self, request: SolverAdapterRequest) -> SolverAdapterResult:
        status = (
            self._statuses[min(request.attempt_index - 1, len(self._statuses) - 1)]
            if self._statuses
            else SolverAttemptState.FAILED
        )
        return SolverAdapterResult(
            attempt_id=request.attempt_id,
            status=status,
            model_patch="diff --git a/x b/x\n+ok"
            if status == SolverAttemptState.SUCCEEDED
            else None,
            raw_output="ok",
            usage_source="response.usage",
            cost_source="response.cost",
            usage={"input_tokens": 1},
            cost={"total_cost": 0.1},
            metadata={"telemetry": list(self._telemetry)},
        )


class TelemetryErrorAdapter(CaptureAdapter):
    def __init__(
        self, statuses: list[str], patch: str = "", timed: float = 0.0
    ) -> None:
        super().__init__(statuses, patch=patch, timed=timed)
        self.telemetry_calls: list[str] = []

    def collect_telemetry(self, attempt_id: str) -> tuple[dict[str, str], ...]:
        self.telemetry_calls.append(attempt_id)
        raise RuntimeError("telemetry unavailable")


class FinalizeErrorAdapter(ReplayAdapter):
    def __init__(self, statuses: list[str], fail_finalize_attempts: set[int]) -> None:
        super().__init__(statuses)
        self.fail_finalize_attempts = fail_finalize_attempts

    def finalize_output(
        self, request: SolverAdapterRequest, result: SolverAdapterResult
    ) -> SolverAdapterResult:
        if request.attempt_index in self.fail_finalize_attempts:
            raise RuntimeError("finalize failed")
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
        workspace_path=None,
    ) -> SolverAdapterRequest:
        return SolverAdapterRequest(
            attempt_id=attempt_id,
            attempt_index=attempt_index,
            job=job,
            instance_row=None,
            workspace_path=workspace_path,
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
    if path.suffix == ".parquet":
        try:
            import pyarrow.parquet as pq  # type: ignore

            table = pq.read_table(path)
            return table.to_pylist()
        except Exception:
            pass
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _create_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_command(["git", "init", "-b", "main"], cwd=str(repo))
    run_command(["git", "config", "user.name", "ci"], cwd=str(repo))
    run_command(["git", "config", "user.email", "ci@example.com"], cwd=str(repo))
    (repo / "src.py").write_text("print('before')\n", encoding="utf-8")
    run_command(["git", "add", "src.py"], cwd=str(repo))
    run_command(["git", "commit", "-m", "base"], cwd=str(repo))
    commit = run_command(["git", "-C", str(repo), "rev-parse", "HEAD"]).stdout.strip()
    return repo, commit


class WorkspaceAwareAdapter(SolverAdapter):
    def __init__(self) -> None:
        self.workspace_paths: list[Path] = []

    def requires_workspace(self) -> bool:
        return True

    def prepare_request(
        self,
        *,
        job: PlannedRunJob,
        attempt_id: str,
        attempt_index: int,
        instance_row=None,
        workspace_path=None,
    ) -> SolverAdapterRequest:
        return SolverAdapterRequest(
            attempt_id=attempt_id,
            attempt_index=attempt_index,
            job=job,
            instance_row=instance_row,
            workspace_path=workspace_path,
        )

    def execute_attempt(self, request: SolverAdapterRequest) -> SolverAdapterResult:
        assert request.workspace_path is not None
        self.workspace_paths.append(request.workspace_path)
        before = (request.workspace_path / "src.py").read_text(encoding="utf-8")
        assert before == "print('before')\n"
        return SolverAdapterResult(
            attempt_id=request.attempt_id,
            status=SolverAttemptState.SUCCEEDED,
            raw_output=(
                "diff --git a/src.py b/src.py\n"
                "index 1111111..2222222 100644\n"
                "--- a/src.py\n"
                "+++ b/src.py\n"
                "@@ -1 +1 @@\n"
                "-print('before')\n"
                "+print('after')\n"
            ),
        )

    def finalize_output(
        self, request: SolverAdapterRequest, result: SolverAdapterResult
    ) -> SolverAdapterResult:
        return result


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


def test_scheduler_persists_usage_cost_sources_in_attempt_rows(tmp_path: Path) -> None:
    job = _job(job_id="run-1:i-1:solver-a:0")
    scheduler = SolverScheduler(
        config=SolverSchedulerConfig(
            default_solver_budget=1,
            persist_attempts_to=tmp_path / "attempts.jsonl",
        )
    )
    adapter = SourceAwareAdapter(
        status=SolverAttemptState.SUCCEEDED,
        usage={"input_tokens": 7},
        cost={"total_cost": 0.07},
        usage_source="response.usage",
        cost_source="response.cost",
    )

    summary = scheduler.run([job], adapters={"solver-a": adapter})

    assert summary.jobs[0].final_status == SolverAttemptState.SUCCEEDED
    attempt_rows = _read_jsonl(tmp_path / "attempts.jsonl")
    assert len(attempt_rows) == 1
    assert attempt_rows[0]["usage_source"] == "response.usage"
    assert attempt_rows[0]["cost_source"] == "response.cost"


def test_scheduler_writes_normalized_attempt_parquet_rows(tmp_path: Path) -> None:
    job = _job(job_id="run-1:i-1:solver-a:0")
    attempts_jsonl = tmp_path / "attempts.jsonl"
    attempts_parquet = tmp_path / "attempts.parquet"
    scheduler = SolverScheduler(
        config=SolverSchedulerConfig(
            default_solver_budget=1,
            persist_attempts_to=attempts_jsonl,
            persist_attempts_parquet=attempts_parquet,
        )
    )
    adapter = SourceAwareAdapter(
        status=SolverAttemptState.SUCCEEDED,
        usage={"input_tokens": 7},
        cost={"total_cost": 0.07},
        usage_source="response.usage",
        cost_source="response.cost",
    )

    summary = scheduler.run([job], adapters={"solver-a": adapter})

    assert summary.jobs[0].final_status == SolverAttemptState.SUCCEEDED
    parquet_rows = _read_jsonl(attempts_parquet)
    assert len(parquet_rows) == 1
    row = parquet_rows[0]
    assert row["run_id"] == "run-1"
    assert row["provider_id"] == "mock"
    assert row["attempt_index"] == 1
    assert row["attempt_started_at"]
    assert row["attempt_ended_at"]
    assert row["attempt_state"] == SolverAttemptState.SUCCEEDED
    assert row["prompt_policy_hash"] == "p"
    assert row["tool_policy_hash"] == "t"
    assert row["solver_config_hash"] == "s"


def test_scheduler_persists_per_attempt_stdout_and_stderr_logs(tmp_path: Path) -> None:
    job = _job(job_id="run-1:i-1:solver-a:0")
    logs_root = tmp_path / "attempt_logs"
    scheduler = SolverScheduler(
        config=SolverSchedulerConfig(
            default_solver_budget=1,
            persist_attempts_to=tmp_path / "attempts.jsonl",
            persist_attempt_logs_root=logs_root,
        )
    )
    adapter = StdoutStderrAdapter()

    summary = scheduler.run([job], adapters={"solver-a": adapter})

    assert summary.jobs[0].final_status == SolverAttemptState.FAILED
    attempt_rows = _read_jsonl(tmp_path / "attempts.jsonl")
    assert len(attempt_rows) == 1
    stdout_log = Path(attempt_rows[0]["stdout_log_path"])
    stderr_log = Path(attempt_rows[0]["stderr_log_path"])
    assert stdout_log.exists()
    assert stderr_log.exists()
    assert stdout_log.read_text(encoding="utf-8") == "stdout payload\nsecond line\n"
    assert stderr_log.read_text(encoding="utf-8") == "stderr payload\n"
    assert stdout_log.parent.parent == logs_root


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

    assert summary.jobs[0].final_status == SolverAttemptState.TIMED_OUT
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


def test_scheduler_runs_workspace_backed_attempts_in_isolated_worktree(
    tmp_path: Path,
) -> None:
    repo_root, commit = _create_repo(tmp_path)
    job = _job(job_id="run-1:i-1:solver-a:0")
    attempts_jsonl = tmp_path / "attempts.jsonl"
    scheduler = SolverScheduler(
        config=SolverSchedulerConfig(
            default_solver_budget=1,
            persist_attempts_to=attempts_jsonl,
            source_repo_root=repo_root,
            attempt_workspaces_root=tmp_path / "attempt_workspaces",
        )
    )
    adapter = WorkspaceAwareAdapter()
    dataset_rows = {
        "i-1": {
            "instance_id": "i-1",
            "repo": "sample/repo",
            "base_commit": commit,
            "version": "1.0.0",
            "problem_statement": "Change the output text.",
        }
    }

    summary = scheduler.run(
        [job],
        adapters={"solver-a": adapter},
        dataset_rows=dataset_rows,
    )

    assert summary.jobs[0].final_status == SolverAttemptState.SUCCEEDED
    assert len(adapter.workspace_paths) == 1
    assert not adapter.workspace_paths[0].exists()

    attempt_rows = _read_jsonl(attempts_jsonl)
    assert len(attempt_rows) == 1
    row = attempt_rows[0]
    assert row["attempt_state"] == SolverAttemptState.SUCCEEDED
    assert "diff --git a/src.py b/src.py" in row["model_patch"]
    assert row["metadata"]["instruction_pack_path"]
    assert row["metadata"]["raw_output_path"]
    assert row["metadata"]["normalized_patch_path"]
    assert row["metadata"]["patch_stats"]["files_touched"] == 1


def test_scheduler_persists_task_feature_bundle_in_attempt_rows(tmp_path: Path) -> None:
    job = _job(job_id="run-1:i-1:solver-a:0")
    scheduler = SolverScheduler(
        config=SolverSchedulerConfig(
            default_solver_budget=1,
            persist_attempts_to=tmp_path / "attempts.jsonl",
        )
    )
    adapter = CaptureAdapter([SolverAttemptState.SUCCEEDED])
    dataset_rows = {
        "i-1": {
            "instance_id": "i-1",
            "repo": "sample/repo",
            "base_commit": "deadbeef",
            "version": "1.0.0",
            "problem_statement": "Traceback while loading cache from disk.",
        }
    }

    summary = scheduler.run(
        [job],
        adapters={"solver-a": adapter},
        dataset_rows=dataset_rows,
    )

    assert summary.jobs[0].final_status == SolverAttemptState.SUCCEEDED

    attempt_rows = _read_jsonl(tmp_path / "attempts.jsonl")
    assert len(attempt_rows) == 1
    attempt_row = attempt_rows[0]
    assert attempt_row["task_feature_version"] == "task-features-v1"
    assert attempt_row["task_cluster"] == "len=short|signal=stacktrace|version=semantic"
    assert attempt_row["task_features"]["problem_statement_signal"] == "stacktrace"
    assert attempt_row["metadata"]["task_feature_version"] == "task-features-v1"


def test_scheduler_prepare_error_is_recorded_and_retried_on_budget(
    tmp_path: Path,
) -> None:
    job = _job(job_id="run-1:i-1:solver-a:0", solver_id="solver-a")
    scheduler = SolverScheduler(
        config=SolverSchedulerConfig(
            default_solver_budget=2,
            persist_attempts_to=tmp_path / "attempts.jsonl",
        )
    )
    adapter = PrepareErrorAdapter(
        [SolverAttemptState.SUCCEEDED],
        fail_prepare_attempts={1},
    )

    summary = scheduler.run([job], adapters={"solver-a": adapter})

    assert summary.jobs[0].final_status == SolverAttemptState.SUCCEEDED
    assert summary.jobs[0].attempts == 2

    attempt_rows = _read_jsonl(tmp_path / "attempts.jsonl")
    assert len(attempt_rows) == 2
    assert attempt_rows[0]["attempt_state"] == SolverAttemptState.FAILED
    assert attempt_rows[0]["exit_reason"] == "adapter_prepare_error: prepare failed"
    assert attempt_rows[1]["attempt_state"] == SolverAttemptState.SUCCEEDED


def test_scheduler_telemetry_error_is_embedded_in_metadata(tmp_path: Path) -> None:
    job = _job(job_id="run-1:i-1:solver-a:0", solver_id="solver-a")
    scheduler = SolverScheduler(
        config=SolverSchedulerConfig(
            default_solver_budget=1,
            persist_attempts_to=tmp_path / "attempts.jsonl",
        )
    )
    adapter = TelemetryErrorAdapter([SolverAttemptState.SUCCEEDED])

    summary = scheduler.run([job], adapters={"solver-a": adapter})

    assert summary.jobs[0].final_status == SolverAttemptState.SUCCEEDED

    attempt_rows = _read_jsonl(tmp_path / "attempts.jsonl")
    assert len(attempt_rows) == 1
    assert attempt_rows[0]["metadata"]["attempt_state"] == SolverAttemptState.SUCCEEDED
    assert attempt_rows[0]["metadata"]["telemetry"] == [
        {"error": "telemetry_error: telemetry unavailable"}
    ]


def test_scheduler_finalize_error_marks_attempt_as_failed(tmp_path: Path) -> None:
    job = _job(job_id="run-1:i-1:solver-a:0", solver_id="solver-a")
    scheduler = SolverScheduler(
        config=SolverSchedulerConfig(
            default_solver_budget=1,
            persist_attempts_to=tmp_path / "attempts.jsonl",
        )
    )
    adapter = FinalizeErrorAdapter(
        [SolverAttemptState.SUCCEEDED], fail_finalize_attempts={1}
    )

    summary = scheduler.run([job], adapters={"solver-a": adapter})

    assert summary.jobs[0].final_status == SolverAttemptState.FAILED
    attempt_rows = _read_jsonl(tmp_path / "attempts.jsonl")
    assert len(attempt_rows) == 1
    assert attempt_rows[0]["attempt_state"] == SolverAttemptState.FAILED
    assert attempt_rows[0]["exit_reason"] == "adapter_finalize_error: finalize failed"


def test_scheduler_applies_solver_budget_and_marks_terminal_state(
    tmp_path: Path,
) -> None:
    job = _job(job_id="run-1:i-1:solver-b:0", solver_id="solver-b")
    scheduler = SolverScheduler(config=SolverSchedulerConfig(default_solver_budget=1))
    adapter = CaptureAdapter([SolverAttemptState.INVALID_PATCH])

    summary = scheduler.run([job], adapters={"solver-b": adapter})
    assert summary.jobs[0].final_status == SolverAttemptState.INVALID_PATCH
    assert summary.jobs[0].attempts == 1


def test_scheduler_workspace_normalizes_from_model_patch_when_raw_output_is_jsonl(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_command(["git", "init", "-b", "main"], cwd=str(repo))
    run_command(["git", "config", "user.name", "ci"], cwd=str(repo))
    run_command(["git", "config", "user.email", "ci@example.com"], cwd=str(repo))
    (repo / "src.py").write_text("print('before')\n", encoding="utf-8")
    run_command(["git", "add", "src.py"], cwd=str(repo))
    run_command(["git", "commit", "-m", "base"], cwd=str(repo))
    commit = run_command(["git", "-C", str(repo), "rev-parse", "HEAD"]).stdout.strip()

    job = _job(job_id="run-1:i-1:solver-a:0", solver_id="solver-a")
    scheduler = SolverScheduler(
        config=SolverSchedulerConfig(
            default_solver_budget=1,
            source_repo_root=repo,
            attempt_workspaces_root=tmp_path / "attempt_workspaces",
            persist_attempts_to=tmp_path / "attempts.jsonl",
        )
    )
    dataset_rows = {
        "i-1": {
            "instance_id": "i-1",
            "repo": "sample/repo",
            "base_commit": commit,
            "version": "1.0.0",
            "problem_statement": "Update the log line.",
        }
    }
    adapter = CanonicalPatchAdapter(
        patch=(
            "diff --git a/src.py b/src.py\n"
            "index 1111111..2222222 100644\n"
            "--- a/src.py\n"
            "+++ b/src.py\n"
            "@@ -1 +1 @@\n"
            "-print('before')\n"
            "+print('after')\n"
        ),
        raw_output='{"type":"item.completed","item":{"text":"diff captured in telemetry"}}\n',
    )

    summary = scheduler.run(
        [job],
        adapters={"solver-a": adapter},
        dataset_rows=dataset_rows,
    )

    assert summary.jobs[0].final_status == SolverAttemptState.SUCCEEDED
    attempt_rows = _read_jsonl(tmp_path / "attempts.jsonl")
    assert len(attempt_rows) == 1
    row = attempt_rows[0]
    assert row["attempt_state"] == SolverAttemptState.SUCCEEDED
    assert row["metadata"]["normalized_patch_path"]
    assert (
        Path(row["metadata"]["normalized_patch_path"])
        .read_text(encoding="utf-8")
        .startswith("diff --git a/src.py b/src.py\n")
    )


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


def test_scheduler_updates_progress_reporter_with_job_outcomes(
    monkeypatch, tmp_path: Path
) -> None:
    class RecordingProgress:
        def __init__(self) -> None:
            self.statuses: list[str] = []
            self.closed = False

        def update(self, status: str) -> None:
            self.statuses.append(status)

        def close(self) -> None:
            self.closed = True

    progress = RecordingProgress()
    monkeypatch.setattr(
        "repogauge.runner.scheduler._create_progress_reporter",
        lambda total: progress,
    )

    jobs = [
        _job(job_id="run-1:i-1:solver-a:0", instance_id="i-1", solver_id="solver-a"),
        _job(job_id="run-1:i-2:solver-b:0", instance_id="i-2", solver_id="solver-b"),
    ]
    scheduler = SolverScheduler(config=SolverSchedulerConfig(default_solver_budget=1))

    summary = scheduler.run(
        jobs,
        adapters={
            "solver-a": CaptureAdapter([SolverAttemptState.SUCCEEDED]),
            "solver-b": CaptureAdapter([SolverAttemptState.INVALID_PATCH]),
        },
    )

    assert [job.final_status for job in summary.jobs] == [
        SolverAttemptState.SUCCEEDED,
        SolverAttemptState.INVALID_PATCH,
    ]
    assert sorted(progress.statuses) == sorted(
        [SolverAttemptState.SUCCEEDED, SolverAttemptState.INVALID_PATCH]
    )
    assert progress.closed is True


def test_scheduler_marks_unfinished_jobs_failed_when_worker_raises(
    monkeypatch, tmp_path: Path
) -> None:
    jobs = [
        _job(job_id="run-1:i-1:solver-a:0", instance_id="i-1", solver_id="solver-a"),
        _job(job_id="run-1:i-2:solver-b:0", instance_id="i-2", solver_id="solver-b"),
    ]
    scheduler = SolverScheduler(
        config=SolverSchedulerConfig(
            default_solver_budget=1,
            persist_jobs_to=tmp_path / "jobs.jsonl",
        )
    )
    original_execute_job = scheduler._execute_job

    def flaky_execute_job(*, job, adapter, dataset_row=None):
        if job.solver_id == "solver-b":
            raise RuntimeError("scheduler worker boom")
        return original_execute_job(job=job, adapter=adapter, dataset_row=dataset_row)

    monkeypatch.setattr(scheduler, "_execute_job", flaky_execute_job)

    try:
        scheduler.run(
            jobs,
            adapters={
                "solver-a": CaptureAdapter([SolverAttemptState.SUCCEEDED]),
                "solver-b": CaptureAdapter([SolverAttemptState.SUCCEEDED]),
            },
        )
    except RuntimeError as exc:
        assert str(exc) == "scheduler worker boom"
    else:
        raise AssertionError("expected scheduler.run to raise")

    job_rows = _read_jsonl(tmp_path / "jobs.jsonl")
    latest_by_job = {row["job_id"]: row for row in job_rows}
    assert (
        latest_by_job["run-1:i-1:solver-a:0"]["status"] == SolverAttemptState.SUCCEEDED
    )
    assert latest_by_job["run-1:i-2:solver-b:0"]["status"] == SolverAttemptState.FAILED
