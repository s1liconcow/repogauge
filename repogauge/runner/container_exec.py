"""Container-backed solver execution helpers."""

from __future__ import annotations

import hashlib
import json
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
from swebench.harness.constants import BASE_IMAGE_BUILD_DIR, DEFAULT_DOCKER_SPECS
from swebench.harness.constants import DOCKER_USER, DOCKER_WORKDIR
from swebench.harness.constants import ENV_IMAGE_BUILD_DIR
from swebench.harness.docker_build import (
    build_image,
    build_env_images,
    close_logger,
    setup_logger,
)
from swebench.harness.docker_utils import cleanup_container
from swebench.harness.dockerfiles import get_dockerfile_base, get_dockerfile_env
from swebench.harness.test_spec.test_spec import make_test_spec


_CONTAINER_ATTEMPT_ROOT = "/repogauge"
_CONTAINER_PROMPT_PATH = f"{_CONTAINER_ATTEMPT_ROOT}/prompt.txt"
_CONTAINER_STDOUT_PATH = f"{_CONTAINER_ATTEMPT_ROOT}/stdout.txt"
_CONTAINER_STDERR_PATH = f"{_CONTAINER_ATTEMPT_ROOT}/stderr.txt"
_CONTAINER_SETUP_STDOUT_PATH = f"{_CONTAINER_ATTEMPT_ROOT}/setup_stdout.txt"
_CONTAINER_SETUP_STDERR_PATH = f"{_CONTAINER_ATTEMPT_ROOT}/setup_stderr.txt"
_CONTAINER_HOST_TOOLS_ROOT = f"{_CONTAINER_ATTEMPT_ROOT}/host-tools"
_CONTAINER_CODEX_SEED_HOME = f"{_CONTAINER_ATTEMPT_ROOT}/codex-home"
_CONTAINER_CODEX_RUNTIME_HOME = "/home/nonroot/.repogauge-codex-home"
_CONTAINER_OPENCODE_SEED_HOME = f"{_CONTAINER_ATTEMPT_ROOT}/opencode-home"
_CONTAINER_OPENCODE_RUNTIME_HOME = "/home/nonroot/.repogauge-opencode-home"
_CONTAINER_ENV_DROP_KEYS = frozenset(
    {
        "PATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "CONDA_DEFAULT_ENV",
        "CONDA_PREFIX",
        "CONDA_PROMPT_MODIFIER",
        "CONDA_EXE",
        "_CE_CONDA",
        "_CE_M",
    }
)
_CONTAINER_ENV_DROP_PREFIXES = ("CONDA_",)


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


@dataclass(frozen=True)
class _ResolvedContainerSpec:
    image: str
    platform: str | None
    cap_add: tuple[str, ...]
    setup_commands: tuple[str, ...]


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
) -> _ResolvedContainerSpec:
    logger = setup_logger(attempt_id, attempt_root / "container_image.log", mode="a")
    try:
        try:
            test_spec = make_test_spec(dict(instance_row), namespace=None)
        except Exception as exc:  # pragma: no cover - defensive
            raise WorkspaceContainerError(
                "unable to resolve the default solver container image from the dataset "
                f"row for {instance_id}: {exc}"
            ) from exc

        run_args = test_spec.docker_specs.get("run_args", {})
        cap_add = tuple(run_args.get("cap_add", []))
        setup_commands = _local_repo_setup_commands(test_spec)
        if image_override:
            return _ResolvedContainerSpec(
                image=image_override,
                platform=None,
                cap_add=cap_add,
                setup_commands=setup_commands,
            )

        import docker.errors as docker_errors  # type: ignore[import]

        try:
            client.images.get(test_spec.env_image_key)
        except docker_errors.ImageNotFound:
            build_env_images(
                client,
                [test_spec],
                force_rebuild=False,
                max_workers=1,
                namespace=None,
                instance_image_tag="latest",
                env_image_tag="latest",
            )

        return _ResolvedContainerSpec(
            image=test_spec.env_image_key,
            platform=test_spec.platform,
            cap_add=cap_add,
            setup_commands=setup_commands,
        )
    finally:
        close_logger(logger)


