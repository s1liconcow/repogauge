from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


def load_script_module():
    script_path = (
        Path(__file__).resolve().parents[2] / "scripts" / "repair_imported_bead_dependencies.py"
    )
    spec = importlib.util.spec_from_file_location("repair_imported_bead_dependencies", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_build_repair_plan_finds_missing_and_extra_edges(tmp_path):
    module = load_script_module()

    markdown = tmp_path / "import.md"
    markdown.write_text(
        """## Root task

### ID old-000

## Middle task

### ID old-001

### Dependencies

- old-000

## Leaf task

### ID old-002

### Dependencies

- old-000
- old-001
""",
        encoding="utf-8",
    )

    issues_jsonl = tmp_path / "issues.jsonl"
    issues_jsonl.write_text(
        "\n".join(
            [
                json.dumps({"id": "new-a", "title": "Root task"}),
                json.dumps(
                    {
                        "id": "new-b",
                        "title": "Middle task",
                        "dependencies": [
                            {"issue_id": "new-b", "depends_on_id": "new-a"},
                        ],
                    }
                ),
                json.dumps(
                    {
                        "id": "new-c",
                        "title": "Leaf task",
                        "dependencies": [
                            {"issue_id": "new-c", "depends_on_id": "new-a"},
                            {"issue_id": "new-a", "depends_on_id": "new-b"},
                        ],
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    imported = module.parse_import_markdown(markdown)
    issue_records = module.load_issue_records(issues_jsonl)
    plan = module.build_repair_plan(imported, issue_records)

    assert set(plan.expected_edges) == {
        ("new-b", "new-a"),
        ("new-c", "new-a"),
        ("new-c", "new-b"),
    }
    assert plan.missing_edges == (("new-c", "new-b"),)
    assert plan.extra_edges == (("new-a", "new-b"),)


def test_apply_repair_plan_to_records_adds_and_prunes_edges(tmp_path):
    module = load_script_module()

    issue_records = [
        {
            "id": "new-a",
            "title": "Root task",
            "dependencies": [
                {"issue_id": "new-a", "depends_on_id": "new-b"},
            ],
        },
        {
            "id": "new-b",
            "title": "Middle task",
        },
        {"id": "new-c", "title": "Leaf task"},
    ]
    plan = module.RepairPlan(
        imported_ids=frozenset({"new-a", "new-b", "new-c"}),
        expected_edges=frozenset({("new-b", "new-a"), ("new-c", "new-b")}),
        missing_edges=(("new-b", "new-a"), ("new-c", "new-b")),
        extra_edges=(("new-a", "new-b"),),
    )

    updated = module.apply_repair_plan_to_records(
        issue_records,
        plan,
        prune_extra=True,
        actor="tester",
    )

    by_id = {record["id"]: record for record in updated}
    assert "dependencies" not in by_id["new-a"]
    assert {dep["depends_on_id"] for dep in by_id["new-b"]["dependencies"]} == {"new-a"}
    assert {dep["depends_on_id"] for dep in by_id["new-c"]["dependencies"]} == {"new-b"}
    assert by_id["new-b"]["dependencies"][0]["created_by"] == "tester"
