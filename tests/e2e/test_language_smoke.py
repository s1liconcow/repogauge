from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from repogauge.cli import main
from repogauge.exec import run_command_checked


def _run_main(args: list[str]) -> int:
    return main(args)


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _init_repo(path: Path) -> None:
    run_command_checked(["git", "init", "-b", "main"], cwd=str(path))
    run_command_checked(["git", "config", "user.name", "ci"], cwd=str(path))
    run_command_checked(["git", "config", "user.email", "ci@example.com"], cwd=str(path))


def _commit_all(path: Path, message: str) -> None:
    run_command_checked(["git", "add", "."], cwd=str(path))
    run_command_checked(["git", "commit", "-m", message], cwd=str(path))


def _create_js_repo(path: Path) -> None:
    (path / "src").mkdir()
    (path / "tests").mkdir()
    (path / "package.json").write_text(
        json.dumps(
            {
                "name": "demo-js",
                "version": "0.1.0",
                "devDependencies": {"vitest": "^2.0.0"},
                "scripts": {"test": "vitest run"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    (path / "src" / "index.js").write_text("export const add = (a, b) => a + b;\n", encoding="utf-8")
    _commit_all(path, "base")

    (path / "src" / "index.js").write_text("export const add = (a, b) => a + b + 1;\n", encoding="utf-8")
    (path / "tests" / "index.test.js").write_text("test('add', () => {});\n", encoding="utf-8")
    _commit_all(path, "prod+tests")


def _create_java_repo(path: Path) -> None:
    (path / "src" / "main" / "java").mkdir(parents=True)
    (path / "src" / "test" / "java").mkdir(parents=True)
    (path / "pom.xml").write_text(
        """
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example</groupId>
  <artifactId>demo-java</artifactId>
  <version>0.1.0</version>
</project>
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (path / "src" / "main" / "java" / "App.java").write_text("class App {}\n", encoding="utf-8")
    _commit_all(path, "base")

    (path / "src" / "main" / "java" / "App.java").write_text("class App { int value() { return 1; } }\n", encoding="utf-8")
    (path / "src" / "test" / "java" / "AppTest.java").write_text("class AppTest {}\n", encoding="utf-8")
    _commit_all(path, "prod+tests")


def _create_go_repo(path: Path) -> None:
    (path / "go.mod").write_text("module example.com/demo\n\ngo 1.22\n", encoding="utf-8")
    (path / "main.go").write_text("package demo\nfunc Add(a int, b int) int { return a + b }\n", encoding="utf-8")
    _commit_all(path, "base")

    (path / "main.go").write_text("package demo\nfunc Add(a int, b int) int { return a + b + 1 }\n", encoding="utf-8")
    (path / "main_test.go").write_text("package demo\nfunc TestAdd(t *testing.T) {}\n", encoding="utf-8")
    _commit_all(path, "prod+tests")


def _create_rust_repo(path: Path) -> None:
    (path / "src").mkdir()
    (path / "tests").mkdir()
    (path / "Cargo.toml").write_text(
        '[package]\nname = "demo-rust"\nversion = "0.1.0"\nedition = "2021"\n',
        encoding="utf-8",
    )
    (path / "src" / "lib.rs").write_text("pub fn add(a: i32, b: i32) -> i32 { a + b }\n", encoding="utf-8")
    _commit_all(path, "base")

    (path / "src" / "lib.rs").write_text("pub fn add(a: i32, b: i32) -> i32 { a + b + 1 }\n", encoding="utf-8")
    (path / "tests" / "integration.rs").write_text("fn integration() {}\n", encoding="utf-8")
    _commit_all(path, "prod+tests")


def _accept_test_candidates(mine_out: Path, decisions_path: Path) -> None:
    rows = _read_jsonl(mine_out / "candidates.jsonl")
    accepted_ids = [row["id"] for row in rows if row["metadata"].get("n_test_files", 0) > 0]
    assert accepted_ids
    decisions_path.write_text(
        "\n".join(
            json.dumps({"id": candidate_id, "state": "accepted", "reviewer_notes": "e2e"})
            for candidate_id in accepted_ids
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("creator", "expected_language", "expected_parser"),
    [
        (_create_js_repo, "javascript", "junit_js"),
        (_create_java_repo, "java", "junit_java"),
        (_create_go_repo, "go", "go_json"),
        (_create_rust_repo, "rust", "cargo_human"),
    ],
)
def test_language_smoke_mine_review_export(
    tmp_path: Path,
    creator,
    expected_language: str,
    expected_parser: str,
) -> None:
    repo = tmp_path / expected_language
    repo.mkdir()
    _init_repo(repo)
    creator(repo)

    workspace = repo / ".rg_smoke"
    workspace.mkdir()
    mine_out = workspace / "mine"
    review_out = workspace / "review"
    export_out = workspace / "export"
    decisions = workspace / "decisions.jsonl"

    assert _run_main(["mine", str(repo), "--out", str(mine_out), "--llm-mode", "off"]) == 0
    profile = json.loads((mine_out / "repo_profile.json").read_text(encoding="utf-8"))
    assert profile["language"] == expected_language

    _accept_test_candidates(mine_out, decisions)
    assert _run_main(
        [
            "review",
            str(mine_out / "candidates.jsonl"),
            "--out",
            str(review_out),
            "--decisions",
            str(decisions),
            "--llm-mode",
            "off",
        ]
    ) == 0
    assert _run_main(
        ["export", str(review_out / "reviewed.jsonl"), "--out", str(export_out), "--llm-mode", "off"]
    ) == 0

    spec = json.loads((export_out / "specs.json").read_text(encoding="utf-8"))
    assert spec["language"] == expected_language
    assert spec["parser"] == expected_parser

    adapter_path = next(export_out.glob("adapter_*.py"))
    module_spec = importlib.util.spec_from_file_location("generated_adapter", adapter_path)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    assert module.PARSER == expected_parser
