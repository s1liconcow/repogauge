"""Phase 0 byte-compat gate for self-gauge artifacts."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "diff_self_gauge.sh"


def test_phase0_back_compat_script_passes() -> None:
    env = os.environ.copy()
    env.setdefault("UV_CACHE_DIR", "/tmp/codex-uv-cache")
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "allowed new JSON keys: language, language_version, runtime_version" in (
        result.stdout
    )
