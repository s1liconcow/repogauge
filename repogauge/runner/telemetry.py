"""Usage/cost telemetry primitives for solver attempts and judge outcomes."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from repogauge.config import AttemptRow as _AttemptRow


@dataclass
class UsageSnapshot:
    source: str = ""
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AttemptTelemetry:
    attempt_id: str
    provider: str
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        + "Z"
    )
    ended_at: Optional[str] = None
    usage: UsageSnapshot = field(default_factory=UsageSnapshot)
    errors: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_attempt_row(self, base: _AttemptRow) -> _AttemptRow:
        base.duration_ms = self.duration_ms
        base.usage = self.usage.to_dict()
        return base

    @property
    def duration_ms(self) -> int:
        if self.ended_at is None:
            return 0
        start = datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(self.ended_at.replace("Z", "+00:00"))
        return int((end - start).total_seconds() * 1000)
