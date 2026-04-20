"""Shared terminal progress helpers for RepoGauge execution."""

from __future__ import annotations

from collections import Counter
import sys
from typing import TextIO

try:
    from rich.console import Console
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TaskProgressColumn,
        TextColumn,
        TimeElapsedColumn,
    )
except Exception:  # pragma: no cover - optional UI dependency
    Console = None
    Progress = None
    SpinnerColumn = None
    TextColumn = None
    BarColumn = None
    TaskProgressColumn = None
    TimeElapsedColumn = None
    MofNCompleteColumn = None


_STYLE_BY_KIND = {
    "info": "cyan",
    "start": "bright_cyan",
    "success": "green",
    "warning": "yellow",
    "error": "red",
    "skip": "yellow",
}


def emit_progress_line(
    prefix: str,
    message: str,
    *,
    stream: TextIO | None = None,
    kind: str = "info",
) -> None:
    target = stream or sys.stderr
    style = _STYLE_BY_KIND.get(kind, "cyan")
    if Console is not None and hasattr(target, "isatty") and target.isatty():
        console = Console(file=target)
        console.print(f"[{style}]{prefix}: {message}[/{style}]")
        return
    print(f"{prefix}: {message}", file=target, flush=True)


class CountedProgressReporter:
    """Progress reporter that emits lines and, on TTYs, a live Rich bar."""

    def __init__(self, *, prefix: str, total: int, noun: str, stream: TextIO | None = None) -> None:
        self.prefix = prefix
        self.total = max(0, total)
        self.noun = noun
        self.stream = stream or sys.stderr
        self.counts: Counter[str] = Counter()
        self._progress = None
        self._task_id = None
        interactive = (
            Progress is not None
            and hasattr(self.stream, "isatty")
            and self.stream.isatty()
            and self.total > 0
        )
        if interactive:
            console = Console(file=self.stream)
            self._progress = Progress(
                SpinnerColumn(style="cyan"),
                TextColumn(f"[bold cyan]{self.prefix}[/bold cyan]"),
                TextColumn("{task.description}"),
                BarColumn(bar_width=None),
                TaskProgressColumn(),
                MofNCompleteColumn(),
                TimeElapsedColumn(),
                console=console,
                transient=False,
            )
            self._progress.start()
            self._task_id = self._progress.add_task(self.noun, total=self.total)

    def start(self, message: str) -> None:
        emit_progress_line(self.prefix, message, stream=self.stream, kind="start")

    def advance(
        self,
        *,
        status: str,
        message: str,
        description: str | None = None,
        kind: str | None = None,
    ) -> None:
        self.counts[status] += 1
        if self._progress is not None and self._task_id is not None:
            if description:
                self._progress.update(self._task_id, description=description)
            self._progress.advance(self._task_id, 1)
        emit_progress_line(
            self.prefix,
            message,
            stream=self.stream,
            kind=kind or _kind_for_status(status),
        )

    def close(self, *, summary: str | None = None) -> None:
        if summary:
            emit_progress_line(self.prefix, summary, stream=self.stream, kind="info")
        if self._progress is not None:
            self._progress.stop()


def _kind_for_status(status: str) -> str:
    normalized = status.strip().lower()
    if normalized in {"resolved", "succeeded", "success"}:
        return "success"
    if normalized in {"skipped", "skip"}:
        return "skip"
    if normalized in {"error", "failed", "timed_out", "budget_exceeded"}:
        return "error"
    return "info"
