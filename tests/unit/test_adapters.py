from __future__ import annotations

import unittest

from repogauge.runner.adapters import (
    MockSolverAdapter,
    SolverAdapterError,
    build_solver_adapters,
)
from repogauge.runner.matrix import MatrixProvider, MatrixSolver
from repogauge.runner.planner import PlannedRunJob
from repogauge.runner.scheduler import SolverAttemptState
from repogauge.runner.solvers import (
    SOLVER_ADAPTER_MOCK,
    SOLVER_ADAPTER_OPENAI_RESPONSES,
)


def _job(*, job_id: str) -> PlannedRunJob:
    return PlannedRunJob(
        run_id="run-1",
        job_id=job_id,
        instance_id="repo__sample-1",
        solver_id="solver-a",
        provider_id="mock",
        seed=7,
        prompt_policy_hash="h1",
        tool_policy_hash="h2",
        solver_config_hash="h3",
        dataset_path="/tmp/dataset.jsonl",
        matrix_path="/tmp/matrix.yaml",
        metadata={"provider": "mock"},
    )


def _provider() -> MatrixProvider:
    return MatrixProvider(
        provider_id="mock",
        kind="mock",
        config={},
        redacted_config={},
        raw={},
    )


def _solver(*, adapter: str = SOLVER_ADAPTER_MOCK) -> MatrixSolver:
    return MatrixSolver(
        solver_id="solver-a",
        provider_id="mock",
        adapter=adapter,
        prompt_policy={},
        tool_policy={},
        behavior={},
        raw={},
    )


class TestAdapters(unittest.TestCase):
    def test_build_solver_adapters_constructs_mock_adapter(self) -> None:
        adapters = build_solver_adapters(
            solvers=(_solver(),),
            providers=(_provider(),),
        )
        self.assertEqual(list(adapters.keys()), ["solver-a"])

        adapter = adapters["solver-a"]
        request = adapter.prepare_request(
            job=_job(job_id="run-1:repo__sample-1:solver-a:0"),
            attempt_id="run-1:repo__sample-1:solver-a:0:attempt-1",
            attempt_index=1,
        )
        result = adapter.execute_attempt(request)
        self.assertEqual(result.status, SolverAttemptState.SUCCEEDED)

    def test_mock_adapter_rejects_unsupported_status(self) -> None:
        with self.assertRaisesRegex(
            SolverAdapterError,
            "unsupported mock status",
        ):
            MockSolverAdapter(
                solver_id="solver-a",
                provider_id="mock",
                behavior={"mock_statuses": ["succeeded", "bogus"]},
            )

    def test_build_solver_adapters_rejects_missing_provider(self) -> None:
        solver = _solver()
        with self.assertRaisesRegex(
            SolverAdapterError,
            "references unknown provider",
        ):
            build_solver_adapters(
                solvers=(solver,),
                providers=(),
            )

    def test_build_solver_adapters_rejects_unsupported_adapter(self) -> None:
        with self.assertRaisesRegex(
            SolverAdapterError,
            "not implemented in this release",
        ):
            build_solver_adapters(
                solvers=(_solver(adapter=SOLVER_ADAPTER_OPENAI_RESPONSES),),
                providers=(_provider(),),
            )
