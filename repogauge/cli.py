"""CLI entrypoint for RepoGauge commands.

The current implementation intentionally focuses on scaffold shape. Commands resolve
flags and exit codes in a stable way so downstream modules can be built without
changing public invocation semantics.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

from repogauge.export import run_export, run_materialization
from repogauge.review import run_review

from .logging_utils import log_event
from .manifest import Manifest, ManifestStepStatus
from .mining.inspect import inspect_repository
from .mining.scan import scan_repository
from repogauge.runner.analyze import (
    build_analysis_report,
    load_attempt_rows,
    load_instance_result_rows,
    summarize_attempt_metrics,
    write_summary_csv,
    write_summary_html,
    write_summary_json,
    write_summary_parquet,
)
from repogauge.runner.features import TASK_FEATURE_VERSION
from repogauge.runner.matrix import MatrixConfigurationError, load_matrix_config
from repogauge.runner.adapters import SolverAdapterError, build_solver_adapters
from repogauge.runner.router import (
    build_router_training_rows,
    run_router_training,
    write_router_training_rows,
)
from repogauge.runner.scheduler import (
    SolverAttemptState,
    SolverScheduler,
    SolverSchedulerConfig,
    SolverSchedulerError,
)
from repogauge.runner.planner import (
    RunManifest,
    plan_jobs,
    write_jobs,
    write_matrix_copy,
    write_run_manifest,
)

OUT_DIR_HELP = "Path where artifacts are written (created when needed)."
CONFIG_HELP = "Configuration file path. Values are merged over project defaults."
RESUME_HELP = "Resume into an existing artifact directory."
DRY_RUN_HELP = (
    "Validate inputs and render intended commands without mutating artifacts."
)
LLM_MODE_HELP = "Control model usage: off/local_only/allow_remote."
VERBOSE_HELP = "Enable verbose output."

COMMIT_RANGE_HELP = "Commit range to scan (for example HEAD~50..HEAD)."
MAX_COMMITS_HELP = "Maximum number of commits to inspect."
EXCLUDE_MERGES_HELP = "Skip merge commits during scanning."
ENRICH_GITHUB_HELP = "Add issue/PR metadata from GitHub API when refs are present."
GITHUB_TOKEN_HELP = "GitHub token for API calls (defaults to GITHUB_TOKEN env var)."
GITHUB_ENRICHMENT_CACHE_HELP = "Path for optional GitHub metadata cache (default: <out>/github_enrichment_cache.json)."


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RepoGauge CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ["mine", "review", "export", "eval", "run", "analyze", "train-router"]:
        cmd = subparsers.add_parser(name, help=f"{name} command")
        cmd.add_argument("path", nargs="?", help="Command input path.")
        cmd.add_argument("--config", help=CONFIG_HELP)
        cmd.add_argument("--out", help=OUT_DIR_HELP)
        cmd.add_argument("--resume", action="store_true", help=RESUME_HELP)
        cmd.add_argument("--dry-run", action="store_true", help=DRY_RUN_HELP)
        cmd.add_argument(
            "--llm-mode",
            help=LLM_MODE_HELP,
            choices=["off", "local_only", "allow_remote"],
        )
        cmd.add_argument("--verbose", action="store_true", help=VERBOSE_HELP)
        if name == "review":
            cmd.add_argument(
                "--decisions",
                help="Optional JSON/JSONL file with scripted review decisions.",
            )
            cmd.add_argument(
                "--triage-hints",
                help="Optional structured JSON/JSONL file with advisory triage hints.",
            )
            cmd.add_argument(
                "--llm-model", help="Model identifier for advisory triage outputs."
            )
            cmd.add_argument("--llm-provider", help="LLM provider for advisory triage.")
        if name == "mine":
            cmd.add_argument("--commit-range", help=COMMIT_RANGE_HELP)
            cmd.add_argument(
                "--max-commits", default=100, type=int, help=MAX_COMMITS_HELP
            )
            cmd.add_argument(
                "--exclude-merges", action="store_true", help=EXCLUDE_MERGES_HELP
            )
            cmd.add_argument(
                "--enrich-github",
                action="store_true",
                help=ENRICH_GITHUB_HELP,
            )
            cmd.add_argument(
                "--github-token",
                help=GITHUB_TOKEN_HELP,
            )
            cmd.add_argument(
                "--github-enrichment-cache",
                help=GITHUB_ENRICHMENT_CACHE_HELP,
            )
        if name == "eval":
            cmd.add_argument(
                "--gold",
                action="store_true",
                help="Evaluate gold predictions (predictions.gold.jsonl).",
            )
            cmd.add_argument(
                "--predictions",
                help="Explicit predictions JSONL file (overrides --gold).",
            )
            cmd.add_argument(
                "--timeout",
                default=120,
                type=int,
                help="Per-instance pytest timeout in seconds.",
            )
            cmd.add_argument(
                "--adapter",
                help="Path to adapter_*.py generated by repogauge export. Auto-discovered when omitted.",
            )
        if name == "run":
            cmd.add_argument(
                "--run-id",
                help="Stable run identifier used as runs/<run-id> output directory.",
            )
            cmd.add_argument(
                "--dataset",
                help="Dataset path override (defaults to matrix.yaml dataset.path).",
            )
        if name == "analyze":
            cmd.add_argument(
                "--group-by",
                default="solver_id",
                help="Comma-separated dimensions to aggregate summaries by.",
            )
            cmd.add_argument(
                "--expensive-cost-threshold",
                default=1.0,
                type=float,
                help="Threshold for classifying expensive attempts in metrics.",
            )
        if name == "train-router":
            cmd.add_argument(
                "--seed",
                default=0,
                type=int,
                help="Deterministic split/model seed for router training.",
            )
            cmd.add_argument(
                "--train-fraction",
                default=0.8,
                type=float,
                help="Fraction of rows to use for router training.",
            )
            cmd.add_argument(
                "--validation-fraction",
                default=0.1,
                type=float,
                help="Fraction of rows to use for router validation.",
            )
            cmd.add_argument(
                "--max-depth",
                default=3,
                type=int,
                help="Maximum tree depth for the supervised router baseline.",
            )

    return parser


def _inputs_hash(command: str, namespace: argparse.Namespace) -> str:
    github_token = ""
    if command == "mine":
        if getattr(namespace, "github_token", None):
            github_token = namespace.github_token
        else:
            github_token = os.getenv("GITHUB_TOKEN", "")
    payload = {
        "command": command,
        "path": namespace.path or "",
        "group_by": namespace.group_by if command == "analyze" else "",
        "expensive_cost_threshold": (
            namespace.expensive_cost_threshold if command == "analyze" else 0.0
        ),
        "config": namespace.config or "",
        "dataset": namespace.dataset if command == "run" else "",
        "run_id": namespace.run_id if command == "run" else "",
        "commit_range": namespace.commit_range if command == "mine" else "",
        "max_commits": namespace.max_commits if command == "mine" else 0,
        "exclude_merges": namespace.exclude_merges if command == "mine" else False,
        "enrich_github": namespace.enrich_github if command == "mine" else False,
        "github_token": github_token,
        "github_enrichment_cache_path": namespace.github_enrichment_cache
        if command == "mine"
        else "",
        "dry_run": bool(namespace.dry_run),
        "llm_mode": namespace.llm_mode or "",
        "llm_model": getattr(namespace, "llm_model", ""),
        "llm_provider": getattr(namespace, "llm_provider", ""),
        "triage_hints": getattr(namespace, "triage_hints", ""),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _resolve_eval_paths(source: Path) -> tuple[Path, Path]:
    """Return (dataset_path, gold_predictions_path) from a dataset path or directory."""
    if source.is_dir():
        dataset = source / "dataset" / "dataset.jsonl"
        predictions = source / "dataset" / "predictions.gold.jsonl"
    elif source.name == "dataset.jsonl":
        dataset = source
        predictions = source.parent / "predictions.gold.jsonl"
    else:
        dataset = source
        predictions = source.parent / "predictions.gold.jsonl"
    return dataset, predictions


def _parse_group_by(value: str) -> tuple[str, ...]:
    if not value:
        return ("solver_id",)
    parts = [part.strip() for part in value.split(",") if part.strip()]
    if not parts:
        return ("solver_id",)
    return tuple(parts)


def _resolve_repo_root(path_value: str | Path) -> Path:
    path = Path(path_value).resolve()
    if path.is_file():
        path = path.parent
    if (path / ".git").exists():
        return path
    try:
        from repogauge.utils.git import get_repo_root

        return Path(get_repo_root(path))
    except Exception:
        pass
    for ancestor in path.parents:
        if (ancestor / ".git").exists():
            return ancestor
    raise RuntimeError(f"cannot resolve repository root from {path_value}")


def _load_dataset_rows(dataset_path: Path) -> dict[str, dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    for line in dataset_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line.strip())
        if not isinstance(row, dict):
            raise RuntimeError("dataset row must be a JSON object")

        instance_id = str(row.get("instance_id", "")).strip()
        if not instance_id:
            raise RuntimeError("dataset row missing instance_id")
        rows[instance_id] = dict(row)
    return rows


def _run_command(namespace: argparse.Namespace) -> int:
    command = namespace.command
    source = Path(namespace.path).resolve() if namespace.path else Path(".").resolve()
    if command == "analyze":
        run_root = source if source.is_dir() else source.parent
        out_root = (
            Path(namespace.out).resolve() if namespace.out else run_root / "analyze"
        )
    else:
        run_root = Path(".")
        out_root = Path(namespace.out or Path(".") / ".repogauge" / command).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    manifest_path = out_root / "manifest.json"
    events_path = out_root / "events.jsonl"
    command_timestamp = (
        datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"
    )

    if namespace.resume and manifest_path.exists():
        try:
            manifest = Manifest.load(manifest_path)
            if (
                manifest.inputs_hash == _inputs_hash(command, namespace)
                and manifest.status == "succeeded"
            ):
                manifest.mark_step(
                    "resume", ManifestStepStatus.SKIPPED, started_at=command_timestamp
                )
                manifest.finish(
                    status="succeeded",
                    metadata={"resume": True, "reason": "reused_existing_manifest"},
                )
                log_event(
                    {
                        "event": "command.resume",
                        "command": command,
                        "status": manifest.status,
                    },
                    events_path,
                )
                manifest.write(manifest_path)
                return 0
            manifest = Manifest.start(command)
        except Exception:
            manifest = Manifest.start(command)
    else:
        manifest = Manifest.start(command)

    manifest.inputs_hash = _inputs_hash(command, namespace)
    manifest.artifact_paths.update(
        {
            "manifest": str(manifest_path),
            "events": str(events_path),
        }
    )
    manifest.host_info = {
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
    }
    manifest.mark_step(
        "bootstrap", ManifestStepStatus.RUNNING, started_at=command_timestamp
    )
    manifest.write(manifest_path)
    log_event(
        {
            "event": "command.start",
            "command": command,
            "status": manifest.status,
            "path": namespace.path,
            "inputs_hash": manifest.inputs_hash,
            "timestamp": command_timestamp,
            "dry_run": bool(namespace.dry_run),
        },
        events_path,
    )

    if namespace.dry_run:
        manifest.mark_step("inspect", ManifestStepStatus.SKIPPED)
        manifest.mark_step("execute", ManifestStepStatus.SKIPPED)
        manifest.finish(status="succeeded", metadata={"reason": "dry_run"})
        manifest.mark_step("finish", ManifestStepStatus.SUCCEEDED)
        manifest.write(manifest_path)
        log_event(
            {
                "event": "command.finish",
                "command": command,
                "status": manifest.status,
                "timestamp": datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            },
            events_path,
        )
        return 0

    if command == "review":
        if not namespace.path:
            manifest.mark_step(
                "inspect",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.mark_step("execute", ManifestStepStatus.SKIPPED)
            manifest.finish(
                status="failed", metadata={"reason": "missing_review_input"}
            )
            manifest.mark_step(
                "finish",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.write(manifest_path)
            log_event(
                {
                    "event": "command.finish",
                    "command": command,
                    "status": manifest.status,
                    "timestamp": manifest.ended_at,
                    "error": "missing review input path",
                },
                events_path,
            )
            return 1

        source = Path(namespace.path).resolve()
        candidates_path = source
        if source.is_dir():
            candidates_path = source / "candidates.jsonl"

        out_root = (
            Path(namespace.out or str(source.parent)).resolve()
            if source.is_file()
            else source.resolve()
        )
        manifest.mark_step(
            "inspect", ManifestStepStatus.RUNNING, started_at=command_timestamp
        )
        try:
            review_summary = run_review(
                candidates_path=candidates_path,
                out_root=out_root,
                decisions_path=Path(namespace.decisions).resolve()
                if namespace.decisions
                else None,
                llm_mode=namespace.llm_mode,
                triage_hints_path=Path(namespace.triage_hints).resolve()
                if namespace.triage_hints
                else None,
                llm_model_name=namespace.llm_model
                if hasattr(namespace, "llm_model")
                else None,
                llm_provider=namespace.llm_provider
                if hasattr(namespace, "llm_provider")
                else None,
            )
            for warning in review_summary.get("warnings", []):
                if warning:
                    print(f"warning: {warning}", file=sys.stderr)
            manifest.mark_step("inspect", ManifestStepStatus.SUCCEEDED)
            manifest.mark_step(
                "execute",
                ManifestStepStatus.SUCCEEDED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.artifact_paths["reviewed"] = review_summary["reviewed_path"]
            manifest.artifact_paths["review_markdown"] = review_summary["markdown_path"]
            manifest.artifact_paths["review_html"] = review_summary["html_path"]
            manifest.metadata["review"] = review_summary
        except Exception as exc:
            manifest.mark_step(
                "inspect",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.mark_step("execute", ManifestStepStatus.SKIPPED)
            manifest.finish(
                status="failed", metadata={"reason": "review_failed", "error": str(exc)}
            )
            manifest.mark_step(
                "finish",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.write(manifest_path)
            log_event(
                {
                    "event": "command.finish",
                    "command": command,
                    "status": manifest.status,
                    "timestamp": manifest.ended_at,
                    "error": str(exc),
                },
                events_path,
            )
            return 1

        manifest.mark_step(
            "finish",
            ManifestStepStatus.SUCCEEDED,
            ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
        )
        manifest.finish(
            status="succeeded",
            metadata={"reason": "review_complete", "path": namespace.path},
        )
        manifest.write(manifest_path)
        log_event(
            {
                "event": "command.finish",
                "command": command,
                "status": manifest.status,
                "timestamp": manifest.ended_at,
            },
            events_path,
        )
        return 0

    if command == "mine":
        manifest.mark_step(
            "inspect", ManifestStepStatus.RUNNING, started_at=command_timestamp
        )
        try:
            profile = inspect_repository(namespace.path or ".")
        except Exception as exc:  # pragma: no cover - defensive for unsupported paths
            manifest.mark_step(
                "inspect",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.mark_step("execute", ManifestStepStatus.SKIPPED)
            manifest.finish(
                status="failed",
                metadata={"reason": "inspect_failed", "error": str(exc)},
            )
            manifest.mark_step(
                "finish",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.write(manifest_path)
            log_event(
                {
                    "event": "command.finish",
                    "command": command,
                    "status": manifest.status,
                    "timestamp": manifest.ended_at,
                    "error": str(exc),
                },
                events_path,
            )
            return 1

        repo_profile_path = out_root / "repo_profile.json"
        repo_profile_path.write_text(
            json.dumps(profile, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        manifest.artifact_paths["repo_profile"] = str(repo_profile_path)
        manifest.mark_step("inspect", ManifestStepStatus.SUCCEEDED)

        manifest.mark_step(
            "scan", ManifestStepStatus.RUNNING, started_at=command_timestamp
        )
        try:
            scan_rows = scan_repository(
                namespace.path or ".",
                repo_name=profile["repo_name"],
                max_count=int(namespace.max_commits),
                commit_range=namespace.commit_range,
                include_merges=not namespace.exclude_merges,
                enrich_github=bool(namespace.enrich_github),
                enrichment_cache_path=(
                    Path(namespace.github_enrichment_cache).expanduser()
                    if namespace.github_enrichment_cache
                    else out_root / "github_enrichment_cache.json"
                )
                if namespace.enrich_github
                else None,
                github_token=(
                    namespace.github_token
                    if namespace.github_token is not None
                    else os.getenv("GITHUB_TOKEN")
                ),
            )
        except Exception as exc:
            manifest.mark_step(
                "scan",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.mark_step("execute", ManifestStepStatus.SKIPPED)
            manifest.finish(
                status="failed", metadata={"reason": "scan_failed", "error": str(exc)}
            )
            manifest.mark_step(
                "finish",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.write(manifest_path)
            log_event(
                {
                    "event": "command.finish",
                    "command": command,
                    "status": manifest.status,
                    "timestamp": manifest.ended_at,
                    "error": str(exc),
                },
                events_path,
            )
            return 1

        scan_path = out_root / "scan.jsonl"
        scan_path.write_text(
            "".join(
                json.dumps(row.to_dict(), sort_keys=True) + "\n" for row in scan_rows
            ),
            encoding="utf-8",
        )
        manifest.artifact_paths["scan"] = str(scan_path)

        candidates_path = out_root / "candidates.jsonl"
        candidates_path.write_text(
            "".join(
                json.dumps(row.to_dict(), sort_keys=True) + "\n" for row in scan_rows
            ),
            encoding="utf-8",
        )
        manifest.artifact_paths["candidates"] = str(candidates_path)
        manifest.mark_step(
            "scan",
            ManifestStepStatus.SUCCEEDED,
            ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
        )
        manifest.metadata["scan"] = {
            "scan_count": len(scan_rows),
            "commit_range": namespace.commit_range,
            "max_commits": namespace.max_commits,
            "include_merges": not namespace.exclude_merges,
        }
        manifest.mark_step(
            "execute",
            ManifestStepStatus.SUCCEEDED,
            ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
        )
        manifest.mark_step(
            "finish",
            ManifestStepStatus.SUCCEEDED,
            ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
        )
        manifest.finish(
            status="succeeded",
            metadata={"reason": "scan_complete", "path": namespace.path},
        )
        manifest.write(manifest_path)
        log_event(
            {
                "event": "command.finish",
                "command": command,
                "status": manifest.status,
                "timestamp": manifest.ended_at,
            },
            events_path,
        )
        return 0

    if command == "export":
        if not namespace.path:
            manifest.mark_step(
                "inspect",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.mark_step("execute", ManifestStepStatus.SKIPPED)
            manifest.finish(
                status="failed", metadata={"reason": "missing_export_input"}
            )
            manifest.mark_step(
                "finish",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.write(manifest_path)
            log_event(
                {
                    "event": "command.finish",
                    "command": command,
                    "status": manifest.status,
                    "timestamp": manifest.ended_at,
                    "error": "missing reviewed input path",
                },
                events_path,
            )
            return 1

        source = Path(namespace.path).resolve()
        reviewed_path = source / "reviewed.jsonl" if source.is_dir() else source
        if not reviewed_path.exists():
            manifest.mark_step(
                "inspect",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.mark_step("execute", ManifestStepStatus.SKIPPED)
            manifest.finish(
                status="failed",
                metadata={
                    "reason": "missing_reviewed_input",
                    "path": str(reviewed_path),
                },
            )
            manifest.mark_step(
                "finish",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.write(manifest_path)
            log_event(
                {
                    "event": "command.finish",
                    "command": command,
                    "status": manifest.status,
                    "timestamp": manifest.ended_at,
                    "error": f"reviewed artifact not found: {reviewed_path}",
                },
                events_path,
            )
            return 1

        manifest.mark_step(
            "inspect", ManifestStepStatus.RUNNING, started_at=command_timestamp
        )
        try:
            repo_root = _resolve_repo_root(source)
            export_summary = run_materialization(
                reviewed_path=reviewed_path,
                out_root=out_root,
                repo_root=repo_root,
            )
            dataset_summary = run_export(
                materialized_path=export_summary["materialized_path"],
                out_root=out_root,
            )

            # Generate adapter spec so eval can load per-repo harness settings.
            from repogauge.export.adapter import generate_adapter

            adapter_repo_name = ""
            adapter_env_plan: dict = {}
            if export_summary.get("ready_count", 0) > 0:
                mat_path = Path(export_summary["materialized_path"])
                mat_rows = [
                    json.loads(ln)
                    for ln in mat_path.read_text(encoding="utf-8").splitlines()
                    if ln.strip()
                ]
                if mat_rows:
                    adapter_repo_name = mat_rows[0].get("repo", "")
            # Best-effort: look for environment_plan in sibling mine output.
            for profile_candidate in [
                source.parent / "mine" / "repo_profile.json",
                out_root.parent / "mine" / "repo_profile.json",
                source / "repo_profile.json",
            ]:
                if profile_candidate.exists():
                    try:
                        profile_data = json.loads(
                            profile_candidate.read_text(encoding="utf-8")
                        )
                        adapter_env_plan = profile_data.get("environment_plan", {})
                    except Exception:
                        pass
                    break
            if adapter_repo_name:
                adapter_result = generate_adapter(
                    adapter_repo_name, adapter_env_plan, out_root=out_root
                )
                manifest.artifact_paths["adapter"] = adapter_result["adapter_path"]
                manifest.artifact_paths["specs"] = adapter_result["specs_path"]

            manifest.mark_step("inspect", ManifestStepStatus.SUCCEEDED)
            manifest.artifact_paths["materialized"] = export_summary[
                "materialized_path"
            ]
            manifest.artifact_paths["materialization_rejections"] = export_summary[
                "rejected_path"
            ]
            manifest.artifact_paths["dataset"] = dataset_summary["dataset_path"]
            manifest.artifact_paths["predictions"] = dataset_summary["predictions_path"]
            manifest.metadata["export"] = {
                "ready_count": export_summary["ready_count"],
                "rejected_count": export_summary["rejected_count"],
                "total_count": export_summary["total_count"],
                "dataset_count": dataset_summary["dataset_count"],
                "prediction_count": dataset_summary["prediction_count"],
            }
            manifest.mark_step(
                "execute",
                ManifestStepStatus.SUCCEEDED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.mark_step(
                "finish",
                ManifestStepStatus.SUCCEEDED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.finish(
                status="succeeded",
                metadata={"reason": "export_complete", "path": namespace.path},
            )
            manifest.write(manifest_path)
            log_event(
                {
                    "event": "command.finish",
                    "command": command,
                    "status": manifest.status,
                    "timestamp": manifest.ended_at,
                },
                events_path,
            )
            return 0
        except Exception as exc:
            manifest.mark_step(
                "inspect",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.mark_step("execute", ManifestStepStatus.SKIPPED)
            manifest.finish(
                status="failed", metadata={"reason": "export_failed", "error": str(exc)}
            )
            manifest.mark_step(
                "finish",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.write(manifest_path)
            log_event(
                {
                    "event": "command.finish",
                    "command": command,
                    "status": manifest.status,
                    "timestamp": manifest.ended_at,
                    "error": str(exc),
                },
                events_path,
            )
            return 1

    if command == "eval":
        if not namespace.path:
            manifest.mark_step(
                "inspect",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.mark_step("execute", ManifestStepStatus.SKIPPED)
            manifest.finish(status="failed", metadata={"reason": "missing_eval_input"})
            manifest.mark_step(
                "finish",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.write(manifest_path)
            log_event(
                {
                    "event": "command.finish",
                    "command": command,
                    "status": manifest.status,
                    "timestamp": manifest.ended_at,
                    "error": "missing dataset path",
                },
                events_path,
            )
            return 1

        source = Path(namespace.path).resolve()
        dataset_path, gold_predictions = _resolve_eval_paths(source)

        if namespace.predictions:
            predictions_path = Path(namespace.predictions).resolve()
            gold_if_missing = False
            if not predictions_path.exists():
                manifest.finish(
                    status="failed",
                    metadata={
                        "reason": "predictions_not_found",
                        "path": str(predictions_path),
                    },
                )
                manifest.mark_step(
                    "finish",
                    ManifestStepStatus.FAILED,
                    ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                    + "Z",
                )
                manifest.write(manifest_path)
                log_event(
                    {
                        "event": "command.finish",
                        "command": command,
                        "status": manifest.status,
                        "timestamp": manifest.ended_at,
                        "error": f"predictions not found: {predictions_path}",
                    },
                    events_path,
                )
                return 1
        elif namespace.gold:
            predictions_path = gold_predictions
            gold_if_missing = True
        else:
            manifest.finish(
                status="failed",
                metadata={"reason": "predictions_not_specified"},
            )
            manifest.mark_step(
                "finish",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.write(manifest_path)
            log_event(
                {
                    "event": "command.finish",
                    "command": command,
                    "status": manifest.status,
                    "timestamp": manifest.ended_at,
                    "error": "either --predictions or --gold is required",
                },
                events_path,
            )
            return 1

        if not dataset_path.exists():
            manifest.finish(
                status="failed",
                metadata={"reason": "dataset_not_found", "path": str(dataset_path)},
            )
            manifest.mark_step(
                "finish",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.write(manifest_path)
            log_event(
                {
                    "event": "command.finish",
                    "command": command,
                    "status": manifest.status,
                    "timestamp": manifest.ended_at,
                    "error": f"dataset not found: {dataset_path}",
                },
                events_path,
            )
            return 1

        if not gold_if_missing and not predictions_path.exists():
            manifest.finish(
                status="failed",
                metadata={
                    "reason": "predictions_not_found",
                    "path": str(predictions_path),
                },
            )
            manifest.mark_step(
                "finish",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.write(manifest_path)
            log_event(
                {
                    "event": "command.finish",
                    "command": command,
                    "status": manifest.status,
                    "timestamp": manifest.ended_at,
                    "error": f"predictions not found: {predictions_path}",
                },
                events_path,
            )
            return 1

        manifest.mark_step(
            "inspect", ManifestStepStatus.RUNNING, started_at=command_timestamp
        )

        # --- Load/locate adapter (per-repo harness settings) -------------------
        adapter_path: Path | None = None
        if namespace.adapter:
            candidate = Path(namespace.adapter).resolve()
            if not candidate.exists():
                manifest.finish(
                    status="failed",
                    metadata={"reason": "adapter_not_found", "path": str(candidate)},
                )
                manifest.mark_step(
                    "finish",
                    ManifestStepStatus.FAILED,
                    ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                    + "Z",
                )
                manifest.write(manifest_path)
                log_event(
                    {
                        "event": "command.finish",
                        "command": command,
                        "status": manifest.status,
                        "timestamp": manifest.ended_at,
                        "error": f"adapter not found: {candidate}",
                    },
                    events_path,
                )
                return 1
            adapter_path = candidate
        else:
            seen_dirs: set[Path] = set()
            search_roots: list[Path] = [source]
            if source.is_file():
                search_roots.append(source.parent)
            search_roots.extend(source.parents[:3])
            for root in search_roots:
                if root in seen_dirs:
                    continue
                seen_dirs.add(root)
                if not root.is_dir():
                    continue
                candidates = sorted(root.glob("adapter_*.py"))
                if candidates:
                    adapter_path = candidates[0]
                    break

            if adapter_path is None and source.name == "dataset":
                for root in [source.parent, source.parent.parent]:
                    if not root.is_dir():
                        continue
                    candidates = sorted(root.glob("adapter_*.py"))
                    if candidates:
                        adapter_path = candidates[0]
                        break

        if adapter_path is not None:
            print(f"repogauge eval: adapter={adapter_path}", file=sys.stderr)
            specs_path = adapter_path.parent / "specs.json"
            if specs_path.exists():
                print(f"repogauge eval: specs={specs_path}", file=sys.stderr)
        else:
            print(
                "repogauge eval: no adapter found; attempting built-in SWE-bench behavior",
                file=sys.stderr,
            )

        print(f"repogauge eval: dataset={dataset_path}", file=sys.stderr)
        print(f"repogauge eval: predictions={predictions_path}", file=sys.stderr)

        if gold_if_missing and not predictions_path.exists():
            print(
                "repogauge eval: generating gold predictions from dataset",
                file=sys.stderr,
            )

        try:
            from repogauge.runner.judge import run_harness_evaluation

            eval_summary = run_harness_evaluation(
                dataset_path=dataset_path,
                predictions_path=predictions_path,
                out_root=out_root,
                adapter_path=adapter_path,
                workers=1,
                timeout_seconds=getattr(namespace, "timeout", 120),
                gold_if_missing=gold_if_missing,
            )
            manifest.mark_step("inspect", ManifestStepStatus.SUCCEEDED)
            manifest.mark_step(
                "execute",
                ManifestStepStatus.SUCCEEDED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.artifact_paths["validation"] = eval_summary.validation_path
            if eval_summary.results_path is not None:
                manifest.artifact_paths["results"] = eval_summary.results_path
            if eval_summary.instance_results_path is not None:
                manifest.artifact_paths["instance_results"] = (
                    eval_summary.instance_results_path
                )
            manifest.metadata["eval"] = {
                "total": eval_summary.total,
                "resolved": eval_summary.resolved,
                "not_resolved": eval_summary.not_resolved,
                "error": eval_summary.error,
                "skipped": eval_summary.skipped,
                "resolve_rate": eval_summary.resolve_rate,
                "harness_output": eval_summary.harness_output,
            }
            manifest.mark_step(
                "finish",
                ManifestStepStatus.SUCCEEDED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.finish(status="succeeded", metadata={"reason": "eval_complete"})
            manifest.write(manifest_path)
            log_event(
                {
                    "event": "command.finish",
                    "command": command,
                    "status": manifest.status,
                    "timestamp": manifest.ended_at,
                },
                events_path,
            )
            total = eval_summary.total
            resolved = eval_summary.resolved
            rate = eval_summary.resolve_rate
            print(
                f"repogauge eval: {resolved}/{total} resolved ({rate:.1%})",
                file=sys.stderr,
            )
            return 0
        except Exception as exc:
            manifest.mark_step(
                "inspect",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.mark_step("execute", ManifestStepStatus.SKIPPED)
            manifest.finish(
                status="failed", metadata={"reason": "eval_failed", "error": str(exc)}
            )
            manifest.mark_step(
                "finish",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.write(manifest_path)
            log_event(
                {
                    "event": "command.finish",
                    "command": command,
                    "status": manifest.status,
                    "timestamp": manifest.ended_at,
                    "error": str(exc),
                },
                events_path,
            )
            print(f"repogauge eval: error: {exc}", file=sys.stderr)
            return 1

    if command == "analyze":
        if not namespace.path:
            manifest.mark_step(
                "inspect",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.mark_step("execute", ManifestStepStatus.SKIPPED)
            manifest.finish(
                status="failed", metadata={"reason": "missing_analyze_input"}
            )
            manifest.mark_step(
                "finish",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.write(manifest_path)
            log_event(
                {
                    "event": "command.finish",
                    "command": command,
                    "status": manifest.status,
                    "timestamp": manifest.ended_at,
                    "error": "missing analyze input path",
                },
                events_path,
            )
            return 1

        analyze_root = run_root if run_root.is_dir() else run_root.parent
        if not analyze_root.is_dir():
            analyze_root = Path(".").resolve()

        attempts_path = None
        for candidate in (
            analyze_root / "attempts.jsonl",
            analyze_root / "attempts.parquet",
        ):
            if candidate.exists():
                attempts_path = candidate
                break
        if attempts_path is None:
            manifest.mark_step(
                "inspect",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.mark_step("execute", ManifestStepStatus.SKIPPED)
            manifest.finish(
                status="failed",
                metadata={
                    "reason": "analyze_missing_attempts",
                    "path": str(analyze_root),
                },
            )
            manifest.mark_step(
                "finish",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.write(manifest_path)
            log_event(
                {
                    "event": "command.finish",
                    "command": command,
                    "status": manifest.status,
                    "timestamp": manifest.ended_at,
                    "error": f"attempt artifacts not found in {analyze_root}",
                },
                events_path,
            )
            return 1

        instance_results_path = None
        for candidate in (
            analyze_root / "instance_results.jsonl",
            analyze_root / "validation.jsonl",
        ):
            if candidate.exists():
                instance_results_path = candidate
                break
        if instance_results_path is None:
            manifest.mark_step(
                "inspect",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.mark_step("execute", ManifestStepStatus.SKIPPED)
            manifest.finish(
                status="failed",
                metadata={
                    "reason": "analyze_missing_instance_results",
                    "path": str(analyze_root),
                },
            )
            manifest.mark_step(
                "finish",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.write(manifest_path)
            log_event(
                {
                    "event": "command.finish",
                    "command": command,
                    "status": manifest.status,
                    "timestamp": manifest.ended_at,
                    "error": f"instance_results artifact not found in {analyze_root}",
                },
                events_path,
            )
            return 1

        manifest.mark_step(
            "inspect", ManifestStepStatus.RUNNING, started_at=command_timestamp
        )
        try:
            attempt_rows = load_attempt_rows(attempts_path)
            instance_results = load_instance_result_rows(instance_results_path)
            router_rows = build_router_training_rows(attempt_rows, instance_results)
            router_train_path = run_root / "router_train.parquet"
            summary_rows = summarize_attempt_metrics(
                attempts=attempt_rows,
                instance_results=instance_results,
                group_by=_parse_group_by(namespace.group_by),
                expensive_cost_threshold=float(namespace.expensive_cost_threshold),
            )
            solver_rows = summarize_attempt_metrics(
                attempts=attempt_rows,
                instance_results=instance_results,
                group_by=("solver_id",),
                expensive_cost_threshold=float(namespace.expensive_cost_threshold),
            )
            analysis_report = build_analysis_report(
                attempts=attempt_rows,
                instance_results=instance_results,
                grouped_summaries=summary_rows,
                solver_summaries=solver_rows,
                group_by=_parse_group_by(namespace.group_by),
                expensive_cost_threshold=float(namespace.expensive_cost_threshold),
                metadata={
                    "run_root": str(analyze_root),
                    "group_by": _parse_group_by(namespace.group_by),
                    "expensive_cost_threshold": namespace.expensive_cost_threshold,
                    "task_feature_version": TASK_FEATURE_VERSION,
                    "attempt_rows": len(attempt_rows),
                    "instance_result_rows": len(instance_results),
                    "router_training_rows": len(router_rows),
                },
            )

            summary_path = out_root / "summary.json"
            report_csv_path = out_root / "report.csv"
            report_parquet_path = out_root / "report.parquet"
            report_html_path = out_root / "report.html"

            write_summary_json(
                summary_path,
                summary_rows,
                metadata={
                    "run_root": str(analyze_root),
                    "group_by": _parse_group_by(namespace.group_by),
                    "expensive_cost_threshold": namespace.expensive_cost_threshold,
                    "task_feature_version": TASK_FEATURE_VERSION,
                    "attempt_rows": len(attempt_rows),
                    "instance_result_rows": len(instance_results),
                    "router_training_rows": len(router_rows),
                },
                report=analysis_report,
            )
            write_summary_csv(
                report_csv_path,
                summary_rows,
                group_by=_parse_group_by(namespace.group_by),
            )
            write_summary_parquet(
                report_parquet_path,
                summary_rows,
                group_by=_parse_group_by(namespace.group_by),
            )
            write_summary_html(
                report_html_path,
                summary_rows,
                group_by=_parse_group_by(namespace.group_by),
                metadata={
                    "run_root": str(analyze_root),
                    "group_by": _parse_group_by(namespace.group_by),
                    "task_feature_version": TASK_FEATURE_VERSION,
                    "attempt_rows": len(attempt_rows),
                    "instance_result_rows": len(instance_results),
                    "router_training_rows": len(router_rows),
                },
                report=analysis_report,
            )
            write_router_training_rows(router_train_path, router_rows)

            manifest.mark_step("inspect", ManifestStepStatus.SUCCEEDED)
            manifest.mark_step(
                "execute",
                ManifestStepStatus.SUCCEEDED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.artifact_paths["analyze_summary"] = str(summary_path)
            manifest.artifact_paths["analyze_report_csv"] = str(report_csv_path)
            manifest.artifact_paths["analyze_report_parquet"] = str(report_parquet_path)
            manifest.artifact_paths["analyze_report_html"] = str(report_html_path)
            manifest.artifact_paths["router_train"] = str(router_train_path)
            manifest.artifact_paths["analyze_attempts"] = str(attempts_path)
            manifest.artifact_paths["analyze_instance_results"] = str(
                instance_results_path
            )
            manifest.artifact_paths["analyze_run_root"] = str(analyze_root)
            manifest.metadata["analyze"] = {
                "group_by": _parse_group_by(namespace.group_by),
                "expensive_cost_threshold": namespace.expensive_cost_threshold,
                "task_feature_version": TASK_FEATURE_VERSION,
                "attempt_rows": len(attempt_rows),
                "instance_result_rows": len(instance_results),
                "summary_rows": len(summary_rows),
                "router_training_rows": len(router_rows),
                "source": str(analyze_root),
            }
            manifest.mark_step(
                "finish",
                ManifestStepStatus.SUCCEEDED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.finish(status="succeeded", metadata={"reason": "analyze_complete"})
            manifest.write(manifest_path)
            log_event(
                {
                    "event": "command.finish",
                    "command": command,
                    "status": manifest.status,
                    "timestamp": manifest.ended_at,
                },
                events_path,
            )
            return 0
        except Exception as exc:
            manifest.mark_step(
                "inspect",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.mark_step("execute", ManifestStepStatus.SKIPPED)
            manifest.finish(
                status="failed",
                metadata={"reason": "analyze_failed", "error": str(exc)},
            )
            manifest.mark_step(
                "finish",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.write(manifest_path)
            log_event(
                {
                    "event": "command.finish",
                    "command": command,
                    "status": manifest.status,
                    "timestamp": manifest.ended_at,
                    "error": str(exc),
                },
                events_path,
            )
            return 1

    if command == "train-router":
        if not namespace.path:
            manifest.mark_step(
                "inspect",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.mark_step("execute", ManifestStepStatus.SKIPPED)
            manifest.finish(
                status="failed", metadata={"reason": "missing_router_training_input"}
            )
            manifest.mark_step(
                "finish",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.write(manifest_path)
            log_event(
                {
                    "event": "command.finish",
                    "command": command,
                    "status": manifest.status,
                    "timestamp": manifest.ended_at,
                    "error": "missing router training path",
                },
                events_path,
            )
            return 1

        source = Path(namespace.path).resolve()
        router_train_path: Path | None
        if source.is_file():
            router_train_path = source
        else:
            candidates = [
                source / "router_train.parquet",
                source / "analyze" / "router_train.parquet",
            ]
            router_train_path = next(
                (candidate for candidate in candidates if candidate.exists()), None
            )

        if router_train_path is None or not router_train_path.exists():
            manifest.mark_step(
                "inspect",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.mark_step("execute", ManifestStepStatus.SKIPPED)
            manifest.finish(
                status="failed",
                metadata={
                    "reason": "router_training_input_not_found",
                    "path": str(source),
                },
            )
            manifest.mark_step(
                "finish",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.write(manifest_path)
            log_event(
                {
                    "event": "command.finish",
                    "command": command,
                    "status": manifest.status,
                    "timestamp": manifest.ended_at,
                    "error": f"router training artifact not found: {source}",
                },
                events_path,
            )
            return 1

        report_out_root = (
            Path(namespace.out).resolve() if namespace.out else router_train_path.parent
        )

        manifest.mark_step(
            "inspect", ManifestStepStatus.RUNNING, started_at=command_timestamp
        )
        try:
            report = run_router_training(
                router_train_path,
                out_root=report_out_root,
                seed=int(getattr(namespace, "seed", 0)),
                train_fraction=float(getattr(namespace, "train_fraction", 0.8)),
                validation_fraction=float(
                    getattr(namespace, "validation_fraction", 0.1)
                ),
                max_depth=int(getattr(namespace, "max_depth", 3)),
            )
            report_path = Path(report["router_report_path"])
            manifest.mark_step("inspect", ManifestStepStatus.SUCCEEDED)
            manifest.mark_step(
                "execute",
                ManifestStepStatus.SUCCEEDED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.artifact_paths["router_train"] = str(router_train_path)
            manifest.artifact_paths["router_model"] = report["router_model_path"]
            manifest.artifact_paths["router_report"] = str(report_path)
            manifest.metadata["train_router"] = {
                "router_train_path": str(router_train_path),
                "router_model_path": report["router_model_path"],
                "router_report_path": str(report_path),
                "instance_count": report["instance_count"],
                "seed": getattr(namespace, "seed", 0),
                "train_fraction": getattr(namespace, "train_fraction", 0.8),
                "validation_fraction": getattr(namespace, "validation_fraction", 0.1),
                "max_depth": getattr(namespace, "max_depth", 3),
                "report": report["report"],
            }
            manifest.mark_step(
                "finish",
                ManifestStepStatus.SUCCEEDED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.finish(
                status="succeeded", metadata={"reason": "train_router_complete"}
            )
            manifest.write(manifest_path)
            log_event(
                {
                    "event": "command.finish",
                    "command": command,
                    "status": manifest.status,
                    "timestamp": manifest.ended_at,
                },
                events_path,
            )
            return 0
        except Exception as exc:
            manifest.mark_step(
                "inspect",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.mark_step("execute", ManifestStepStatus.SKIPPED)
            manifest.finish(
                status="failed",
                metadata={"reason": "train_router_failed", "error": str(exc)},
            )
            manifest.mark_step(
                "finish",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.write(manifest_path)
            log_event(
                {
                    "event": "command.finish",
                    "command": command,
                    "status": manifest.status,
                    "timestamp": manifest.ended_at,
                    "error": str(exc),
                },
                events_path,
            )
            return 1

    if command == "run":
        if not namespace.path:
            manifest.mark_step(
                "inspect",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.mark_step("execute", ManifestStepStatus.SKIPPED)
            manifest.finish(status="failed", metadata={"reason": "missing_run_matrix"})
            manifest.mark_step(
                "finish",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.write(manifest_path)
            log_event(
                {
                    "event": "command.finish",
                    "command": command,
                    "status": manifest.status,
                    "timestamp": manifest.ended_at,
                    "error": "missing matrix path",
                },
                events_path,
            )
            return 1

        manifest.mark_step(
            "inspect", ManifestStepStatus.RUNNING, started_at=command_timestamp
        )
        try:
            matrix = load_matrix_config(
                namespace.path,
                run_id=namespace.run_id or None,
                dataset_path=namespace.dataset or None,
            )
            jobs = plan_jobs(matrix)
            run_root = out_root / matrix.run_id
            matrix_out = run_root / "matrix.yaml"
            jobs_out = run_root / "jobs.jsonl"
            run_manifest_out = run_root / "manifest.json"
            run_jobs_out = run_root / "run_jobs.jsonl"
            attempts_out = run_root / "attempts.jsonl"
            attempts_parquet_out = run_root / "attempts.parquet"
            run_summary_out = run_root / "run_summary.json"
            write_matrix_copy(matrix_out, Path(matrix.matrix_path))
            write_jobs(jobs, jobs_out)
            run_manifest = RunManifest.from_matrix(
                matrix=matrix,
                jobs=jobs,
                run_root=run_root,
                matrix_out=matrix_out,
                jobs_out=jobs_out,
            )
            write_run_manifest(run_manifest, run_manifest_out)
            dataset_rows = _load_dataset_rows(Path(matrix.dataset.path))
            adapters = build_solver_adapters(
                solvers=matrix.solvers,
                providers=matrix.providers,
            )
            scheduler = SolverScheduler(
                config=SolverSchedulerConfig(
                    persist_jobs_to=run_jobs_out,
                    persist_attempts_to=attempts_out,
                    persist_attempts_parquet=attempts_parquet_out,
                )
            )
            summary = scheduler.run(
                jobs=jobs,
                adapters=adapters,
                dataset_rows=dataset_rows,
            )
            run_summary = {
                "completed_at": summary.completed_at,
                "job_count": len(summary.jobs),
                "solved": sum(
                    1
                    for item in summary.jobs
                    if item.final_status == SolverAttemptState.SUCCEEDED
                ),
                "jobs": [asdict(item) for item in summary.jobs],
            }
            run_summary_out.write_text(
                json.dumps(run_summary, sort_keys=True) + "\n", encoding="utf-8"
            )

            manifest.mark_step(
                "inspect", ManifestStepStatus.SUCCEEDED, ended_at=command_timestamp
            )
            manifest.mark_step(
                "execute",
                ManifestStepStatus.SUCCEEDED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.artifact_paths["run_root"] = str(run_root)
            manifest.artifact_paths["matrix"] = str(matrix_out)
            manifest.artifact_paths["jobs"] = str(jobs_out)
            manifest.artifact_paths["run_manifest"] = str(run_manifest_out)
            manifest.artifact_paths["run_jobs"] = str(run_jobs_out)
            manifest.artifact_paths["attempts"] = str(attempts_out)
            manifest.artifact_paths["attempts_parquet"] = str(attempts_parquet_out)
            manifest.artifact_paths["run_summary"] = str(run_summary_out)
            manifest.finish(
                status="succeeded",
                metadata={
                    "reason": "run_complete",
                    "run_id": matrix.run_id,
                    "job_count": len(jobs),
                },
            )
            manifest.write(manifest_path)
            log_event(
                {
                    "event": "command.finish",
                    "command": command,
                    "status": manifest.status,
                    "timestamp": manifest.ended_at,
                    "run_id": matrix.run_id,
                    "jobs": len(jobs),
                    "solved": run_summary["solved"],
                },
                events_path,
            )
            return 0
        except MatrixConfigurationError as exc:
            manifest.mark_step(
                "inspect",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.mark_step("execute", ManifestStepStatus.SKIPPED)
            manifest.finish(
                status="failed",
                metadata={"reason": "run_matrix_invalid", "error": str(exc)},
            )
            manifest.mark_step(
                "finish",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.write(manifest_path)
            log_event(
                {
                    "event": "command.finish",
                    "command": command,
                    "status": manifest.status,
                    "timestamp": manifest.ended_at,
                    "error": str(exc),
                },
                events_path,
            )
            return 1
        except (
            SolverAdapterError,
            SolverSchedulerError,
            RuntimeError,
            ValueError,
        ) as exc:
            manifest.mark_step(
                "execute",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.finish(
                status="failed",
                metadata={
                    "reason": "run_execution_failed",
                    "error": str(exc),
                },
            )
            manifest.mark_step(
                "finish",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.write(manifest_path)
            log_event(
                {
                    "event": "command.finish",
                    "command": command,
                    "status": manifest.status,
                    "timestamp": manifest.ended_at,
                    "error": str(exc),
                },
                events_path,
            )
            return 1
        except Exception as exc:  # pragma: no cover - defensive
            manifest.mark_step(
                "inspect",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.mark_step("execute", ManifestStepStatus.SKIPPED)
            manifest.finish(
                status="failed",
                metadata={"reason": "run_unexpected_failure", "error": str(exc)},
            )
            manifest.mark_step(
                "finish",
                ManifestStepStatus.FAILED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                + "Z",
            )
            manifest.write(manifest_path)
            log_event(
                {
                    "event": "command.finish",
                    "command": command,
                    "status": manifest.status,
                    "timestamp": manifest.ended_at,
                    "error": str(exc),
                },
                events_path,
            )
            return 1

    # Scaffold implementations are intentionally explicit no-ops for unimplemented commands.
    manifest.mark_step(
        "execute", ManifestStepStatus.SUCCEEDED, started_at=command_timestamp
    )
    manifest.mark_step(
        "finish",
        ManifestStepStatus.SUCCEEDED,
        ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
    )
    manifest.finish(
        status="succeeded", metadata={"reason": "scaffolded", "path": namespace.path}
    )
    manifest.write(manifest_path)
    log_event(
        {
            "event": "command.finish",
            "command": command,
            "status": manifest.status,
            "timestamp": manifest.ended_at,
        },
        events_path,
    )

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "command", None) is None:
        parser.print_help()
        return 2

    return _run_command(args)


if __name__ == "__main__":
    sys.exit(main())
