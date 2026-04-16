"""Unit tests for validation test-selection helpers."""

from repogauge.validation.testsel import (
    build_targeted_test_inputs,
    build_targeted_test_plan,
)


def test_build_targeted_test_plan_prefers_node_ids_from_test_patch() -> None:
    test_patch = (
        "diff --git a/tests/unit/test_thing.py b/tests/unit/test_thing.py\n"
        "--- a/tests/unit/test_thing.py\n"
        "+++ b/tests/unit/test_thing.py\n"
        "@@ -1,3 +1,3 @@\n"
        "-def test_old():\n"
        "+def test_new():\n"
        "    assert True\n"
    )

    cmd, inputs = build_targeted_test_plan("python -m pytest", test_patch)

    assert cmd == "python -m pytest --tb=no -q --junit-xml={junit_xml}"
    assert inputs == ["tests/unit/test_thing.py::test_new"]


def test_build_targeted_test_plan_targets_changed_test_file_when_no_node_ids() -> None:
    test_patch = (
        "diff --git a/src/core.py b/src/core.py\n"
        "--- a/src/core.py\n"
        "+++ b/src/core.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-x = 1\n"
        "+x = 2\n"
        "diff --git a/tests/unit/test_core.py b/tests/unit/test_core.py\n"
        "--- a/tests/unit/test_core.py\n"
        "+++ b/tests/unit/test_core.py\n"
        "@@ -1,1 +1,2 @@\n"
        "+import pytest\n"
    )

    cmd, inputs = build_targeted_test_plan("pytest", test_patch)

    assert cmd == "pytest --tb=no -q --junit-xml={junit_xml}"
    assert inputs == ["tests/unit/test_core.py"]


def test_build_targeted_test_plan_falls_back_to_tests_root_on_support_only_change() -> (
    None
):
    test_patch = (
        "diff --git a/tests/conftest.py b/tests/conftest.py\n"
        "--- a/tests/conftest.py\n"
        "+++ b/tests/conftest.py\n"
        "@@ -1,1 +1,2 @@\n"
        "+import warnings\n"
    )

    cmd, inputs = build_targeted_test_plan("pytest", test_patch)

    assert cmd == "pytest --tb=no -q --junit-xml={junit_xml}"
    assert inputs == ["tests"]


def test_build_targeted_test_plan_stays_empty_for_non_pytest_command() -> None:
    test_patch = (
        "diff --git a/tests/unit/test_thing.py b/tests/unit/test_thing.py\n"
        "--- a/tests/unit/test_thing.py\n"
        "+++ b/tests/unit/test_thing.py\n"
        "@@ -1,1 +1,2 @@\n"
        "+\n"
    )

    cmd, inputs = build_targeted_test_plan("python -m unittest", test_patch)

    assert cmd == "python -m unittest"
    assert inputs == []


def test_build_targeted_test_plan_keeps_existing_junit_xml_flag() -> None:
    test_patch = (
        "diff --git a/tests/unit/test_thing.py b/tests/unit/test_thing.py\n"
        "--- a/tests/unit/test_thing.py\n"
        "+++ b/tests/unit/test_thing.py\n"
        "@@ -1,1 +1,2 @@\n"
        "+import pytest\n"
    )

    cmd, inputs = build_targeted_test_plan(
        "pytest --junit-xml=my-tests.xml", test_patch
    )

    assert cmd == "pytest --junit-xml=my-tests.xml --tb=no -q"
    assert inputs == ["tests/unit/test_thing.py"]


def test_build_targeted_test_plan_keeps_existing_junitxml_flag() -> None:
    test_patch = (
        "diff --git a/tests/unit/test_thing.py b/tests/unit/test_thing.py\n"
        "--- a/tests/unit/test_thing.py\n"
        "+++ b/tests/unit/test_thing.py\n"
        "@@ -1,1 +1,2 @@\n"
        "+import pytest\n"
    )

    cmd, inputs = build_targeted_test_plan(
        "pytest --junitxml=my-tests.xml", test_patch
    )

    assert cmd == "pytest --junitxml=my-tests.xml --tb=no -q"
    assert inputs == ["tests/unit/test_thing.py"]


def test_build_targeted_test_inputs_prefers_function_nodes() -> None:
    test_patch = (
        "diff --git a/tests/unit/test_nested.py b/tests/unit/test_nested.py\n"
        "--- a/tests/unit/test_nested.py\n"
        "+++ b/tests/unit/test_nested.py\n"
        "@@ -1,2 +1,4 @@\n"
        "+class TestNested:\n"
        "+    def test_case(self):\n"
        "         pass\n"
    )

    inputs = build_targeted_test_inputs(test_patch)

    assert inputs == ["tests/unit/test_nested.py::TestNested::test_case"]
