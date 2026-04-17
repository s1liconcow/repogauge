from __future__ import annotations

import tempfile
import unittest
from unittest import mock
from pathlib import Path

from repogauge.exec import CommandResult
from repogauge.runner.adapters import (
    ClaudeCLIAdapter,
    CodexCLIAdapter,
    OpenAICompatibleAdapter,
    OpenAIResponsesAdapter,
    MockSolverAdapter,
    SolverAdapterError,
    _claude_cli_child_env_for_home,
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

    def test_codex_cli_adapter_estimates_cost_from_public_pricing(self) -> None:
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
            '{"usage":{"input_tokens":1000,"output_tokens":100,'
            '"cached_input_tokens":200}}\n'
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

        self.assertEqual(result.cost_source, "public_api_pricing")
        self.assertEqual(result.cost, {"total_cost_usd": 0.00355})

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

    def test_codex_cli_adapter_uses_custom_timeout_seconds(self) -> None:
        provider = _provider_for_command("codex")
        adapter = CodexCLIAdapter(
            solver_id="solver-a",
            provider_id="codex",
            provider_config=provider.config,
            behavior={"model": "gpt-5.4", "timeout_seconds": 300},
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

        self.assertEqual(mock_run_command.call_args.kwargs["timeout_seconds"], 300)

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
                    "version": "1.0",
                    "patch": "diff --git a/x b/x\n+prod",
                    "test_patch": "diff --git a/tests/x b/tests/x\n+test",
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

    def test_codex_cli_adapter_uses_container_execution_when_enabled(self) -> None:
        provider = MatrixProvider(
            provider_id="codex",
            kind="codex_cli",
            config={"command": "codex", "image": "ghcr.io/example/codex:latest"},
            redacted_config={},
            raw={},
        )
        adapter = CodexCLIAdapter(
            solver_id="solver-a",
            provider_id="codex",
            provider_config=provider.config,
            behavior={"model": "gpt-5.4"},
            containerized=True,
            container_host="unix:///tmp/podman.sock",
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
                    "version": "1.0",
                    "patch": "diff --git a/x b/x\n+prod",
                    "test_patch": "diff --git a/tests/x b/tests/x\n+test",
                },
                workspace_path=Path(workspace),
            )
            command_result = CommandResult(
                command=("codex",),
                returncode=0,
                stdout='{"message":{"content":"diff --git a/x b/x\\n+ok"}}\n',
                stderr="",
            )
            with mock.patch(
                "repogauge.runner.adapters.run_solver_command_in_container",
                return_value=command_result,
            ) as mock_container_exec:
                adapter.execute_attempt(request)

        kwargs = mock_container_exec.call_args.kwargs
        command = kwargs["command"]
        assert command[command.index("--cd") + 1] == "/testbed"
        assert kwargs["container_host"] == "unix:///tmp/podman.sock"
        assert kwargs["image_override"] == "ghcr.io/example/codex:latest"
        assert kwargs["environment"]["HOME"] == str(
            Path(workspace).parent / "codex-home"
        )
        assert kwargs["instance_row"]["test_patch"] == "diff --git a/tests/x b/tests/x\n+test"
        assert kwargs["instance_row"]["version"] == "1.0"

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

    def test_codex_cli_finalize_output_preserves_failed_result_without_model_patch(
        self,
    ) -> None:
        provider = _provider_for_command("codex")
        adapter = CodexCLIAdapter(
            solver_id="solver-a",
            provider_id="codex",
            provider_config=provider.config,
            behavior={"model": "gpt-5.4"},
        )
        request = adapter.prepare_request(
            job=_job(job_id="run-1:repo__sample-1:solver-a:6"),
            attempt_id="run-1:repo__sample-1:solver-a:6:attempt-1",
            attempt_index=1,
            instance_row={
                "instance_id": "repo__sample-1",
                "repo": "repo",
                "base_commit": "abc123",
                "problem_statement": "fix bug",
            },
        )
        result = adapter.finalize_output(
            request,
            SolverAdapterResult(
                attempt_id=request.attempt_id,
                status=SolverAttemptState.FAILED,
                model_patch=None,
                raw_output="",
                stderr_output="boom",
                exit_reason="boom",
            ),
        )

        self.assertEqual(result.status, SolverAttemptState.FAILED)
        self.assertIsNone(result.model_patch)
        self.assertEqual(result.exit_reason, "boom")
        self.assertEqual(result.stderr_output, "")

    def _claude_provider(self, command: str = "claude") -> MatrixProvider:
        return MatrixProvider(
            provider_id="claude",
            kind="claude_cli",
            config={"command": command},
            redacted_config={},
            raw={},
        )

    def _claude_request(
        self, adapter: ClaudeCLIAdapter, *, workspace: Path | None = None
    ):
        return adapter.prepare_request(
            job=_job(job_id="run-1:repo__sample-1:solver-a:4"),
            attempt_id="run-1:repo__sample-1:solver-a:4:attempt-1",
            attempt_index=1,
            instance_row={
                "instance_id": "repo__sample-1",
                "repo": "repo",
                "base_commit": "abc123",
                "problem_statement": "fix bug",
                "version": "1.0",
                "patch": "diff --git a/x b/x\n+prod",
                "test_patch": "diff --git a/tests/x b/tests/x\n+test",
            },
            workspace_path=workspace,
        )

    def test_claude_cli_adapter_marks_usage_and_cost_source(self) -> None:
        provider = self._claude_provider("/bin/echo")
        adapter = ClaudeCLIAdapter(
            solver_id="solver-a",
            provider_id="claude",
            provider_config=provider.config,
            behavior={"model": "claude-sonnet-4-6"},
        )
        request = self._claude_request(adapter)
        output = (
            '{"type":"system","subtype":"init"}\n'
            '{"type":"assistant","message":{"content":[{"type":"text",'
            '"text":"diff --git a/x b/x\\n+ok"}],'
            '"usage":{"input_tokens":10,"output_tokens":3}}}\n'
            '{"type":"result","subtype":"success","cost_usd":0.12,'
            '"result":"diff --git a/x b/x\\n+ok"}\n'
        )
        command_result = CommandResult(
            command=[
                "/bin/echo",
                "-p",
                "--output-format",
                "stream-json",
                "--verbose",
                "--model",
                "claude-sonnet-4-6",
                "--dangerously-skip-permissions",
            ],
            returncode=0,
            stdout=output,
            stderr="",
        )
        with mock.patch(
            "repogauge.runner.adapters.run_command", return_value=command_result
        ):
            result = adapter.execute_attempt(request)

        self.assertEqual(result.status, SolverAttemptState.SUCCEEDED)
        self.assertEqual(result.usage_source, "claude_cli.event.message.usage")
        self.assertEqual(result.cost_source, "claude_cli.event.cost_usd")
        self.assertEqual(result.usage, {"input_tokens": 10, "output_tokens": 3})
        self.assertEqual(result.cost, {"total_cost_usd": 0.12})
        self.assertEqual((result.model_patch or "").strip(), "diff --git a/x b/x\n+ok")

    def test_claude_cli_adapter_preserves_usage_and_cost_on_failure(
        self,
    ) -> None:
        provider = self._claude_provider("/bin/echo")
        adapter = ClaudeCLIAdapter(
            solver_id="solver-a",
            provider_id="claude",
            provider_config=provider.config,
            behavior={"model": "claude-sonnet-4-6"},
        )
        request = self._claude_request(adapter)
        output = (
            '{"type":"result","subtype":"error","cost_usd":0.5,'
            '"usage":{"input_tokens":7},"result":"partial"}\n'
        )
        command_result = CommandResult(
            command=["/bin/echo"],
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
        self.assertEqual(result.usage_source, "claude_cli.event.usage")
        self.assertEqual(result.cost_source, "claude_cli.event.cost_usd")
        self.assertEqual(result.usage, {"input_tokens": 7})
        self.assertEqual(result.cost, {"total_cost_usd": 0.5})
        self.assertEqual(result.raw_output, output)
        self.assertEqual(result.stderr_output, "boom")

    def test_claude_cli_adapter_reads_total_cost_usd_from_result_event(self) -> None:
        provider = self._claude_provider("/bin/echo")
        adapter = ClaudeCLIAdapter(
            solver_id="solver-a",
            provider_id="claude",
            provider_config=provider.config,
            behavior={"model": "claude-sonnet-4-6"},
        )
        request = self._claude_request(adapter)
        output = (
            '{"type":"assistant","message":{"usage":{"input_tokens":10,"output_tokens":3}}}\n'
            '{"type":"result","subtype":"success","total_cost_usd":0.41018495,'
            '"result":"diff --git a/x b/x\\n+ok"}\n'
        )
        command_result = CommandResult(
            command=["/bin/echo"],
            returncode=0,
            stdout=output,
            stderr="",
        )
        with mock.patch(
            "repogauge.runner.adapters.run_command", return_value=command_result
        ):
            result = adapter.execute_attempt(request)

        self.assertEqual(result.cost_source, "claude_cli.event.total_cost_usd")
        self.assertEqual(result.cost, {"total_cost_usd": 0.41018495})

    def test_claude_cli_adapter_estimates_cost_from_public_pricing(self) -> None:
        provider = self._claude_provider("/bin/echo")
        adapter = ClaudeCLIAdapter(
            solver_id="solver-a",
            provider_id="claude",
            provider_config=provider.config,
            behavior={"model": "claude-sonnet-4-6"},
        )
        request = self._claude_request(adapter)
        output = (
            '{"type":"assistant","message":{"usage":{"input_tokens":15,'
            '"output_tokens":11153,"cache_read_input_tokens":424394,'
            '"cache_creation_input_tokens":30589,'
            '"cache_creation":{"ephemeral_1h_input_tokens":30589,'
            '"ephemeral_5m_input_tokens":0}}}}\n'
            '{"type":"result","subtype":"success","result":"diff --git a/x b/x\\n+ok"}\n'
        )
        command_result = CommandResult(
            command=["/bin/echo"],
            returncode=0,
            stdout=output,
            stderr="",
        )
        with mock.patch(
            "repogauge.runner.adapters.run_command", return_value=command_result
        ):
            result = adapter.execute_attempt(request)

        self.assertEqual(result.cost_source, "public_api_pricing")
        self.assertAlmostEqual(result.cost["total_cost_usd"], 0.4781922)

    def test_claude_cli_adapter_classifies_wall_clock_timeout(self) -> None:
        provider = self._claude_provider("/bin/echo")
        adapter = ClaudeCLIAdapter(
            solver_id="solver-a",
            provider_id="claude",
            provider_config=provider.config,
            behavior={"model": "claude-sonnet-4-6"},
        )
        request = self._claude_request(adapter)
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

    def test_claude_cli_adapter_targets_attempt_workspace(self) -> None:
        provider = self._claude_provider("claude")
        adapter = ClaudeCLIAdapter(
            solver_id="solver-a",
            provider_id="claude",
            provider_config=provider.config,
            behavior={"model": "claude-sonnet-4-6"},
        )
        with tempfile.TemporaryDirectory() as workspace:
            request = self._claude_request(adapter, workspace=Path(workspace))
            command_result = CommandResult(
                command=[],
                returncode=0,
                stdout='{"type":"result","result":"diff --git a/x b/x\\n+ok"}\n',
                stderr="",
            )
            with mock.patch(
                "repogauge.runner.adapters.run_command", return_value=command_result
            ) as mock_run_command:
                adapter.execute_attempt(request)

        command = mock_run_command.call_args.args[0]
        kwargs = mock_run_command.call_args.kwargs
        self.assertEqual(command[0], "claude")
        self.assertIn("-p", command)
        self.assertIn("--output-format", command)
        self.assertIn("stream-json", command)
        self.assertIn("--dangerously-skip-permissions", command)
        self.assertEqual(command[command.index("--model") + 1], "claude-sonnet-4-6")
        self.assertEqual(kwargs["cwd"], workspace)

    def test_claude_cli_adapter_uses_container_execution_when_enabled(self) -> None:
        provider = MatrixProvider(
            provider_id="claude",
            kind="claude_cli",
            config={"command": "claude", "image": "ghcr.io/example/claude:latest"},
            redacted_config={},
            raw={},
        )
        adapter = ClaudeCLIAdapter(
            solver_id="solver-a",
            provider_id="claude",
            provider_config=provider.config,
            behavior={"model": "claude-sonnet-4-6"},
            containerized=True,
            container_host="unix:///tmp/podman.sock",
        )
        with tempfile.TemporaryDirectory() as workspace:
            request = self._claude_request(adapter, workspace=Path(workspace))
            command_result = CommandResult(
                command=("claude",),
                returncode=0,
                stdout='{"type":"result","result":"diff --git a/x b/x\\n+ok"}\n',
                stderr="",
            )
            with mock.patch(
                "repogauge.runner.adapters.run_solver_command_in_container",
                return_value=command_result,
            ) as mock_container_exec:
                adapter.execute_attempt(request)

        kwargs = mock_container_exec.call_args.kwargs
        self.assertEqual(kwargs["container_host"], "unix:///tmp/podman.sock")
        self.assertEqual(kwargs["image_override"], "ghcr.io/example/claude:latest")
        self.assertEqual(
            kwargs["environment"]["HOME"],
            str(Path(workspace).parent / "claude-home"),
        )
        self.assertEqual(
            kwargs["instance_row"]["test_patch"],
            "diff --git a/tests/x b/tests/x\n+test",
        )
        self.assertEqual(kwargs["instance_row"]["version"], "1.0")

    def test_claude_cli_adapter_inherits_env_without_local_credentials(
        self,
    ) -> None:
        provider = self._claude_provider("claude")
        adapter = ClaudeCLIAdapter(
            solver_id="solver-a",
            provider_id="claude",
            provider_config=provider.config,
            behavior={"model": "claude-sonnet-4-6"},
        )
        request = self._claude_request(adapter)
        command_result = CommandResult(
            command=[],
            returncode=0,
            stdout='{"type":"result","result":"ok"}\n',
            stderr="",
        )
        with tempfile.TemporaryDirectory() as fake_home:
            with (
                mock.patch.dict(
                    "os.environ",
                    {"HOME": fake_home, "ANTHROPIC_API_KEY": "sk-env"},
                    clear=False,
                ),
                mock.patch(
                    "repogauge.runner.adapters.run_command",
                    return_value=command_result,
                ) as mock_run_command,
            ):
                adapter.execute_attempt(request)

        self.assertIsNone(mock_run_command.call_args.kwargs["env"])

    def test_claude_cli_adapter_scrubs_api_key_when_local_creds_exist(
        self,
    ) -> None:
        provider = self._claude_provider("claude")
        adapter = ClaudeCLIAdapter(
            solver_id="solver-a",
            provider_id="claude",
            provider_config=provider.config,
            behavior={"model": "claude-sonnet-4-6"},
        )
        request = self._claude_request(adapter)
        command_result = CommandResult(
            command=[],
            returncode=0,
            stdout='{"type":"result","result":"ok"}\n',
            stderr="",
        )
        with tempfile.TemporaryDirectory() as fake_home:
            claude_dir = Path(fake_home) / ".claude"
            claude_dir.mkdir()
            (claude_dir / ".credentials.json").write_text("{}")
            with (
                mock.patch.dict(
                    "os.environ",
                    {
                        "HOME": fake_home,
                        "ANTHROPIC_API_KEY": "sk-env",
                        "ANTHROPIC_AUTH_TOKEN": "tok-env",
                    },
                    clear=False,
                ),
                mock.patch(
                    "repogauge.runner.adapters.run_command",
                    return_value=command_result,
                ) as mock_run_command,
            ):
                adapter.execute_attempt(request)

        env = mock_run_command.call_args.kwargs["env"]
        self.assertIsNotNone(env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertEqual(env["HOME"], fake_home)

    def test_claude_cli_child_env_for_home_scrubs_keys_for_isolated_home(self) -> None:
        with tempfile.TemporaryDirectory() as fake_home:
            claude_dir = Path(fake_home) / ".claude"
            claude_dir.mkdir()
            (claude_dir / ".credentials.json").write_text("{}", encoding="utf-8")
            with mock.patch.dict(
                "os.environ",
                {
                    "ANTHROPIC_API_KEY": "sk-env",
                    "ANTHROPIC_AUTH_TOKEN": "tok-env",
                },
                clear=False,
            ):
                env = _claude_cli_child_env_for_home(Path(fake_home))

        assert env is not None
        assert env["HOME"] == fake_home
        assert "ANTHROPIC_API_KEY" not in env
        assert "ANTHROPIC_AUTH_TOKEN" not in env

    def test_claude_cli_adapter_requires_command(self) -> None:
        with self.assertRaises(SolverAdapterError):
            ClaudeCLIAdapter(
                solver_id="solver-a",
                provider_id="claude",
                provider_config={},
                behavior={"model": "claude-sonnet-4-6"},
            )
