"""Adapter spec contracts used by the generated SWE-bench registration module."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List

from repogauge.config import AdapterSpec


@dataclass
class AdapterConfig:
    specs: List[AdapterSpec] = field(default_factory=list)
    repository_map: Dict[str, str] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "specs": [s.to_dict() for s in self.specs],
            "repository_map": self.repository_map,
            "metadata": self.metadata,
        }


@dataclass
class AdapterRenderContext:
    repository: str
    module_name: str
    spec: AdapterSpec
    metadata: Dict[str, Any] = field(default_factory=dict)

    def as_json(self) -> str:
        import json

        return json.dumps(
            {
                "module_name": self.module_name,
                "repository": self.repository,
                "spec": asdict(self.spec),
                "metadata": self.metadata,
            },
            indent=2,
            sort_keys=True,
        )
