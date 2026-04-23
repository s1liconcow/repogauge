from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import docker.errors

from repogauge.runner.container_exec import (
    WorkspaceContainerSession,
    WorkspaceContainerError,
    _adapter_setup_commands,
    _containerize_environment,
    _ensure_solver_command_available,
    _local_repo_setup_commands,
    _resolve_image_from_adapter_spec,
    _resolve_host_tool_fallback,
    _scoped_container_name,
    _shared_cache_mounts,
    _solver_shell_command,
    _workspace_setup_timeout,
)


class _FakeContainer:
    def __init__(self, *, exit_code: int) -> None:
        self.exit_code = exit_code
        self.calls: list[tuple[list[str], bool]] = []

    def exec_run(self, command: list[str], demux: bool = False):
        self.calls.append((command, demux))
        return self.exit_code, b""


def test_ensure_solver_command_available_accepts_present_binary() -> None:
    container = _FakeContainer(exit_code=0)

    _ensure_solver_command_available(
        container=container,
        command=["codex", "--json"],
        image="ghcr.io/example/codex:latest",
    )

    assert container.calls == [
        (["/bin/bash", "-lc", "command -v codex >/dev/null"], False)
    ]


def test_ensure_solver_command_available_reports_missing_binary() -> None:
    container = _FakeContainer(exit_code=127)

    try:
        _ensure_solver_command_available(
            container=container,
            command=["claude", "--verbose"],
            image="ghcr.io/example/base:latest",
        )
    except WorkspaceContainerError as exc:
        message = str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("expected WorkspaceContainerError")

    assert "solver command 'claude' was not found" in message
    assert "ghcr.io/example/base:latest" in message
    assert "providers.<id>.image" in message


def test_local_repo_setup_commands_strip_remote_clone_bootstrap() -> None:
    test_spec = SimpleNamespace(
        repo_script_list=[
            "git clone -o origin https://github.com/owner/repo /testbed",
            "git clone -o origin --single-branch https://github.com/owner/repo /testbed",
            "chmod -R 777 /testbed",
            "cd /testbed",
            "git reset --hard deadbeef",
            "git remote remove origin",
            "TARGET_TIMESTAMP=$(git show -s --format=%ci deadbeef)",
            'git tag -l | while read tag; do TAG_COMMIT=$(git rev-list -n 1 "$tag"); done',
            "git reflog expire --expire=now --all",
            "git gc --prune=now --aggressive",
            "AFTER_TIMESTAMP=$(date -d \"$TARGET_TIMESTAMP + 1 second\" '+%Y-%m-%d %H:%M:%S')",
            'COMMIT_COUNT=$(git log --oneline --all --since="$AFTER_TIMESTAMP" | wc -l)',
            '[ "$COMMIT_COUNT" -eq 0 ] || exit 1',
            "pip install uv",
            "uv sync --active --all-groups",
        ]
    )

    assert _local_repo_setup_commands(test_spec) == (
        "git config --global --add safe.directory /testbed || true",
        "chmod -R a+rwX /testbed",
        "cd /testbed",
        "command -v uv >/dev/null || pip install uv",
        "uv sync --active --all-groups",
    )


def test_adapter_setup_commands_install_uv_when_needed() -> None:
    assert _adapter_setup_commands(
        {
            "pre_install": [],
            "install": ["uv sync --active --all-groups", "python -m pip install pytest"],
            "build": [],
        }
    ) == (
        "git config --global --add safe.directory /testbed || true",
        "chmod -R 777 /testbed || true",
        "cd /testbed",
        "command -v uv >/dev/null || pip install uv",
        "uv sync --active --all-groups",
        "python -m pip install pytest",
    )


def test_resolve_image_from_adapter_spec_normalizes_python_for_swebench(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, str, dict[str, object]]] = []

    class _MissingImages:
        def get(self, image_name: str):
            raise docker.errors.ImageNotFound("missing")

    class _FakeClient:
        def __init__(self) -> None:
            self.images = _MissingImages()

    def fake_get_dockerfile_base(platform, arch, language, **kwargs):
        calls.append(("base", language, dict(kwargs)))
        return "FROM scratch\n"

    def fake_get_dockerfile_env(platform, arch, language, base_image_key, **kwargs):
        calls.append(("env", language, dict(kwargs)))
        return "FROM scratch\n"

    monkeypatch.setattr(
        "repogauge.runner.container_exec.get_dockerfile_base",
        fake_get_dockerfile_base,
    )
    monkeypatch.setattr(
        "repogauge.runner.container_exec.get_dockerfile_env",
        fake_get_dockerfile_env,
    )
    monkeypatch.setattr(
        "repogauge.runner.container_exec.build_image",
        lambda **kwargs: None,
    )

    resolved = _resolve_image_from_adapter_spec(
        attempt_id="attempt-1",
        attempt_root=tmp_path,
        instance_row={"repo": "owner/repo"},
        adapter_spec={
            "repo": "owner/repo",
            "language": "python",
            "docker_specs": {"python_version": "3.11"},
        },
        client=_FakeClient(),
    )

    assert resolved.image.startswith("rg.local.env.owner-repo-python.")
    assert [call[:2] for call in calls] == [("base", "py"), ("env", "py")]


