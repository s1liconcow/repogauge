"""Runner package."""

from .planner import (
    PlannedRunJob,
    RunManifest,
    plan_jobs,
    write_jobs,
    write_matrix_copy,
    write_run_manifest,
)
from .adapters import (
    SolverAdapterError,
    build_solver_adapters,
)
from .features import TASK_FEATURE_VERSION, TaskFeatureBundle, build_task_feature_bundle
from .scheduler import (
    SolverAdapter,
    SolverAdapterRequest,
    SolverAdapterResult,
    SolverAttemptState,
    SolverScheduleResult,
    SolverScheduler,
    SolverSchedulerConfig,
    SolverSchedulerError,
)
from .solvers import (
    DEFAULT_SOLVER_ADAPTER_BY_PROVIDER,
    KNOWN_SOLVER_FIELDS,
    SOLVER_ADAPTER_ALIAS,
    SOLVER_ADAPTER_CLAUDE,
    SOLVER_ADAPTER_CODEX_CLI,
    SOLVER_ADAPTER_MOCK,
    SOLVER_ADAPTER_OPENAI_COMPATIBLE,
    SOLVER_ADAPTER_OPENAI_RESPONSES,
    SOLVER_ADAPTER_OPEN_CODEX_SERVER,
    SolverConfig,
    SolverConfigurationError,
    normalize_solver,
)

__all__ = [
    "PlannedRunJob",
    "RunManifest",
    "plan_jobs",
    "write_jobs",
    "write_matrix_copy",
    "write_run_manifest",
    "build_solver_adapters",
    "TASK_FEATURE_VERSION",
    "TaskFeatureBundle",
    "build_task_feature_bundle",
    "SolverAdapterError",
    "SolverAdapter",
    "SolverAdapterRequest",
    "SolverAdapterResult",
    "SolverAttemptState",
    "SolverScheduleResult",
    "SolverScheduler",
    "SolverSchedulerConfig",
    "SolverSchedulerError",
    "DEFAULT_SOLVER_ADAPTER_BY_PROVIDER",
    "KNOWN_SOLVER_FIELDS",
    "SOLVER_ADAPTER_ALIAS",
    "SOLVER_ADAPTER_CLAUDE",
    "SOLVER_ADAPTER_CODEX_CLI",
    "SOLVER_ADAPTER_MOCK",
    "SOLVER_ADAPTER_OPENAI_COMPATIBLE",
    "SOLVER_ADAPTER_OPENAI_RESPONSES",
    "SOLVER_ADAPTER_OPEN_CODEX_SERVER",
    "SolverConfig",
    "SolverConfigurationError",
    "normalize_solver",
]
