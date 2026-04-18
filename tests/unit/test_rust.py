from __future__ import annotations

import textwrap
from pathlib import Path

from repogauge.export.adapter import build_adapter_spec
from repogauge.lang._rust_test_parser import parse_cargo_human
from repogauge.lang.rust import RustAdapter
from repogauge.mining.file_roles import classify_file
from repogauge.validation.junit_parser import OUTCOME_FAIL, OUTCOME_PASS, OUTCOME_SKIP


def _write_file(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_rust_adapter_detects_single_crate_and_workspace(tmp_path: Path) -> None:
    adapter = RustAdapter()
    crate = tmp_path / "crate"
    crate.mkdir()
    _write_file(
        crate / "Cargo.toml",
        '[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n',
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_file(
        workspace / "Cargo.toml",
        '[workspace]\nmembers = ["crate-a"]\n[workspace.package]\nversion = "1.2.0"\n',
    )

    assert adapter.detect(crate).confidence == 1.0
    workspace_detection = adapter.detect(workspace)
    assert workspace_detection.confidence == 1.0
    assert "workspace" in workspace_detection.signals


def test_rust_adapter_inspect_and_build_env_plan(tmp_path: Path) -> None:
    adapter = RustAdapter()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_file(
        repo / "Cargo.toml",
        textwrap.dedent(
            """\
            [package]
            name = "demo"
            version = "0.1.0"
            edition = "2021"
            """
        ),
    )
    _write_file(
        repo / "rust-toolchain.toml",
        '[toolchain]\nchannel = "1.74"\n',
    )
    (repo / "tests").mkdir()

    profile = adapter.inspect(repo)
    plan = adapter.build_env_plan(profile)

    assert profile["language_version"] == "1.74"
    assert profile["repo_version"] == "0.1.0"
    assert profile["language_hints"]["edition"] == "2021"
    assert profile["language_hints"]["runtime_version"] == "1.74"
    assert plan.install == ["cargo fetch"]
    assert plan.test_cmd_base == "cargo test --no-fail-fast"
    assert plan.strategy_name == "cargo:cargo-test"


def test_parse_cargo_human_parses_crates_doctests_and_outcomes() -> None:
    parsed = parse_cargo_human(
        """
        Running unittests src/lib.rs (target/debug/deps/demo-1234abcd)
        test tests::adds_one ... ok
        test tests::subtracts_one ... FAILED
        Running unittests tests/integration.rs (target/debug/deps/integration-2345bcde)
        test integration::works ... ignored
        Doc-tests demo
        test src/lib.rs - add (line 12) ... ok
        """
    )

    assert parsed == {
        "demo::tests::adds_one": OUTCOME_PASS,
        "demo::tests::subtracts_one": OUTCOME_FAIL,
        "integration::integration::works": OUTCOME_SKIP,
        "demo::src/lib.rs::doctest_12": OUTCOME_PASS,
    }


def test_rust_file_role_rules_and_harness_template_vars(tmp_path: Path) -> None:
    adapter = RustAdapter()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_file(
        repo / "Cargo.toml",
        '[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n',
    )
    _write_file(repo / "src/lib.rs", "pub fn add() {}\n")
    _write_file(repo / "tests/integration.rs", "fn main() {}\n")
    (repo / "target" / "debug").mkdir(parents=True)
    _write_file(repo / "target/debug/demo", "")
    spec = build_adapter_spec(
        "owner/demo-rust",
        {
            "language": "rust",
            "runtime_version": "stable",
            "python_version": "3.11",
            "install": ["cargo fetch"],
            "test_cmd_base": "cargo test --no-fail-fast",
            "strategy_name": "cargo:cargo-test",
        },
    )

    rules = [adapter.file_role_rules()]
    assert classify_file(repo / "src/lib.rs", rules=rules).role == "prod"
    assert classify_file(repo / "tests/integration.rs", rules=rules).role == "test"
    assert classify_file(repo / "target/debug/demo", rules=rules).role == "generated_vendor"

    env = adapter.env_overrides(Path("/tmp/worktree"))
    assert env["CARGO_HOME"].endswith(".cargo")
    assert env["CARGO_TARGET_DIR"].endswith("target")

    assert spec["parser"] == "cargo_human"
    assert (
        spec["parser_import"]
        == "repogauge.lang._rust_test_parser.parse_cargo_human"
    )
    assert spec["ext"] == "rs"
