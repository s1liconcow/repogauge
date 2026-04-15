"""Subprocess invocation helpers."""

from __future__ import annotations

from dataclasses import dataclass
import subprocess
from time import perf_counter
from typing import Dict, Optional, Sequence
from pathlib import Path


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
        proc = subprocess.run(
            command_list,
            cwd=cwd,
            env=env,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            command=command_list,
            returncode=-1,
            stdout=(exc.stdout or ""),
            stderr=(exc.stderr or ""),
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
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
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
    raise RuntimeError(f"command failed: {' '.join(result.command)} (code={result.returncode}, timed_out={result.timed_out})\n{result.stderr}")
