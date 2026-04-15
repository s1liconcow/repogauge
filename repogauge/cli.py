"""CLI entrypoint for RepoGauge commands.

The current implementation intentionally focuses on scaffold shape. Commands resolve
flags and exit codes in a stable way so downstream modules can be built without
changing public invocation semantics.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from repogauge.review import run_review
from repogauge.export import run_materialization

OUT_DIR_HELP = "Path where artifacts are written (created when needed)."
CONFIG_HELP = "Configuration file path. Values are merged over project defaults."
RESUME_HELP = "Resume into an existing artifact directory."
DRY_RUN_HELP = "Validate inputs and render intended commands without mutating artifacts."
LLM_MODE_HELP = "Control model usage: off/local_only/allow_remote."
VERBOSE_HELP = "Enable verbose output."

from .manifest import Manifest, ManifestStepStatus
from .logging_utils import log_event
from .mining.inspect import inspect_repository
from .mining.scan import scan_repository

COMMIT_RANGE_HELP = "Commit range to scan (for example HEAD~50..HEAD)."
MAX_COMMITS_HELP = "Maximum number of commits to inspect."
EXCLUDE_MERGES_HELP = "Skip merge commits during scanning."


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
        cmd.add_argument("--llm-mode", help=LLM_MODE_HELP, choices=["off", "local_only", "allow_remote"])
        cmd.add_argument("--verbose", action="store_true", help=VERBOSE_HELP)
        if name == "review":
            cmd.add_argument("--decisions", help="Optional JSON/JSONL file with scripted review decisions.")
            cmd.add_argument("--triage-hints", help="Optional structured JSON/JSONL file with advisory triage hints.")
            cmd.add_argument("--llm-model", help="Model identifier for advisory triage outputs.")
            cmd.add_argument("--llm-provider", help="LLM provider for advisory triage.")
        if name == "mine":
            cmd.add_argument("--commit-range", help=COMMIT_RANGE_HELP)
            cmd.add_argument("--max-commits", default=100, type=int, help=MAX_COMMITS_HELP)
            cmd.add_argument("--exclude-merges", action="store_true", help=EXCLUDE_MERGES_HELP)

    return parser


def _inputs_hash(command: str, namespace: argparse.Namespace) -> str:
    payload = {
        "command": command,
        "path": namespace.path or "",
        "config": namespace.config or "",
        "commit_range": namespace.commit_range if command == "mine" else "",
        "max_commits": namespace.max_commits if command == "mine" else 0,
        "exclude_merges": namespace.exclude_merges if command == "mine" else False,
        "dry_run": bool(namespace.dry_run),
        "llm_mode": namespace.llm_mode or "",
        "llm_model": getattr(namespace, "llm_model", ""),
        "llm_provider": getattr(namespace, "llm_provider", ""),
        "triage_hints": getattr(namespace, "triage_hints", ""),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


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


def _run_command(namespace: argparse.Namespace) -> int:
    command = namespace.command
    out_root = Path(namespace.out or Path(".") / ".repogauge" / command).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    manifest_path = out_root / "manifest.json"
    events_path = out_root / "events.jsonl"
    command_timestamp = datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"

    if namespace.resume and manifest_path.exists():
        try:
            manifest = Manifest.load(manifest_path)
            if manifest.inputs_hash == _inputs_hash(command, namespace) and manifest.status == "succeeded":
                manifest.mark_step("resume", ManifestStepStatus.SKIPPED, started_at=command_timestamp)
                manifest.finish(status="succeeded", metadata={"resume": True, "reason": "reused_existing_manifest"})
                log_event({"event": "command.resume", "command": command, "status": manifest.status}, events_path)
                manifest.write(manifest_path)
                return 0
            manifest = Manifest.start(command)
        except Exception:
            manifest = Manifest.start(command)
    else:
        manifest = Manifest.start(command)

    manifest.inputs_hash = _inputs_hash(command, namespace)
    manifest.artifact_paths.update({
        "manifest": str(manifest_path),
        "events": str(events_path),
    })
    manifest.host_info = {
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
    }
    manifest.mark_step("bootstrap", ManifestStepStatus.RUNNING, started_at=command_timestamp)
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
                "timestamp": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
            },
            events_path,
        )
        return 0

    if command == "review":
        if not namespace.path:
            manifest.mark_step("inspect", ManifestStepStatus.FAILED, ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z")
            manifest.mark_step("execute", ManifestStepStatus.SKIPPED)
            manifest.finish(status="failed", metadata={"reason": "missing_review_input"})
            manifest.mark_step("finish", ManifestStepStatus.FAILED, ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z")
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

        out_root = Path(namespace.out or str(source.parent)).resolve() if source.is_file() else source.resolve()
        manifest.mark_step("inspect", ManifestStepStatus.RUNNING, started_at=command_timestamp)
        try:
            review_summary = run_review(
                candidates_path=candidates_path,
                out_root=out_root,
                decisions_path=Path(namespace.decisions).resolve() if namespace.decisions else None,
                llm_mode=namespace.llm_mode,
                triage_hints_path=Path(namespace.triage_hints).resolve() if namespace.triage_hints else None,
                llm_model_name=namespace.llm_model if hasattr(namespace, "llm_model") else None,
                llm_provider=namespace.llm_provider if hasattr(namespace, "llm_provider") else None,
            )
            manifest.mark_step("inspect", ManifestStepStatus.SUCCEEDED)
            manifest.mark_step(
                "execute",
                ManifestStepStatus.SUCCEEDED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
            )
            manifest.artifact_paths["reviewed"] = review_summary["reviewed_path"]
            manifest.artifact_paths["review_markdown"] = review_summary["markdown_path"]
            manifest.artifact_paths["review_html"] = review_summary["html_path"]
            manifest.metadata["review"] = review_summary
        except Exception as exc:
            manifest.mark_step("inspect", ManifestStepStatus.FAILED, ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z")
            manifest.mark_step("execute", ManifestStepStatus.SKIPPED)
            manifest.finish(status="failed", metadata={"reason": "review_failed", "error": str(exc)})
            manifest.mark_step("finish", ManifestStepStatus.FAILED, ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z")
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

        manifest.mark_step("finish", ManifestStepStatus.SUCCEEDED, ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z")
        manifest.finish(status="succeeded", metadata={"reason": "review_complete", "path": namespace.path})
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
        manifest.mark_step("inspect", ManifestStepStatus.RUNNING, started_at=command_timestamp)
        try:
            profile = inspect_repository(namespace.path or ".")
        except Exception as exc:  # pragma: no cover - defensive for unsupported paths
            manifest.mark_step("inspect", ManifestStepStatus.FAILED, ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z")
            manifest.mark_step("execute", ManifestStepStatus.SKIPPED)
            manifest.finish(status="failed", metadata={"reason": "inspect_failed", "error": str(exc)})
            manifest.mark_step("finish", ManifestStepStatus.FAILED, ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z")
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
        repo_profile_path.write_text(json.dumps(profile, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        manifest.artifact_paths["repo_profile"] = str(repo_profile_path)
        manifest.mark_step("inspect", ManifestStepStatus.SUCCEEDED)

        manifest.mark_step("scan", ManifestStepStatus.RUNNING, started_at=command_timestamp)
        try:
            scan_rows = scan_repository(
                namespace.path or ".",
                repo_name=profile["repo_name"],
                max_count=int(namespace.max_commits),
                commit_range=namespace.commit_range,
                include_merges=not namespace.exclude_merges,
            )
        except Exception as exc:
            manifest.mark_step("scan", ManifestStepStatus.FAILED, ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z")
            manifest.mark_step("execute", ManifestStepStatus.SKIPPED)
            manifest.finish(status="failed", metadata={"reason": "scan_failed", "error": str(exc)})
            manifest.mark_step("finish", ManifestStepStatus.FAILED, ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z")
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
        scan_path.write_text("".join(json.dumps(row.to_dict(), sort_keys=True) + "\n" for row in scan_rows), encoding="utf-8")
        manifest.artifact_paths["scan"] = str(scan_path)

        candidates_path = out_root / "candidates.jsonl"
        candidates_path.write_text("".join(json.dumps(row.to_dict(), sort_keys=True) + "\n" for row in scan_rows), encoding="utf-8")
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
        manifest.mark_step("finish", ManifestStepStatus.SUCCEEDED, ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z")
        manifest.finish(status="succeeded", metadata={"reason": "scan_complete", "path": namespace.path})
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
            manifest.mark_step("inspect", ManifestStepStatus.FAILED, ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z")
            manifest.mark_step("execute", ManifestStepStatus.SKIPPED)
            manifest.finish(status="failed", metadata={"reason": "missing_export_input"})
            manifest.mark_step("finish", ManifestStepStatus.FAILED, ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z")
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
            manifest.mark_step("inspect", ManifestStepStatus.FAILED, ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z")
            manifest.mark_step("execute", ManifestStepStatus.SKIPPED)
            manifest.finish(status="failed", metadata={"reason": "missing_reviewed_input", "path": str(reviewed_path)})
            manifest.mark_step("finish", ManifestStepStatus.FAILED, ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z")
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

        manifest.mark_step("inspect", ManifestStepStatus.RUNNING, started_at=command_timestamp)
        try:
            repo_root = _resolve_repo_root(source)
            export_summary = run_materialization(
                reviewed_path=reviewed_path,
                out_root=out_root,
                repo_root=repo_root,
            )
            manifest.mark_step("inspect", ManifestStepStatus.SUCCEEDED)
            manifest.artifact_paths["materialized"] = export_summary["materialized_path"]
            manifest.artifact_paths["materialization_rejections"] = export_summary["rejected_path"]
            manifest.metadata["export"] = {
                "ready_count": export_summary["ready_count"],
                "rejected_count": export_summary["rejected_count"],
                "total_count": export_summary["total_count"],
            }
            manifest.mark_step(
                "execute",
                ManifestStepStatus.SUCCEEDED,
                ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
            )
            manifest.mark_step("finish", ManifestStepStatus.SUCCEEDED, ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z")
            manifest.finish(status="succeeded", metadata={"reason": "export_complete", "path": namespace.path})
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
            manifest.mark_step("inspect", ManifestStepStatus.FAILED, ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z")
            manifest.mark_step("execute", ManifestStepStatus.SKIPPED)
            manifest.finish(status="failed", metadata={"reason": "export_failed", "error": str(exc)})
            manifest.mark_step("finish", ManifestStepStatus.FAILED, ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z")
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
    manifest.mark_step("execute", ManifestStepStatus.SUCCEEDED, started_at=command_timestamp)
    manifest.mark_step("finish", ManifestStepStatus.SUCCEEDED, ended_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z")
    manifest.finish(status="succeeded", metadata={"reason": "scaffolded", "path": namespace.path})
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
