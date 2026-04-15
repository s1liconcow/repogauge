"""Artifact location helpers for deterministic command outputs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ArtifactLayout:
    root: Path

    def join(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    @property
    def dataset_file(self) -> Path:
        return self.join("dataset", "dataset.jsonl")

    @property
    def predictions_file(self) -> Path:
        return self.join("dataset", "predictions.gold.jsonl")

    @property
    def validation_file(self) -> Path:
        return self.join("dataset", "validation.jsonl")
