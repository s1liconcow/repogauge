"""Repo/instance adapter generation for SWE-bench evaluator integration (bead q5b).

Generates two artifacts in the output directory:
  specs.json    -- serialised environment spec consumed by the eval runtime
  adapter.py    -- a tiny Python module that registers the repo with the harness

Both are produced deterministically from the repo profile produced by `mine`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Mapping

from repogauge.lang import find_adapter


# ---------------------------------------------------------------------------
# Spec serialisation
# ---------------------------------------------------------------------------


def _safe_module_name(repo: str) -> str:
    """Turn ``owner/repo`` into a valid Python identifier ``owner__repo``."""
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", repo or "")
    if not sanitized:
        return "_repogauge_repo"
    if not re.match(r"[A-Za-z_]", sanitized):
        sanitized = f"_{sanitized}"
    return sanitized


_GO_PATCH_DEFAULTS = {
    "1.18": "1.18.10",
    "1.19": "1.19.13",
    "1.20": "1.20.14",
    "1.21": "1.21.13",
    "1.22": "1.22.12",
    "1.23": "1.23.8",
}


def _normalize_go_version(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return _GO_PATCH_DEFAULTS["1.22"]
    if value.count(".") >= 2:
        return value
    return _GO_PATCH_DEFAULTS.get(value, value)


def _default_docker_specs(
    *, language: str, runtime_version: str, python_version: str
) -> Dict[str, Any]:
    if language == "python":
        return {"python_version": python_version}
    if language == "go":
        return {"go_version": _normalize_go_version(runtime_version)}
    if language == "javascript":
        return {
            "node_version": runtime_version or "20",
            "python_version": python_version,
        }
    if language == "java":
        return {"java_version": runtime_version or "17"}
    if language == "rust":
        return {"rust_version": runtime_version or "stable"}
    return {"python_version": python_version}


def build_adapter_spec(
    repo_name: str, environment_plan: Dict[str, Any]
) -> Dict[str, Any]:
    """Build a serialisable adapter spec dict from a repo name and env plan."""
    python_version = str(environment_plan.get("python_version", "3.11") or "3.11")
    language = str(environment_plan.get("language", "python") or "python")
    runtime_version_value = environment_plan.get("runtime_version")
    runtime_version = (
        str(runtime_version_value)
        if runtime_version_value not in (None, "")
        else (python_version if language == "python" else "")
    )
    docker_specs = _default_docker_specs(
        language=language,
        runtime_version=runtime_version,
        python_version=python_version,
    )
    explicit_docker_specs = environment_plan.get("docker_specs")
    if isinstance(explicit_docker_specs, Mapping):
        docker_specs.update(dict(explicit_docker_specs))
    pre_install = environment_plan.get("pre_install", [])
    install = environment_plan.get("install", ["pip install -e ."])
    build = environment_plan.get("build", [])
    test_cmd_base = environment_plan.get("test_cmd_base", "python -m pytest")
    strategy_name = environment_plan.get("strategy_name", "")
    spec = {
        "repo": repo_name,
        "version": str(environment_plan.get("version", "0.0.0")),
        "language": language,
        "runtime_version": runtime_version,
        "python_version": python_version,
        "pre_install": pre_install,
        "install": install,
        "build": build,
        "test_cmd_base": test_cmd_base,
        "strategy_name": strategy_name,
        "parser": "junit",
        "docker_specs": docker_specs,
        "module_name": _safe_module_name(repo_name),
    }
    adapter = find_adapter(language)
    template_context = dict(spec)
    for key in ("language_hints", "test_runner_hints"):
        value = environment_plan.get(key)
        if isinstance(value, Mapping):
            template_context[key] = dict(value)

    template_vars = adapter.harness_template_vars(template_context)
    if not isinstance(template_vars, Mapping):
        raise TypeError("language adapter harness_template_vars() must return a mapping")

    parser_name = str(template_vars.get("parser_name", spec["parser"]) or spec["parser"])
    parser_import = str(template_vars.get("parser_import", "") or "")
    parser_import_module = str(template_vars.get("parser_import_module", "") or "")
    parser_import_name = str(template_vars.get("parser_import_name", "") or "")
    if parser_import and (not parser_import_module or not parser_import_name):
        if "." in parser_import:
            parser_import_module, parser_import_name = parser_import.rsplit(".", 1)
        else:
            parser_import_name = parser_import
    if not parser_import and parser_import_module and parser_import_name:
        parser_import = f"{parser_import_module}.{parser_import_name}"
    ext = str(template_vars.get("ext", "py") or "py")
    install_str_join = str(template_vars.get("install_str_join", " && ") or " && ")

    spec.update(
        {
            "parser": parser_name,
            "parser_import": parser_import,
            "parser_import_module": parser_import_module,
            "parser_import_name": parser_import_name,
            "ext": ext,
            "install_str_join": install_str_join,
        }
    )

    return spec


# ---------------------------------------------------------------------------
# Code generation
# ---------------------------------------------------------------------------


_ADAPTER_TEMPLATE = '''\
# AUTO-GENERATED by repogauge -- do not edit by hand.
# Re-generate with: repogauge export ...
"""Harness registration adapter for {repo}."""

from __future__ import annotations

from {parser_import_module} import {parser_import_name}

REPO = {repo_repr}
VERSION = {version_repr}
MODULE_NAME = {module_name_repr}
LANGUAGE = {language_repr}
RUNTIME_VERSION = {runtime_version_repr}
PYTHON_VERSION = {python_version_repr}
PRE_INSTALL = {pre_install_repr}
INSTALL = {install_repr}
BUILD = {build_repr}
TEST_CMD_BASE = {test_cmd_base_repr}
PARSER = {parser_name_json}
STRATEGY_NAME = {strategy_name_repr}
DOCKER_SPECS = {docker_specs_repr}

MAP_REPO_TO_EXT = {map_repo_to_ext_repr}
MAP_REPO_VERSION_TO_SPECS = {map_repo_version_specs_repr}
MAP_REPO_TO_PARSER = {{REPO: {parser_import_name}}}


def get_spec() -> dict:
    """Return the environment spec dict for this repo."""
    return {{
        "repo": REPO,
        "version": VERSION,
        "module_name": MODULE_NAME,
        "language": LANGUAGE,
        "runtime_version": RUNTIME_VERSION,
        "python_version": PYTHON_VERSION,
        "pre_install": PRE_INSTALL,
        "install": INSTALL,
        "build": BUILD,
        "test_cmd_base": TEST_CMD_BASE,
        "parser": PARSER,
        "strategy_name": STRATEGY_NAME,
        "docker_specs": DOCKER_SPECS,
    }}


def registration_context() -> dict:
    """Return the harness registration payload this adapter contributes."""
    return {{
        "repo": REPO,
        "version": VERSION,
        "module_name": MODULE_NAME,
        "maps": {{
            "repo_to_ext": MAP_REPO_TO_EXT,
            "repo_version_to_specs": MAP_REPO_VERSION_TO_SPECS,
            "repo_to_parser": MAP_REPO_TO_PARSER,
        }},
    }}
'''


def _swebench_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Convert our internal spec to the key names swebench 4.x expects."""
    language = str(spec.get("language", "python") or "python").lower()
    install_cmds = spec.get("install", [])
    build_cmds = spec.get("build", [])
    install_str_join = spec.get("install_str_join", " && ")
    install_value: str | list[str]
    if isinstance(install_cmds, list):
        if language == "python":
            install_value = (
                install_str_join.join(install_cmds) if install_cmds else "pip install -e ."
            )
        else:
            install_value = list(install_cmds)
    else:
        install_value = install_cmds or "pip install -e ."

    pre_install = list(spec.get("pre_install", []))
    # Ensure uv is available in the conda environment when the install uses it.
    install_text = (
        install_value
        if isinstance(install_value, str)
        else "\n".join(str(item) for item in install_value)
    )
    if "uv" in install_text and "pip install uv" not in pre_install:
        pre_install = ["pip install uv"] + pre_install

    return {
        "python": spec["python_version"],
        "python_version": spec["python_version"],
        "language": spec["language"],
        "runtime_version": spec["runtime_version"],
        "pre_install": pre_install,
        "install": install_value,
        "test_cmd": spec["test_cmd_base"],
        "test_cmd_base": spec["test_cmd_base"],
        "build": list(build_cmds) if isinstance(build_cmds, list) else build_cmds,
        "parser": spec["parser"],
        "strategy_name": spec["strategy_name"],
        "docker_specs": spec["docker_specs"],
    }


