#!/usr/bin/env python3
"""Repair dependencies for beads imported from a markdown plan.

The script reads a `br create -f` source markdown file, maps the original issue
titles onto the current `.beads/issues.jsonl` entries, and then adds any missing
dependency edges. It can also prune unexpected dependency edges inside the
imported issue set.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import getpass
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class ImportedIssue:
    old_id: str
    title: str
    dependencies: tuple[str, ...]


@dataclass(frozen=True)
class RepairPlan:
    imported_ids: frozenset[str]
    expected_edges: frozenset[tuple[str, str]]
    missing_edges: tuple[tuple[str, str], ...]
    extra_edges: tuple[tuple[str, str], ...]


def parse_import_markdown(path: Path) -> dict[str, ImportedIssue]:
    lines = path.read_text(encoding="utf-8").splitlines()
    imported: dict[str, ImportedIssue] = {}
    i = 0
    while i < len(lines):
        if not lines[i].startswith("## "):
            i += 1
            continue

        title = lines[i][3:].strip()
        i += 1
        old_id: str | None = None
        dependencies: list[str] = []

        while i < len(lines) and not lines[i].startswith("## "):
            line = lines[i]
            if line.startswith("### ID "):
                old_id = line[len("### ID ") :].strip()
            elif line.startswith("### Dependencies"):
                i += 1
                while i < len(lines) and not lines[i].strip():
                    i += 1
                while i < len(lines) and lines[i].startswith("- "):
                    dependencies.append(lines[i][2:].strip())
                    i += 1
                continue
            i += 1

        if old_id is None:
            raise ValueError(f"Missing ID for markdown section {title!r}")
        imported[old_id] = ImportedIssue(
            old_id=old_id,
            title=title,
            dependencies=tuple(dependencies),
        )

    return imported


def load_issue_records(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def map_old_ids_to_new_ids(
    imported: dict[str, ImportedIssue], issue_records: Iterable[dict]
) -> dict[str, str]:
    title_to_ids: dict[str, list[str]] = {}
    for record in issue_records:
        title_to_ids.setdefault(record["title"], []).append(record["id"])

    mapping: dict[str, str] = {}
    errors: list[str] = []
    for old_id, imported_issue in imported.items():
        matches = title_to_ids.get(imported_issue.title, [])
        if len(matches) == 1:
            mapping[old_id] = matches[0]
            continue
        if not matches:
            errors.append(
                f"{old_id}: no bead with title {imported_issue.title!r} exists in .beads/issues.jsonl"
            )
        else:
            errors.append(
                f"{old_id}: title {imported_issue.title!r} matched multiple bead IDs: {', '.join(matches)}"
            )

    if errors:
        raise ValueError(
            "Unable to build a unique title mapping:\n- " + "\n- ".join(errors)
        )
    return mapping


def collect_current_edges(issue_records: Iterable[dict]) -> set[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    for record in issue_records:
        for dependency in record.get("dependencies", []):
            edges.add((dependency["issue_id"], dependency["depends_on_id"]))
    return edges


def build_repair_plan(
    imported: dict[str, ImportedIssue], issue_records: list[dict]
) -> RepairPlan:
    old_to_new = map_old_ids_to_new_ids(imported, issue_records)
    imported_ids = frozenset(old_to_new.values())

    expected_edges: set[tuple[str, str]] = set()
    for old_id, imported_issue in imported.items():
        issue_id = old_to_new[old_id]
        for old_dep in imported_issue.dependencies:
            if old_dep not in old_to_new:
                raise ValueError(
                    f"{old_id} depends on {old_dep}, but {old_dep} was not present in the import markdown"
                )
            expected_edges.add((issue_id, old_to_new[old_dep]))

    current_edges = collect_current_edges(issue_records)
    current_import_edges = {
        edge
        for edge in current_edges
        if edge[0] in imported_ids and edge[1] in imported_ids
    }

    return RepairPlan(
        imported_ids=imported_ids,
        expected_edges=frozenset(expected_edges),
        missing_edges=tuple(sorted(expected_edges - current_import_edges)),
        extra_edges=tuple(sorted(current_import_edges - expected_edges)),
    )


def make_dependency_record(
    issue_id: str, depends_on_id: str, *, actor: str
) -> dict[str, str]:
    return {
        "issue_id": issue_id,
        "depends_on_id": depends_on_id,
        "type": "blocks",
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "created_by": actor,
        "metadata": "{}",
        "thread_id": "",
    }


def apply_repair_plan_to_records(
    issue_records: list[dict], plan: RepairPlan, *, prune_extra: bool, actor: str
) -> list[dict]:
    by_id = {record["id"]: record for record in issue_records}

    for issue_id, depends_on_id in plan.missing_edges:
        record = by_id[issue_id]
        dependencies = record.setdefault("dependencies", [])
        if not any(
            dep["issue_id"] == issue_id and dep["depends_on_id"] == depends_on_id
            for dep in dependencies
        ):
            dependencies.append(
                make_dependency_record(issue_id, depends_on_id, actor=actor)
            )
            dependencies.sort(
                key=lambda dep: (dep["depends_on_id"], dep.get("created_at", ""))
            )

    if prune_extra:
        for issue_id, depends_on_id in plan.extra_edges:
            record = by_id[issue_id]
            dependencies = [
                dep
                for dep in record.get("dependencies", [])
                if not (
                    dep["issue_id"] == issue_id
                    and dep["depends_on_id"] == depends_on_id
                )
            ]
            if dependencies:
                record["dependencies"] = dependencies
            else:
                record.pop("dependencies", None)

    return issue_records


def write_issue_records(path: Path, issue_records: list[dict]) -> None:
    path.write_text(
        "".join(
            json.dumps(record, separators=(",", ":")) + "\n" for record in issue_records
        ),
        encoding="utf-8",
    )


def print_plan(plan: RepairPlan, *, prune_extra: bool) -> None:
    print(f"imported issues: {len(plan.imported_ids)}")
    print(f"expected dependency edges: {len(plan.expected_edges)}")
    print(f"missing dependency edges: {len(plan.missing_edges)}")
    print(f"unexpected dependency edges: {len(plan.extra_edges)}")

    if plan.missing_edges:
        print("\nMissing edges to add:")
        for issue_id, depends_on_id in plan.missing_edges:
            print(f"  + {issue_id} <- {depends_on_id}")

    if plan.extra_edges:
        heading = (
            "Unexpected edges to remove:"
            if prune_extra
            else "Unexpected edges detected:"
        )
        print(f"\n{heading}")
        for issue_id, depends_on_id in plan.extra_edges:
            print(f"  - {issue_id} <- {depends_on_id}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "import_markdown",
        nargs="?",
        default="docs/import_multilang_support.md",
        help="Source markdown that was imported with `br create -f`.",
    )
    parser.add_argument(
        "--issues-jsonl",
        default=".beads/issues.jsonl",
        help="Current bead JSONL export.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Rewrite the JSONL export with missing dependency edges added.",
    )
    parser.add_argument(
        "--prune-extra",
        action="store_true",
        help="Remove unexpected dependency edges inside the imported issue set.",
    )
    parser.add_argument(
        "--actor",
        default=getpass.getuser(),
        help="Actor name to record on newly added dependency edges.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    imported = parse_import_markdown(Path(args.import_markdown))
    issue_records = load_issue_records(Path(args.issues_jsonl))
    plan = build_repair_plan(imported, issue_records)
    print_plan(plan, prune_extra=args.prune_extra)

    if args.apply:
        updated_records = apply_repair_plan_to_records(
            issue_records,
            plan,
            prune_extra=args.prune_extra,
            actor=args.actor,
        )
        write_issue_records(Path(args.issues_jsonl), updated_records)
        print("\nUpdated issues JSONL written.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