def _local_repo_setup_commands(test_spec) -> tuple[str, ...]:
    commands: list[str] = []
    for raw_command in getattr(test_spec, "repo_script_list", []):
        command = str(raw_command).strip()
        if not command:
            continue
        if command.startswith("git clone -o origin https://github.com/"):
            continue
        if command.startswith("git reset --hard "):
            continue
        if command == "git remote remove origin":
            continue
        commands.append(command)
    if not commands:
        return ()
    return (
        f"git config --global --add safe.directory {shlex.quote(DOCKER_WORKDIR)} || true",
        *commands,
    )


def _resolve_image_from_adapter_spec(
    *,
    attempt_id: str,
    attempt_root: Path,
    instance_row: Mapping[str, object],
    adapter_spec: Mapping[str, object],
    client,
) -> _ResolvedContainerSpec:
    import docker.errors as docker_errors  # type: ignore[import]

    repo = str(adapter_spec.get("repo") or instance_row.get("repo") or "").strip()
    language = str(adapter_spec.get("language") or "python").strip().lower() or "python"
    docker_specs = dict(adapter_spec.get("docker_specs") or {})
    run_args = docker_specs.get("run_args", {})
    cap_add = (
        tuple(run_args.get("cap_add", []))
        if isinstance(run_args, Mapping)
        else ()
    )
    arch = "x86_64"
    platform = _default_platform_for_arch(arch)
    base_image_key, env_image_key = _local_eval_image_keys(
        repo=repo or language,
        language=language,
        docker_specs=docker_specs,
        arch=arch,
    )

    logger = setup_logger(attempt_id, attempt_root / "container_image.log", mode="a")
    try:
        merged_specs = {**DEFAULT_DOCKER_SPECS, **docker_specs}
        try:
            client.images.get(base_image_key)
        except docker_errors.ImageNotFound:
            build_image(
                image_name=base_image_key,
                setup_scripts={},
                dockerfile=get_dockerfile_base(platform, arch, language, **merged_specs),
                platform=platform,
                client=client,
                build_dir=BASE_IMAGE_BUILD_DIR / base_image_key.replace(":", "__"),
                nocache=False,
            )

        try:
            client.images.get(env_image_key)
        except docker_errors.ImageNotFound:
            build_image(
                image_name=env_image_key,
                setup_scripts={"setup_env.sh": "#!/bin/bash\nset -euxo pipefail\n"},
                dockerfile=get_dockerfile_env(
                    platform,
                    arch,
                    language,
                    base_image_key,
                    **merged_specs,
                ),
                platform=platform,
                client=client,
                build_dir=ENV_IMAGE_BUILD_DIR / env_image_key.replace(":", "__"),
                nocache=False,
            )
    finally:
        close_logger(logger)

    return _ResolvedContainerSpec(
        image=env_image_key,
        platform=platform,
        cap_add=cap_add,
        setup_commands=_adapter_setup_commands(adapter_spec),
    )


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
    workspace_path: Path | None = None,
) -> dict[str, str]:
    command_env = dict(environment or {})
    # Keep host activation state from shadowing toolchains installed in the image.
    for key in list(command_env):
        if key in _CONTAINER_ENV_DROP_KEYS or any(
            key.startswith(prefix) for prefix in _CONTAINER_ENV_DROP_PREFIXES
        ):
            command_env.pop(key, None)
    host_attempt_root = str(attempt_root.resolve())
    host_workspace = str(workspace_path.resolve()) if workspace_path is not None else None
    for key, value in list(command_env.items()):
        if host_workspace is not None and value.startswith(host_workspace):
            command_env[key] = f"{DOCKER_WORKDIR}{value.removeprefix(host_workspace)}"
            continue
        if value.startswith(host_attempt_root):
            command_env[key] = (
                f"{_CONTAINER_ATTEMPT_ROOT}{value.removeprefix(host_attempt_root)}"
            )
    return command_env


