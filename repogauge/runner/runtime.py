"""Shared container runtime helpers for RepoGauge execution."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Iterator, Mapping


class ContainerRuntimeError(RuntimeError):
    """Raised when the configured container runtime cannot be used."""


def coerce_container_host(
    *, container_runtime: str, container_host: str | None
) -> str | None:
    runtime = (container_runtime or "docker").strip().lower() or "docker"
    if runtime not in {"docker", "podman"}:
        raise ContainerRuntimeError(
            f"unsupported container runtime: {container_runtime}"
        )
    explicit = (container_host or "").strip()
    if explicit:
        return explicit
    if runtime == "podman":
        return "unix:///tmp/podman.sock"
    return None


@contextmanager
def temporary_environment(overrides: Mapping[str, str | None]) -> Iterator[None]:
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


def docker_client(*, container_host: str | None):
    import docker as docker_module  # type: ignore[import]

    overrides = {"DOCKER_HOST": container_host} if container_host else {}
    with temporary_environment(overrides):
        return docker_module.from_env()


def unix_socket_path(container_host: str | None) -> Path | None:
    host = (container_host or "").strip()
    if not host.startswith("unix://"):
        return None
    return Path(host.removeprefix("unix://"))


def is_unix_socket_reachable(socket_path: Path) -> bool:
    if not socket_path.exists():
        return False
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(0.2)
        sock.connect(str(socket_path))
        return True
    except OSError:
        return False
    finally:
        sock.close()


@contextmanager
def ensure_container_runtime(
    *,
    container_runtime: str,
    container_host: str | None,
    log_prefix: str = "repogauge",
) -> Iterator[str | None]:
    runtime = (container_runtime or "docker").strip().lower() or "docker"
    host = coerce_container_host(
        container_runtime=runtime,
        container_host=container_host,
    )
    if runtime != "podman":
        yield host
        return

    socket_path = unix_socket_path(host)
    if socket_path is None:
        yield host
        return
    if is_unix_socket_reachable(socket_path):
        yield host
        return

    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        try:
            socket_path.unlink()
        except OSError as exc:
            raise ContainerRuntimeError(
                f"podman socket exists but is not reachable: {socket_path}"
            ) from exc

    print(f"{log_prefix}: starting podman service at {host}", file=sys.stderr)
    try:
        process = subprocess.Popen(
            ["podman", "system", "service", "--time", "0", host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        raise ContainerRuntimeError(
            "podman executable not found; install Podman or use --container-runtime docker"
        ) from exc

    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if is_unix_socket_reachable(socket_path):
                yield host
                return
            if process.poll() is not None:
                break
            time.sleep(0.1)

        stderr_output = ""
        if process.poll() is not None and process.stderr is not None:
            stderr_output = process.stderr.read().strip()
        raise ContainerRuntimeError(
            "failed to start podman system service"
            + (f": {stderr_output}" if stderr_output else "")
        )
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
