from repogauge.validation.env_detect import build_environment_plan


def test_build_environment_plan_prefers_poetry_and_pytest() -> None:
    profile = {
        "python_hints": {
            "versions": ["3.11"],
            "package_managers": ["poetry", "pyproject"],
        },
        "install_hints": ["poetry install", "pip install -e ."],
        "test_runner_hints": {
            "commands": ["pytest"],
        },
    }
    plan = build_environment_plan(profile)

    assert plan.python_version == "3.11"
    assert plan.install == ["poetry install"]
    assert plan.pre_install == []
    assert plan.build == []
    assert plan.test_cmd_base == "pytest"
    assert plan.strategy_name == "poetry:pytest"
    assert plan.confidence > 0.9
    assert "install_strategy:poetry" in plan.provenance
    assert "test_runner:pytest" in plan.provenance


def test_build_environment_plan_selects_setuptools_without_pytest_dependency() -> None:
    profile = {
        "python_hints": {
            "versions": ["3.10"],
            "package_managers": ["setuptools"],
        },
        "install_hints": ["pip install -e ."],
        "test_runner_hints": {
            "commands": ["python -m unittest"],
        },
    }
    plan = build_environment_plan(profile)

    assert plan.python_version == "3.10"
    assert plan.install == ["pip install -e ."]
    assert plan.test_cmd_base == "python -m unittest"
    assert plan.strategy_name == "setuptools:unittest"
    assert plan.confidence > 0.8


def test_build_environment_plan_selects_first_sorted_requirements_file() -> None:
    profile = {
        "python_hints": {
            "versions": ["3.12"],
            "package_managers": ["requirements"],
        },
        "install_hints": [
            "pip install -r requirements-dev.txt",
            "pip install -r requirements.txt",
        ],
        "test_runner_hints": {"commands": []},
    }
    plan = build_environment_plan(profile)

    assert plan.python_version == "3.12"
    assert plan.install == ["pip install -r requirements-dev.txt", "pip install pytest"]
    assert plan.strategy_name == "requirements:pytest-default"
    assert plan.test_cmd_base == "python -m pytest"


def test_build_environment_plan_chooses_minimum_python_version_on_conflict() -> None:
    profile = {
        "python_hints": {
            "versions": ["3.11", "3.10", "3.9"],
            "package_managers": ["setuptools", "requirements"],
        },
        "install_hints": ["pip install -e ."],
        "test_runner_hints": {"commands": ["pytest", "nox"]},
    }
    plan = build_environment_plan(profile)

    assert plan.python_version == "3.9"
    assert "python_version:conflict" in plan.provenance
    assert "python_version:chose-minimum" in plan.provenance
    assert plan.confidence < 1.0
