from __future__ import annotations

import textwrap
from pathlib import Path

from repogauge.export.adapter import build_adapter_spec
from repogauge.lang.java import JavaAdapter, parse_java_junit
from repogauge.mining.file_roles import classify_file
from repogauge.validation.junit_parser import OUTCOME_FAIL, OUTCOME_PASS


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

    empty_repo = tmp_path / "empty"
    empty_repo.mkdir()

    assert adapter.detect(maven_repo).confidence == 1.0
    assert adapter.detect(gradle_repo).runtime_version == "gradle"
    assert adapter.detect(empty_repo).confidence == 0.0


def test_java_adapter_inspect_parses_maven_runtime_and_framework(tmp_path: Path) -> None:
    adapter = JavaAdapter()
    repo = tmp_path / "repo"
    _write_file(
        repo / "pom.xml",
        textwrap.dedent(
            """\
            <project xmlns="http://maven.apache.org/POM/4.0.0">
              <modelVersion>4.0.0</modelVersion>
              <groupId>com.example</groupId>
              <artifactId>demo</artifactId>
              <version>1.2.3</version>
              <properties>
                <maven.compiler.release>21</maven.compiler.release>
              </properties>
              <dependencies>
                <dependency>
                  <groupId>org.junit.jupiter</groupId>
                  <artifactId>junit-jupiter</artifactId>
                </dependency>
              </dependencies>
            </project>
            """
        ),
    )
    _write_file(repo / "src/test/java/AppTest.java", "class AppTest {}")

    profile = adapter.inspect(repo)

    assert profile["language_version"] == "21"
    assert profile["repo_version"] == "1.2.3"
    assert profile["install_hints"] == ["mvn -B -DskipTests install"]
    assert profile["test_runner_hints"]["commands"] == ["mvn -B test"]
    assert profile["language_hints"]["framework"] == "junit5"
    assert profile["test_paths"] == ["src/test/java"]


def test_java_adapter_inspect_parses_gradle_runtime_framework_and_wrapper_fallback(
    tmp_path: Path,
) -> None:
    adapter = JavaAdapter()
    repo = tmp_path / "repo"
    _write_file(
        repo / "build.gradle",
        textwrap.dedent(
            """\
            plugins { id 'java' }
            version = '2.0.0'
            sourceCompatibility = '17'
            test {
              useJUnit()
            }
            dependencies {
              testImplementation 'junit:junit:4.13.2'
            }
            """
        ),
    )
    _write_file(repo / "gradlew", "#!/bin/sh\n")

    profile = adapter.inspect(repo)

    assert profile["language_version"] == "17"
    assert profile["repo_version"] == "2.0.0"
    assert profile["language_hints"]["framework"] == "junit4"
    assert profile["install_hints"] == ["gradle assemble"]
    assert profile["test_runner_hints"]["commands"] == ["gradle test"]
    assert any(
        warning["type"] == "gradle_wrapper_not_executable"
        for warning in profile["profile_warnings"]
    )


def test_java_build_env_plan_uses_detected_build_tool_and_framework() -> None:
    adapter = JavaAdapter()

    maven_plan = adapter.build_env_plan(
        {
            "language": "java",
            "language_version": "21",
            "language_hints": {
                "build_tool": "maven",
                "runtime_version": "21",
                "framework": "junit5",
            },
            "install_hints": ["mvn -B -DskipTests install"],
            "test_runner_hints": {"commands": ["mvn -B test"]},
        }
    )
    gradle_plan = adapter.build_env_plan(
        {
            "language": "java",
            "language_version": "17",
            "language_hints": {
                "build_tool": "gradle",
                "runtime_version": "17",
                "framework": "junit4",
            },
            "install_hints": ["./gradlew assemble"],
            "test_runner_hints": {"commands": ["./gradlew test"]},
        }
    )

    assert maven_plan.install == ["mvn -B -DskipTests install"]
    assert maven_plan.test_cmd_base == "mvn -B test"
    assert maven_plan.strategy_name == "maven:junit5"

    assert gradle_plan.install == ["./gradlew assemble"]
    assert gradle_plan.test_cmd_base == "./gradlew test"
    assert gradle_plan.strategy_name == "gradle:junit4"


def test_parse_java_junit_supports_single_and_multiple_xml_inputs(tmp_path: Path) -> None:
    xml_a = tmp_path / "TEST-com.example.FooTest.xml"
    xml_b = tmp_path / "TEST-com.example.BarTest.xml"
    xml_a.write_text(
        textwrap.dedent(
            """\
            <?xml version="1.0" encoding="utf-8"?>
            <testsuite>
              <testcase classname="com.example.FooTest" name="testPass"/>
            </testsuite>
            """
        ),
        encoding="utf-8",
    )
    xml_b.write_text(
        textwrap.dedent(
            """\
            <?xml version="1.0" encoding="utf-8"?>
            <testsuite>
              <testcase classname="com.example.BarTest" name="testFail">
                <failure/>
              </testcase>
            </testsuite>
            """
        ),
        encoding="utf-8",
    )

    single = parse_java_junit(xml_a)
    merged = parse_java_junit(str(tmp_path / "TEST-*.xml"))

    assert single == {"com.example.FooTest::testPass": OUTCOME_PASS}
    assert merged == {
        "com.example.BarTest::testFail": OUTCOME_FAIL,
        "com.example.FooTest::testPass": OUTCOME_PASS,
    }


def test_java_file_role_rules_and_harness_template_vars() -> None:
    adapter = JavaAdapter()
    rules = adapter.file_role_rules()
    spec = build_adapter_spec(
        "owner/demo-java",
        {
            "language": "java",
            "runtime_version": "21",
            "python_version": "3.11",
            "install": ["mvn -B -DskipTests install"],
            "test_cmd_base": "mvn -B test",
            "strategy_name": "maven:junit5",
        },
    )

    assert "pom.xml" in rules.config_build_filenames
    assert "target" in rules.vendor_dir_names
    assert classify_file("src/main/java/Foo.java").role == "prod"
    assert classify_file("src/test/java/FooTest.java").role == "test"
    assert classify_file("target/surefire-reports/TEST-Foo.xml").role == "generated_vendor"

    assert spec["parser"] == "junit_java"
    assert spec["parser_import"] == "repogauge.lang.java.parse_java_junit"
    assert spec["ext"] == "java"
