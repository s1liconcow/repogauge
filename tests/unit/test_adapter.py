"""Tests for adapter/spec generation (bead q5b)."""

import json
import importlib
import importlib.util
from pathlib import Path
from inspect import isfunction

from repogauge.lang import DetectionResult, FileRoleRules, register_adapter
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

    def test_populates_language_specific_docker_specs(self):
        go_spec = build_adapter_spec(
            "owner/go-repo",
            {
                "language": "go",
                "runtime_version": "1.22",
                "python_version": "3.11",
            },
        )
        js_spec = build_adapter_spec(
            "owner/js-repo",
            {
                "language": "javascript",
                "runtime_version": "20",
                "python_version": "3.11",
            },
        )
        java_spec = build_adapter_spec(
            "owner/java-repo",
            {
                "language": "java",
                "runtime_version": "21",
                "python_version": "3.11",
            },
        )
        rust_spec = build_adapter_spec(
            "owner/rust-repo",
            {
                "language": "rust",
                "runtime_version": "1.74",
                "python_version": "3.11",
            },
        )

        assert go_spec["docker_specs"] == {"go_version": "1.22.12"}
        assert js_spec["docker_specs"] == {
            "node_version": "20",
            "python_version": "3.11",
        }
        assert java_spec["docker_specs"] == {"java_version": "21"}
        assert rust_spec["docker_specs"] == {"rust_version": "1.74"}

    def test_explicit_docker_specs_override_defaults(self):
        spec = build_adapter_spec(
            "owner/go-repo",
            {
                "language": "go",
                "runtime_version": "1.22",
                "python_version": "3.11",
                "docker_specs": {"go_version": "1.22.99"},
            },
        )

        assert spec["docker_specs"] == {"go_version": "1.22.99"}

    def test_non_python_swebench_spec_preserves_install_list(self, tmp_path):
        plan = {
            "language": "go",
            "python_version": "3.11",
            "runtime_version": "1.22",
            "install": ["go mod download"],
            "build": ["go test ./..."],
            "test_cmd_base": "go test -json ./...",
        }
        result = generate_adapter("owner/go-repo", plan, out_root=tmp_path)

        mod_spec = importlib.util.spec_from_file_location(
            "go_adapter_test", Path(result["adapter_path"])
        )
        mod = importlib.util.module_from_spec(mod_spec)
        mod_spec.loader.exec_module(mod)

        swebench_spec = mod.MAP_REPO_VERSION_TO_SPECS["owner/go-repo"]["0.0.0"]
        assert swebench_spec["install"] == ["go mod download"]
        assert swebench_spec["build"] == ["go test ./..."]


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

    def test_generate_adapter_uses_language_adapter_template_vars(
        self, tmp_path, monkeypatch
    ):
        parser_module = tmp_path / "custom_parser.py"
        parser_module.write_text(
            "def parse_custom(report, test_spec=None):\n"
            "    return {'parsed': True}\n",
            encoding="utf-8",
        )
        monkeypatch.syspath_prepend(str(tmp_path))

        class CustomAdapter:
            def name(self) -> str:
                return "custom"

            def detect(self, repo_root: Path) -> DetectionResult:
                return DetectionResult(language="custom", confidence=1.0, signals=[])

            def inspect(self, repo_root: Path) -> dict[str, object]:
                return {"language": "custom"}

            def build_env_plan(self, profile: dict[str, object]) -> object:
                return {"language": "custom"}

            def parse_test_output(
                self, report: object, test_spec: object | None
            ) -> dict[str, str]:
                return {}

            def file_role_rules(self):
                return FileRoleRules(set(), [], set(), set(), set())

            def harness_template_vars(self, spec: dict[str, object]) -> dict[str, object]:
                return {
                    "parser_import_module": "custom_parser",
                    "parser_import_name": "parse_custom",
                    "parser_name": "custom",
                    "ext": "go",
                    "install_str_join": " && ",
                }

            def signature_labels(self, profile: dict[str, object]) -> dict[str, str]:
                return {
                    "runtime_label": "custom",
                    "test_label": "custom",
                    "package_label": "custom",
                }

            def dependency_signature_inputs(
                self, repo_root: Path, profile: dict[str, object]
            ) -> list[str]:
                return ["custom"]

            def env_overrides(self, worktree: Path) -> dict[str, str]:
                return {}

            def test_command_attempts(self, test_cmd_base: str) -> list[list[str]]:
                return [[test_cmd_base]]

            def test_report_filename(self) -> str | None:
                return None

            def test_report_glob(self) -> str | None:
                return None

        register_adapter(CustomAdapter())

        plan = {
            "language": "custom",
            "python_version": "3.11",
            "runtime_version": "1.0",
            "install": ["uv sync", "go test"],
            "test_cmd_base": "go test ./...",
            "version": "v9",
        }
        result = generate_adapter("org/custom-repo", plan, out_root=tmp_path)

        spec = json.loads(Path(result["specs_path"]).read_text())
        assert spec["parser"] == "custom"
        assert spec["ext"] == "go"
        assert spec["install_str_join"] == " && "

        mod_spec = importlib.util.spec_from_file_location(
            "custom_adapter_test", Path(result["adapter_path"])
        )
        mod = importlib.util.module_from_spec(mod_spec)
        mod_spec.loader.exec_module(mod)

        custom_parser = importlib.import_module("custom_parser")
        assert mod.PARSER == "custom"
        assert mod.MAP_REPO_TO_EXT["org/custom-repo"] == "go"
        assert mod.MAP_REPO_TO_PARSER["org/custom-repo"] is custom_parser.parse_custom
        assert (
            mod.MAP_REPO_VERSION_TO_SPECS["org/custom-repo"]["v9"]["install"]
            == ["uv sync", "go test"]
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