def test_resolve_host_tool_fallback_for_codex(monkeypatch, tmp_path: Path) -> None:
    node = tmp_path / "usr/bin/node"
    node.parent.mkdir(parents=True)
    node.write_text("", encoding="utf-8")
    script = tmp_path / "bun/install/global/node_modules/@openai/codex/bin/codex.js"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")

    def fake_which(name: str) -> str | None:
        if name == "codex":
            return str(script)
        if name == "node":
            return str(node)
        return None

    monkeypatch.setattr("repogauge.runner.container_exec.shutil.which", fake_which)

    fallback = _resolve_host_tool_fallback(["codex", "exec", "--json"])

    assert fallback is not None
    assert fallback.command == [
        "/repogauge/host-tools/node/node",
        "/repogauge/host-tools/codex/node_modules/@openai/codex/bin/codex.js",
        "exec",
        "--json",
    ]
    assert fallback.mounts == {
        str(node.resolve()): {
            "bind": "/repogauge/host-tools/node/node",
            "mode": "ro",
        },
        str((tmp_path / "bun/install/global/node_modules").resolve()): {
            "bind": "/repogauge/host-tools/codex/node_modules",
            "mode": "ro",
        },
    }


def test_resolve_host_tool_fallback_for_claude(monkeypatch, tmp_path: Path) -> None:
    binary = tmp_path / "claude"
    binary.write_text("", encoding="utf-8")

    def fake_which(name: str) -> str | None:
        if name == "claude":
            return str(binary)
        return None

    monkeypatch.setattr("repogauge.runner.container_exec.shutil.which", fake_which)

    fallback = _resolve_host_tool_fallback(["claude", "--print"])

    assert fallback is not None
    assert fallback.command == [
        "/repogauge/host-tools/claude/claude",
        "--print",
    ]
    assert fallback.mounts == {
        str(binary.resolve()): {
            "bind": "/repogauge/host-tools/claude/claude",
            "mode": "ro",
        }
    }


def test_resolve_host_tool_fallback_for_opencode(monkeypatch, tmp_path: Path) -> None:
    binary = tmp_path / "opencode"
    binary.write_text("", encoding="utf-8")

    def fake_which(name: str) -> str | None:
        if name == "opencode":
            return str(binary)
        return None

    monkeypatch.setattr("repogauge.runner.container_exec.shutil.which", fake_which)

    fallback = _resolve_host_tool_fallback(["opencode", "run", "--format", "json"])

    assert fallback is not None
    assert fallback.command == [
        "/repogauge/host-tools/opencode/opencode",
        "run",
        "--format",
        "json",
    ]
    assert fallback.mounts == {
        str(binary.resolve()): {
            "bind": "/repogauge/host-tools/opencode/opencode",
            "mode": "ro",
        }
    }


def test_containerize_environment_rewrites_attempt_root_paths(tmp_path: Path) -> None:
    attempt_root = tmp_path / "attempt-1"
    codex_home = attempt_root / "codex-home"
    workspace = attempt_root / "workspace"
    command_env = _containerize_environment(
        environment={
            "HOME": str(codex_home),
            "CODEX_HOME": str(codex_home / ".codex"),
            "PYTHONPATH": str(workspace),
            "GOCACHE": str(workspace / ".gocache"),
            "GOROOT": "/usr/lib/go",
            "GOPATH": "/home/david/go",
            "GOMODCACHE": "/home/david/go/pkg/mod",
            "PATH": "/usr/bin:/bin",
            "VIRTUAL_ENV": "/tmp/venv",
            "CONDA_PREFIX": "/tmp/conda",
            "OPENAI_API_KEY": "test-key",
        },
        attempt_root=attempt_root,
        workspace_path=workspace,
    )

    assert command_env == {
        "HOME": "/repogauge/codex-home",
        "CODEX_HOME": "/repogauge/codex-home/.codex",
        "PYTHONPATH": "/testbed",
        "GOCACHE": "/testbed/.gocache",
        "OPENAI_API_KEY": "test-key",
    }


def test_shared_cache_mounts_include_absolute_cache_dirs(tmp_path: Path) -> None:
    uv_cache = tmp_path / "uv-cache"
    pip_cache = tmp_path / "pip-cache"

    mounts = _shared_cache_mounts(
        {
            "UV_CACHE_DIR": str(uv_cache),
            "PIP_CACHE_DIR": str(pip_cache),
            "RELATIVE_CACHE": "cache",
        }
    )

    assert mounts == {
        str(uv_cache): {"bind": str(uv_cache), "mode": "rw"},
        str(pip_cache): {"bind": str(pip_cache), "mode": "rw"},
    }
    assert uv_cache.is_dir()
    assert pip_cache.is_dir()


