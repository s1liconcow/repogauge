"""Leak-free task feature extraction and deterministic clustering helpers.

Feature definitions are intentionally conservative:

- they only use fields available before solving or during a cheap probe;
- they ignore gold-derived patch fields such as ``patch`` and ``test_patch``;
- they are versioned so downstream reports can be reproduced exactly.

The cluster labels produced here are meant for reporting and coarse policy
analysis, not for a learned router with hidden state.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import re
from typing import Any, Mapping

TASK_FEATURE_VERSION = "task-features-v1"

_STACKTRACE_RE = re.compile(
    r"(?i)\b(traceback|stack ?trace|stacktrace|exception|panic|segfault)\b"
)
_ERROR_RE = re.compile(
    r"(?i)\b(error|failed|failing|failure|bug|broken|crash|incorrect|invalid|regression)\b"
)
_TEST_RE = re.compile(
    r"(?i)\b(test|tests|pytest|unittest|assert|fixture|repro|regression)\b"
)
_PATH_RE = re.compile(r"(?:\b[\w./-]+\.[A-Za-z0-9]{1,6}\b|/[\w./-]+)")


@dataclass(frozen=True)
class TaskFeatureBundle:
    """Versioned task features plus a coarse cluster label."""

    feature_version: str
    features: dict[str, Any]
    cluster_label: str
    feature_hash: str

    def to_metadata(self) -> dict[str, Any]:
        return {
            "task_feature_version": self.feature_version,
            "task_feature_hash": self.feature_hash,
            "task_cluster": self.cluster_label,
            "task_features": self.features,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["metadata"] = self.to_metadata()
        return payload


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _coerce_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return {}


def _read_source_value(row: Mapping[str, Any], *keys: str) -> str:
    metadata = _coerce_mapping(row.get("metadata"))
    for key in keys:
        value = _coerce_text(row.get(key))
        if value:
            return value
        value = _coerce_text(metadata.get(key))
        if value:
            return value
    return ""


def _statement_word_count(statement: str) -> int:
    if not statement:
        return 0
    return len([token for token in re.split(r"\s+", statement.strip()) if token])


def _bucket_length(word_count: int) -> str:
    if word_count <= 12:
        return "short"
    if word_count <= 30:
        return "medium"
    if word_count <= 60:
        return "long"
    return "very_long"


def _bucket_version(version: str) -> str:
    if not version:
        return "unknown"
    if re.search(r"(?i)\bpy\d{2,3}\b", version) or "python" in version.lower():
        return "python-tagged"
    if re.search(r"\d+\.\d+(?:\.\d+)?", version):
        return "semantic"
    if any(sep in version for sep in ("-", "_", "+", "/")):
        return "compound"
    return "opaque"


def _statement_signal(statement: str) -> str:
    if _STACKTRACE_RE.search(statement):
        return "stacktrace"
    if _ERROR_RE.search(statement):
        return "error"
    if _TEST_RE.search(statement):
        return "test"
    if _PATH_RE.search(statement):
        return "path"
    return "neutral"


def build_task_feature_bundle(row: Mapping[str, Any]) -> TaskFeatureBundle:
    """Build a leak-free feature bundle for one task or attempt row."""

    repo = _read_source_value(row, "repo", "instance_repo", "source_repo")
    base_commit = _read_source_value(row, "base_commit", "instance_base_commit")
    version = _read_source_value(row, "version", "instance_version")
    problem_statement = _read_source_value(row, "problem_statement")

    problem_statement_char_count = len(problem_statement)
    problem_statement_line_count = (
        len([line for line in problem_statement.splitlines() if line.strip()])
        if problem_statement
        else 0
    )
    problem_statement_word_count = _statement_word_count(problem_statement)
    statement_signal = _statement_signal(problem_statement)
    statement_length_bucket = _bucket_length(problem_statement_word_count)
    version_bucket = _bucket_version(version)

    features = {
        "repo": repo,
        "base_commit_present": bool(base_commit),
        "version": version,
        "version_bucket": version_bucket,
        "problem_statement_char_count": problem_statement_char_count,
        "problem_statement_line_count": problem_statement_line_count,
        "problem_statement_word_count": problem_statement_word_count,
        "problem_statement_length_bucket": statement_length_bucket,
        "problem_statement_signal": statement_signal,
        "repo_segment_count": repo.count("/") + 1 if repo else 0,
        "repo_slug_length": len(repo),
        "problem_statement_has_stacktrace": statement_signal == "stacktrace",
        "problem_statement_has_error_terms": statement_signal in {"stacktrace", "error"},
        "problem_statement_has_test_terms": statement_signal == "test",
        "problem_statement_has_path_terms": statement_signal == "path",
    }

    cluster_label = "|".join(
        (
            f"len={statement_length_bucket}",
            f"signal={statement_signal}",
            f"version={version_bucket}",
        )
    )

    feature_hash = hashlib.sha256(
        json.dumps(
            {"feature_version": TASK_FEATURE_VERSION, "features": features},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]

    return TaskFeatureBundle(
        feature_version=TASK_FEATURE_VERSION,
        features=features,
        cluster_label=cluster_label,
        feature_hash=feature_hash,
    )


__all__ = [
    "TASK_FEATURE_VERSION",
    "TaskFeatureBundle",
    "build_task_feature_bundle",
]