def _coerce_shell_commands(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if isinstance(value, (list, tuple)):
        commands: list[str] = []
        for item in value:
            if item is None:
                continue
            text = str(item).strip()
            if text:
                commands.append(text)
        return commands
    text = str(value).strip()
    return [text] if text else []


def _adapter_setup_commands(adapter_spec: Mapping[str, object]) -> tuple[str, ...]:
    commands: list[str] = [
        f"git config --global --add safe.directory {shlex.quote(DOCKER_WORKDIR)} || true",
        f"chmod -R 777 {shlex.quote(DOCKER_WORKDIR)} || true",
        f"cd {shlex.quote(DOCKER_WORKDIR)}",
    ]
    for field in ("pre_install", "install", "build"):
        commands.extend(_coerce_shell_commands(adapter_spec.get(field)))
    return tuple(commands)


def _default_platform_for_arch(arch: str) -> str:
    if arch == "x86_64":
        return "linux/x86_64"
    if arch == "arm64":
        return "linux/arm64/v8"
    raise WorkspaceContainerError(f"unsupported architecture: {arch}")


def _local_eval_image_keys(
    *, repo: str, language: str, docker_specs: Mapping[str, object], arch: str
) -> tuple[str, str]:
    slug_seed = re.sub(r"[^a-z0-9]+", "-", f"{repo}-{language}".lower()).strip("-")
    slug = slug_seed or "repo"
    specs_json = json.dumps(dict(docker_specs), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(specs_json.encode("utf-8")).hexdigest()
    base_key = f"rg.local.base.{slug}.{arch}.{digest[:12]}:latest"
    env_key = f"rg.local.env.{slug}.{arch}.{digest[:22]}:latest"
    return base_key, env_key


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


def _run_workspace_setup_in_container(
    *,
    attempt_root: Path,
    container,
    image: str,
    setup_commands: tuple[str, ...],
    timeout_seconds: int | None,
) -> None:
    if not setup_commands:
        return

    stdout_path = attempt_root / "setup_stdout.txt"
    stderr_path = attempt_root / "setup_stderr.txt"
    for path in (stdout_path, stderr_path):
        if path.exists():
            path.unlink()

    shell_cmd = (
        "{ set -euxo pipefail && "
        + " && ".join(setup_commands)
        + "; }"
        + f" > {shlex.quote(_CONTAINER_SETUP_STDOUT_PATH)}"
        + f" 2> {shlex.quote(_CONTAINER_SETUP_STDERR_PATH)}"
    )
    outcome = _exec_in_container(
        container,
        f"/bin/bash -lc {shlex.quote(shell_cmd)}",
        timeout_seconds,
    )
    if outcome.timed_out:
        raise WorkspaceContainerError(
            "container workspace setup timed out in image "
            f"{image}; check {stdout_path} and {stderr_path}"
        )
    if outcome.exit_code == 0:
        return
    raise WorkspaceContainerError(
        "container workspace setup failed in image "
        f"{image} with exit code {outcome.exit_code}; "
        f"check {stdout_path} and {stderr_path}"
    )


def _workspace_command_shell(command: list[str], *, stdout_path: str, stderr_path: str) -> str:
    prep_steps = [
        f"if [ -e {shlex.quote(DOCKER_WORKDIR)} ]; then chmod -R a+rwX {shlex.quote(DOCKER_WORKDIR)}; fi"
    ]
    return (
        "{ "
        + " && ".join(prep_steps)
        + f" && cd {shlex.quote(DOCKER_WORKDIR)}"
        + " && "
        + shlex.join(command)
        + "; status=$?; "
        + " && ".join(prep_steps)
        + "; exit $status; }"
        + f" > {shlex.quote(stdout_path)}"
        + f" 2> {shlex.quote(stderr_path)}"
    )


def run_workspace_command_in_container(
    *,
    attempt_id: str,
    workspace_path: Path,
    command: list[str],
    timeout_seconds: int,
    container_host: str | None,
    artifacts_root: Path | None = None,
    environment: Mapping[str, str] | None = None,
    image_override: str | None = None,
    instance_row: Mapping[str, object] | None = None,
    adapter_spec: Mapping[str, object] | None = None,
) -> CommandResult:
    attempt_root = (artifacts_root or workspace_path.parent).resolve()
    attempt_root.mkdir(parents=True, exist_ok=True)
    stdout_path = attempt_root / "stdout.txt"
    stderr_path = attempt_root / "stderr.txt"
    for path in (stdout_path, stderr_path):
        if path.exists():
            path.unlink()

    client = _docker_client(container_host=container_host)
    container = None
    logger = setup_logger(attempt_id, attempt_root / "container_run.log", mode="a")
    try:
        if adapter_spec is not None:
            resolved = _resolve_image_from_adapter_spec(
                attempt_id=attempt_id,
                attempt_root=attempt_root,
                instance_row=dict(instance_row or {}),
                adapter_spec=adapter_spec,
                client=client,
            )
        else:
            if instance_row is None:
                raise WorkspaceContainerError(
                    "instance_row is required when adapter_spec is not provided"
                )
            resolved = _resolve_image(
                attempt_id=attempt_id,
                attempt_root=attempt_root,
                instance_id=str(instance_row.get("instance_id", "")),
                instance_row=instance_row,
                image_override=image_override,
                client=client,
            )

        container_name = _sanitize_container_name(f"repogauge-{attempt_id}-workspace")
        try:
            existing = client.containers.get(container_name)
            existing.remove(force=True)
        except Exception:
            pass

        create_kwargs = {
            "image": resolved.image,
            "name": container_name,
            "user": DOCKER_USER,
            "detach": True,
            "command": "tail -f /dev/null",
            "volumes": {
                str(workspace_path): {"bind": DOCKER_WORKDIR, "mode": "rw"},
                str(attempt_root): {"bind": _CONTAINER_ATTEMPT_ROOT, "mode": "rw"},
            },
            "environment": _containerize_environment(
                environment=environment,
                attempt_root=attempt_root,
                workspace_path=workspace_path,
            ),
            "cap_add": list(resolved.cap_add),
        }
        if resolved.platform:
            create_kwargs["platform"] = resolved.platform
        container = client.containers.create(**create_kwargs)
        container.start()

        _run_workspace_setup_in_container(
            attempt_root=attempt_root,
            container=container,
            image=resolved.image,
            setup_commands=resolved.setup_commands,
            timeout_seconds=timeout_seconds,
        )

        shell_cmd = _workspace_command_shell(
            command,
            stdout_path=_CONTAINER_STDOUT_PATH,
            stderr_path=_CONTAINER_STDERR_PATH,
        )
        outcome = _exec_in_container(
            container,
            f"/bin/bash -lc {shlex.quote(shell_cmd)}",
            timeout_seconds,
        )
        stdout = stdout_path.read_text(encoding="utf-8") if stdout_path.exists() else ""
        stderr = stderr_path.read_text(encoding="utf-8") if stderr_path.exists() else ""
        return CommandResult(
            command=tuple(command),
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
        resolved = _resolve_image(
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
            "image": resolved.image,
            "name": container_name,
            "user": DOCKER_USER,
            "detach": True,
            "command": "tail -f /dev/null",
            "volumes": volumes,
            "environment": _containerize_environment(
                environment=environment,
                attempt_root=attempt_root,
                workspace_path=workspace_path,
            ),
            "cap_add": list(resolved.cap_add),
        }
        if resolved.platform:
            create_kwargs["platform"] = resolved.platform
        container = client.containers.create(**create_kwargs)
        container.start()
        resolved_command = list(command)
        try:
            _ensure_solver_command_available(
                container=container, command=resolved_command, image=resolved.image
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
                container=container, command=resolved_command, image=resolved.image
            )

        _run_workspace_setup_in_container(
            attempt_root=attempt_root,
            container=container,
            image=resolved.image,
            setup_commands=resolved.setup_commands,
            timeout_seconds=timeout_seconds,
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
