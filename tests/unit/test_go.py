from __future__ import annotations

import textwrap
from pathlib import Path

from repogauge.export.adapter import build_adapter_spec
from repogauge.lang._go_test_parser import parse_go_test_json
from repogauge.lang.go import GoAdapter
from repogauge.mining.file_roles import classify_file
from repogauge.validation.junit_parser import OUTCOME_FAIL, OUTCOME_PASS, OUTCOME_SKIP


def _write_file(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_go_adapter_detects_go_mod_and_source_fallback(tmp_path: Path) -> None:
    adapter = GoAdapter()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_file(repo / "go.mod", "module example.com/m\n\ngo 1.22\n")
    source_only = tmp_path / "source_only"
    source_only.mkdir()
    _write_file(source_only / "main.go", "package main\n")
    empty = tmp_path / "empty"
    empty.mkdir()

    assert adapter.detect(repo).confidence == 1.0
    assert adapter.detect(source_only).confidence == 0.6
    assert adapter.detect(empty).confidence == 0.0


def test_go_adapter_inspect_and_build_env_plan(tmp_path: Path) -> None:
    adapter = GoAdapter()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_file(
        repo / "go.mod",
        textwrap.dedent(
            """\
            module example.com/demo/v2

            go 1.22

            require (
              github.com/stretchr/testify v1.9.0
            )
            """
        ),
    )
    _write_file(repo / "go.sum", "sum\n")
    (repo / "vendor").mkdir()

    profile = adapter.inspect(repo)
    plan = adapter.build_env_plan(profile)

    assert profile["language_version"] == "1.22"
    assert profile["repo_version"] == "v2"
    assert profile["language_hints"]["module_path"] == "example.com/demo/v2"
    assert profile["language_hints"]["require_count"] == 1
    assert "vendor" in profile["language_hints"]["signals"]
    assert plan.install == []
    assert plan.test_cmd_base == "go test -json ./..."
    assert plan.strategy_name == "go-modules:go-test"


def test_parse_go_test_json_handles_pass_fail_skip_and_subtests() -> None:
    parsed = parse_go_test_json(
        """
        {"Action":"run","Package":"example.com/demo","Test":"TestAdd"}
        {"Action":"pass","Package":"example.com/demo","Test":"TestAdd"}
        {"Action":"fail","Package":"example.com/demo","Test":"TestSubtract"}
        {"Action":"skip","Package":"example.com/demo","Test":"TestTable/case_a"}
        {"Action":"output","Package":"example.com/demo","Output":"noise"}
        """
    )

    assert parsed == {
        "example.com/demo::TestAdd": OUTCOME_PASS,
        "example.com/demo::TestSubtract": OUTCOME_FAIL,
        "example.com/demo::TestTable/case_a": OUTCOME_SKIP,
    }


def test_go_file_role_rules_and_harness_template_vars(tmp_path: Path) -> None:
    adapter = GoAdapter()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_file(repo / "go.mod", "module example.com/demo\n\ngo 1.22\n")
    _write_file(repo / "foo.go", "package demo\n")
    _write_file(repo / "foo_test.go", "package demo\n")
    (repo / "vendor" / "pkg").mkdir(parents=True)
    _write_file(repo / "vendor/pkg/foo.go", "package pkg\n")
    spec = build_adapter_spec(
        "owner/demo-go",
        {
            "language": "go",
            "runtime_version": "1.22",
            "python_version": "3.11",
            "install": ["go mod download"],
            "test_cmd_base": "go test -json ./...",
            "strategy_name": "go-modules:go-test",
        },
    )

    assert classify_file(repo / "foo.go").role == "prod"
    assert classify_file(repo / "foo_test.go").role == "test"
    assert classify_file(repo / "vendor/pkg/foo.go").role == "generated_vendor"

    assert spec["parser"] == "go_json"
    assert spec["parser_import"] == "repogauge.lang._go_test_parser.parse_go_test_json"
    assert spec["ext"] == "go"
    assert adapter.env_overrides(Path("/tmp/worktree"))["GOCACHE"].endswith(".gocache")
