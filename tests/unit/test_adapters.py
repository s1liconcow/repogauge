from __future__ import annotations

import tempfile
import unittest
from unittest import mock
from pathlib import Path

from repogauge.exec import CommandResult
from repogauge.runner.adapters import (
    CodexCLIAdapter,
    OpenAICompatibleAdapter,
    OpenAIResponsesAdapter,
    MockSolverAdapter,
    SolverAdapterError,
    build_solver_adapters,
)
from repogauge.runner.matrix import MatrixProvider, MatrixSolver
from repogauge.runner.planner import PlannedRunJob
from repogauge.runner.scheduler import SolverAttemptState
from repogauge.runner.scheduler import SolverAdapterResult
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


def _provider_for_command(command: str = "echo") -> MatrixProvider:
    return MatrixProvider(
        provider_id="codex",
        kind="codex_cli",
        config={"command": command},
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
        instance_row = {
            "instance_id": "repo__sample-1",
            "repo": "repo",
            "base_commit": "abc123",
            "problem_statement": "fix bug",
        }
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
            instance_row=instance_row,
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
        instance_row = {
            "instance_id": "repo__sample-1",
            "repo": "repo",
            "base_commit": "abc123",
            "problem_statement": "fix bug",
        }
        openai_adapters = build_solver_adapters(
            solvers=(_solver(adapter=SOLVER_ADAPTER_OPENAI_RESPONSES),),
            providers=(_provider(),),
        )
        self.assertEqual(list(openai_adapters.keys()), ["solver-a"])

        adapter = openai_adapters["solver-a"]
        request = adapter.prepare_request(
            job=_job(job_id="run-1:repo__sample-1:solver-a:1"),
            attempt_id="run-1:repo__sample-1:solver-a:1:attempt-1",
            attempt_index=1,
            instance_row=instance_row,
        )
        result = adapter.execute_attempt(request)
        self.assertEqual(result.status, SolverAttemptState.FAILED)

    def test_build_solver_adapters_rejects_unknown_adapter(self) -> None:
        provider = MatrixProvider(
            provider_id="mock",
            kind="mock",
            config={},
            redacted_config={},
            raw={},
        )
        with self.assertRaisesRegex(
            SolverAdapterError,
            "unsupported solver adapter",
        ):
            build_solver_adapters(
                solvers=(_solver(adapter="bogus"),),
                providers=(provider,),
            )

    def test_openai_responses_adapter_marks_usage_and_cost_source(self) -> None:
        adapter = OpenAIResponsesAdapter(
            solver_id="solver-a",
            provider_id="openai",
            provider_config={"api_key": "k", "base_url": "https://example.test"},
            behavior={"model": "gpt-4o-mini"},
        )
        request = adapter.prepare_request(
            job=_job(job_id="run-1:repo__sample-1:solver-a:2"),
            attempt_id="run-1:repo__sample-1:solver-a:2:attempt-1",
            attempt_index=1,
            instance_row={
                "instance_id": "repo__sample-1",
                "repo": "repo",
                "base_commit": "abc123",
                "problem_statement": "fix bug",
            },
        )
        payload = {
            "usage": {"input_tokens": 1},
            "cost": {"input_cost": 0.1},
            "output_text": "diff --git a/x b/x\n+ok",
        }
        with mock.patch("repogauge.runner.adapters._post_json", return_value=payload):
            result = adapter.execute_attempt(request)

        self.assertEqual(result.usage_source, "response.usage")
        self.assertEqual(result.cost_source, "response.cost")
        self.assertEqual(result.usage, {"input_tokens": 1})
        self.assertEqual(result.cost, {"input_cost": 0.1})

    def test_openai_compatible_adapter_marks_usage_and_cost_source(self) -> None:
        adapter = OpenAICompatibleAdapter(
            solver_id="solver-a",
            provider_id="openai",
            provider_config={"api_key": "k", "base_url": "https://example.test"},
            behavior={"model": "gpt-4o-mini"},
        )
        request = adapter.prepare_request(
            job=_job(job_id="run-1:repo__sample-1:solver-a:3"),
            attempt_id="run-1:repo__sample-1:solver-a:3:attempt-1",
            attempt_index=1,
            instance_row={
                "instance_id": "repo__sample-1",
                "repo": "repo",
                "base_commit": "abc123",
                "problem_statement": "fix bug",
            },
        )
        payload = {
            "usage": {"prompt_tokens": 10},
            "cost": {"total_cost": 0.2},
            "choices": [{"message": {"content": "diff --git a/x b/x\n+ok"}}],
        }
        with mock.patch("repogauge.runner.adapters._post_json", return_value=payload):
            result = adapter.execute_attempt(request)

        self.assertEqual(result.usage_source, "response.usage")
        self.assertEqual(result.cost_source, "response.cost")
        self.assertEqual(result.usage, {"prompt_tokens": 10})
        self.assertEqual(result.cost, {"total_cost": 0.2})

    def test_codex_cli_adapter_marks_usage_and_cost_source(self) -> None:
        provider = _provider_for_command("/bin/echo")
        adapter = CodexCLIAdapter(
            solver_id="solver-a",
            provider_id="codex",
            provider_config=provider.config,
            behavior={"model": "gpt-5.4"},
        )
        request = adapter.prepare_request(
            job=_job(job_id="run-1:repo__sample-1:solver-a:4"),
            attempt_id="run-1:repo__sample-1:solver-a:4:attempt-1",
            attempt_index=1,
            instance_row={
                "instance_id": "repo__sample-1",
                "repo": "repo",
                "base_commit": "abc123",
                "problem_statement": "fix bug",
            },
        )
        output = (
            '{"usage":{"input_tokens":5}}\n'
            '{"cost":{"total_cost":0.5}}\n'
            '{"message":{"content":"diff --git a/x b/x\\n+ok"}}\n'
        )
        command_result = CommandResult(
            command=["/bin/echo", "exec", "--json", "--model", "gpt-5.4"],
            returncode=0,
            stdout=output,
            stderr="",
        )
        with mock.patch(
            "repogauge.runner.adapters.run_command", return_value=command_result
        ):
            result = adapter.execute_attempt(request)

        self.assertEqual(result.usage_source, "codex_cli.event.usage")
        self.assertEqual(result.cost_source, "codex_cli.event.cost")
        self.assertEqual(result.usage, {"input_tokens": 5})
        self.assertEqual(result.cost, {"total_cost": 0.5})
        self.assertEqual(result.stderr_output, "")

    def test_codex_cli_adapter_preserves_usage_and_cost_on_failure(self) -> None:
        provider = _provider_for_command("/bin/echo")
        adapter = CodexCLIAdapter(
            solver_id="solver-a",
            provider_id="codex",
            provider_config=provider.config,
            behavior={"model": "gpt-5.4"},
        )
        request = adapter.prepare_request(
            job=_job(job_id="run-1:repo__sample-1:solver-a:4"),
            attempt_id="run-1:repo__sample-1:solver-a:4:attempt-1",
            attempt_index=1,
            instance_row={
                "instance_id": "repo__sample-1",
                "repo": "repo",
                "base_commit": "abc123",
                "problem_statement": "fix bug",
            },
        )
        output = (
            '{"usage":{"input_tokens":5}}\n'
            '{"cost":{"total_cost":0.5}}\n'
            '{"message":{"content":"partial"}}\n'
        )
        command_result = CommandResult(
            command=["/bin/echo", "exec", "--json", "--model", "gpt-5.4"],
            returncode=1,
            stdout=output,
            stderr="boom",
        )
        with mock.patch(
            "repogauge.runner.adapters.run_command", return_value=command_result
        ):
            result = adapter.execute_attempt(request)

        self.assertEqual(result.status, SolverAttemptState.FAILED)
        self.assertEqual(result.exit_reason, "boom")
        self.assertEqual(result.usage_source, "codex_cli.event.usage")
        self.assertEqual(result.cost_source, "codex_cli.event.cost")
        self.assertEqual(result.usage, {"input_tokens": 5})
        self.assertEqual(result.cost, {"total_cost": 0.5})
        self.assertEqual(result.raw_output, output)
        self.assertEqual(result.stderr_output, "boom")

    def test_codex_cli_adapter_reclassifies_infra_timeouts_as_failed(self) -> None:
        provider = _provider_for_command("/bin/echo")
        adapter = CodexCLIAdapter(
            solver_id="solver-a",
            provider_id="codex",
            provider_config=provider.config,
            behavior={"model": "gpt-5.4"},
        )
        request = adapter.prepare_request(
            job=_job(job_id="run-1:repo__sample-1:solver-a:4"),
            attempt_id="run-1:repo__sample-1:solver-a:4:attempt-1",
            attempt_index=1,
            instance_row={
                "instance_id": "repo__sample-1",
                "repo": "repo",
                "base_commit": "abc123",
                "problem_statement": "fix bug",
            },
        )
        stderr = (
            "command timeout: Reading prompt from stdin...\n"
            "ERROR codex_core::tools::router: error=exec_command failed "
            'CreateProcess { message: "Rejected(\\"Failed to create unified exec '
            'process: No such file or directory (os error 2)\\")" }'
        )
        command_result = CommandResult(
            command=["/bin/echo"],
            returncode=-1,
            stdout='{"text":"Reading prompt from stdin..."}\n',
            stderr=stderr,
            timed_out=True,
        )
        with mock.patch(
            "repogauge.runner.adapters.run_command", return_value=command_result
        ):
            result = adapter.execute_attempt(request)

        self.assertEqual(result.status, SolverAttemptState.FAILED)
        self.assertEqual(result.metadata["failure_code"], "infra_tool_exec")
        self.assertEqual(result.metadata["timeout_classification"], "infra")

    def test_codex_cli_adapter_keeps_wall_clock_timeouts_as_timed_out(self) -> None:
        provider = _provider_for_command("/bin/echo")
        adapter = CodexCLIAdapter(
            solver_id="solver-a",
            provider_id="codex",
            provider_config=provider.config,
            behavior={"model": "gpt-5.4"},
        )
        request = adapter.prepare_request(
            job=_job(job_id="run-1:repo__sample-1:solver-a:4"),
            attempt_id="run-1:repo__sample-1:solver-a:4:attempt-1",
            attempt_index=1,
            instance_row={
                "instance_id": "repo__sample-1",
                "repo": "repo",
                "base_commit": "abc123",
                "problem_statement": "fix bug",
            },
        )
        command_result = CommandResult(
            command=["/bin/echo"],
            returncode=-1,
            stdout="",
            stderr="solver timed out",
            timed_out=True,
        )
        with mock.patch(
            "repogauge.runner.adapters.run_command", return_value=command_result
        ):
            result = adapter.execute_attempt(request)

        self.assertEqual(result.status, SolverAttemptState.TIMED_OUT)
        self.assertEqual(result.metadata["timeout_classification"], "wall_clock")
        self.assertNotIn("failure_code", result.metadata)

    def test_codex_cli_adapter_disables_ambient_codex_config(self) -> None:
        provider = _provider_for_command("codex")
        adapter = CodexCLIAdapter(
            solver_id="solver-a",
            provider_id="codex",
            provider_config=provider.config,
            behavior={"model": "gpt-5.4"},
        )
        request = adapter.prepare_request(
            job=_job(job_id="run-1:repo__sample-1:solver-a:4"),
            attempt_id="run-1:repo__sample-1:solver-a:4:attempt-1",
            attempt_index=1,
            instance_row={
                "instance_id": "repo__sample-1",
                "repo": "repo",
                "base_commit": "abc123",
                "problem_statement": "fix bug",
            },
        )
        command_result = CommandResult(
            command=[],
            returncode=0,
            stdout='{"message":{"content":"diff --git a/x b/x\\n+ok"}}\n',
            stderr="",
        )
        with mock.patch(
            "repogauge.runner.adapters.run_command", return_value=command_result
        ) as mock_run_command:
            adapter.execute_attempt(request)

        command = mock_run_command.call_args.args[0]
        self.assertEqual(
            command[:7],
            [
                "codex",
                "--ask-for-approval",
                "never",
                "exec",
                "-c",
                "notify=[]",
                "-c",
            ],
        )
        self.assertEqual(command[7], "mcp_servers={}")
        self.assertIn("--json", command)
        self.assertIn("--sandbox", command)
        self.assertIn("danger-full-access", command)
        self.assertEqual(command[-2:], ["--model", "gpt-5.4"])

    def test_codex_cli_adapter_targets_attempt_workspace(self) -> None:
        provider = _provider_for_command("codex")
        adapter = CodexCLIAdapter(
            solver_id="solver-a",
            provider_id="codex",
            provider_config=provider.config,
            behavior={"model": "gpt-5.4"},
        )
        with tempfile.TemporaryDirectory() as workspace:
            request = adapter.prepare_request(
                job=_job(job_id="run-1:repo__sample-1:solver-a:4"),
                attempt_id="run-1:repo__sample-1:solver-a:4:attempt-1",
                attempt_index=1,
                instance_row={
                    "instance_id": "repo__sample-1",
                    "repo": "repo",
                    "base_commit": "abc123",
                    "problem_statement": "fix bug",
                },
                workspace_path=Path(workspace),
            )
            command_result = CommandResult(
                command=[],
                returncode=0,
                stdout='{"message":{"content":"diff --git a/x b/x\\n+ok"}}\n',
                stderr="",
            )
            with mock.patch(
                "repogauge.runner.adapters.run_command", return_value=command_result
            ) as mock_run_command:
                adapter.execute_attempt(request)

        command = mock_run_command.call_args.args[0]
        kwargs = mock_run_command.call_args.kwargs
        assert "--cd" in command
        assert command[command.index("--cd") + 1] == workspace
        assert kwargs["cwd"] == workspace
        assert kwargs["env"]["HOME"] == str(Path(workspace).parent / "codex-home")
        assert kwargs["env"]["CODEX_HOME"] == str(
            Path(workspace).parent / "codex-home" / ".codex"
        )

    def test_codex_cli_finalize_output_prefers_model_patch(self) -> None:
        provider = _provider_for_command("codex")
        adapter = CodexCLIAdapter(
            solver_id="solver-a",
            provider_id="codex",
            provider_config=provider.config,
            behavior={"model": "gpt-5.4"},
        )
        request = adapter.prepare_request(
            job=_job(job_id="run-1:repo__sample-1:solver-a:5"),
            attempt_id="run-1:repo__sample-1:solver-a:5:attempt-1",
            attempt_index=1,
            instance_row={
                "instance_id": "repo__sample-1",
                "repo": "repo",
                "base_commit": "abc123",
                "problem_statement": "fix bug",
            },
        )
        patch = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -0,0 +1 @@\n+ok\n"
        result = adapter.finalize_output(
            request,
            SolverAdapterResult(
                attempt_id=request.attempt_id,
                status=SolverAttemptState.SUCCEEDED,
                model_patch=patch,
                raw_output='{"type":"item.completed","item":{"text":"not a plain diff"}}\n',
            ),
        )

        self.assertEqual(result.status, SolverAttemptState.SUCCEEDED)
        self.assertEqual(result.model_patch, patch)
