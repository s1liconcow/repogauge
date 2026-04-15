"""Mining helpers for RepoGauge.

Deterministic repository inspection and related discovery utilities live here.
"""

from .inspect import inspect_repository
from .file_roles import FileRole, FileRoleClassification, classify_file, classify_files
from .scan import scan_repository
from .score import ScoredCommit, score_scan_commit

__all__ = [
    "inspect_repository",
    "scan_repository",
    "FileRole",
    "FileRoleClassification",
    "classify_file",
    "classify_files",
    "ScoredCommit",
    "score_scan_commit",
]
