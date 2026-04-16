"""Structured logging helpers used by command execution."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict


def setup_json_logger(path: Path, *, level: int = logging.INFO) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path)
    handler.setLevel(level)
    formatter = logging.Formatter("%(message)s")
    handler.setFormatter(formatter)

    logging.basicConfig(level=level)
    logging.getLogger().addHandler(handler)


def log_event(payload: Dict[str, Any], path: Path) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, sort_keys=True) + "\n")