def test_workspace_setup_timeout_has_higher_floor_than_test_timeout() -> None:
    assert _workspace_setup_timeout(None) is None
    assert _workspace_setup_timeout(120) == 600
    assert _workspace_setup_timeout(900) == 900


def test_scoped_container_name_distinguishes_parallel_workspaces(tmp_path: Path) -> None:
    first_attempt_root = tmp_path / "eval-a"
    second_attempt_root = tmp_path / "eval-b"
    first_workspace = first_attempt_root / "checkout"
    second_workspace = second_attempt_root / "checkout"

    first = _scoped_container_name(
        attempt_id="eval-inst-1-session",
        role="workspace",
        scope_paths=(first_attempt_root, first_workspace),
    )
    second = _scoped_container_name(
        attempt_id="eval-inst-1-session",
        role="workspace",
        scope_paths=(second_attempt_root, second_workspace),
    )

    assert first != second
    assert first.startswith("repogauge-eval-inst-1-session-workspace-")
    assert second.startswith("repogauge-eval-inst-1-session-workspace-")


def test_workspace_container_session_reads_from_mounted_root_and_copies_outputs(
    tmp_path: Path,
) -> None:
    mounted_root = tmp_path / "mounted"
    requested_root = tmp_path / "requested"
    mounted_root.mkdir()

    def fake_exec_in_container(container, cmd: str, timeout_seconds: int):
        (mounted_root / "stdout.txt").write_text("hello\n", encoding="utf-8")
        (mounted_root / "stderr.txt").write_text("warning\n", encoding="utf-8")
        return SimpleNamespace(exit_code=0, timed_out=False, elapsed_ms=17)

    session = WorkspaceContainerSession(
        container=object(),
        workspace_path=tmp_path / "workspace",
        artifacts_root=mounted_root,
    )

    with patch(
        "repogauge.runner.container_exec._exec_in_container",
        side_effect=fake_exec_in_container,
    ):
        result = session.run(
            command=["python", "-V"],
            timeout_seconds=30,
            artifacts_root=requested_root,
        )

    assert result.stdout == "hello\n"
    assert result.stderr == "warning\n"
    assert (requested_root / "stdout.txt").read_text(encoding="utf-8") == "hello\n"
    assert (requested_root / "stderr.txt").read_text(encoding="utf-8") == "warning\n"


def test_solver_shell_command_runs_solver_as_nonroot_with_redirection() -> None:
    shell_cmd = _solver_shell_command(["codex", "exec", "--json"])

    assert "chmod -R a+rwX /testbed" in shell_cmd
    assert "chmod -R a+rwX /repogauge/prompt.txt" in shell_cmd
    assert "chmod -R a+rwX /repogauge/codex-home" in shell_cmd
    assert "rm -rf /home/nonroot/.repogauge-codex-home" in shell_cmd
    assert (
        "cp -a /repogauge/codex-home/. /home/nonroot/.repogauge-codex-home/"
        in shell_cmd
    )
    assert "CODEX_HOME=/home/nonroot/.repogauge-codex-home/.codex" in shell_cmd
    assert (
        "su -m -s /bin/bash nonroot -c 'env "
        "HOME=/home/nonroot/.repogauge-codex-home "
        "XDG_CONFIG_HOME=/home/nonroot/.repogauge-codex-home/.config "
        "CODEX_HOME=/home/nonroot/.repogauge-codex-home/.codex "
        "codex exec --json'"
    ) in shell_cmd
    assert "status=$?" in shell_cmd
    assert "< /repogauge/prompt.txt" in shell_cmd
    assert "> /repogauge/stdout.txt" in shell_cmd
    assert "2> /repogauge/stderr.txt" in shell_cmd


def test_solver_shell_command_seeds_opencode_runtime_home() -> None:
    shell_cmd = _solver_shell_command(["opencode", "run", "--format", "json"])

    assert "chmod -R a+rwX /repogauge/opencode-home" in shell_cmd
    assert "rm -rf /home/nonroot/.repogauge-opencode-home" in shell_cmd
    assert (
        "cp -a /repogauge/opencode-home/. /home/nonroot/.repogauge-opencode-home/"
        in shell_cmd
    )
    assert (
        "XDG_DATA_HOME=/home/nonroot/.repogauge-opencode-home/.local/share" in shell_cmd
    )
    assert "OPENCODE_CONFIG_CONTENT={}" in shell_cmd
    assert (
        "su -m -s /bin/bash nonroot -c 'env "
        "HOME=/home/nonroot/.repogauge-opencode-home "
        "XDG_CONFIG_HOME=/home/nonroot/.repogauge-opencode-home/.config "
        "XDG_DATA_HOME=/home/nonroot/.repogauge-opencode-home/.local/share "
    ) in shell_cmd
    assert "opencode run --format json'" in shell_cmd
