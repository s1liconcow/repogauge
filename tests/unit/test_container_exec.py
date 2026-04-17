from pathlib import Path

from repogauge.runner.container_exec import (
    WorkspaceContainerError,
    _containerize_environment,
    _ensure_solver_command_available,
    _resolve_host_tool_fallback,
    _solver_shell_command,
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


def test_containerize_environment_rewrites_attempt_root_paths(tmp_path: Path) -> None:
    attempt_root = tmp_path / "attempt-1"
    codex_home = attempt_root / "codex-home"
    command_env = _containerize_environment(
        environment={
            "HOME": str(codex_home),
            "CODEX_HOME": str(codex_home / ".codex"),
            "PATH": "/usr/bin:/bin",
        },
        attempt_root=attempt_root,
    )

    assert command_env == {
        "HOME": "/repogauge/codex-home",
        "CODEX_HOME": "/repogauge/codex-home/.codex",
        "PATH": "/usr/bin:/bin",
    }


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
