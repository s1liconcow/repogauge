from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from repogauge.exec import CommandResult, run_command
from repogauge.runner.adapters import CodexCLIAdapter
from repogauge.runner.normalize_patch import normalize_solver_output
from repogauge.runner.planner import PlannedRunJob
from repogauge.runner.scheduler import SolverAdapterResult, SolverAttemptState
from repogauge.runner.workspaces import prepare_attempt_workspace


def _provider_config() -> dict[str, str]:
    return {"command": "/bin/echo"}


def _job(job_id: str) -> PlannedRunJob:
    return PlannedRunJob(
        run_id="run-1",
        job_id=job_id,
        instance_id="repo__sample-1",
        solver_id="solver-a",
        provider_id="codex",
        seed=7,
        prompt_policy_hash="p",
        tool_policy_hash="t",
        solver_config_hash="s",
        dataset_path="/tmp/dataset.jsonl",
        matrix_path="/tmp/matrix.yaml",
        metadata={"provider": "codex"},
    )


def _instance_row() -> dict[str, str]:
    return {
        "instance_id": "repo__sample-1",
        "repo": "demo/repo",
        "base_commit": "abc123",
        "problem_statement": "fix bug",
    }


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


def test_codex_cli_adapter_extracts_nested_diff_from_jsonl_events() -> None:
    adapter = CodexCLIAdapter(
        solver_id="solver-a",
        provider_id="codex",
        provider_config=_provider_config(),
        behavior={"model": "gpt-5.4"},
    )
    request = adapter.prepare_request(
        job=_job("run-1:repo__sample-1:solver-a:0"),
        attempt_id="run-1:repo__sample-1:solver-a:0:attempt-1",
        attempt_index=1,
        instance_row=_instance_row(),
    )
    output = "\n".join(
        [
            '{"type":"item.completed","item":{"type":"agent_message","text":"I am inspecting files."}}',
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": (
                            "diff --git a/src.py b/src.py\n"
                            "--- a/src.py\n"
                            "+++ b/src.py\n"
                            "@@ -1 +1 @@\n"
                            "-print('before')\n"
                            "+print('after')\n"
                        ),
                    },
                }
            ),
        ]
    )
    with mock.patch(
        "repogauge.runner.adapters.run_command",
        return_value=CommandResult(
            command=["/bin/echo"], returncode=0, stdout=output, stderr=""
        ),
    ):
        result = adapter.execute_attempt(request)

    assert result.status == SolverAttemptState.SUCCEEDED
    assert result.model_patch.startswith("diff --git a/src.py b/src.py\n")


def test_codex_cli_adapter_extracts_fenced_diff_from_nested_jsonl_events() -> None:
    adapter = CodexCLIAdapter(
        solver_id="solver-a",
        provider_id="codex",
        provider_config=_provider_config(),
        behavior={"model": "gpt-5.4"},
    )
    request = adapter.prepare_request(
        job=_job("run-1:repo__sample-1:solver-a:0"),
        attempt_id="run-1:repo__sample-1:solver-a:0:attempt-2",
        attempt_index=1,
        instance_row=_instance_row(),
    )
    output = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": (
                    "```diff\n"
                    "diff --git a/src.py b/src.py\n"
                    "--- a/src.py\n"
                    "+++ b/src.py\n"
                    "@@ -1 +1 @@\n"
                    "-print('before')\n"
                    "+print('after')\n"
                    "```"
                ),
            },
        }
    )
    with mock.patch(
        "repogauge.runner.adapters.run_command",
        return_value=CommandResult(
            command=["/bin/echo"], returncode=0, stdout=output, stderr=""
        ),
    ):
        result = adapter.execute_attempt(request)

    assert result.status == SolverAttemptState.SUCCEEDED
    assert result.model_patch.startswith("diff --git a/src.py b/src.py\n")
    assert not result.model_patch.startswith("f\\n")


def test_finalize_output_recovers_nested_edit_plan_from_raw_jsonl() -> None:
    adapter = CodexCLIAdapter(
        solver_id="solver-a",
        provider_id="codex",
        provider_config=_provider_config(),
        behavior={"model": "gpt-5.4"},
    )
    request = adapter.prepare_request(
        job=_job("run-1:repo__sample-1:solver-a:1"),
        attempt_id="run-1:repo__sample-1:solver-a:1:attempt-1",
        attempt_index=1,
        instance_row=_instance_row(),
    )
    edit_plan = {"files": [{"path": "src.py", "content": "print('after')\n"}]}
    raw_output = json.dumps(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": json.dumps(edit_plan)},
        }
    )

    result = adapter.finalize_output(
        request,
        SolverAdapterResult(
            attempt_id=request.attempt_id,
            status=SolverAttemptState.SUCCEEDED,
            model_patch="summary only",
            raw_output=raw_output,
        ),
    )

    assert result.status == SolverAttemptState.SUCCEEDED
    assert json.loads(result.model_patch) == edit_plan


def test_normalize_solver_output_recovers_nested_diff_from_jsonl(
    tmp_path: Path,
) -> None:
    repo, commit = _create_repo(tmp_path)
    row = {
        "instance_id": "repo__sample-1",
        "repo": "demo/repo",
        "base_commit": commit,
        "problem_statement": "Update the log line.",
        "version": "1",
    }
    raw_output = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": (
                    "diff --git a/src.py b/src.py\n"
                    "index 1111111..2222222 100644\n"
                    "--- a/src.py\n"
                    "+++ b/src.py\n"
                    "@@ -1 +1 @@\n"
                    "-print('before')\n"
                    "+print('after')\n"
                ),
            },
        }
    )

    with prepare_attempt_workspace(
        repo_root=repo,
        instance_row=row,
        attempt_id="att-jsonl-diff",
        solver_id="solver-a",
        workspaces_root=tmp_path / "workspaces",
    ) as attempt:
        result = normalize_solver_output(raw_output, attempt=attempt)

    assert result.patch.startswith("diff --git a/src.py b/src.py\n")


def test_normalize_solver_output_recovers_nested_edit_plan_from_jsonl(
    tmp_path: Path,
) -> None:
    repo, commit = _create_repo(tmp_path)
    row = {
        "instance_id": "repo__sample-1",
        "repo": "demo/repo",
        "base_commit": commit,
        "problem_statement": "Add a new helper file.",
        "version": "1",
    }
    edit_plan = {
        "files": [
            {"path": "src.py", "content": "print('after')\n"},
            {"path": "nested/extra.txt", "content": "extra\n"},
        ]
    }
    raw_output = json.dumps(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": json.dumps(edit_plan)},
        }
    )

    with prepare_attempt_workspace(
        repo_root=repo,
        instance_row=row,
        attempt_id="att-jsonl-edits",
        solver_id="solver-a",
        workspaces_root=tmp_path / "workspaces",
    ) as attempt:
        result = normalize_solver_output(raw_output, attempt=attempt)

    assert "nested/extra.txt" in result.patch
    assert "print('after')" in result.patch
