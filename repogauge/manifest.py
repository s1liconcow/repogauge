"""Manifest helpers for command-level progress tracking and resumability."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional
from pathlib import Path

from .config import ContractRecord


class ManifestStepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class Manifest(ContractRecord):
    """Top-level artifact manifest for a command execution."""

    command: str = ""
    status: str = "pending"
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    inputs_hash: Optional[str] = None
    steps: Dict[str, str] = field(default_factory=dict)
    artifact_paths: Dict[str, str] = field(default_factory=dict)
    host_info: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    step_statuses: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def start(cls, command: str) -> "Manifest":
        return cls(
            command=command,
            status="running",
            started_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
            + "Z",
        )

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(**payload)

    def finish(self, *, status: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        self.status = status
        if metadata:
            self.metadata.update(metadata)
        self.ended_at = (
            datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"
        )

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), sort_keys=True) + "\n", encoding="utf-8"
        )

    def mark_step(
        self,
        step: str,
        status: str | ManifestStepStatus,
        *,
        started_at: str | None = None,
        ended_at: str | None = None,
    ) -> None:
        normalized = status.value if isinstance(status, ManifestStepStatus) else status
        self.step_statuses[step] = normalized
        if started_at is not None:
            self.steps.setdefault(step + "_started_at", started_at)
        if ended_at is not None:
            self.steps.setdefault(step + "_ended_at", ended_at)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["schema_version"] = self.schema_version
        return payload
