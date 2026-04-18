from __future__ import annotations

import textwrap
from pathlib import Path

from repogauge.export.adapter import build_adapter_spec
from repogauge.lang import DetectionResult, detect_language
from repogauge.lang.javascript import JavaScriptAdapter, parse_js_junit
from repogauge.mining.file_roles import classify_file
from repogauge.validation.junit_parser import OUTCOME_FAIL, OUTCOME_PASS, OUTCOME_SKIP


def _write_file(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    return repo


def test_javascript_adapter_detects_package_json_and_tsconfig(tmp_path: Path) -> None:
    adapter = JavaScriptAdapter()
    repo = _make_repo(tmp_path, "repo")
    _write_file(repo / "package.json", '{"name":"demo"}')
    _write_file(repo / "tsconfig.json", "{}")
    _write_file(repo / "package-lock.json", "{}")

    result = adapter.detect(repo)

    assert result == DetectionResult(
        language="javascript",
        confidence=0.95,
        signals=["package.json", "tsconfig.json", "package-lock.json"],
        runtime_version=None,
    )


def test_javascript_adapter_ignores_plain_js_files(tmp_path: Path) -> None:
    adapter = JavaScriptAdapter()
    repo = _make_repo(tmp_path, "repo")
    _write_file(repo / "src/index.js", "export const value = 1;\n")
    _write_file(repo / "src/index.test.js", "test('x', () => expect(true).toBe(true));\n")

    result = adapter.detect(repo)

    assert result == DetectionResult(language="javascript", confidence=0.0, signals=[])


def test_detect_language_prefers_python_on_mixed_python_and_node_repo(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path, "repo")
    _write_file(repo / "package.json", '{"name":"demo"}')
    _write_file(
        repo / "pyproject.toml",
        "[project]\nname = 'demo'\nversion = '0.1.0'\n",
    )
    _write_file(repo / "src/app.py", "print('hello')\n")

    result = detect_language(repo)

    assert result.language == "python"
    assert result.confidence == 1.0


def test_javascript_inspect_prefers_vitest_and_pnpm_when_multiple_signals_exist(
    tmp_path: Path,
) -> None:
    adapter = JavaScriptAdapter()
    repo = _make_repo(tmp_path, "repo")
    _write_file(
        repo / "package.json",
        textwrap.dedent(
            """\
            {
              "name": "demo",
              "version": "1.2.3",
              "scripts": {"test": "vitest run"},
              "engines": {"node": ">=20"},
              "devDependencies": {
                "vitest": "^2.0.0",
                "jest": "^29.7.0",
                "typescript": "^5.0.0"
              },
              "jest": {"collectCoverage": true}
            }
            """
        ),
    )
    _write_file(repo / "pnpm-lock.yaml", "lockfileVersion: '9.0'\n")
    _write_file(repo / "package-lock.json", "{}\n")
    _write_file(repo / "vitest.config.ts", "export default {};\n")
    _write_file(repo / "tsconfig.json", "{}\n")

    profile = adapter.inspect(repo)

    assert profile["language"] == "javascript"
    assert profile["language_version"] == "20"
    assert profile["repo_version"] == "1.2.3"
    assert profile["install_hints"] == ["pnpm install --frozen-lockfile"]
    assert profile["test_runner_hints"]["commands"] == [
        "npx vitest run --reporter=junit --outputFile=report.xml"
    ]
    assert profile["language_hints"]["package_manager"] == "pnpm"
    assert profile["language_hints"]["framework"] == "vitest"
    assert profile["language_hints"]["typescript"] is True
    assert "package.json#vitest" in profile["test_runner_hints"]["signals"]
    assert "package.json#jest" in profile["test_runner_hints"]["signals"]


def test_javascript_inspect_detects_all_supported_package_managers(
    tmp_path: Path,
) -> None:
    adapter = JavaScriptAdapter()

    cases = [
        ("npm", "package-lock.json", "npm ci"),
        ("pnpm", "pnpm-lock.yaml", "pnpm install --frozen-lockfile"),
        ("yarn", "yarn.lock", "yarn install --frozen-lockfile"),
        ("bun", "bun.lockb", "bun install --frozen-lockfile"),
    ]
    for manager, lockfile, install_cmd in cases:
        repo = _make_repo(tmp_path, manager)
        _write_file(
            repo / "package.json",
            '{"name":"demo","devDependencies":{"jest":"^29.7.0"}}',
        )
        _write_file(repo / lockfile, "lock\n")

        profile = adapter.inspect(repo)

        assert profile["language_hints"]["package_manager"] == manager
        assert profile["install_hints"] == [install_cmd]


def test_javascript_build_env_plan_covers_vitest_and_jest_profiles() -> None:
    adapter = JavaScriptAdapter()

    vitest_plan = adapter.build_env_plan(
        {
            "language": "javascript",
            "language_version": "20",
            "language_hints": {
                "framework": "vitest",
                "package_managers": ["pnpm"],
                "lockfiles": ["pnpm-lock.yaml"],
                "runtime_version": "20",
                "test_commands": [
                    "npx vitest run --reporter=junit --outputFile=report.xml"
                ],
            },
            "test_runner_hints": {
                "commands": [
                    "npx vitest run --reporter=junit --outputFile=report.xml"
                ]
            },
        }
    )
    jest_plan = adapter.build_env_plan(
        {
            "language": "javascript",
            "language_version": "20",
            "language_hints": {
                "framework": "jest",
                "package_managers": ["npm"],
                "lockfiles": ["package-lock.json"],
                "runtime_version": "20",
                "test_commands": [
                    "npx jest --reporters=default --reporters=jest-junit "
                    "--testResultsProcessor=jest-junit"
                ],
            },
            "test_runner_hints": {
                "commands": [
                    "npx jest --reporters=default --reporters=jest-junit "
                    "--testResultsProcessor=jest-junit"
                ]
            },
        }
    )

    assert vitest_plan.install == ["pnpm install --frozen-lockfile"]
    assert (
        vitest_plan.test_cmd_base
        == "npx vitest run --reporter=junit --outputFile=report.xml"
    )
    assert vitest_plan.strategy_name == "pnpm:vitest"

    assert jest_plan.install == ["npm ci"]
    assert "jest-junit" in jest_plan.test_cmd_base
    assert jest_plan.strategy_name == "npm:jest"


def test_javascript_env_overrides_include_jest_report_path(tmp_path: Path) -> None:
    adapter = JavaScriptAdapter()
    repo = _make_repo(tmp_path, "repo")
    _write_file(
        repo / "package.json",
        '{"name":"demo","devDependencies":{"jest":"^29.7.0"}}',
    )

    env = adapter.env_overrides(repo)

    assert env["CI"] == "1"
    assert env["NODE_ENV"] == "test"
    assert env["JEST_JUNIT_OUTPUT_FILE"] == str(repo / "report.xml")


def test_parse_js_junit_normalizes_file_separators_and_outcomes(tmp_path: Path) -> None:
    xml_path = tmp_path / "results.xml"
    xml_path.write_text(
        textwrap.dedent(
            """\
            <?xml version="1.0" encoding="utf-8"?>
            <testsuite>
              <testcase classname="src\\foo.test.ts" name="math > adds values"/>
              <testcase classname="src\\foo.test.ts" name="math > subtracts values">
                <failure/>
              </testcase>
              <testcase classname="src\\foo.test.ts" name="math > skips values">
                <skipped/>
              </testcase>
            </testsuite>
            """
        ),
        encoding="utf-8",
    )

    results = parse_js_junit(xml_path)

    assert results == {
        "src/foo.test.ts::math > adds values": OUTCOME_PASS,
        "src/foo.test.ts::math > subtracts values": OUTCOME_FAIL,
        "src/foo.test.ts::math > skips values": OUTCOME_SKIP,
    }


def test_javascript_file_role_rules_and_harness_template_vars() -> None:
    adapter = JavaScriptAdapter()
    rules = adapter.file_role_rules()
    spec = build_adapter_spec(
        "owner/demo",
        {
            "language": "javascript",
            "runtime_version": "20",
            "python_version": "3.11",
            "install": ["npm ci"],
            "test_cmd_base": "npx vitest run --reporter=junit --outputFile=report.xml",
            "strategy_name": "npm:vitest",
            "language_hints": {"typescript": True},
        },
    )

    assert rules.test_dir_names == {"__tests__"}
    assert "package.json" in rules.config_build_filenames
    assert "node_modules" in rules.vendor_dir_names
    assert classify_file("src/foo.ts").role == "prod"
    assert classify_file("src/foo.test.ts").role == "test"
    assert classify_file("node_modules/pkg/index.js").role == "generated_vendor"

    assert spec["parser"] == "junit_js"
    assert spec["parser_import"] == "repogauge.lang.javascript.parse_js_junit"
    assert spec["ext"] == "ts"
