from __future__ import annotations

from pathlib import Path

import pytest

from repogauge.lang import DetectionResult
from repogauge.lang.java import JavaAdapter


def _write_file(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_java_adapter_detects_maven_gradle_and_empty(tmp_path: Path) -> None:
    adapter = JavaAdapter()

    maven_repo = tmp_path / "maven"
    maven_repo.mkdir()
    _write_file(maven_repo / "pom.xml", "<project/>")

    gradle_repo = tmp_path / "gradle"
    gradle_repo.mkdir()
    _write_file(gradle_repo / "build.gradle", "plugins { id 'java' }")

    both_repo = tmp_path / "both"
    both_repo.mkdir()
    _write_file(both_repo / "pom.xml", "<project/>")
    _write_file(both_repo / "build.gradle.kts", "plugins { java }")

    empty_repo = tmp_path / "empty"
    empty_repo.mkdir()

    maven = adapter.detect(maven_repo)
    gradle = adapter.detect(gradle_repo)
    both = adapter.detect(both_repo)
    empty = adapter.detect(empty_repo)

    assert maven == DetectionResult(
        language="java",
        confidence=1.0,
        signals=["pom.xml"],
        runtime_version="maven",
    )
    assert gradle == DetectionResult(
        language="java",
        confidence=1.0,
        signals=["build.gradle"],
        runtime_version="gradle",
    )
    assert both.confidence == pytest.approx(0.95)
    assert set(both.signals) == {"build.gradle.kts", "pom.xml"}
    assert both.runtime_version == "maven"
    assert empty == DetectionResult(language="java", confidence=0.0, signals=[])


def test_java_adapter_inspect_reports_build_tool_and_kotlin_presence(
    tmp_path: Path,
) -> None:
    adapter = JavaAdapter()
    repo = tmp_path / "repo"
    _write_file(repo / "build.gradle.kts", "plugins { java }")
    _write_file(repo / "src/main/kotlin/App.kt", "class App")
    _write_file(repo / "src/test/java/AppTest.java", "class AppTest")

    profile = adapter.inspect(repo)

    assert profile["language"] == "java"
    assert profile["language_hints"]["build_tool"] == "gradle"
    assert profile["language_hints"]["kotlin_present"] is True
    assert "build.gradle.kts" in profile["language_hints"]["signals"]
    assert profile["install_hints"] == ["./gradlew --no-daemon classes"]
    assert profile["test_runner_hints"]["commands"] == [
        "./gradlew --no-daemon test"
    ]
    assert profile["test_paths"] == ["src/test/java"]


def test_java_adapter_build_env_plan_uses_java_defaults(tmp_path: Path) -> None:
    adapter = JavaAdapter()
    repo = tmp_path / "repo"
    _write_file(repo / "pom.xml", "<project/>")
    profile = adapter.inspect(repo)

    plan = adapter.build_env_plan(profile)

    assert plan.language == "java"
    assert plan.install == ["mvn -q -DskipTests compile"]
    assert plan.build == []
    assert plan.test_cmd_base == "mvn -q test"
    assert plan.strategy_name == "maven:maven-test"
    assert "build_tool:maven" in plan.provenance
