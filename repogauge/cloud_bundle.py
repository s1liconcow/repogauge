"""Source-conscious cloud bundle packaging for local RepoGauge artifacts."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import mimetypes
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from repogauge import __version__

CLOUD_ARTIFACT_BUNDLE_SCHEMA_VERSION = "2026-04-24.1"
DETERMINISTIC_CREATED_AT = "1970-01-01T00:00:00Z"
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

REQUIRED_REPORT = "analysis_report.json"
REQUIRED_RESULT_ARTIFACTS = ("attempts.jsonl", "instance_results.jsonl")
DEFAULT_INCLUDED_ARTIFACTS = (
    REQUIRED_REPORT,
    "summary.json",
    "attempts.jsonl",
    "instance_results.jsonl",
    "router_train.parquet",
    "report.html",
)
SOURCE_ADJACENT_ARTIFACTS = frozenset(
    {"attempts.jsonl", "instance_results.jsonl", "router_train.parquet", "report.html"}
)
TEXT_EXTENSIONS = {".json", ".jsonl", ".txt", ".md", ".html", ".csv", ".yaml", ".yml"}
ABSOLUTE_PATH_PATTERN = re.compile(r"(?<![A-Za-z0-9_.-])(?:/[A-Za-z0-9_.-]+){2,}")
SOURCE_SNIPPET_PATTERN = re.compile(
    r"\b(?:def|class|function|import|package|public|private|const|let|var)\s+"
)


@dataclass(frozen=True)
class CloudBundleArtifact:
    path: str
    sha256: str
    byte_size: int
    privacy_class: str
    content_type: str

    def to_manifest(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "byte_size": self.byte_size,
            "class": self.privacy_class,
            "content_type": self.content_type,
        }


@dataclass(frozen=True)
class CloudBundleResult:
    bundle_path: Path
    manifest_path: Path
    bundle_id: str
    bundle_sha256: str
    artifacts: tuple[CloudBundleArtifact, ...]
    warnings: tuple[str, ...]


def package_cloud_bundle(
    source_dir: Path,
    *,
    bundle_path: Path,
    manifest_path: Path | None = None,
    repo_display_name: str | None = None,
) -> CloudBundleResult:
    """Package a RepoGauge output directory into a deterministic cloud bundle."""

    source_root = source_dir.expanduser().resolve()
    if not source_root.is_dir():
        raise ValueError(f"cloud bundle source is not a directory: {source_dir}")

    artifact_paths = _discover_artifact_paths(source_root)
    _validate_minimum_artifact_set(artifact_paths)
    artifacts = tuple(_build_artifact(source_root, path) for path in artifact_paths)
    warnings = tuple(_collect_warnings(source_root, artifact_paths))

    bundle_id = _bundle_id(artifacts)
    manifest = _build_manifest(
        bundle_id=bundle_id,
        artifacts=artifacts,
        repo_display_name=repo_display_name or source_root.name,
    )

    resolved_manifest_path = (
        manifest_path.expanduser().resolve()
        if manifest_path is not None
        else bundle_path.expanduser().resolve().with_suffix(".manifest.json")
    )
    resolved_bundle_path = bundle_path.expanduser().resolve()
    resolved_bundle_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_manifest_path.parent.mkdir(parents=True, exist_ok=True)

    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    resolved_manifest_path.write_bytes(manifest_bytes)
    _write_zip_bundle(
        resolved_bundle_path,
        manifest_bytes=manifest_bytes,
        source_root=source_root,
        artifact_paths=artifact_paths,
    )

    bundle_sha256 = _sha256_bytes(resolved_bundle_path.read_bytes())
    return CloudBundleResult(
        bundle_path=resolved_bundle_path,
        manifest_path=resolved_manifest_path,
        bundle_id=bundle_id,
        bundle_sha256=bundle_sha256,
        artifacts=artifacts,
        warnings=warnings,
    )


def _discover_artifact_paths(source_root: Path) -> tuple[str, ...]:
    discovered = [
        relative_path
        for relative_path in DEFAULT_INCLUDED_ARTIFACTS
        if (source_root / relative_path).is_file()
    ]
    return tuple(sorted(discovered))


def _validate_minimum_artifact_set(artifact_paths: tuple[str, ...]) -> None:
    available = set(artifact_paths)
    if REQUIRED_REPORT not in available:
        raise ValueError("cloud bundle requires analysis_report.json")
    if not any(path in available for path in REQUIRED_RESULT_ARTIFACTS):
        required = " or ".join(REQUIRED_RESULT_ARTIFACTS)
        raise ValueError(f"cloud bundle requires at least one of {required}")


def _build_artifact(source_root: Path, relative_path: str) -> CloudBundleArtifact:
    artifact_path = source_root / relative_path
    body = artifact_path.read_bytes()
    privacy_class = (
        "B_SOURCE_ADJACENT"
        if relative_path in SOURCE_ADJACENT_ARTIFACTS
        else "A_SOURCE_FREE_METRIC"
    )
    return CloudBundleArtifact(
        path=relative_path,
        sha256=_sha256_bytes(body),
        byte_size=len(body),
        privacy_class=privacy_class,
        content_type=_content_type(relative_path),
    )


def _build_manifest(
    *,
    bundle_id: str,
    artifacts: tuple[CloudBundleArtifact, ...],
    repo_display_name: str,
) -> dict[str, Any]:
    allowed_source_adjacent = sorted(
        artifact.path
        for artifact in artifacts
        if artifact.privacy_class == "B_SOURCE_ADJACENT"
    )
    return {
        "schema_version": CLOUD_ARTIFACT_BUNDLE_SCHEMA_VERSION,
        "bundle_id": bundle_id,
        "created_at": DETERMINISTIC_CREATED_AT,
        "repogauge_version": __version__,
        "repo_fingerprint": {
            "algorithm": "sha256",
            "value": _repo_fingerprint(artifacts),
        },
        "repo_display_name": repo_display_name,
        "source_policy": {
            "contains_raw_source": False,
            "contains_source_adjacent_artifacts": bool(allowed_source_adjacent),
            "allowed_source_adjacent_artifacts": allowed_source_adjacent,
        },
        "artifacts": [artifact.to_manifest() for artifact in artifacts],
    }


def _write_zip_bundle(
    bundle_path: Path,
    *,
    manifest_bytes: bytes,
    source_root: Path,
    artifact_paths: tuple[str, ...],
) -> None:
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        _writestr_deterministic(archive, "manifest.json", manifest_bytes)
        for relative_path in artifact_paths:
            _writestr_deterministic(
                archive,
                relative_path,
                (source_root / relative_path).read_bytes(),
            )


def _writestr_deterministic(
    archive: zipfile.ZipFile, archive_path: str, body: bytes
) -> None:
    normalized = str(PurePosixPath(archive_path))
    info = zipfile.ZipInfo(normalized, ZIP_EPOCH)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    archive.writestr(info, body)


def _collect_warnings(source_root: Path, artifact_paths: tuple[str, ...]) -> list[str]:
    warnings: list[str] = []
    for relative_path in artifact_paths:
        path = source_root / relative_path
        if path.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if ABSOLUTE_PATH_PATTERN.search(text):
            warnings.append(f"{relative_path}: contains absolute-path-like text")
        if SOURCE_SNIPPET_PATTERN.search(text):
            warnings.append(f"{relative_path}: contains source-snippet-like text")
    return warnings


def _content_type(relative_path: str) -> str:
    if relative_path.endswith(".jsonl"):
        return "application/x-ndjson"
    guessed, _ = mimetypes.guess_type(relative_path)
    return guessed or "application/octet-stream"


def _bundle_id(artifacts: tuple[CloudBundleArtifact, ...]) -> str:
    digest = _artifact_digest(artifacts)
    return f"art_{digest[:24]}"


def _repo_fingerprint(artifacts: tuple[CloudBundleArtifact, ...]) -> str:
    return _artifact_digest(artifacts)


def _artifact_digest(artifacts: tuple[CloudBundleArtifact, ...]) -> str:
    payload = [
        {"path": artifact.path, "sha256": artifact.sha256, "byte_size": artifact.byte_size}
        for artifact in artifacts
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _sha256_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()
