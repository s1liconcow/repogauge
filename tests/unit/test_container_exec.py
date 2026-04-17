from repogauge.runner.container_exec import (
    WorkspaceContainerError,
    _ensure_solver_command_available,
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
