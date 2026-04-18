"""Tests for adapter/spec generation (bead q5b)."""

import json
import importlib.util
from pathlib import Path
from inspect import isfunction

from repogauge.export.adapter import build_adapter_spec, generate_adapter
from repogauge.parsers.junit import parse_repogauge_junit


class TestBuildAdapterSpec:
    def test_populates_all_required_fields(self):
        plan = {
            "python_version": "3.11",
            "language": "python",
            "runtime_version": "3.11",
            "version": "1.2.3",
            "pre_install": [],
            "install": ["pip install -e ."],
            "build": [],
            "test_cmd_base": "pytest",
            "strategy_name": "setuptools:pytest",
        }
        spec = build_adapter_spec("owner/repo", plan)
        assert spec["repo"] == "owner/repo"
        assert spec["version"] == "1.2.3"
        assert spec["language"] == "python"
        assert spec["runtime_version"] == "3.11"
        assert spec["python_version"] == "3.11"
        assert spec["install"] == ["pip install -e ."]
        assert spec["test_cmd_base"] == "pytest"
        assert spec["module_name"] == "owner_repo"
        assert spec["parser"] == "junit"
        assert spec["strategy_name"] == "setuptools:pytest"

    def test_defaults_when_plan_is_sparse(self):
        spec = build_adapter_spec("owner/repo", {})
        assert spec["python_version"] == "3.11"
        assert spec["language"] == "python"
        assert spec["runtime_version"] == "3.11"
        assert spec["install"] == ["pip install -e ."]
        assert spec["test_cmd_base"] == "python -m pytest"
        assert spec["parser"] == "junit"


class TestGenerateAdapter:
    def test_writes_specs_json_and_adapter_py(self, tmp_path):
        plan = {
            "python_version": "3.12",
            "language": "python",
            "runtime_version": "3.12",
            "install": ["poetry install"],
            "test_cmd_base": "pytest",
            "strategy_name": "poetry:pytest",
        }
        result = generate_adapter("myorg/myrepo", plan, out_root=tmp_path)
        assert Path(result["specs_path"]).exists()
        assert Path(result["adapter_path"]).exists()

    def test_specs_json_is_valid_json_with_required_keys(self, tmp_path):
        plan = {
            "python_version": "3.10",
            "language": "python",
            "runtime_version": "3.10",
            "install": ["pip install -e ."],
            "test_cmd_base": "pytest",
        }
        result = generate_adapter("a/b", plan, out_root=tmp_path)
        spec = json.loads(Path(result["specs_path"]).read_text())
        for key in (
            "repo",
            "version",
            "language",
            "runtime_version",
            "python_version",
            "install",
            "test_cmd_base",
            "parser",
            "module_name",
            "docker_specs",
        ):
            assert key in spec, f"missing key: {key}"
        assert spec["parser"] == "junit"

    def test_adapter_py_is_importable_and_get_spec_returns_dict(self, tmp_path):
        plan = {
            "python_version": "3.11",
            "language": "python",
            "runtime_version": "3.11",
            "install": ["pip install -e ."],
            "test_cmd_base": "pytest",
        }
        result = generate_adapter("owner/proj", plan, out_root=tmp_path)
        spec_path = Path(result["adapter_path"])
        mod_spec = importlib.util.spec_from_file_location("adapter_test", spec_path)
        mod = importlib.util.module_from_spec(mod_spec)
        mod_spec.loader.exec_module(mod)
        assert mod.REPO == "owner/proj"
        assert mod.LANGUAGE == "python"
        assert mod.RUNTIME_VERSION == "3.11"
        assert mod.PYTHON_VERSION == "3.11"
        assert mod.PARSER == "junit"
        assert mod.MODULE_NAME == "owner_proj"
        assert mod.MAP_REPO_TO_EXT["owner/proj"] == "py"
        context = mod.registration_context()
        assert context["repo"] == "owner/proj"
        assert context["maps"]["repo_to_ext"] == mod.MAP_REPO_TO_EXT
        assert context["maps"]["repo_version_to_specs"] == mod.MAP_REPO_VERSION_TO_SPECS
        assert context["maps"]["repo_to_parser"] == mod.MAP_REPO_TO_PARSER
        adapter_spec = mod.get_spec()
        assert isinstance(adapter_spec, dict)
        assert adapter_spec["repo"] == "owner/proj"
        assert adapter_spec["module_name"] == "owner_proj"
        assert adapter_spec["version"] == "0.0.0"
        assert adapter_spec["language"] == "python"
        assert adapter_spec["runtime_version"] == "3.11"

    def test_adapter_registration_maps_are_stable(self, tmp_path):
        plan = {
            "python_version": "3.11",
            "install": ["pip install -e ."],
            "test_cmd_base": "python -m pytest",
            "version": "v1",
        }
        result = generate_adapter("org/my-repo.v2", plan, out_root=tmp_path)
        spec_path = Path(result["adapter_path"])
        mod_spec = importlib.util.spec_from_file_location("adapter_stable", spec_path)
        mod = importlib.util.module_from_spec(mod_spec)
        mod_spec.loader.exec_module(mod)

        assert "org/my-repo.v2" in mod.MAP_REPO_TO_EXT
        assert "org/my-repo.v2" in mod.MAP_REPO_TO_PARSER
        assert isfunction(mod.MAP_REPO_TO_PARSER["org/my-repo.v2"])
        assert mod.MAP_REPO_TO_PARSER["org/my-repo.v2"] is parse_repogauge_junit
        assert (
            mod.MAP_REPO_VERSION_TO_SPECS["org/my-repo.v2"]["v1"]["parser"] == "junit"
        )

    def test_generation_is_deterministic(self, tmp_path):
        plan = {
            "python_version": "3.11",
            "install": ["pip install -e ."],
            "test_cmd_base": "pytest",
        }
        r1 = generate_adapter("x/y", plan, out_root=tmp_path / "a")
        r2 = generate_adapter("x/y", plan, out_root=tmp_path / "b")
        assert Path(r1["specs_path"]).read_text() == Path(r2["specs_path"]).read_text()
        assert (
            Path(r1["adapter_path"]).read_text() == Path(r2["adapter_path"]).read_text()
        )

    def test_repo_slug_with_special_chars_produces_valid_filename(self, tmp_path):
        plan = {"python_version": "3.11", "install": [], "test_cmd_base": "pytest"}
        result = generate_adapter("org/my-repo.v2", plan, out_root=tmp_path)
        adapter_path = Path(result["adapter_path"])
        assert adapter_path.exists()
        # Filename must be a valid Python identifier base
        assert adapter_path.name.startswith("adapter_")
        assert adapter_path.suffix == ".py"
