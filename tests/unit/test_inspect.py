from pathlib import Path

from repogauge.exec import run_command
from repogauge.mining.inspect import inspect_repository


def _init_git_repo(path: Path) -> None:
    run_command(["git", "init", "-b", "main"], cwd=str(path))
    run_command(["git", "config", "user.name", "repogauge"], cwd=str(path))
    run_command(["git", "config", "user.email", "ci@example.com"], cwd=str(path))


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _build_repo(path: Path, *, remote: str = "https://github.com/example/repo.git") -> None:
    _init_git_repo(path)
    run_command(["git", "-C", str(path), "remote", "add", "origin", remote], cwd=str(path))


def test_inspect_detects_poetry_pytest_and_remote(tmp_path: Path) -> None:
    repo = tmp_path / "poetry_repo"
    repo.mkdir()
    _build_repo(repo)
    _write_file(
        repo / "pyproject.toml",
        """
        [tool.poetry]
        name = "repogauge-demo"
        version = "0.1.0"
        python = ">=3.11,<3.12"
        [tool.poetry.dependencies]
        python = ">=3.11,<3.12"
        """,
    )
    _write_file(repo / ".python-version", "3.11.8")
    (repo / "tests").mkdir()
    _write_file(repo / "pytest.ini", "[pytest]\naddopts = -q")
    profile = inspect_repository(repo)
    assert profile["repo_name"] == "example/repo"
    assert profile["repo_version"] == "0.1.0"
    assert profile["default_branch"] == "main"
    assert profile["python_hints"]["versions"] == ["3.11"]
    assert profile["install_hints"] == ["poetry install"]
    assert profile["test_runner_hints"]["commands"] == ["pytest"]
    assert "tests" in profile["test_paths"]
    assert profile["profile_warnings"] == []
    assert profile["environment_signature"]["repo_version"] == "0.1.0"
    assert profile["environment_signature"]["signature"] == profile["version"]


def test_inspect_tox_only_generates_fallback_hints(tmp_path: Path) -> None:
    repo = tmp_path / "tox_repo"
    repo.mkdir()
    _build_repo(repo)
    _write_file(repo / "tox.ini", "[tox]\nenvlist = py311")

    profile = inspect_repository(repo)
    assert profile["test_runner_hints"]["commands"] == ["tox"]
    assert "pip install -e ." in profile["install_hints"]
    assert any(item["type"] == "missing_package_manager" for item in profile["profile_warnings"])
    assert profile["repo_version"] == "repover_unknown"
    assert profile["environment_signature"]["repo_version"] == "repover_unknown"


def test_inspect_flags_python_version_conflict(tmp_path: Path) -> None:
    repo = tmp_path / "conflict_repo"
    repo.mkdir()
    _build_repo(repo)
    _write_file(repo / "pyproject.toml", '[project]\\nname="demo"\\nversion="0.1"\\nrequires-python=">=3.10,<3.11"')
    _write_file(repo / ".python-version", "3.11.2")
    _write_file(repo / "tox.ini", "[tox]\\nenvlist = py310,py311")

    first = inspect_repository(repo)
    second = inspect_repository(repo)
    assert first == second
    assert first["python_hints"]["versions"] == ["3.10", "3.11"]
    assert any(item["type"] == "python_version_conflict" for item in first["profile_warnings"])
    assert first["environment_signature"] == second["environment_signature"]


def test_inspect_environment_signature_is_stable(tmp_path: Path) -> None:
    repo = tmp_path / "sig_repo"
    repo.mkdir()
    _build_repo(repo)
    _write_file(
        repo / "pyproject.toml",
        """
        [tool.poetry]
        name = "repogauge-demo"
        version = "0.2.0"
        [tool.poetry.dependencies]
        python = ">=3.10,<3.11"
        """,
    )
    _write_file(repo / "requirements.txt", "requests==2.31.0\n")
    profile = inspect_repository(repo)
    assert profile["version"] == profile["environment_signature"]["version"]
    assert profile["version"].startswith("0.2.0__py310__")
