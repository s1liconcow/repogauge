"""Subprocess invocation helpers."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import signal
import subprocess
from time import perf_counter
from typing import Dict, Optional, Sequence


@dataclass
class CommandResult:
    command: Sequence[str]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    elapsed_ms: int = 0
    cwd: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def run_command(
    command: Sequence[str],
    *,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    timeout_seconds: Optional[int] = None,
    input_text: Optional[str] = None,
) -> CommandResult:
    start = perf_counter()
    command_list = list(command)
    try:
        proc = subprocess.Popen(
            command_list,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        stdout, stderr = proc.communicate(input=input_text, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (PermissionError, ProcessLookupError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        try:
            stdout, stderr = proc.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            stdout, stderr = proc.communicate()
        return CommandResult(
            command=command_list,
            returncode=-1,
            stdout=(stdout or exc.stdout or ""),
            stderr=(stderr or exc.stderr or ""),
            timed_out=True,
            elapsed_ms=int((perf_counter() - start) * 1000),
            cwd=cwd or str(Path.cwd()),
        )
    except FileNotFoundError as exc:
        return CommandResult(
            command=command_list,
            returncode=127,
            stdout="",
            stderr=str(exc),
            timed_out=False,
            elapsed_ms=int((perf_counter() - start) * 1000),
            cwd=cwd or str(Path.cwd()),
        )
    return CommandResult(
        command=command_list,
        returncode=proc.returncode,
        stdout=stdout or "",
        stderr=stderr or "",
        timed_out=False,
        elapsed_ms=int((perf_counter() - start) * 1000),
        cwd=cwd or str(Path.cwd()),
    )


def run_command_checked(
    command: Sequence[str],
    *,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    timeout_seconds: Optional[int] = None,
    input_text: Optional[str] = None,
) -> CommandResult:
    """Run a command and raise RuntimeError when it fails."""
    result = run_command(
        command,
        cwd=cwd,
        env=env,
        timeout_seconds=timeout_seconds,
        input_text=input_text,
    )
    if result.success:
        return result
    raise RuntimeError(
        f"command failed: {' '.join(result.command)} (code={result.returncode}, timed_out={result.timed_out})\n{result.stderr}"
    )
