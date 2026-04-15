from pathlib import Path

from repogauge.mining.signature import REPO_VERSION_UNKNOWN, build_environment_signature


def _base_profile(repo_root: Path) -> dict:
    return {
        "repo_root": str(repo_root),
        "repo_name": "owner/repo",
        "repo_version": "1.2.3",
        "python_hints": {
            "versions": ["3.11", "3.10"],
            "package_managers": ["poetry", "setuptools"],
            "package_style": "src",
            "signals": ["tool.poetry"],
        },
        "install_hints": ["pip install -e .", "poetry install"],
        "test_runner_hints": {
            "commands": ["pytest", "tox"],
            "signals": [],
        },
    }


def test_environment_signature_is_deterministic(tmp_path: Path) -> None:
    profile_a = _base_profile(tmp_path)
    profile_b = _base_profile(tmp_path)
    profile_a["python_hints"]["package_style"] = "src"
    profile_b["python_hints"]["package_style"] = "src"
    profile_a["python_hints"]["versions"] = ["3.11", "3.10"]
    profile_b["python_hints"]["versions"] = ["3.10", "3.11"]
    profile_a["python_hints"]["package_managers"] = ["setuptools", "poetry"]
    profile_b["python_hints"]["package_managers"] = ["poetry", "setuptools"]

    first = build_environment_signature(profile_a)
    second = build_environment_signature(profile_b)

    assert first == second
    assert first["version"] == "1.2.3__py310_py311__pytest+tox__poetry+setuptools__reqhash_" + first["dependency_signature"]
    assert first["signature"] == first["version"]


def test_environment_signature_uses_repo_version_fallback(tmp_path: Path) -> None:
    profile = {
        "repo_root": str(tmp_path),
        "repo_name": "owner/repo",
        "python_hints": {
            "versions": ["3.11"],
            "package_managers": ["requirements"],
            "package_style": "unknown",
            "signals": [],
        },
        "install_hints": ["pip install -r requirements.txt"],
        "test_runner_hints": {
            "commands": ["pytest"],
            "signals": [],
        },
    }
    first = build_environment_signature(profile)
    assert first["repo_version"] == REPO_VERSION_UNKNOWN
    assert first["version"].startswith(f"{REPO_VERSION_UNKNOWN}__py311__pytest__requirements__reqhash_")


def test_environment_signature_normalizes_requirement_content(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("requests==2.31.0\n\n# comment\npytest>=8\n", encoding="utf-8")

    profile = _base_profile(tmp_path)
    profile["repo_root"] = str(tmp_path)
    first = build_environment_signature(profile)

    requirements.write_text("\npytest>=8  # test dep\nrequests==2.31.0\n", encoding="utf-8")
    second = build_environment_signature(profile)

    assert first["dependency_signature"] == second["dependency_signature"]
    assert first["version"] == second["version"]
