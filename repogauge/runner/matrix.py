"""Matrix configuration parsing and validation for experiment runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from os import PathLike
from pathlib import Path
import hashlib
import json
from typing import Any, Dict, Iterable, Mapping

from .providers import (
    ProviderConfigurationError,
    normalize_provider,
)
from .solvers import SolverConfigurationError, normalize_solver


class MatrixConfigurationError(ValueError):
    """Raised when a matrix file cannot be parsed or is invalid."""


def _coerce_int(value: Any, *, default: int | None = None) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)) and int(value) == value:
        return int(value)
    raise MatrixConfigurationError(
        f"expected integer value, got {type(value).__name__}"
    )


def _coerce_str(value: Any) -> str:
    if value is None:
        raise MatrixConfigurationError("expected string value, got null")
    if isinstance(value, PathLike):
        return str(value)
    if not isinstance(value, str):
        raise MatrixConfigurationError(
            f"expected string value, got {type(value).__name__}"
        )
    return value.strip()


def _coerce_string_list(value: Any, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise MatrixConfigurationError(f"{field_name} must be a list")
    normalized: list[str] = []
    for item in value:
        normalized.append(_coerce_str(item))
    return normalized


def _coerce_mapping(value: Any, *, field_name: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise MatrixConfigurationError(f"{field_name} must be a mapping")
    return value


def _normalize_relative_path(matrix_dir: Path, value: Any) -> Path:
    text = _coerce_str(value)
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = matrix_dir / candidate
    return candidate.resolve()


def _read_yaml(path: Path) -> Dict[str, Any]:
    try:
        import yaml
    except (
        ModuleNotFoundError
    ) as exc:  # pragma: no cover - optional dependency fallback
        raise MatrixConfigurationError(
            "PyYAML is required to parse matrix.yaml; install pyyaml"
        ) from exc

    with path.open("r", encoding="utf-8") as stream:
        raw = yaml.safe_load(stream)
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise MatrixConfigurationError("matrix must be a YAML mapping")
    return dict(raw)


def _normalize_providers(value: Any) -> dict[str, Mapping[str, Any]]:
    providers: dict[str, Mapping[str, Any]] = {}
    source = _coerce_mapping(value, field_name="providers")
    if isinstance(source, Mapping):
        for provider_id, payload in source.items():
            if isinstance(payload, Mapping):
                providers[_coerce_str(provider_id)] = dict(payload)
            else:
                raise MatrixConfigurationError(
                    f"provider '{provider_id}' must be a mapping"
                )
        return providers
    raise MatrixConfigurationError("providers must be a mapping")


@dataclass(frozen=True)
class MatrixProvider:
    provider_id: str
    kind: str
    config: Mapping[str, Any] = field(default_factory=dict)
    redacted_config: Mapping[str, Any] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def resolved(self) -> Mapping[str, Any]:
        return self.config

    def to_run_manifest_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "kind": self.kind,
            "config": self.redacted_config,
        }


@dataclass(frozen=True)
class MatrixSolver:
    solver_id: str
    provider_id: str
    adapter: str
    prompt_policy: Mapping[str, Any] = field(default_factory=dict)
    tool_policy: Mapping[str, Any] = field(default_factory=dict)
    behavior: Mapping[str, Any] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict)

    def to_run_manifest_dict(self) -> dict[str, Any]:
        return {
            "solver_id": self.solver_id,
            "provider_id": self.provider_id,
            "adapter": self.adapter,
            "prompt_policy": self.prompt_policy,
            "tool_policy": self.tool_policy,
            "config": self.behavior,
        }


@dataclass(frozen=True)
class MatrixExecution:
    repeats: int = 1
    seeds: tuple[int, ...] = (0,)
    shuffle: bool = False
    shuffle_seed: int | None = None


@dataclass(frozen=True)
class MatrixDataset:
    path: str
    include_instance_ids: tuple[str, ...] = ()
    exclude_instance_ids: tuple[str, ...] = ()

    @property
    def dataset_path(self) -> Path:
        return Path(self.path)


@dataclass(frozen=True)
class MatrixConfig:
    run_id: str
    matrix_path: str
    dataset: MatrixDataset
    execution: MatrixExecution
    providers: tuple[MatrixProvider, ...]
    solvers: tuple[MatrixSolver, ...]
    fairness: Mapping[str, Any]
    raw: Mapping[str, Any]


def _stable_policy_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _coerce_solver_list(value: Any) -> tuple[Any, ...]:
    if not isinstance(value, list) or not value:
        raise MatrixConfigurationError("solvers must be a non-empty list")
    return tuple(value)


def _load_execution_config(raw_execution: Any) -> MatrixExecution:
    payload = _coerce_mapping(raw_execution, field_name="execution")

    repeats = _coerce_int(payload.get("repeats"), default=None)
    if repeats is None:
        repeats = _coerce_int(payload.get("repeat"), default=1)

    if repeats is None or repeats < 1:
        raise MatrixConfigurationError("execution.repeats must be >= 1")

    seeds_value = payload.get("seeds")
    if seeds_value is None:
        base_seed = _coerce_int(payload.get("seed"), default=0)
        assert base_seed is not None
        seeds = tuple(base_seed + i for i in range(repeats))
    else:
        if not isinstance(seeds_value, Iterable) or isinstance(
            seeds_value, (str, bytes)
        ):
            raise MatrixConfigurationError("execution.seeds must be a list")
        seeds = tuple(_coerce_int(item) for item in seeds_value)
        if not seeds:
            raise MatrixConfigurationError("execution.seeds cannot be empty")

    shuffle = bool(payload.get("shuffle", False))
    shuffle_seed = _coerce_int(payload.get("shuffle_seed"), default=None)

    return MatrixExecution(
        repeats=repeats,
        seeds=seeds,
        shuffle=shuffle,
        shuffle_seed=shuffle_seed,
    )


def load_matrix_config(
    path: str | Path,
    *,
    run_id: str | None = None,
    dataset_path: str | Path | None = None,
) -> MatrixConfig:
    matrix_path = Path(path).expanduser().resolve()
    if not matrix_path.exists():
        raise MatrixConfigurationError(f"matrix file not found: {matrix_path}")

    raw = _read_yaml(matrix_path)
    matrix_run_id = (
        run_id or _coerce_str(raw.get("run_id"))
        if raw.get("run_id")
        else matrix_path.stem
    )

    dataset_section = _coerce_mapping(raw.get("dataset"), field_name="dataset")
    dataset_path_value = dataset_path or dataset_section.get("path")
    if dataset_path_value is None:
        raise MatrixConfigurationError(
            "dataset.path is required unless --dataset is provided"
        )
    dataset_full_path = _normalize_relative_path(matrix_path.parent, dataset_path_value)
    if not dataset_full_path.exists():
        raise MatrixConfigurationError(f"dataset path not found: {dataset_full_path}")
    include_instance_ids = _coerce_string_list(
        dataset_section.get("include_instance_ids"),
        field_name="dataset.include_instance_ids",
    )
    exclude_instance_ids = _coerce_string_list(
        dataset_section.get("exclude_instance_ids"),
        field_name="dataset.exclude_instance_ids",
    )

    provider_rows = _normalize_providers(raw.get("providers", {}))
    provider_rows_list: list[MatrixProvider] = []
    for provider_id, payload in provider_rows.items():
        try:
            provider_config = normalize_provider(
                provider_id, payload, matrix_root=matrix_path.parent
            )
        except ProviderConfigurationError as exc:
            raise MatrixConfigurationError(str(exc)) from exc
        provider_rows_list.append(
            MatrixProvider(
                provider_id=provider_config.provider_id,
                kind=provider_config.kind,
                config=provider_config.resolved,
                redacted_config=provider_config.redacted,
                raw=dict(provider_config.raw),
            )
        )

    provider_kinds = {
        provider.provider_id: provider.kind for provider in provider_rows_list
    }

    solver_rows = raw.get("solvers")
    if solver_rows is None:
        solver_rows = []
    solver_payloads = _coerce_solver_list(solver_rows)
    solvers: list[MatrixSolver] = []
    for row in solver_payloads:
        try:
            solver = normalize_solver(row, provider_kinds=provider_kinds)
        except SolverConfigurationError as exc:
            raise MatrixConfigurationError(str(exc)) from exc

        solvers.append(
            MatrixSolver(
                solver_id=solver.solver_id,
                provider_id=solver.provider_id,
                adapter=solver.adapter,
                prompt_policy=solver.prompt_policy,
                tool_policy=solver.tool_policy,
                behavior=solver.behavior,
                raw=dict(solver.raw),
            )
        )

    execution = _load_execution_config(raw.get("execution"))

    return MatrixConfig(
        run_id=matrix_run_id,
        matrix_path=str(matrix_path),
        dataset=MatrixDataset(
            path=str(dataset_full_path),
            include_instance_ids=tuple(include_instance_ids),
            exclude_instance_ids=tuple(exclude_instance_ids),
        ),
        execution=execution,
        providers=tuple(provider_rows_list),
        solvers=tuple(solvers),
        fairness=_coerce_mapping(raw.get("fairness"), field_name="fairness"),
        raw=raw,
    )


__all__ = [
    "MatrixConfigurationError",
    "MatrixConfig",
    "MatrixDataset",
    "MatrixExecution",
    "MatrixProvider",
    "MatrixSolver",
    "load_matrix_config",
    "_stable_policy_hash",
]