def _render_adapter(spec: Dict[str, Any]) -> str:
    parser_import = str(spec.get("parser_import", "") or "")
    if not parser_import:
        parser_import = "repogauge.parsers.junit.parse_repogauge_junit"
    parser_import_module = str(spec.get("parser_import_module", "") or "")
    parser_import_name = str(spec.get("parser_import_name", "") or "")
    if not parser_import_module or not parser_import_name:
        if "." in parser_import:
            parser_import_module, parser_import_name = parser_import.rsplit(".", 1)
        else:
            parser_import_module = "repogauge.parsers.junit"
            parser_import_name = parser_import or "parse_repogauge_junit"
    return _ADAPTER_TEMPLATE.format(
        repo=spec["repo"],
        repo_repr=repr(spec["repo"]),
        version_repr=repr(spec["version"]),
        module_name_repr=repr(spec["module_name"]),
        language_repr=repr(spec["language"]),
        runtime_version_repr=repr(spec["runtime_version"]),
        python_version_repr=repr(spec["python_version"]),
        pre_install_repr=repr(spec["pre_install"]),
        install_repr=repr(spec["install"]),
        build_repr=repr(spec["build"]),
        test_cmd_base_repr=repr(spec["test_cmd_base"]),
        parser_import_module=parser_import_module,
        parser_import_name=parser_import_name,
        parser_name_json=json.dumps(spec["parser"]),
        strategy_name_repr=repr(spec["strategy_name"]),
        docker_specs_repr=repr(spec["docker_specs"]),
        map_repo_to_ext_repr=repr({spec["repo"]: spec["ext"]}),
        map_repo_version_specs_repr=repr(
            {
                spec["repo"]: {
                    spec["version"]: _swebench_spec(spec),
                }
            }
        ),
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def generate_adapter(
    repo_name: str,
    environment_plan: Dict[str, Any],
    *,
    out_root: Path,
) -> Dict[str, str]:
    """Write ``specs.json`` and ``adapter.py`` under *out_root*.

    Returns a dict with keys ``specs_path`` and ``adapter_path``.
    """
    out_root.mkdir(parents=True, exist_ok=True)
    spec = build_adapter_spec(repo_name, environment_plan)

    specs_path = out_root / "specs.json"
    specs_path.write_text(
        json.dumps(spec, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    adapter_path = out_root / f"adapter_{spec['module_name']}.py"
    adapter_path.write_text(_render_adapter(spec), encoding="utf-8")

    return {
        "specs_path": str(specs_path),
        "adapter_path": str(adapter_path),
        "repo": repo_name,
    }
