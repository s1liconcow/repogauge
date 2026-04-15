"""Mining helpers for RepoGauge.

Deterministic repository inspection and related discovery utilities live here.
"""

from .inspect import inspect_repository
from .file_roles import FileRole, FileRoleClassification, classify_file, classify_files

__all__ = [
    "inspect_repository",
    "FileRole",
    "FileRoleClassification",
    "classify_file",
    "classify_files",
]
