"""Canonical data contracts for RepoGauge artifacts.

Contracts are intentionally explicit, JSONL-friendly, and versioned.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

REPOGAUGE_SCHEMA_VERSION = "0.1.0"


class ContractState(str, Enum):
    OPEN = "open"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    FLAKY = "flaky"


@dataclass
class ContractRecord:
    """Base contract with stable version stamping and JSON conversion helpers."""

    schema_version: str = REPOGAUGE_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        if (
            self.schema_version == REPOGAUGE_SCHEMA_VERSION
            and "schema_version" not in payload
        ):
            payload["schema_version"] = REPOGAUGE_SCHEMA_VERSION
        return payload

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ContractRecord":
        return cls(**payload)


@dataclass
class RepoProfile(ContractRecord):
    repo: str = ""
    default_branch: str = "main"
    source_path: str = ""
    python_version: Optional[str] = None
    package_manager: Optional[str] = None
    install_cmds: List[str] = field(default_factory=list)
    test_cmds: List[str] = field(default_factory=list)
    updated_at: str = field(
        default_factory=lambda: (
            datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"
        )
    )
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ScanRow(ContractRecord):
    id: str = ""
    repo: str = ""
    commit: str = ""
    parent_commit: Optional[str] = None
    diff: str = ""
    files_touched: List[str] = field(default_factory=list)
    changed_lines: int = 0
    heuristic_score: float = 0.0
    state: str = "discovered"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CandidateRow(ContractRecord):
    id: str = ""
    repo: str = ""
    source_scan: str = ""
    review_state: ContractState = ContractState.OPEN
    problem_statement: Optional[str] = None
    file_roles: Dict[str, List[str]] = field(default_factory=dict)
    evidence: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ReviewedCandidate(ContractRecord):
    id: str = ""
    candidate_id: str = ""
    repo: str = ""
    reviewer_notes: str = ""
    state: ContractState = ContractState.ACCEPTED
    reason: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DatasetInstance(ContractRecord):
    instance_id: str = ""
    repo: str = ""
    base_commit: str = ""
    problem_statement: str = ""
    version: str = ""
    patch: str = ""
    test_patch: str = ""
    FAIL_TO_PASS: List[str] = field(default_factory=list)
    PASS_TO_PASS: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PredictionRow(ContractRecord):
    instance_id: str = ""
    model_name_or_path: str = ""
    model_patch: str = ""
    solver_id: Optional[str] = None
    prompt_hash: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationRow(ContractRecord):
    instance_id: str = ""
    status: ContractState = ContractState.PENDING
    fail_to_pass: List[str] = field(default_factory=list)
    pass_to_pass: List[str] = field(default_factory=list)
    flake_runs: int = 0
    outcome_summary: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AdapterSpec(ContractRecord):
    repo: str = ""
    version: str = ""
    docker_specs: Dict[str, Any] = field(default_factory=dict)
    install_cmds: List[str] = field(default_factory=list)
    test_cmds: List[str] = field(default_factory=list)
    module_name: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class JobRow(ContractRecord):
    job_id: str = ""
    instance_id: str = ""
    solver_id: str = ""
    status: str = "queued"
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    attempts: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AttemptRow(ContractRecord):
    attempt_id: str = ""
    job_id: str = ""
    instance_id: str = ""
    solver_id: str = ""
    duration_ms: int = 0
    exit_reason: str = ""
    model_patch: Optional[str] = None
    usage: Dict[str, Any] = field(default_factory=dict)
    usage_source: str = ""
    cost: Dict[str, Any] = field(default_factory=dict)
    cost_source: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DiffJudgeRow(ContractRecord):
    attempt_id: str = ""
    job_id: str = ""
    instance_id: str = ""
    solver_id: str = ""
    resolved: bool = False
    harness_outcome: str = "unknown"
    attempt_state: str = "unknown"
    overall_delta: float = 0.0
    overall_label: str = "same"
    confidence: float = 0.0
    summary: str = ""
    dimensions: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class InstanceEvalRow(ContractRecord):
    instance_id: str = ""
    solver_id: str = ""
    model_patch: Optional[str] = None
    harness_outcome: str = "unknown"
    resolved: bool = False
    resolved_at: Optional[str] = None
    failure_reason: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


ALL_RECORD_TYPES = [
    RepoProfile,
    ScanRow,
    CandidateRow,
    ReviewedCandidate,
    DatasetInstance,
    PredictionRow,
    ValidationRow,
    AdapterSpec,
    JobRow,
    AttemptRow,
    DiffJudgeRow,
    InstanceEvalRow,
]

__all__ = ["REPOGAUGE_SCHEMA_VERSION", "ContractState", "ContractRecord"] + [
    cls.__name__ for cls in ALL_RECORD_TYPES
]
