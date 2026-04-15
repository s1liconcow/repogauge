"""Subprocess invocation helpers."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Dict, Optional, Sequence


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def run_command(
    command: Sequence[str],
    *,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    timeout_seconds: Optional[int] = None,
) -> CommandResult:
    proc = subprocess.run(
        list(command),
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)
