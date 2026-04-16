"""Job planning utilities for solver runs."""

from __future__ import annotations

import hashlib
import json
import random
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable

import yaml

from repogauge.config import REPOGAUGE_SCHEMA_VERSION
from .matrix import (
    MatrixConfig,
    MatrixConfigurationError,
    MatrixProvider,
    MatrixSolver,
    _stable_policy_hash,
)


@dataclass(frozen=True)
class PlannedRunJob:
    run_id: str
    job_id: str
    instance_id: str
    solver_id: str
    provider_id: str
    seed: int
    prompt_policy_hash: str
    tool_policy_hash: str
    solver_config_hash: str
    dataset_path: str
    matrix_path: str
    metadata: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["schema_version"] = REPOGAUGE_SCHEMA_VERSION
        return payload


@dataclass(frozen=True)
class RunManifest:
    """Persisted metadata for a run-plan expansion."""

    schema_version: str
    command: str
    run_id: str
    created_at: str
    matrix_path: str
    matrix_path_hash: str
    run_root: str
    matrix_snapshot_path: str
    jobs_path: str
    dataset_path: str
    include_instance_ids: tuple[str, ...]
    exclude_instance_ids: tuple[str, ...]
    repeats: int
    seeds: tuple[int, ...]
    shuffle: bool
    shuffle_seed: int | None
    solver_count: int
    provider_count: int
    job_count: int
    providers: tuple[dict[str, Any], ...]
    solvers: tuple[dict[str, Any], ...]

    @classmethod
    def from_matrix(
        cls,
        *,
        matrix: MatrixConfig,
        jobs: list[PlannedRunJob],
        run_root: Path,
        matrix_out: Path,
        jobs_out: Path,
    ) -> "RunManifest":
        matrix_payload = json.dumps(
            matrix.raw, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return cls(
            schema_version=REPOGAUGE_SCHEMA_VERSION,
            command="run",
            run_id=matrix.run_id,
            created_at=(
                datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"
            ),
            matrix_path=matrix.matrix_path,
            matrix_path_hash=hashlib.sha256(matrix_payload).hexdigest(),
            run_root=str(run_root),
            matrix_snapshot_path=str(matrix_out),
            jobs_path=str(jobs_out),
            dataset_path=matrix.dataset.path,
            include_instance_ids=matrix.dataset.include_instance_ids,
            exclude_instance_ids=matrix.dataset.exclude_instance_ids,
            repeats=matrix.execution.repeats,
            seeds=matrix.execution.seeds,
            shuffle=matrix.execution.shuffle,
            shuffle_seed=matrix.execution.shuffle_seed,
            solver_count=len(matrix.solvers),
            provider_count=len(matrix.providers),
            job_count=len(jobs),
            providers=tuple(
                provider.to_run_manifest_dict() for provider in matrix.providers
            ),
            solvers=tuple(solver.to_run_manifest_dict() for solver in matrix.solvers),
        )

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["schema_version"] = self.schema_version
        return payload


def _apply_filters(
    instance_ids: list[str],
    include_instance_ids: Iterable[str],
    exclude_instance_ids: Iterable[str],
) -> list[str]:
    included = list(instance_ids)
    include_set = [id_ for id_ in include_instance_ids if id_]
    exclude_set = set(exclude_instance_ids)

    if include_set:
        included = [
            instance_id for instance_id in included if instance_id in include_set
        ]
    included = [id_ for id_ in included if id_ not in exclude_set]

    seen = set()
    filtered: list[str] = []
    for instance_id in included:
        if instance_id in seen:
            continue
        seen.add(instance_id)
        filtered.append(instance_id)
    return filtered


def _read_instance_ids(path: Path) -> list[str]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value:
            continue
        payload = json.loads(value)
        instance_id = str(payload.get("instance_id", "")).strip()
        if not instance_id:
            raise MatrixConfigurationError("dataset row missing instance_id")
        rows.append(instance_id)
    return rows


def _providers_by_id(matrix: MatrixConfig) -> dict[str, MatrixProvider]:
    return {provider.provider_id: provider for provider in matrix.providers}


def _solve_seed_hash(
    solver: MatrixSolver,
    *,
    provider: MatrixProvider,
    seed: int,
    instance_id: str,
) -> str:
    payload = {
        "solver_id": solver.solver_id,
        "provider_id": solver.provider_id,
        "provider_kind": provider.kind,
        "adapter": solver.adapter,
        "seed": seed,
        "instance_id": instance_id,
        "prompt_policy": dict(solver.prompt_policy),
        "tool_policy": dict(solver.tool_policy),
        "behavior": dict(solver.behavior),
        "provider_config": dict(provider.config),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _plan_instance_order(
    instance_ids: list[str], shuffle: bool, seed: int | None
) -> list[str]:
    ordered = list(instance_ids)
    if not shuffle:
        return ordered

    rng = random.Random(seed or 0)
    rng.shuffle(ordered)
    return ordered


def plan_jobs(matrix: MatrixConfig) -> list[PlannedRunJob]:
    """Expand a matrix configuration into executable job rows."""
    instance_ids = _read_instance_ids(Path(matrix.dataset.path))
    instances = _apply_filters(
        instance_ids,
        include_instance_ids=matrix.dataset.include_instance_ids,
        exclude_instance_ids=matrix.dataset.exclude_instance_ids,
    )
    instances = _plan_instance_order(
        instances,
        shuffle=matrix.execution.shuffle,
        seed=matrix.execution.shuffle_seed,
    )
    providers_by_id = _providers_by_id(matrix)

    jobs: list[PlannedRunJob] = []
    for instance_id in instances:
        for solver in matrix.solvers:
            provider = providers_by_id[solver.provider_id]
            prompt_policy_hash = _stable_policy_hash(dict(solver.prompt_policy))
            tool_policy_hash = _stable_policy_hash(dict(solver.tool_policy))
            for seed in matrix.execution.seeds:
                job_id = f"{matrix.run_id}:{instance_id}:{solver.solver_id}:{seed}"
                jobs.append(
                    PlannedRunJob(
                        run_id=matrix.run_id,
                        job_id=job_id,
                        instance_id=instance_id,
                        solver_id=solver.solver_id,
                        provider_id=solver.provider_id,
                        seed=seed,
                        prompt_policy_hash=prompt_policy_hash,
                        tool_policy_hash=tool_policy_hash,
                        solver_config_hash=_solve_seed_hash(
                            solver,
                            provider=provider,
                            seed=seed,
                            instance_id=instance_id,
                        ),
                        dataset_path=matrix.dataset.path,
                        matrix_path=matrix.matrix_path,
                        metadata={
                            "provider": solver.provider_id,
                            "provider_kind": provider.kind,
                            "provider_config": dict(provider.redacted_config),
                            "solver_adapter": solver.adapter,
                            "solver_config": dict(solver.behavior),
                            "prompt_policy": dict(solver.prompt_policy),
                            "tool_policy": dict(solver.tool_policy),
                        },
                    )
                )
    return jobs


def write_jobs(jobs: list[PlannedRunJob], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(job.to_dict(), sort_keys=True) + "\n" for job in jobs),
        encoding="utf-8",
    )


def write_run_manifest(manifest: RunManifest, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest.to_dict(), sort_keys=True) + "\n", encoding="utf-8"
    )


def write_matrix_snapshot(path: Path, matrix: MatrixConfig) -> None:
    # Persist a normalized snapshot so run artifacts stay reproducible without
    # copying inline secrets back out of the source matrix file.
    snapshot = deepcopy(matrix.raw)
    if isinstance(snapshot, dict):
        snapshot["providers"] = {
            provider.provider_id: {
                "kind": provider.kind,
                **dict(provider.redacted_config),
            }
            for provider in matrix.providers
        }

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(snapshot, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )


__all__ = [
    "PlannedRunJob",
    "RunManifest",
    "plan_jobs",
    "write_jobs",
    "write_matrix_snapshot",
    "write_run_manifest",
]
