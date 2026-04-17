"""Container-backed solver execution helpers."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from repogauge.exec import CommandResult
from swebench.harness.constants import DOCKER_USER, DOCKER_WORKDIR
from swebench.harness.docker_build import (
    build_instance_image,
    close_logger,
    setup_logger,
)
from swebench.harness.docker_utils import cleanup_container
from swebench.harness.test_spec.test_spec import make_test_spec


_CONTAINER_ATTEMPT_ROOT = "/repogauge"
_CONTAINER_PROMPT_PATH = f"{_CONTAINER_ATTEMPT_ROOT}/prompt.txt"
_CONTAINER_STDOUT_PATH = f"{_CONTAINER_ATTEMPT_ROOT}/stdout.txt"
_CONTAINER_STDERR_PATH = f"{_CONTAINER_ATTEMPT_ROOT}/stderr.txt"
_CONTAINER_HOST_TOOLS_ROOT = f"{_CONTAINER_ATTEMPT_ROOT}/host-tools"
_CONTAINER_CODEX_SEED_HOME = f"{_CONTAINER_ATTEMPT_ROOT}/codex-home"
_CONTAINER_CODEX_RUNTIME_HOME = "/home/nonroot/.repogauge-codex-home"
_CONTAINER_OPENCODE_SEED_HOME = f"{_CONTAINER_ATTEMPT_ROOT}/opencode-home"
_CONTAINER_OPENCODE_RUNTIME_HOME = "/home/nonroot/.repogauge-opencode-home"


class WorkspaceContainerError(RuntimeError):
    """Raised when RepoGauge cannot execute a solver inside a container."""


@dataclass(frozen=True)
class _ExecOutcome:
    exit_code: int
    timed_out: bool
    elapsed_ms: int


@dataclass(frozen=True)
class _HostToolFallback:
    command: list[str]
    mounts: dict[str, dict[str, str]]


def _sanitize_container_name(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip()).strip("-.")
    if not normalized:
        normalized = "repogauge-attempt"
    return normalized.lower()[:120]


@contextmanager
def _temporary_environment(overrides: Mapping[str, str | None]):
    original: dict[str, str | None] = {}
    try:
        for key, value in overrides.items():
            original[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _docker_client(*, container_host: str | None):
    import docker as docker_module  # type: ignore[import]

    overrides = {"DOCKER_HOST": container_host} if container_host else {}
    with _temporary_environment(overrides):
        return docker_module.from_env()


def _resolve_image(
    *,
    attempt_id: str,
    attempt_root: Path,
    instance_id: str,
    instance_row: Mapping[str, object],
    image_override: str | None,
    client,
) -> tuple[str, str | None, list[str]]:
    if image_override:
        return image_override, None, []

    logger = setup_logger(attempt_id, attempt_root / "container_image.log", mode="a")
    try:
        try:
            test_spec = make_test_spec(dict(instance_row), namespace=None)
        except Exception as exc:  # pragma: no cover - defensive
            raise WorkspaceContainerError(
                "unable to resolve the default solver container image from the dataset "
                f"row for {instance_id}: {exc}"
            ) from exc

        build_instance_image(test_spec, client, logger, nocache=False)
        run_args = test_spec.docker_specs.get("run_args", {})
        cap_add = list(run_args.get("cap_add", []))
        return test_spec.instance_image_key, test_spec.platform, cap_add
    finally:
        close_logger(logger)


def _exec_in_container(
    container, cmd: str, timeout_seconds: int | None
) -> _ExecOutcome:
    exec_id: str | None = None
    exception: BaseException | None = None

    def _run() -> None:
        nonlocal exec_id, exception
        try:
            exec_id = container.client.api.exec_create(container.id, cmd)["Id"]
            stream = container.client.api.exec_start(exec_id, stream=True)
            for _chunk in stream:
                # Output is redirected into mounted files; consume the stream so the
                # exec finishes and timeout bookkeeping stays accurate.
                pass
        except BaseException as exc:  # pragma: no cover - defensive
            exception = exc

    start = time.monotonic()
    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout_seconds)

    if exception is not None:
        raise exception

    timed_out = thread.is_alive()
    if timed_out and exec_id is not None:
        try:
            pid = container.client.api.exec_inspect(exec_id)["Pid"]
            if pid:
                container.exec_run(f"kill -TERM {pid}", detach=True)
        except Exception:
            pass
        thread.join(2)
        if thread.is_alive() and exec_id is not None:
            try:
                pid = container.client.api.exec_inspect(exec_id)["Pid"]
                if pid:
                    container.exec_run(f"kill -KILL {pid}", detach=True)
            except Exception:
                pass
            thread.join(1)

    exit_code = -1
    if exec_id is not None and not timed_out:
        try:
            details = container.client.api.exec_inspect(exec_id)
            exit_code = int(details.get("ExitCode", -1))
        except Exception:
            exit_code = -1

    return _ExecOutcome(
        exit_code=exit_code,
        timed_out=timed_out,
        elapsed_ms=int((time.monotonic() - start) * 1000),
    )


def _ensure_solver_command_available(
    *, container, command: list[str], image: str
) -> None:
    if not command:
        raise WorkspaceContainerError("solver command is empty")

    executable = command[0].strip()
    if not executable:
        raise WorkspaceContainerError("solver command is empty")

    probe = f"command -v {shlex.quote(executable)} >/dev/null"
    try:
        exit_code, _output = container.exec_run(
            ["/bin/bash", "-lc", probe],
            demux=False,
        )
    except Exception as exc:  # pragma: no cover - defensive
        raise WorkspaceContainerError(
            f"unable to verify solver command {executable!r} in container image "
            f"{image}: {exc}"
        ) from exc

    if exit_code == 0:
        return

    raise WorkspaceContainerError(
        f"solver command {executable!r} was not found in container image {image}; "
        "set providers.<id>.image to an image that includes this CLI"
    )


def _codex_host_tool_fallback(command: list[str]) -> _HostToolFallback | None:
    host_codex = shutil.which("codex")
    host_node = shutil.which("node")
    if host_codex is None or host_node is None:
        return None

    codex_script = Path(host_codex).resolve()
    node_binary = Path(host_node).resolve()
    if not codex_script.exists() or not node_binary.exists():
        return None

    node_modules_root: Path | None = None
    for parent in codex_script.parents:
        if parent.name == "node_modules":
            node_modules_root = parent
            break
    if node_modules_root is None or not node_modules_root.exists():
        return None

    container_node = f"{_CONTAINER_HOST_TOOLS_ROOT}/node/node"
    container_node_modules = f"{_CONTAINER_HOST_TOOLS_ROOT}/codex/node_modules"
    relative_script = codex_script.relative_to(node_modules_root)
    return _HostToolFallback(
        command=[
            container_node,
            f"{container_node_modules}/{relative_script.as_posix()}",
            *command[1:],
        ],
        mounts={
            str(node_binary): {"bind": container_node, "mode": "ro"},
            str(node_modules_root): {"bind": container_node_modules, "mode": "ro"},
        },
    )


def _claude_host_tool_fallback(command: list[str]) -> _HostToolFallback | None:
    host_claude = shutil.which("claude")
    if host_claude is None:
        return None

    claude_binary = Path(host_claude).resolve()
    if not claude_binary.exists():
        return None

    container_claude = f"{_CONTAINER_HOST_TOOLS_ROOT}/claude/claude"
    return _HostToolFallback(
        command=[container_claude, *command[1:]],
        mounts={str(claude_binary): {"bind": container_claude, "mode": "ro"}},
    )


def _opencode_host_tool_fallback(command: list[str]) -> _HostToolFallback | None:
    host_opencode = shutil.which("opencode")
    if host_opencode is None:
        return None

    opencode_binary = Path(host_opencode).resolve()
    if not opencode_binary.exists():
        return None

    container_opencode = f"{_CONTAINER_HOST_TOOLS_ROOT}/opencode/opencode"
    return _HostToolFallback(
        command=[container_opencode, *command[1:]],
        mounts={
            str(opencode_binary): {
                "bind": container_opencode,
                "mode": "ro",
            }
        },
    )


def _resolve_host_tool_fallback(command: list[str]) -> _HostToolFallback | None:
    if not command:
        return None

    executable = command[0].strip()
    if executable == "codex":
        return _codex_host_tool_fallback(command)
    if executable == "claude":
        return _claude_host_tool_fallback(command)
    if executable == "opencode":
        return _opencode_host_tool_fallback(command)
    return None


def _containerize_environment(
    *,
    environment: Mapping[str, str] | None,
    attempt_root: Path,
) -> dict[str, str]:
    command_env = dict(environment or {})
    host_attempt_root = str(attempt_root.resolve())
    for key, value in list(command_env.items()):
        if value.startswith(host_attempt_root):
            command_env[key] = (
                f"{_CONTAINER_ATTEMPT_ROOT}{value.removeprefix(host_attempt_root)}"
            )
    return command_env


def _is_codex_command(command: list[str]) -> bool:
    return any("codex" in part.lower() for part in command[:2])


def _is_opencode_command(command: list[str]) -> bool:
    return any(part.lower() == "opencode" for part in command[:2])


def _solver_shell_command(command: list[str]) -> str:
    writable_targets = (
        DOCKER_WORKDIR,
        _CONTAINER_PROMPT_PATH,
        f"{_CONTAINER_ATTEMPT_ROOT}/codex-home",
        f"{_CONTAINER_ATTEMPT_ROOT}/claude-home",
        f"{_CONTAINER_ATTEMPT_ROOT}/opencode-home",
    )
    prep_steps = [
        f"if [ -e {shlex.quote(target)} ]; then chmod -R a+rwX {shlex.quote(target)}; fi"
        for target in writable_targets
    ]
    runtime_prep_steps: list[str] = []
    runtime_command = list(command)
    if _is_codex_command(command):
        runtime_prep_steps.extend(
            [
                f"rm -rf {shlex.quote(_CONTAINER_CODEX_RUNTIME_HOME)}",
                f"mkdir -p {shlex.quote(_CONTAINER_CODEX_RUNTIME_HOME)}",
                "if [ -d "
                + shlex.quote(_CONTAINER_CODEX_SEED_HOME)
                + " ]; then cp -a "
                + shlex.quote(f"{_CONTAINER_CODEX_SEED_HOME}/.")
                + " "
                + shlex.quote(f"{_CONTAINER_CODEX_RUNTIME_HOME}/")
                + "; fi",
                f"chown -R 1000:1000 {shlex.quote(_CONTAINER_CODEX_RUNTIME_HOME)}",
                f"chmod -R u+rwX {shlex.quote(_CONTAINER_CODEX_RUNTIME_HOME)}",
            ]
        )
        runtime_command = [
            "env",
            f"HOME={_CONTAINER_CODEX_RUNTIME_HOME}",
            f"XDG_CONFIG_HOME={_CONTAINER_CODEX_RUNTIME_HOME}/.config",
            f"CODEX_HOME={_CONTAINER_CODEX_RUNTIME_HOME}/.codex",
            *runtime_command,
        ]
    elif _is_opencode_command(command):
        runtime_prep_steps.extend(
            [
                f"rm -rf {shlex.quote(_CONTAINER_OPENCODE_RUNTIME_HOME)}",
                f"mkdir -p {shlex.quote(_CONTAINER_OPENCODE_RUNTIME_HOME)}",
                "if [ -d "
                + shlex.quote(_CONTAINER_OPENCODE_SEED_HOME)
                + " ]; then cp -a "
                + shlex.quote(f"{_CONTAINER_OPENCODE_SEED_HOME}/.")
                + " "
                + shlex.quote(f"{_CONTAINER_OPENCODE_RUNTIME_HOME}/")
                + "; fi",
                f"chown -R 1000:1000 {shlex.quote(_CONTAINER_OPENCODE_RUNTIME_HOME)}",
                f"chmod -R u+rwX {shlex.quote(_CONTAINER_OPENCODE_RUNTIME_HOME)}",
            ]
        )
        runtime_command = [
            "env",
            f"HOME={_CONTAINER_OPENCODE_RUNTIME_HOME}",
            f"XDG_CONFIG_HOME={_CONTAINER_OPENCODE_RUNTIME_HOME}/.config",
            f"XDG_DATA_HOME={_CONTAINER_OPENCODE_RUNTIME_HOME}/.local/share",
            "OPENCODE_CONFIG_CONTENT={}",
            *runtime_command,
        ]
    solver_command = shlex.join(runtime_command)
    return (
        " && ".join(prep_steps)
        + (" && " + " && ".join(runtime_prep_steps) if runtime_prep_steps else "")
        + f" && cd {shlex.quote(DOCKER_WORKDIR)}"
        + " && "
        + "{ "
        + f"su -m -s /bin/bash nonroot -c {shlex.quote(solver_command)}"
        + "; status=$?; "
        + " && ".join(prep_steps)
        + "; exit $status; }"
        + f" < {shlex.quote(_CONTAINER_PROMPT_PATH)}"
        + f" > {shlex.quote(_CONTAINER_STDOUT_PATH)}"
        + f" 2> {shlex.quote(_CONTAINER_STDERR_PATH)}"
    )


def run_solver_command_in_container(
    *,
    attempt_id: str,
    workspace_path: Path,
    instance_row: Mapping[str, object],
    command: list[str],
    prompt: str,
    timeout_seconds: int,
    container_host: str | None,
    image_override: str | None,
    environment: Mapping[str, str] | None = None,
) -> CommandResult:
    attempt_root = workspace_path.parent
    prompt_path = attempt_root / "prompt.txt"
    stdout_path = attempt_root / "stdout.txt"
    stderr_path = attempt_root / "stderr.txt"
    for path in (stdout_path, stderr_path):
        if path.exists():
            path.unlink()
    prompt_path.write_text(prompt, encoding="utf-8")

    client = _docker_client(container_host=container_host)
    container = None
    logger = setup_logger(attempt_id, attempt_root / "container_run.log", mode="a")
    try:
        image, platform, cap_add = _resolve_image(
            attempt_id=attempt_id,
            attempt_root=attempt_root,
            instance_id=str(instance_row.get("instance_id", "")),
            instance_row=instance_row,
            image_override=image_override,
            client=client,
        )

        container_name = _sanitize_container_name(f"repogauge-{attempt_id}-solver")
        try:
            existing = client.containers.get(container_name)
            existing.remove(force=True)
        except Exception:
            pass

        volumes = {
            str(workspace_path): {"bind": DOCKER_WORKDIR, "mode": "rw"},
            str(attempt_root): {"bind": _CONTAINER_ATTEMPT_ROOT, "mode": "rw"},
        }
        create_kwargs = {
            "image": image,
            "name": container_name,
            "user": DOCKER_USER,
            "detach": True,
            "command": "tail -f /dev/null",
            "volumes": volumes,
            "environment": _containerize_environment(
                environment=environment,
                attempt_root=attempt_root,
            ),
            "cap_add": cap_add,
        }
        if platform:
            create_kwargs["platform"] = platform
        container = client.containers.create(**create_kwargs)
        container.start()
        resolved_command = list(command)
        try:
            _ensure_solver_command_available(
                container=container, command=resolved_command, image=image
            )
        except WorkspaceContainerError:
            fallback = _resolve_host_tool_fallback(command)
            if fallback is None:
                raise

            container.stop(timeout=1)
            container.remove(force=True)
            create_kwargs["volumes"] = {**volumes, **fallback.mounts}
            container = client.containers.create(**create_kwargs)
            container.start()
            resolved_command = fallback.command
            _ensure_solver_command_available(
                container=container, command=resolved_command, image=image
            )

        shell_cmd = _solver_shell_command(resolved_command)
        outcome = _exec_in_container(
            container,
            f"/bin/bash -lc {shlex.quote(shell_cmd)}",
            timeout_seconds,
        )
        stdout = stdout_path.read_text(encoding="utf-8") if stdout_path.exists() else ""
        stderr = stderr_path.read_text(encoding="utf-8") if stderr_path.exists() else ""
        return CommandResult(
            command=tuple(resolved_command),
            returncode=outcome.exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=outcome.timed_out,
            elapsed_ms=outcome.elapsed_ms,
            cwd=str(workspace_path),
        )
    finally:
        cleanup_container(client, container, logger)
        close_logger(logger)
