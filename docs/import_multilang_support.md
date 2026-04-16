# RepoGauge multi-language support — beads import

Import with: `br create -f .beads/import_multilang_support.md`

The work expands RepoGauge from Python-only to Python + Go + JavaScript/TypeScript + Java + Rust. The plan is staged so the main branch stays releasable: Phase 0 refactors current Python code behind a `LanguageAdapter` registry without changing any user-visible behavior, then each language ships in its own phase as a new adapter that registers with the seam.

Throughout, the existing public surfaces (`repogauge mine|review|export|eval`, `RepoProfile`, `AdapterSpec`, `EnvPlan`, generated `adapter_<repo>.py` files, `dataset.jsonl`) must keep working with no breaking change for Python-only users.

---

## Author ADR for language-adapter registry pattern

### ID mlang-000

### Priority P0

### Type spike

### Labels multi-language, architecture, docs

### Description

- Author `docs/ADRs/0002-language-adapter-registry.md` to record the architectural decision behind multi-language support and lock in the invariants that every later bead must respect.
- Today the codebase enforces "Python-only in v1" as an invariant in `DESIGN.md:38` and `README.md:18-19`. We are explicitly retiring that invariant, but only in favor of a structured pattern, not a free-for-all.
- The pattern: a `LanguageAdapter` Protocol that each supported language implements; a process-wide registry that the CLI consults at every language-sensitive seam (inspect, signature, env plan, file roles, test parsing, harness adapter generation, validate).
- Capture the hard invariants:
  - the adapter registry is the only place language dispatch happens; no module may sniff for `*.py`, `go.mod`, `package.json`, `pom.xml`, etc. outside its adapter;
  - all language-agnostic data structures (`RepoProfile`, `AdapterSpec`, `EnvPlan`) carry an explicit `language` field; back-compat aliases (`python_version`) stay populated for Python repos but are derived, not authoritative;
  - generated `specs.json` and per-repo `adapter_*.py` artifacts for an existing Python repo must be byte-identical pre/post Phase 0 except for new fields;
  - the SWE-bench harness is Python-only; non-Python eval runs through the local `validate.py` worktree path, NOT through swebench's conda/Docker provisioning. This is intentional and must not regress when someone tries to "fix" it later.

### Design

- Place the ADR at `docs/ADRs/0002-language-adapter-registry.md`, following the structure of `docs/ADRs/0001-mvp-architecture.md`.
- Sections required: context, decision, consequences (positive AND negative — duplication across adapters is a known cost), alternatives considered (e.g., a single switch-on-language inside each module — rejected because it spreads dispatch), and a short list of follow-up items that are explicitly out of MVP scope.
- Cross-link the ADR from `DESIGN.md` and `README.md` so reviewers can find it from any entry point.
- Gotcha: keep this ADR purely architectural. Do not inline the full `LanguageAdapter` Protocol signature here — that lives in the contract bead `mlang-001` and the protocol bead `mlang-010`. Inlining the signature now creates two sources of truth that will drift.

### Acceptance Criteria

- ADR exists at `docs/ADRs/0002-language-adapter-registry.md` and is referenced from `DESIGN.md` and `README.md`.
- Reviewers can answer "what stops a future PR from sniffing `pyproject.toml` directly inside `signature.py`?" with "the adapter-registry invariant in ADR-0002."
- The ADR explicitly states that non-Python eval bypasses swebench's docker harness and runs through repogauge's local validate path.
- Every later bead in this set can point back to ADR-0002 for invariant questions.

---

## Define LanguageAdapter contract document

### ID mlang-001

### Priority P0

### Type task

### Labels multi-language, contracts, docs

### Dependencies

- mlang-000

### Description

- Write the canonical `LanguageAdapter` contract document at `docs/language_adapters.md`.
- This document is the source of truth for the Protocol that every language implementation conforms to. Implementers (in mlang-013, mlang-100, mlang-200, mlang-300, mlang-400) read this doc — they should NOT need to reverse-engineer the contract from existing adapter code.
- Cover: every method an adapter must implement; the shape of every input and return value; which fields are authoritative vs. advisory; how detection confidence scoring works; how multi-language repos are recorded; what "back-compat alias" means for Python.
- Standardize the enum-like values used across all adapters: file role names (already defined in `repogauge/mining/file_roles.py`), package-manager strings, test-runner identifiers, harness parser names.

### Design

- Document one canonical contract surface and mirror that contract into:
  - the `Protocol` definition in `repogauge/lang/__init__.py` (mlang-010);
  - per-adapter implementations under `repogauge/lang/<language>.py`;
  - the adapter unit-test fixtures in `tests/unit/test_lang_*.py`.
- Required protocol methods to spec:
  - `name() -> str` — stable identifier ("python", "go", "javascript", "java", "rust").
  - `detect(repo_root: Path) -> DetectionResult` — returns confidence in [0, 1] plus a list of signal strings.
  - `inspect(repo_root: Path) -> dict` — produces the `language_hints` block, package-manager list, test-command list, install hints, runtime versions detected.
  - `build_env_plan(profile: dict) -> EnvPlan` — turns the inspected profile into the install/build/test commands consumed by `validate.py`.
  - `parse_test_output(report: object, test_spec: object | None) -> dict[str, str]` — maps test-runner output to `{test_id: outcome}`.
  - `file_role_rules() -> FileRoleRules` — per-language config_build filenames, prod extensions, test naming patterns, vendor directories.
  - `harness_template_vars(spec: dict) -> dict` — provides parser callable import path, file extension, install command string, test command string for the generated `adapter_<repo>.py`.
  - `signature_labels(profile: dict) -> dict` — provides runtime label, package label, test label for the dataset signature.
- Spec the `DetectionResult` dataclass: `language: str`, `confidence: float`, `signals: list[str]`, `runtime_version: Optional[str]`.
- Spec the `FileRoleRules` dataclass: `prod_extensions: set[str]`, `test_filename_patterns: list[str]`, `test_dir_names: set[str]`, `config_build_filenames: set[str]`, `vendor_dir_names: set[str]`.
- Document the multi-language tie-break rule: highest confidence wins; on equal confidence pick the lexicographically smaller `name()` to stay deterministic.
- Document the back-compat aliasing rule for Python: when `language == "python"`, the Python adapter populates legacy keys (`python_hints`, `python_version`) on the profile in addition to `language_hints` and `language_version`.
- Gotcha: the contract must NOT leak transport assumptions (e.g., "the harness will call you over JSON-RPC"). Adapters are pure in-process Python objects.

### Acceptance Criteria

- `docs/language_adapters.md` exists and fully specifies the Protocol surface, dataclasses, and tie-break rules.
- The doc is referenced from `docs/ADRs/0002-language-adapter-registry.md`.
- A junior engineer reading only this document can implement a new adapter without consulting any existing adapter source file.
- The doc explicitly lists the back-compat aliasing rules for Python.

---

## Create repogauge/lang package with Protocol and registry

### ID mlang-010

### Priority P0

### Type task

### Labels multi-language, phase-0, refactor

### Dependencies

- mlang-001

### Description

- Create the new package `repogauge/lang/` with `__init__.py` that defines the `LanguageAdapter` Protocol and the process-wide registry.
- This bead lands the seam without using it. Other Phase 0 beads (mlang-014 through mlang-019) flip individual modules onto the registry once the seam exists.
- No adapter implementations are added in this bead — only the Protocol, dataclasses, and registry plumbing.

### Design

- New file: `repogauge/lang/__init__.py`. Exports:
  - `class LanguageAdapter(Protocol)` — methods exactly as specified in `docs/language_adapters.md` (mlang-001).
  - `@dataclass class DetectionResult` — `language: str, confidence: float, signals: list[str], runtime_version: Optional[str] = None`.
  - `@dataclass class FileRoleRules` — fields per the contract doc.
  - `register_adapter(adapter: LanguageAdapter) -> None` — appends to a private module-level list; rejects duplicates by `name()`.
  - `find_adapter(name: str) -> LanguageAdapter` — raises `KeyError` if unknown.
  - `iter_adapters() -> Iterable[LanguageAdapter]` — deterministic order (registration order).
  - `detect_language(repo_root: Path) -> DetectionResult` — runs every registered adapter's `detect()`, returns the winner per the tie-break rule. If no adapter has confidence > 0, returns `DetectionResult(language="unknown", confidence=0.0, signals=[])`.
- Use `typing.Protocol` (PEP 544) so adapters do NOT need to subclass — duck typing only. This keeps adapters trivially testable with stubs.
- The registry is a module-level mutable list. Adapters are registered at import time of their respective modules. The Python adapter is registered in `mlang-013`.
- Gotcha: do NOT auto-import adapters from `repogauge/lang/__init__.py`. Auto-import creates circular import problems with `config.py` and `mining/inspect.py`. Instead, expose an explicit `_register_builtin_adapters()` helper that is called once from `repogauge/__init__.py` (or lazily from `find_adapter` if the registry is empty).
- Gotcha: the registry MUST be deterministic in iteration order — if two engineers add adapters in different commits the order should be defined by `name()` lexicographic sort, not by which test happened to import first.

### Acceptance Criteria

- `repogauge/lang/__init__.py` exists and exports `LanguageAdapter`, `DetectionResult`, `FileRoleRules`, `register_adapter`, `find_adapter`, `iter_adapters`, `detect_language`.
- Importing `repogauge.lang` does NOT import any concrete adapter (no circular import risk).
- A unit test that registers a fake adapter, calls `detect_language(some_path)`, and asserts the registry returns the fake's `DetectionResult` passes (covered concretely by mlang-021).
- `find_adapter("nonexistent")` raises `KeyError` with a clear message.

---

## Extend RepoProfile and AdapterSpec contracts with language fields

### ID mlang-011

### Priority P0

### Type task

### Labels multi-language, phase-0, contracts

### Dependencies

- mlang-010

### Description

- Add `language` and `language_version` fields to `RepoProfile` in `repogauge/config.py`. Add `language` and `runtime_version` to `AdapterSpec`.
- Preserve existing `python_version` fields on both as back-compat aliases — they continue to be populated when the detected language is Python so legacy consumers (and the existing test suite) keep working.
- Bump `REPOGAUGE_SCHEMA_VERSION` (currently `"0.1.0"` at `repogauge/config.py:11`) to `"0.2.0"` so anyone consuming JSONL artifacts can detect the contract change.

### Design

- File: `repogauge/config.py`.
  - `RepoProfile` (currently at `config.py:44-58`): add `language: Optional[str] = None` and `language_version: Optional[str] = None` fields. Keep `python_version: Optional[str] = None` as-is. The Python adapter will populate both `language_version` and `python_version` to the same value when `language == "python"`.
  - `AdapterSpec` (currently at `config.py:133-141`): add `language: str = "python"` (default for back-compat) and `runtime_version: str = ""`. Existing fields `docker_specs`, `install_cmds`, `test_cmds`, `module_name`, `metadata` stay unchanged.
  - `REPOGAUGE_SCHEMA_VERSION`: bump to `"0.2.0"`.
- All existing `to_dict` / `from_dict` flows in `ContractRecord` keep working — new fields appear in serialized JSON and from_dict accepts them.
- Gotcha: dataclass field ordering. If a new required field is added before existing optional fields it breaks default-value ordering. Add new fields with explicit defaults (`= None` / `= ""`) so order doesn't matter for `dataclass`.
- Gotcha: anyone reading old JSONL must still load. Test that `RepoProfile.from_dict({...old payload without language fields...})` succeeds.

### Acceptance Criteria

- `RepoProfile` and `AdapterSpec` carry the new fields; old fields untouched.
- `REPOGAUGE_SCHEMA_VERSION == "0.2.0"`.
- Loading any existing JSONL fixture from `tests/unit/` into the dataclasses succeeds without error.
- A new unit test confirms a Python repo's `RepoProfile.python_version` equals its `RepoProfile.language_version` when populated by the Python adapter.

---

## Generalize EnvPlan with language and runtime_version

### ID mlang-012

### Priority P0

### Type task

### Labels multi-language, phase-0, refactor

### Dependencies

- mlang-010

### Description

- Refactor `EnvPlan` in `repogauge/validation/env_detect.py` to carry an explicit `language` and `runtime_version`, with `python_version` becoming a property alias for back-compat.
- Convert `build_environment_plan(profile)` into a thin dispatcher that looks up the adapter for `profile["language"]` and delegates to `adapter.build_env_plan(profile)`.
- The current Python-specific helpers (`_choose_python_version`, `_build_test_command`, `_build_install_commands`, `_augment_for_pytest`) MOVE to `repogauge/lang/python.py` (mlang-013). They are not deleted — they are relocated.

### Design

- File: `repogauge/validation/env_detect.py` (currently `env_detect.py:1-240`).
  - `EnvPlan` (currently `env_detect.py:17-32`): add `language: str = "python"` and `runtime_version: str = ""` fields. Keep `python_version: str` field for back-compat. After Python adapter runs, both `runtime_version` and `python_version` are set to the same value when `language == "python"`.
  - Make `python_version` a `field(default="")` instead of required, so non-Python adapters can leave it empty.
  - `build_environment_plan(profile)`: replace the body with `from repogauge.lang import find_adapter; return find_adapter(profile.get("language", "python")).build_env_plan(profile)`.
  - Move `_choose_python_version`, `_build_test_command`, `_build_install_commands`, `_augment_for_pytest`, `_augment_uv_install_for_pytest`, `_SELF_MANAGING_INSTALL_PREFIXES`, `_DEFAULT_PYTHON_VERSION`, `_DEFAULT_TEST_CMD` to `repogauge/lang/python.py` (mlang-013). DO NOT leave duplicate copies.
- Public API of `env_detect.py` is unchanged: `EnvPlan` and `build_environment_plan` are still importable from the same path. Only the internal implementation moved.
- Gotcha: existing tests in `tests/unit/test_env_detect.py` import these private helpers directly. Update those tests to import from `repogauge.lang.python` (covered in mlang-020), or expose the helpers as public on the Python adapter for testing. Prefer the former — private helpers should stay private.
- Gotcha: keep the `EnvPlan.to_dict()` output stable for Python repos. New fields appear, but no key disappears.

### Acceptance Criteria

- `EnvPlan` has `language`, `runtime_version`, and `python_version` fields. For a Python repo, `runtime_version == python_version`.
- `build_environment_plan(profile)` dispatches via the adapter registry.
- `repogauge/validation/env_detect.py` no longer contains any Python-specific package manager / test runner / version logic.
- `tests/unit/test_env_detect.py` still passes (after mlang-020).

---

## Implement Python LanguageAdapter wrapping existing logic

### ID mlang-013

### Priority P0

### Type task

### Labels multi-language, phase-0, python

### Dependencies

- mlang-010
- mlang-011
- mlang-012

### Description

- Create `repogauge/lang/python.py` and implement the `LanguageAdapter` Protocol for Python by relocating existing logic from `mining/inspect.py`, `mining/signature.py`, `validation/env_detect.py`, `parsers/junit.py`, and `mining/file_roles.py`.
- This bead consolidates Python logic into one adapter. Other Phase 0 refactor beads (mlang-014 onward) thin out the original modules so they delegate via the registry.
- Register the Python adapter at module import time so existing CLI flows work end-to-end as soon as the dispatcher refactors land.

### Design

- New file: `repogauge/lang/python.py`. Contents:
  - `class PythonAdapter` with methods conforming to `LanguageAdapter`.
  - `name() -> "python"`.
  - `detect(repo_root)`: confidence 1.0 if `pyproject.toml` exists; 0.9 if `setup.py` or `setup.cfg`; 0.8 if `requirements*.txt`; 0.5 if any `*.py` files in tree but none of the above; else 0.0. Signals list mirrors which manifest matched.
  - `inspect(repo_root)`: relocates the body of the current Python detection in `mining/inspect.py` (`_detect_package_and_install_hints`, `_detect_test_runner_hints`, `_detect_package_version`, `_parse_version_tokens`, `_parse_requires_python`, `_detect_package_style`, `_detect_test_paths`). Returns the `language_hints` block currently called `python_hints`.
  - `build_env_plan(profile)`: relocates `_choose_python_version`, `_build_test_command`, `_build_install_commands`, `_augment_for_pytest` from `validation/env_detect.py`.
  - `parse_test_output(report, test_spec)`: delegates to `repogauge.validation.junit_parser.parse_junit_xml` / `parse_junit_xml_content` and the existing fallback to `swebench.harness.log_parsers.python.parse_log_pytest_v2`. This is the existing `parse_repogauge_junit` logic from `parsers/junit.py:40-95`.
  - `file_role_rules()`: returns the Python rules — `prod_extensions={".py", ".pyi"}`, `test_filename_patterns=["test_*.py", "*_test.py", "*_test_*.py"]`, `test_dir_names={"tests", "test"}`, `config_build_filenames={".github", ".travis.yml", ".circleci/config.yml", "tox.ini", "noxfile.py", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "requirements-dev.txt", "requirements.in", "pipfile", "pipfile.lock"}`, `vendor_dir_names={"__pycache__", ".mypy_cache", ".pytest_cache", "site-packages", "vendor", ".venv", "venv", ".eggs"}`.
  - `harness_template_vars(spec)`: returns `{"parser_import": "repogauge.parsers.junit.parse_repogauge_junit", "parser_name": "junit", "ext": "py", "install_str_join": " && "}`.
  - `signature_labels(profile)`: returns `{"runtime_label": _to_python_label(versions), "test_label": _to_test_label(commands), "package_label": _to_pkg_label(managers)}`. The `_to_python_label` helper currently lives in `mining/signature.py:30-33` and moves here.
- Register via `register_adapter(PythonAdapter())` either at the bottom of `repogauge/lang/python.py` or via the explicit `_register_builtin_adapters()` helper from mlang-010.
- Back-compat: the Python adapter MUST also write the legacy `python_hints` and `python_version` keys onto the profile so any external consumer that hasn't migrated keeps working.
- Gotcha: existing `parse_repogauge_junit` accepts paths, bytes, str (XML or path-on-disk), and Mapping payloads — preserve every code path. The harness can call with surprising input types.
- Gotcha: do not change the deterministic ordering rules in any helper. Signature stability for existing Python repos depends on `_as_sorted_unique` and minimum-version selection in `_choose_python_version`.

### Acceptance Criteria

- `repogauge/lang/python.py` exists, defines `PythonAdapter`, and registers it with the registry on import.
- All Python-specific helpers from `mining/inspect.py`, `mining/signature.py`, `validation/env_detect.py`, `parsers/junit.py` are reachable from this adapter (either moved or re-exported).
- For a fixture Python repo, `PythonAdapter().detect(repo_root)` returns confidence 1.0 with `pyproject.toml` in signals.
- For the same fixture, `PythonAdapter().build_env_plan(profile).to_dict()` is byte-identical to the pre-refactor `build_environment_plan(profile).to_dict()` output.

---

## Refactor mining/inspect.py to dispatch via registry

### ID mlang-014

### Priority P0

### Type task

### Labels multi-language, phase-0, refactor

### Dependencies

- mlang-013

### Description

- Convert `repogauge/mining/inspect.py` from a Python-only inspector into a registry-backed dispatcher.
- Replace the hardcoded `python_hints` key in the produced profile dict with a generic `language_hints` key, while continuing to write `python_hints` as a duplicate alias when the detected language is Python.
- Run all registered adapters' `detect()` methods, store every result under `profile["language_detection"]`, and use the winner to drive `inspect()`.

### Design

- File: `repogauge/mining/inspect.py` (currently `inspect.py:1-409`).
- The big function `inspect_repository(path)` (currently `inspect.py:319-408`) becomes:
  1. Resolve `repo_root`, `default_branch`, `repo_name` (existing helpers stay).
  2. Call `repogauge.lang.detect_language(repo_root)` to pick the primary language.
  3. Call `iter_adapters()` and record each adapter's `detect()` result under `profile["language_detection"]` (a list of `{name, confidence, signals}` dicts).
  4. Look up the winning adapter via `find_adapter(detection.language)` and call its `inspect(repo_root)` to populate `language_hints`, `install_hints`, `test_runner_hints`, `repo_version`.
  5. Compose the profile dict with the winning language's hints PLUS, when language is Python, the legacy `python_hints` alias.
  6. Set `profile["language"] = detection.language` and `profile["language_version"] = detection.runtime_version`.
  7. Continue to call `build_environment_signature(profile)` and `build_environment_plan(profile).to_dict()` exactly as before.
- All Python-specific helper functions (`_detect_package_and_install_hints`, `_detect_test_runner_hints`, `_detect_package_version`, `_parse_version_tokens`, `_parse_requires_python`, `_detect_package_style`, `_detect_test_paths`) are DELETED from `mining/inspect.py` — they live in `repogauge/lang/python.py` after mlang-013.
- Keep these helpers in `mining/inspect.py`: `_to_repo_path`, `_safe_read_text`, `_extract_toml_value`, `_detect_repo_name`, `_detect_ci_files`, `_as_sorted_unique`, `_parse_repo_profile_warnings`. These are language-agnostic infrastructure.
- `_detect_ci_files` stays generic — `.github/workflows`, `.travis.yml`, `.circleci`, `azure-pipelines.yml` apply to any language.
- Gotcha: `tests/unit/test_inspect.py` asserts on the exact profile shape. After this bead, the profile gains `language`, `language_version`, `language_detection`, `language_hints` keys but Python tests asserting on `python_hints` keep passing because the alias is present. mlang-020 updates these tests to assert on the new keys too.
- Gotcha: a repo with both `pyproject.toml` AND `package.json` will have multiple adapter scores. Tie-break per ADR-0002 (highest confidence; deterministic name-sort on equal). Until non-Python adapters land, only Python returns nonzero confidence.

### Acceptance Criteria

- `mining/inspect.py` no longer contains any reference to Python-specific manifests or test runners.
- `inspect_repository(path)` produces a profile with `language`, `language_version`, `language_hints`, `language_detection` keys.
- For a Python repo, `profile["python_hints"] == profile["language_hints"]` (alias holds).
- `tests/unit/test_inspect.py` continues to pass after mlang-020 updates.

---

## Refactor mining/signature.py for runtime-agnostic labels

### ID mlang-015

### Priority P0

### Type task

### Labels multi-language, phase-0, refactor

### Dependencies

- mlang-013
- mlang-014

### Description

- Generalize the dataset signature builder so it works for any language while keeping byte-identical output for existing Python repos.
- The current signature is `{repo_version}__{python_label}__{test_label}__{package_label}__reqhash_{fingerprint}` (`mining/signature.py:146`). Move the language-specific label builders into the language adapter and have `signature.py` delegate.

### Design

- File: `repogauge/mining/signature.py` (currently `signature.py:1-152`).
- `_to_python_label`, `_to_test_label`, `_to_pkg_label` move to the Python adapter (mlang-013). Each adapter exposes `signature_labels(profile)` returning `{runtime_label, test_label, package_label}`.
- `_read_requirements_signature` is Python-specific — move it to the Python adapter and have it called via `adapter.dependency_signature_inputs(repo_root, profile) -> list[str]`. The hash function `_dependency_hash` stays in `signature.py` (language-agnostic).
- `build_environment_signature(profile)`:
  - Look up adapter via `find_adapter(profile.get("language", "python"))`.
  - Call `adapter.signature_labels(profile)` for the three labels.
  - Call `adapter.dependency_signature_inputs(repo_root, profile)` for the requirements bytes that get hashed.
  - Produce signature in the existing format. Critically: for `language == "python"`, the labels and hash inputs MUST match the current implementation byte-for-byte so existing dataset versions don't change.
  - For non-Python repos, the format stays the same but uses that language's labels.
- Test invariant: build the signature for the repogauge-self repo before and after the refactor — the strings must be identical.
- Gotcha: the tests in `tests/unit/test_signature.py` (or wherever the signature tests live) probably hash sample profiles. Verify hashes match exactly.
- Gotcha: `_normalize_dependency_lines` is also Python-friendly (strips `#` comments), but is still useful for `requirements.txt`-style files. Move it with `_read_requirements_signature` to the Python adapter.

### Acceptance Criteria

- `mining/signature.py` contains no Python-specific filename, label, or version logic.
- `build_environment_signature(profile)` for a Python repo produces output byte-identical to the pre-refactor implementation.
- A unit test confirms signature stability against a fixture Python `RepoProfile`.

---

## Parametrize mining/file_roles.py with per-language rules

### ID mlang-016

### Priority P0

### Type task

### Labels multi-language, phase-0, refactor

### Dependencies

- mlang-013
- mlang-014

### Description

- Refactor `repogauge/mining/file_roles.py` so its classification rules are not hardcoded for Python. Each adapter contributes its own `FileRoleRules` (extensions, test patterns, vendor dirs, config_build files), and `classify_file` consults the rules from the active language adapter.
- For a multi-language repo, run classification with the union of all detected languages' rules, not just the primary, so a Python file in a Go-primary repo still gets `prod` not `unknown`.

### Description (continued)

- Preserve current behavior for Python: today's tests in `tests/unit/test_file_roles.py` should keep passing once the Python adapter contributes the existing rules.

### Design

- File: `repogauge/mining/file_roles.py` (currently `file_roles.py:1-183`).
- Change the signature of `classify_file(path)` to `classify_file(path, *, rules: list[FileRoleRules] | None = None)`. When `rules is None`, fall back to a default list containing every registered adapter's `file_role_rules()`. This keeps existing call sites working.
- Replace each hardcoded literal set in `classify_file` with a lookup against the merged rules:
  - line 45-58 vendor/cache dirs → union of `rules[i].vendor_dir_names`.
  - line 65 build dirs (`dist`, `build`, `.eggs`) → these are language-agnostic; keep inline OR move to a base "common" rule set.
  - line 72-87 config_build filenames → union of `rules[i].config_build_filenames`. Generic CI files (`.github/`, `.travis.yml`, `.circleci`, `dockerfile`, `docker-compose.yml`) stay as a base set in `file_roles.py`.
  - line 118-159 test path detection → use any `rules[i].test_dir_names` and `rules[i].test_filename_patterns`. Keep the `conftest.py` / `pytest.ini` / `tox.ini` test-support detection inside the Python adapter's rules (advisory).
  - line 161-166 production extensions → union of `rules[i].prod_extensions`.
- New top-level helper: `def merged_rules() -> FileRoleRules` that consults `iter_adapters()` and returns the union. Memoize per-process; invalidate via a `reset_cache()` for tests.
- Gotcha: rule precedence matters when two adapters classify the same file (e.g., a `.js` file in a primarily-Python repo with a JS adapter). The default `prod` classification works regardless of which adapter "owns" the extension. Only test-file detection needs care: a `*_test.go` should not be classified as `test` if Go isn't registered, otherwise non-Go projects with a stray `_test.go` in fixtures get misclassified. Keep test-pattern matching scoped to detected languages (use `inspect_repository().language_detection` to filter rules).
- Gotcha: callers that import `classify_file(path)` with one positional arg keep working — the new `rules` param is keyword-only with a default.

### Acceptance Criteria

- `mining/file_roles.py` contains no language-specific extension or filename literals beyond a small "generic CI / docs / build artifact" base.
- `tests/unit/test_file_roles.py` still passes (Python adapter contributes rules that match current behavior).
- Adding a new adapter with a `prod_extensions` of `{".go"}` causes `classify_file("foo.go")` to return `role="prod"` automatically without editing `file_roles.py`.

---

## Generalize parsers/junit.py with parser-name dispatcher

### ID mlang-017

### Priority P0

### Type task

### Labels multi-language, phase-0, refactor

### Dependencies

- mlang-013

### Description

- Add a parser-name dispatcher to `repogauge/parsers/junit.py` so the harness can route a test report to the correct adapter's parser based on the spec's `parser` field.
- Keep `parse_repogauge_junit` as a thin back-compat wrapper that hardcodes the Python parser, so existing generated `adapter_*.py` files don't break.

### Design

- File: `repogauge/parsers/junit.py` (currently `junit.py:1-95`).
- New entry point: `def parse_repogauge_test_output(report, test_spec=None, *, parser_name: str = "junit") -> dict[str, str]`.
  - If `parser_name == "junit"`, behave exactly as the current `parse_repogauge_junit` (Python pytest path).
  - Otherwise, look up the adapter that owns this parser via a small registry keyed by parser name (each adapter declares its parser names via `harness_template_vars()`).
- Existing `parse_repogauge_junit(report, test_spec=None)` stays — implement as `return parse_repogauge_test_output(report, test_spec, parser_name="junit")`.
- Add `_PARSER_REGISTRY: dict[str, Callable[[object, object | None], dict[str, str]]]` populated by adapters at import time (similar to `register_adapter`). Provide `register_parser(name, fn)` and `get_parser(name)`.
- The Python adapter (mlang-013) registers parser name `"junit"` pointing at the existing `parse_repogauge_junit` body.
- Gotcha: the import `from swebench.harness.log_parsers.python import parse_log_pytest_v2` (`junit.py:8`) is Python-specific and stays inside the Python adapter's parser only. Do not import it at the top of `parsers/junit.py` after refactor — make it lazy so a JS-only project can still import `repogauge.parsers.junit` without dragging in swebench's Python parsers.
- Gotcha: existing generated `adapter_*.py` files import `parse_repogauge_junit` directly. That import must keep working forever. Do not rename or remove it.

### Acceptance Criteria

- `parse_repogauge_test_output(report, parser_name="junit")` produces the same output as `parse_repogauge_junit(report)` for any input.
- Calling `parse_repogauge_test_output(report, parser_name="unknown")` raises `KeyError` with a clear message.
- Importing `repogauge.parsers.junit` does not eagerly import `swebench.harness.log_parsers.python`.
- `tests/unit/test_junit_parser.py` and `tests/unit/test_parsers_junit.py` continue to pass.

---

## Refactor export/adapter.py to use language template vars

### ID mlang-018

### Priority P0

### Type task

### Labels multi-language, phase-0, refactor

### Dependencies

- mlang-011
- mlang-013
- mlang-017

### Description

- Generalize the harness adapter generator in `repogauge/export/adapter.py` so the generated `adapter_<repo>.py` references the right parser, file extension, install command, and test command for the repo's language.
- The generated file remains a Python module (the harness consumes Python), but its template variables come from the language adapter rather than being hardcoded.

### Design

- File: `repogauge/export/adapter.py` (currently `adapter.py:1-207`).
- `build_adapter_spec(repo_name, environment_plan)` (currently `adapter.py:33-56`):
  - Read `language` from `environment_plan` (default `"python"` for back-compat).
  - Pull template-relevant fields from `find_adapter(language).harness_template_vars(environment_plan)`: `parser_import`, `parser_name`, `ext`.
  - Replace hardcoded `"parser": "junit"` (line 53) with `parser_name` from the adapter.
  - Add `language` and `runtime_version` fields to the returned spec dict.
  - Keep `python_version` and `docker_specs.python_version` populated for Python repos (back-compat); for non-Python set to `None` and let downstream skip them.
- `_swebench_spec(spec)` (currently `adapter.py:122-146`):
  - For `language == "python"`, behavior unchanged — still emits `python`, `python_version`, swebench-style keys.
  - For non-Python languages: emit a generic dict with `language`, `runtime_version`, `pre_install`, `install`, `test_cmd`, `build`, `parser`, `strategy_name`, `docker_specs` (no `python` key).
  - The "ensure uv is available" branch (line 132-133) is Python-specific — wrap in `if language == "python":`.
- `_render_adapter(spec)` (currently `adapter.py:149-173`):
  - Pull `parser_import`, `ext`, `install_str_join` from the language adapter via `harness_template_vars`.
  - Replace the hardcoded `from repogauge.parsers.junit import parse_repogauge_junit` line in the template (currently `adapter.py:71`) with a dynamic import statement based on `parser_import`.
  - Replace the hardcoded `MAP_REPO_TO_EXT = {repo: "py"}` (line 164) with `{repo: ext}` from the adapter.
  - Replace `MAP_REPO_TO_PARSER = {repo: parse_repogauge_junit}` (line 151, 172) with a name keyed at the parser identifier.
- `_ADAPTER_TEMPLATE` (currently `adapter.py:64-119`): change the `from repogauge.parsers.junit import parse_repogauge_junit` line to a `{parser_import_line}` slot. The Python adapter's `parser_import_line` is the existing string.
- Gotcha: the generated file is committed to user output directories. Python repos must produce a byte-identical `adapter_<repo>.py` after this refactor. Diff before/after as part of acceptance.
- Gotcha: when `parser_import` is `repogauge.parsers.junit.parse_repogauge_junit`, the rendering must split that into `from repogauge.parsers.junit import parse_repogauge_junit` — provide a small helper that converts dotted-path → import statement and a usable callable name.

### Acceptance Criteria

- For an existing Python repo, the generated `adapter_<repo>.py` file is byte-identical to the pre-refactor output (covered by mlang-022).
- `_swebench_spec(spec)` for `language="python"` produces the same dict shape as before.
- A future non-Python language can be supported by changing only `harness_template_vars()` in its adapter, with no edits to `export/adapter.py`.
- `tests/unit/test_adapter.py` continues to pass after mlang-020.

---

## Refactor validation/validate.py to drop pytest hardcoding

### ID mlang-019

### Priority P0

### Type task

### Labels multi-language, phase-0, refactor

### Dependencies

- mlang-013
- mlang-017

### Description

- Generalize `repogauge/validation/validate.py` so it uses the language adapter to derive command attempts, parse test output, and contribute environment variables — instead of hardcoding pytest, JUnit XML paths, and `PYTHONPATH`.
- Behavior for Python repos must be unchanged — current pytest fallbacks (`pytest` → `python -m pytest` → swebench parser) keep working.

### Design

- File: `repogauge/validation/validate.py` (currently `validate.py` ~600 lines).
- Rename the helpers (keep wrappers to avoid breaking imports during transition):
  - `_pytest_command_attempts(test_cmd_base)` → `_test_command_attempts(test_cmd_base, *, adapter)`. The adapter contributes its own retry strategy via a new Protocol method `test_command_attempts(test_cmd_base) -> list[list[str]]`.
  - `_run_pytest(...)` → `_run_test(...)`. Same signature minus the implicit Python assumption. Calls `adapter.parse_test_output(junit_xml_path, test_spec)` instead of `parse_junit_xml` directly.
  - `PytestExecutionError` → `TestExecutionError`. Keep `PytestExecutionError = TestExecutionError` as a back-compat alias.
- `env` injection: today line 150 is `env = {**os.environ, "PYTHONPATH": str(worktree)}`. Make this `env = {**os.environ, **adapter.env_overrides(worktree)}`. The Python adapter returns `{"PYTHONPATH": str(worktree)}`; Go/JS/Java/Rust adapters return `{}` or language-appropriate vars.
- Add a new optional Protocol method `env_overrides(worktree: Path) -> dict[str, str]` to the contract (mlang-001). Default implementation returns `{}`.
- Add a new optional Protocol method `test_command_attempts(test_cmd_base: str) -> list[list[str]]`. Default implementation: `[shlex.split(test_cmd_base)]`. Python adapter overrides to also try `[sys.executable, "-m", "pytest"]` as a fallback when `pytest` is not on PATH (existing behavior).
- The JUnit XML output path (`junit_xml`) is also Python-specific in name. Rename internally to `test_report_path`. The adapter declares its expected report filename (e.g., `report.xml` for JUnit, `report.json` for `go test -json`) via a new `test_report_filename` property.
- Gotcha: this is the highest-risk refactor in Phase 0 because validate.py is the heart of `eval --gold`. Land with extensive test coverage. Keep all retry / flake / B/C/D pass logic identical — only the test-runner shell-out and parser dispatch change.
- Gotcha: `_resolve_test_cmd` (line 77) replaces `python` tokens with `sys.executable`. Move this normalization into the Python adapter's `test_command_attempts`. Other languages don't need it.

### Acceptance Criteria

- `validation/validate.py` contains no string literal `"pytest"` or `"PYTHONPATH"` outside of a Python-specific code path that's clearly gated by language.
- `tests/e2e/test_self_gauge.py` (gold-patch eval against repogauge itself) passes unchanged.
- `tests/unit/test_validate.py` (or equivalent) passes after mlang-020 updates.
- `PytestExecutionError` import path still works for any consumer.

---

## Update existing unit tests for back-compat shape

### ID mlang-020

### Priority P0

### Type task

### Labels multi-language, phase-0, tests

### Dependencies

- mlang-014
- mlang-015
- mlang-016
- mlang-017
- mlang-018
- mlang-019

### Description

- Update the existing unit-test suite to assert on the new profile/spec shapes (with `language`, `language_version`, `language_hints`) while keeping coverage of the legacy aliases (`python_hints`, `python_version`).
- Where helpers were moved from `mining/inspect.py` and `validation/env_detect.py` into `repogauge/lang/python.py`, update imports.

### Design

- Files likely to need updates (run `rg "python_hints\b|python_version\b|_choose_python|_build_test_command|_build_install_commands|parse_repogauge_junit|_pytest_command_attempts|PytestExecutionError|_run_pytest" tests/` to find them):
  - `tests/unit/test_inspect.py` — assert on `language_hints` AND `python_hints`; assert `profile["language"] == "python"`.
  - `tests/unit/test_env_detect.py` — re-import `_choose_python_version` etc. from `repogauge.lang.python`.
  - `tests/unit/test_signature.py` (if it exists) — confirm signatures byte-identical for fixture profiles.
  - `tests/unit/test_file_roles.py` — confirm classification still works; add a fake-adapter test that adds `.foo` extension and asserts new classification.
  - `tests/unit/test_junit_parser.py`, `tests/unit/test_parsers_junit.py` — call new `parse_repogauge_test_output(parser_name="junit")` and assert identical results to `parse_repogauge_junit`.
  - `tests/unit/test_adapter.py` — assert `spec["language"] == "python"`; assert generated adapter file diff is empty for fixture (use a golden file in `tests/fixtures/` if not present).
  - `tests/unit/test_analyze_metrics.py`, `tests/unit/test_report_generation.py` — already modified per `git status`, ensure compatibility.
- Add a new helper `tests/conftest.py` fixture that resets the language registry between tests, so test-only adapters don't leak across modules.
- Gotcha: the e2e test `tests/e2e/test_self_gauge.py` writes artifacts to a temp dir and asserts on file existence. Probably no changes needed, but run it before claiming done.
- Gotcha: avoid weakening tests. If a test asserts on a specific dict shape, add the new keys to the assertion — do not change `==` to `>=`.

### Acceptance Criteria

- `uv run python -m pytest tests/unit -v` exits 0.
- `uv run python -m pytest tests/e2e/test_self_gauge.py -v` exits 0.
- No test was deleted or marked `xfail` to make this pass; only updated.

---

## Add registry routing smoke test

### ID mlang-021

### Priority P1

### Type task

### Labels multi-language, phase-0, tests

### Dependencies

- mlang-010
- mlang-013

### Description

- Add a small dedicated unit test that exercises the language registry end-to-end: register a fake adapter, call `detect_language()`, verify routing, then call `find_adapter(name)` and verify methods work.
- This test is the canary for "the seam works." It should not depend on any real repo fixture.

### Design

- New file: `tests/unit/test_lang_registry.py`.
- Test cases:
  1. `iter_adapters()` includes `PythonAdapter` after `repogauge.lang.python` is imported.
  2. Register a `FakeAdapter` whose `detect()` returns confidence 0.95 and `name == "fake"`. Pass a tmp_path with a sentinel marker file. Confirm `detect_language(tmp_path).language == "fake"`.
  3. Register two adapters with equal confidence and confirm the lexicographic tie-break.
  4. `find_adapter("python")` returns the `PythonAdapter` instance.
  5. `find_adapter("nonexistent")` raises `KeyError`.
  6. After test teardown, the fake adapter is removed from the registry (use the `tests/conftest.py` reset fixture from mlang-020).
- Use `pytest`'s `monkeypatch` or a custom fixture that captures the registry list, replaces it for the test, and restores after.
- Gotcha: don't accidentally import `repogauge.lang.python` only when this test runs first — assert importability is explicit.

### Acceptance Criteria

- `tests/unit/test_lang_registry.py` exists with the six test cases above and passes.
- The test file is independent: it does not import any real repo fixture.

---

## Verify byte-compat of self-gauge artifacts

### ID mlang-022

### Priority P0

### Type task

### Labels multi-language, phase-0, validation

### Dependencies

- mlang-020
- mlang-021

### Description

- Final Phase 0 gate. Run the full mine→review→export pipeline against the repogauge repository itself and confirm the produced `specs.json`, `dataset.jsonl`, and `adapter_<repo>.py` are byte-identical to the pre-refactor outputs except for the new fields (`language: "python"`, `language_version`, `runtime_version`).
- Capture before/after artifacts in a script under `scripts/` so this check can be re-run by reviewers.

### Design

- Capture pre-refactor outputs once (before Phase 0 starts): run `scripts/gauge_self.sh` against `main` HEAD, copy `out/export/dataset/dataset.jsonl`, `out/export/specs.json`, `out/export/adapter_*.py` into `tests/fixtures/golden_self_gauge_v0_1_0/`.
- Add a script `scripts/diff_self_gauge.sh` that:
  1. Runs `scripts/gauge_self.sh` with a clean output dir.
  2. Diffs each generated artifact against the golden fixture.
  3. Reports allowed differences (only `language`, `language_version`, `runtime_version` keys present in JSON, no removed keys, no value changes for any other key).
  4. Exits nonzero on any unexpected diff.
- Add an e2e test `tests/e2e/test_phase0_back_compat.py` that wraps the script logic with assertions.
- Gotcha: the existing `scripts/gauge_self.sh` may need `--llm-mode off` to avoid network. Inspect before running.
- Gotcha: signatures are sensitive to filesystem ordering. Run the diff on a fresh checkout to avoid local artifacts contaminating signatures.
- Gotcha: timestamps. `RepoProfile.updated_at` (config.py:53-57) is per-run; it must be excluded from byte-comparison or stubbed via a fixed clock in the test.

### Acceptance Criteria

- `tests/fixtures/golden_self_gauge_v0_1_0/` contains the captured pre-refactor artifacts.
- `scripts/diff_self_gauge.sh` exits 0 against the post-refactor build.
- `tests/e2e/test_phase0_back_compat.py` passes.
- The diff report explicitly lists only `language` / `language_version` / `runtime_version` as new keys.

---

## Update README.md to drop Python-only framing

### ID mlang-023

### Priority P1

### Type task

### Labels multi-language, phase-0, docs

### Dependencies

- mlang-022

### Description

- Rewrite the "v1 Scope and non-goals" section of `README.md` to reflect multi-language support.
- Update the quickstart and example sections so they don't read as Python-only.

### Design

- File: `README.md`.
- Section to rewrite: "v1 Scope and non-goals" (`README.md:3-22`).
  - Remove "Python-only, local-first CLI" framing.
  - Remove "Multi-language generality" from the v1-is-not list.
  - Add a short bullet list: "Supported languages: Python, Go, JavaScript/TypeScript, Java, Rust." Note that each language ships in its own phase; a language-status section can link to ADR-0002.
- Section to update: "Release guarantees" (`README.md:24-39`). Add a guarantee that `repogauge mine` and `eval` work across all supported languages with `--llm-mode off`.
- Section to update: "Quickstart" (`README.md:144-156`). Change the "follow the workflow below for a fast offline smoke path" wording to mention that `mine` auto-detects the repo's primary language.
- Reference ADR-0002 from the README.
- Gotcha: the README currently has stage-state language ("Current release state is scaffolded and in active development"). Keep that wording — multi-language support is incremental and may legitimately be stamped as "Go, JS/TS supported; Java, Rust in progress" depending on which phase has shipped.

### Acceptance Criteria

- README no longer states or implies Python-only.
- README links to ADR-0002.
- The Quickstart still reads correctly for a Python repo (no regressions).

---

## Update DESIGN.md to drop Python-only invariant

### ID mlang-024

### Priority P1

### Type task

### Labels multi-language, phase-0, docs

### Dependencies

- mlang-022

### Description

- Update `DESIGN.md` to remove the Python-only invariant and add a "Language adapter registry" subsection describing the new pattern.

### Design

- File: `DESIGN.md`.
- Line `DESIGN.md:5` ("Python-first CLI"): change to "language-aware local CLI."
- Line `DESIGN.md:20` ("Python-only in v1") under "Local-first and scoped": delete.
- `DESIGN.md:38` non-goals list: remove "multi-language support."
- Add a new subsection under "Core architectural principles" titled "Language adapter registry" that summarizes the `LanguageAdapter` Protocol (1 paragraph) and links to ADR-0002 and `docs/language_adapters.md` for details.
- Gotcha: do NOT remove "broad platform orchestration beyond CLI flow" or other unrelated non-goals. Surgical edit only.

### Acceptance Criteria

- DESIGN.md no longer mentions Python-only as an invariant.
- A "Language adapter registry" subsection exists and cross-links to ADR-0002 and `docs/language_adapters.md`.

---

## Go adapter: detection (go.mod, *.go)

### ID mlang-100

### Priority P1

### Type task

### Labels multi-language, phase-1, go

### Dependencies

- mlang-022

### Description

- Implement `detect()` for Go in `repogauge/lang/go.py` — recognize Go repos by `go.mod` at root (highest signal) and `*.go` files in tree (medium signal).

### Design

- New file: `repogauge/lang/go.py`. Stub the full `LanguageAdapter` class with `name() -> "go"`.
- `detect(repo_root)`:
  - confidence 1.0 if `repo_root / "go.mod"` exists. Signal: `"go.mod"`.
  - confidence 0.6 if no `go.mod` but at least one `*.go` file exists below `repo_root` (use `Path.rglob("*.go")` with an early-break after first match, capped at depth 4 to avoid huge trees). Signal: `"go-source"`.
  - 0.0 otherwise.
- Register the adapter at module import time. Add an explicit import to `repogauge/lang/__init__.py`'s `_register_builtin_adapters()`.
- Gotcha: `Path.rglob` is slow on large repos. Use `os.scandir` with a manual recursive walk capped at depth 4 and an early break on first hit. Performance matters because `detect()` runs for every adapter on every `inspect`.
- Gotcha: a Python project that vendors a Go binary may have `*.go` source incidentally. The 0.6 confidence is intentionally lower than Python's 1.0 (`pyproject.toml`) so the tie-break favors the manifest-bearing language.

### Acceptance Criteria

- `GoAdapter.detect()` returns confidence 1.0 for a tmp dir containing `go.mod`.
- Returns 0.6 for a tmp dir with only `*.go` files.
- Returns 0.0 for an empty tmp dir.
- A unit test in `tests/unit/test_lang_go.py` covers each case (added in mlang-106).

---

## Go adapter: inspector (parse go.mod, runtime version)

### ID mlang-101

### Priority P1

### Type task

### Labels multi-language, phase-1, go

### Dependencies

- mlang-100

### Description

- Implement `inspect()` for Go: parse `go.mod` for the module path and the `go 1.X` directive, detect `go.sum` and `vendor/`, return the `language_hints` block.

### Design

- File: `repogauge/lang/go.py`.
- `inspect(repo_root)`:
  - Parse `go.mod`. Use a small line-based parser (no dependency on a Go toolchain). Look for:
    - `module <path>` → store under `language_hints.module_path`.
    - `go <version>` → store under `language_hints.versions = [version]` (may be `1.21`, `1.22.0`, etc. — normalize to major.minor).
    - `require (` block → count, store under `language_hints.require_count` (advisory).
  - Detect `repo_root / "go.sum"` → add `"go.sum"` to `language_hints.signals`.
  - Detect `repo_root / "vendor"` directory → add `"vendor"` to signals. This affects EnvPlan: with vendor, `go mod download` is unnecessary.
  - Default version when `go` directive absent: `"1.22"`.
  - `runtime_version`: the parsed `go` directive or default.
- `repo_version`: scrape from `module github.com/owner/repo/v2` style suffixes if present; otherwise `repogauge.mining.signature.REPO_VERSION_UNKNOWN`.
- Gotcha: `go.mod` can have replace directives, retract directives, comment lines (`//`). Skip them safely.
- Gotcha: do not invoke `go` itself. Adapters must work with the Go toolchain absent (we may run on a CI runner without Go installed). Pure Python parsing only.

### Acceptance Criteria

- For a fixture `go.mod` containing `module example.com/m\n\ngo 1.22\n`, `inspect()` returns `language_hints={"module_path": "example.com/m", "versions": ["1.22"], "signals": [...]}`.
- `runtime_version` equals `"1.22"`.
- Defaults applied correctly when `go` directive is missing.

---

## Go adapter: build_env_plan strategy

### ID mlang-102

### Priority P1

### Type task

### Labels multi-language, phase-1, go

### Dependencies

- mlang-101

### Description

- Implement `build_env_plan()` for Go — produce install/build/test commands suitable for `validate.py` to execute in a worktree.

### Design

- `build_env_plan(profile)`:
  - `install = ["go mod download"]` unless `vendor/` is in signals, in which case `install = []`.
  - `build = []` (no separate compile step; `go test` compiles).
  - `test_cmd_base = "go test -json ./..."`.
  - `pre_install = []`.
  - `runtime_version = profile["language_hints"]["versions"][0]` or `"1.22"`.
  - `language = "go"`.
  - `python_version = ""` (back-compat alias is unused for non-Python).
  - `strategy_name = "go-modules:go-test"`.
  - `confidence = 0.9` (deterministic, no version conflicts to weigh).
- Gotcha: `go test ./...` runs all packages including ones without tests. The `-json` flag wraps each event so empty packages still emit `{"Action":"output"}` events; the parser must tolerate this.
- Gotcha: `go test` writes to a build cache by default (`$GOCACHE`). In CI/worktree contexts, this can either help or pollute. Set `GOCACHE` via the adapter's `env_overrides(worktree)` to a worktree-local cache dir to keep runs hermetic.

### Acceptance Criteria

- `GoAdapter().build_env_plan(profile).to_dict()` returns a plan with the fields above.
- `language == "go"`, `test_cmd_base == "go test -json ./..."`.
- Vendor mode skips `go mod download`.

---

## Go adapter: parse_test_output for `go test -json`

### ID mlang-103

### Priority P1

### Type task

### Labels multi-language, phase-1, go

### Dependencies

- mlang-101

### Description

- Implement `parse_test_output()` that consumes `go test -json` output (newline-delimited JSON events) and returns `{test_id: outcome}`.

### Design

- New file: `repogauge/lang/_go_test_parser.py`.
- Parse strategy: each line is a JSON object with fields `Time`, `Action`, `Package`, `Test`. Actions of interest: `pass`, `fail`, `skip`. Events without `Test` describe the whole package — ignore for per-test outcome but track package-level pass/fail for diagnostic context.
- Test ID format: `{Package}::{Test}` (e.g., `example.com/m/foo::TestBar`). Normalize subtests: `TestBar/Subtest_name` stays as-is; the `/` is preserved in the test ID.
- Outcome strings must align with `repogauge/validation/junit_parser.py`'s constants — use `OUTCOME_PASS`, `OUTCOME_FAIL`, `OUTCOME_SKIP` from there.
- Tolerate corrupted / truncated lines: skip with warning rather than raising. The harness sometimes truncates output.
- Tolerate inputs as: `pathlib.Path`, `str` (file path or raw output), `bytes`, or a Mapping (per `parse_repogauge_junit`'s pattern at `parsers/junit.py:74-90`).
- Register parser name `"go_json"` via `register_parser("go_json", parse_go_test_json)` from `parsers/junit.py` (or a new `repogauge/parsers/go_json.py`).
- Gotcha: a test that re-runs (`-count=2`) emits multiple terminal actions per test ID. Use the LAST seen action as authoritative.
- Gotcha: `go test -json` mixes per-test events with package-level events. Filter by presence of the `Test` field to avoid recording packages as tests.

### Acceptance Criteria

- Given a fixture with three tests (1 pass, 1 fail, 1 skip), the parser returns three entries with correct outcomes.
- Subtests like `TestFoo/case_a` parse to test ID `pkg::TestFoo/case_a`.
- Truncated input does not raise.
- Parser is registered under `"go_json"` and reachable via `parse_repogauge_test_output(payload, parser_name="go_json")`.

---

## Go adapter: file role rules

### ID mlang-104

### Priority P2

### Type task

### Labels multi-language, phase-1, go

### Dependencies

- mlang-100

### Description

- Provide `file_role_rules()` for the Go adapter so `mining/file_roles.py` (mlang-016) classifies Go files correctly.

### Design

- `prod_extensions = {".go"}`.
- `test_filename_patterns = ["*_test.go"]`.
- `test_dir_names = set()` (Go has no canonical `tests/` dir; tests live next to source).
- `config_build_filenames = {"go.mod", "go.sum", "go.work", "go.work.sum", ".golangci.yml", ".golangci.yaml"}`.
- `vendor_dir_names = {"vendor"}`.
- Gotcha: `_test.go` files only act as tests when sitting in a Go package. The path-based rule is fine for classification — `mining/file_roles.py` does not run the Go test discovery logic; it just labels files for diff understanding.
- Gotcha: do NOT add `*.go` to test patterns; only `*_test.go` is a test file in Go.

### Acceptance Criteria

- `GoAdapter().file_role_rules()` returns a `FileRoleRules` matching the spec.
- After registration, `classify_file("foo.go")` returns `role="prod"` and `classify_file("foo_test.go")` returns `role="test"`.

---

## Go adapter: harness template vars

### ID mlang-105

### Priority P2

### Type task

### Labels multi-language, phase-1, go

### Dependencies

- mlang-103

### Description

- Provide `harness_template_vars()` for Go so the generated `adapter_<repo>.py` references the Go test parser, install command, and file extension.

### Design

- Return:
  - `parser_import = "repogauge.lang._go_test_parser.parse_go_test_json"` (or wherever mlang-103 lands the function).
  - `parser_name = "go_json"`.
  - `ext = "go"`.
  - `install_str_join = " && "`.
- The generated `adapter_<repo>.py` will contain `from repogauge.lang._go_test_parser import parse_go_test_json` and reference it in `MAP_REPO_TO_PARSER`.
- Gotcha: any change to `parser_import` is a wire-format change — it's referenced from generated user files. Pin the import path early and keep it stable.

### Acceptance Criteria

- For a Go repo's exported `adapter_<repo>.py`, the file is importable and `MAP_REPO_TO_PARSER[repo]` is the Go parser callable.
- A small smoke test in `tests/unit/test_lang_go.py` (mlang-106) imports the generated file and confirms.

---

## Go adapter: unit tests

### ID mlang-106

### Priority P1

### Type task

### Labels multi-language, phase-1, go, tests

### Dependencies

- mlang-100
- mlang-101
- mlang-102
- mlang-103
- mlang-104
- mlang-105

### Description

- Add `tests/unit/test_lang_go.py` covering detection, inspection, env plan, file roles, and test-output parsing.

### Design

- Add fixture directory `tests/fixtures/go_minimal/`:
  - `go.mod` (`module example.com/m\n\ngo 1.22\n\nrequire ()\n`).
  - `main.go` (trivial `package main; func main() {}`).
  - `main_test.go` (one passing test, one failing test, one skipped test).
- Add fixture file `tests/fixtures/go_test_output.json` containing real `go test -json` output covering pass/fail/skip/subtest cases. Capture from running `go test -json` on a tiny throwaway repo if needed; do NOT depend on a Go toolchain at test time.
- Test cases:
  1. `GoAdapter.detect()` confidence 1.0 for fixture; 0.0 for empty dir.
  2. `GoAdapter.inspect()` parses module path and runtime_version.
  3. `GoAdapter.build_env_plan(profile)` returns expected commands.
  4. `parse_go_test_json(fixture)` returns the expected `{test_id: outcome}` dict.
  5. File role rules classify `*.go` and `*_test.go` correctly.
  6. Generated harness adapter file is importable and has `MAP_REPO_TO_PARSER` populated.
- Gotcha: this test file should NOT shell out to `go`. Pure Python parsing of fixture files.

### Acceptance Criteria

- `uv run python -m pytest tests/unit/test_lang_go.py -v` passes.
- All six test cases pass.
- No test depends on the `go` binary being installed.

---

## Go adapter: e2e smoke test

### ID mlang-107

### Priority P2

### Type task

### Labels multi-language, phase-1, go, tests, e2e

### Dependencies

- mlang-106

### Description

- Add a thin e2e smoke test that runs `repogauge mine → review → export` against a small embedded Go fixture and asserts artifact presence and shape. Gold-patch validation is a stretch goal for this bead; if Go isn't installed in CI, mark the eval portion as `xfail_if_no_go`.

### Design

- New test: `tests/e2e/test_go_repo.py`.
- Use the fixture from `tests/fixtures/go_minimal/` (mlang-106), turn it into a real git repo via `subprocess.run(["git", "init", ...])` in tmp_path, add+commit, then run the CLI via `subprocess.run(["uv", "run", "repogauge", "mine", ...])`.
- Assertions:
  - `out/mine/repo_profile.json` has `"language": "go"`.
  - `out/export/specs.json` has `"language": "go"`, `"parser": "go_json"`.
  - `out/export/adapter_*.py` is importable and references `parse_go_test_json`.
- Skip eval phase under `pytest.mark.skipif(shutil.which("go") is None, reason="go toolchain not present")`.
- Gotcha: CLI subprocess tests are slow. Mark this with `@pytest.mark.slow` and run separately from the fast unit suite.

### Acceptance Criteria

- `uv run python -m pytest tests/e2e/test_go_repo.py -v` passes (eval portion skipped if `go` not on PATH).
- Mine/review/export artifacts have correct language metadata.

---

## JS/TS adapter: detection (package.json, tsconfig.json, lockfiles)

### ID mlang-200

### Priority P1

### Type task

### Labels multi-language, phase-2, javascript

### Dependencies

- mlang-022

### Description

- Implement `detect()` for JavaScript/TypeScript in `repogauge/lang/javascript.py`. Recognize Node.js projects via `package.json`; infer TS via `tsconfig.json`.

### Design

- New file: `repogauge/lang/javascript.py` with `class JavaScriptAdapter`. `name() -> "javascript"` (covers JS and TS — TypeScript is recorded as a metadata flag, not a separate language).
- `detect(repo_root)`:
  - confidence 1.0 if `package.json` exists at root. Signals: `"package.json"`, optionally `"tsconfig.json"` if present.
  - 0.0 otherwise. (No fallback on file extensions because random `*.js` could appear in non-Node projects.)
- Lockfile detection (used in inspect, recorded as signal here too): `package-lock.json` → npm, `pnpm-lock.yaml` → pnpm, `yarn.lock` → yarn, `bun.lockb` → bun.
- Gotcha: monorepos with `package.json` only at sub-package level. For MVP, only detect at root. Workspace support is a follow-up.
- Gotcha: a Python repo may have a `package.json` purely for docs tooling. The adapter's confidence remains 1.0; tie-break against Python's 1.0 means deterministic name-sort wins, which is `"javascript" < "python"`. This is a known quirk — document in the adapter docstring and consider lowering JS confidence to 0.95 to keep Python primary in such cases. PROPOSED: confidence 0.95 to deliberately let Python win in mixed repos.

### Acceptance Criteria

- `JavaScriptAdapter.detect()` returns 0.95 for a dir with `package.json`.
- Returns 0.0 for a dir with only `*.js` files and no manifest.
- A test confirms a Python+Node mixed repo resolves to Python primary.

---

## JS/TS adapter: inspector (parse package.json, framework detection)

### ID mlang-201

### Priority P1

### Type task

### Labels multi-language, phase-2, javascript

### Dependencies

- mlang-200

### Description

- Implement `inspect()` for JS/TS: parse `package.json` for name/version/scripts/engines.node; detect the test framework (Jest vs Vitest); detect the package manager from lockfile.

### Design

- `inspect(repo_root)`:
  - Parse `repo_root / "package.json"` with stdlib `json`. Extract: `name`, `version`, `scripts`, `engines.node`, `devDependencies`, `dependencies`, `jest` (if present).
  - Package manager: lockfile → string. If multiple lockfiles, prefer in order: `pnpm-lock.yaml` > `yarn.lock` > `bun.lockb` > `package-lock.json` (deterministic; pnpm is most common in modern monorepos).
  - TypeScript flag: `repo_root / "tsconfig.json"` exists OR `typescript` in deps.
  - Framework detection:
    - Jest: `jest.config.{js,ts,mjs,cjs,json}` exists OR `package.json#jest` key present OR `jest` in devDependencies.
    - Vitest: `vitest.config.{js,ts,mjs,cjs}` exists OR `vitest` in devDependencies.
    - Tie: prefer Vitest (modern). Record both in signals, pick one as primary for `test_cmd_base`.
  - `runtime_version`: parse `engines.node` (e.g., `">=18"`) → minor pin like `"20"`. Default `"20"`.
  - `repo_version`: `package.json#version`, fallback `REPO_VERSION_UNKNOWN`.
  - `language_hints`: `{name, version, scripts, package_manager, framework, typescript, versions: [runtime_version], signals: [...]}`.
  - `install_hints`: `[f"{pm} install --frozen-lockfile"]` (npm uses `npm ci` instead of `--frozen-lockfile`).
  - `test_runner_hints.commands`: e.g., `["npx vitest run --reporter=junit"]`.
- Gotcha: `engines.node` is a semver range, not a version. Convert ranges like `">=18"` or `"^20.10.0"` to a usable pin (lower bound or pin the major). Use a small bespoke parser; don't depend on a `semver` Python library.
- Gotcha: yarn v1 vs v2+ (Berry) have different install semantics. For MVP, treat all yarn the same (`yarn install --frozen-lockfile` works on v1; `yarn install --immutable` is v2+). Document and pick the v1 form for compatibility.

### Acceptance Criteria

- For a fixture `package.json` with Vitest in devDependencies and `engines.node: ">=20"`, `inspect()` reports `framework="vitest"`, `runtime_version="20"`, `package_manager` matching the lockfile.
- TypeScript fixture (with `tsconfig.json`) sets `typescript: True`.
- All four package managers detected correctly from their respective lockfiles.

---

## JS/TS adapter: build_env_plan (Jest vs Vitest)

### ID mlang-202

### Priority P1

### Type task

### Labels multi-language, phase-2, javascript

### Dependencies

- mlang-201

### Description

- Implement `build_env_plan()` for JS/TS — install via the detected package manager, run tests via Jest or Vitest with JUnit XML output.

### Design

- `build_env_plan(profile)`:
  - `pre_install = []`.
  - `install`:
    - npm: `["npm ci"]` if lockfile present, else `["npm install"]`.
    - pnpm: `["pnpm install --frozen-lockfile"]`.
    - yarn: `["yarn install --frozen-lockfile"]`.
    - bun: `["bun install --frozen-lockfile"]`.
  - `test_cmd_base`:
    - Vitest: `"npx vitest run --reporter=junit --outputFile=report.xml"`.
    - Jest: `"npx jest --reporters=default --reporters=jest-junit --testResultsProcessor=jest-junit"`. Set `JEST_JUNIT_OUTPUT_FILE=report.xml` via `env_overrides`.
  - `build = []` (transpilation handled by Vitest/Jest in test runs; for production builds we don't gate on them).
  - `runtime_version` from inspect.
  - `language = "javascript"`.
  - `strategy_name = f"{pm}:{framework}"`.
- `env_overrides(worktree)`:
  - `{"NODE_ENV": "test", "CI": "1"}`. For Jest, also `{"JEST_JUNIT_OUTPUT_FILE": str(worktree / "report.xml")}`.
- `test_report_filename = "report.xml"`.
- Gotcha: `jest-junit` is a separate npm package. The adapter's install commands must NOT install it (we don't want to mutate the user's package.json). If the test fails because `jest-junit` isn't a dep, surface a clear error: "add `jest-junit` to devDependencies for repogauge eval support."
- Gotcha: Vitest's `--outputFile` path is relative to the cwd. Always run from worktree root.
- Gotcha: yarn v2+ does not support `--frozen-lockfile`. The MVP picks the v1 form; if a yarn v2+ project hits this, the install fails with a clear error and the user can override `test_cmd_base`.

### Acceptance Criteria

- For Vitest+pnpm fixture, plan has `install=["pnpm install --frozen-lockfile"]` and `test_cmd_base="npx vitest run --reporter=junit --outputFile=report.xml"`.
- For Jest+npm fixture, plan has `install=["npm ci"]` and `test_cmd_base` referencing `jest-junit`.
- `env_overrides(worktree)` returns the right env vars for each framework.

---

## JS/TS adapter: parse_test_output via JUnit XML

### ID mlang-203

### Priority P1

### Type task

### Labels multi-language, phase-2, javascript

### Dependencies

- mlang-201

### Description

- Implement `parse_test_output()` for JS/TS by reusing `repogauge/validation/junit_parser.py` with a JS-specific test-id canonicalizer.

### Design

- Both Jest (via `jest-junit`) and Vitest (via `--reporter=junit`) emit JUnit XML, so the existing `parse_junit_xml` / `parse_junit_xml_content` from `repogauge/validation/junit_parser.py` does most of the work.
- Add a parser registration: `register_parser("junit_js", parse_js_junit)` in `repogauge/lang/javascript.py`.
- `parse_js_junit(report, test_spec)`:
  - Parse XML with the existing helpers.
  - Override the test-id canonicalization step. Default canonical id is pytest-style (`tests/unit/test_foo.py::TestClass::test_method` per `validation/junit_parser.py:33-58`); JS frameworks emit `<file>.test.ts > describe block > test name` differently:
    - Jest: `classname` is the file path, `name` is `describe block > test name`.
    - Vitest: similar — `classname` is the file (relative path), `name` is `describe > test`.
  - Canonical JS id: `<classname>::<name>` with `>` characters preserved. Strip leading/trailing whitespace.
- Gotcha: `validation/junit_parser.py:33-58` (`_split_classname`, `_canonical_id`) is hardcoded to Python module path → pytest node id. To avoid breaking Python, add a new `canonicalize_test_id(classname, name, *, style="pytest")` function with `"pytest"` and `"js"` styles. The Python adapter uses `style="pytest"` (existing behavior); JS adapter uses `style="js"`.
- Gotcha: file path separators. Jest outputs OS-native separators in `classname`; normalize to forward slashes for stable cross-platform test IDs.

### Acceptance Criteria

- Given a Jest JUnit XML fixture with two tests, `parse_js_junit` returns two entries with canonical IDs of the form `src/foo.test.ts::describe block > test name`.
- Same for a Vitest JUnit XML fixture.
- `validation/junit_parser.py` Python-style canonicalization continues to work (no regression in existing tests).

---

## JS/TS adapter: file role rules

### ID mlang-204

### Priority P2

### Type task

### Labels multi-language, phase-2, javascript

### Dependencies

- mlang-200

### Description

- Provide `file_role_rules()` for JS/TS so file_roles classifies sources, tests, and vendored deps correctly.

### Design

- `prod_extensions = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}`.
- `test_filename_patterns = ["*.test.ts", "*.test.tsx", "*.test.js", "*.test.jsx", "*.spec.ts", "*.spec.tsx", "*.spec.js", "*.spec.jsx"]`.
- `test_dir_names = {"__tests__"}`.
- `config_build_filenames = {"package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb", "tsconfig.json", "jest.config.js", "jest.config.ts", "jest.config.cjs", "jest.config.mjs", "jest.config.json", "vitest.config.js", "vitest.config.ts", "vitest.config.cjs", "vitest.config.mjs", ".eslintrc", ".eslintrc.js", ".eslintrc.json", ".prettierrc", ".prettierrc.json"}`.
- `vendor_dir_names = {"node_modules", "dist", "build", ".next", ".nuxt", "coverage", ".turbo", ".cache"}`.
- Gotcha: `.next` and `dist` are also valid project names; matching by directory NAME at any depth is fine for now (matches current Python rule for `__pycache__`). If false-positives appear, scope to root-level only.
- Gotcha: `*.d.ts` declaration files are technically prod but contain no runtime — leave under `prod_extensions`.

### Acceptance Criteria

- `JavaScriptAdapter().file_role_rules()` matches the spec.
- Classification: `src/foo.ts` → prod, `src/foo.test.ts` → test, `node_modules/...` → generated_vendor.

---

## JS/TS adapter: harness template vars

### ID mlang-205

### Priority P2

### Type task

### Labels multi-language, phase-2, javascript

### Dependencies

- mlang-203

### Description

- Provide `harness_template_vars()` for JS/TS so generated `adapter_<repo>.py` references the JS JUnit parser and correct extension.

### Design

- Return:
  - `parser_import = "repogauge.lang.javascript.parse_js_junit"`.
  - `parser_name = "junit_js"`.
  - `ext = "ts"` if profile shows TypeScript, else `"js"`.
  - `install_str_join = " && "`.

### Acceptance Criteria

- For a TS repo, generated `adapter_<repo>.py` has `MAP_REPO_TO_EXT = {repo: "ts"}` and imports `parse_js_junit`.

---

## JS/TS adapter: unit tests

### ID mlang-206

### Priority P1

### Type task

### Labels multi-language, phase-2, javascript, tests

### Dependencies

- mlang-200
- mlang-201
- mlang-202
- mlang-203
- mlang-204
- mlang-205

### Description

- Add `tests/unit/test_lang_javascript.py` covering detection, inspection (Jest+Vitest, all four package managers), env plan, file roles, and test-output parsing.

### Design

- Fixtures under `tests/fixtures/js_*/`:
  - `js_jest_npm/` — `package.json` with Jest devDep, `package-lock.json`.
  - `js_vitest_pnpm/` — `package.json` with Vitest devDep + `vitest.config.ts`, `pnpm-lock.yaml`.
  - `js_yarn_ts/` — TypeScript with yarn lockfile.
  - `js_bun_minimal/` — Bun lockfile, no framework configured.
- JUnit XML fixtures captured from real Jest/Vitest runs at `tests/fixtures/js_jest_junit.xml` and `tests/fixtures/js_vitest_junit.xml`.
- Tests:
  1. detect: confidence 0.95 for each fixture.
  2. inspect: framework correctly identified, package manager correctly chosen.
  3. build_env_plan: produces correct install/test commands per fixture.
  4. parse_js_junit: returns expected `{test_id: outcome}` for both XML fixtures.
  5. file role rules: classification correct.
  6. mixed Python+Node fixture: Python wins primary detection.

### Acceptance Criteria

- `uv run python -m pytest tests/unit/test_lang_javascript.py -v` passes.
- All test cases pass.

---

## JS/TS adapter: e2e smoke test

### ID mlang-207

### Priority P2

### Type task

### Labels multi-language, phase-2, javascript, tests, e2e

### Dependencies

- mlang-206

### Description

- Add `tests/e2e/test_javascript_repo.py` running mine→review→export against a small JS fixture. Skip eval if `node`/`npm` not on PATH.

### Design

- Fixture under `tests/fixtures/js_minimal_real/`: a tiny Vitest project with one passing and one failing test, plus a known fix commit.
- Test runs `repogauge mine → review → export` via subprocess and asserts:
  - `repo_profile.json` has `language: "javascript"`.
  - `specs.json` has `language: "javascript"`, `parser: "junit_js"`.
- Eval phase wrapped in `@pytest.mark.skipif(shutil.which("npm") is None or shutil.which("node") is None, reason="node toolchain not present")`.
- Mark with `@pytest.mark.slow`.

### Acceptance Criteria

- E2E test passes on a developer machine with node installed.
- Test skips eval on machines without node and still passes.

---

## Java adapter: detection (pom.xml, build.gradle)

### ID mlang-300

### Priority P2

### Type task

### Labels multi-language, phase-3, java

### Dependencies

- mlang-022

### Description

- Implement `detect()` for Java in `repogauge/lang/java.py`. Recognize Maven via `pom.xml` and Gradle via `build.gradle` / `build.gradle.kts`.

### Design

- New file: `repogauge/lang/java.py`. `class JavaAdapter` with `name() -> "java"`.
- `detect(repo_root)`:
  - confidence 1.0 if `pom.xml` exists. Signal: `"pom.xml"`, build_tool `"maven"`.
  - confidence 1.0 if `build.gradle` or `build.gradle.kts` exists. Signal: `"build.gradle"` or `"build.gradle.kts"`, build_tool `"gradle"`.
  - confidence 0.95 if both exist (multi-build-tool — record both, prefer Maven by lexicographic stability).
  - 0.0 otherwise.
- Detect Kotlin via `*.kt` source presence or Gradle Kotlin DSL — but treat as Java for adapter purposes (Kotlin compiles to JVM bytecode and uses the same JUnit/Surefire reports). Add a `kotlin_present` flag to `language_hints` for downstream awareness.
- Gotcha: Android projects have `build.gradle` files at multiple levels. For MVP, detect from root only; document Android as out-of-scope until follow-up.
- Gotcha: Bazel-built JVM projects have neither `pom.xml` nor `build.gradle`. Out of scope; user can write a custom adapter.

### Acceptance Criteria

- `JavaAdapter.detect()` returns 1.0 for Maven fixture, 1.0 for Gradle fixture, 0.0 for empty.
- `language_hints.build_tool` is set correctly.

---

## Java adapter: inspector (Maven vs Gradle, version detection)

### ID mlang-301

### Priority P2

### Type task

### Labels multi-language, phase-3, java

### Dependencies

- mlang-300

### Description

- Implement `inspect()` for Java: parse `pom.xml` or Gradle build files for project name, version, Java version, and test runner config.

### Design

- For Maven (`pom.xml`):
  - Parse XML with stdlib `xml.etree.ElementTree`.
  - Extract `groupId`, `artifactId`, `version` → use `groupId:artifactId` as project name; `version` → `repo_version`.
  - Java version: prefer `<maven.compiler.release>`, fallback `<maven.compiler.source>`, fallback `<java.version>`. Default `"17"`.
- For Gradle (`build.gradle` / `build.gradle.kts`):
  - No proper parser available (Groovy/Kotlin DSL). Use line-based regex extraction:
    - `sourceCompatibility = '17'` or `JavaVersion.VERSION_17` → version `"17"`.
    - `version = '1.2.3'` → `repo_version`.
  - Defaults: version `"17"`, repo_version `REPO_VERSION_UNKNOWN`.
- Detect test framework:
  - JUnit 5: `junit-jupiter` in dependencies (Maven) or `useJUnitPlatform()` in Gradle.
  - JUnit 4: `junit:junit` in dependencies, `useJUnit()` in Gradle.
  - TestNG: `testng` in deps, `useTestNG()` in Gradle.
  - Default: JUnit 5 (modern default).
- `runtime_version`: detected Java version, default `"17"`.
- Gotcha: `pom.xml` can declare versions via parent POM properties (`${java.version}`). Best-effort interpolation: substitute `${prop}` from the `<properties>` block; if unresolved, fall back to default.
- Gotcha: Gradle build scripts can be code-heavy (Groovy DSL). Regex extraction is fragile but acceptable for MVP — document the limitation in the docstring.

### Acceptance Criteria

- For a Maven fixture with `<maven.compiler.release>21</maven.compiler.release>`, inspect returns `runtime_version="21"`.
- For a Gradle fixture with `sourceCompatibility = '17'`, inspect returns `runtime_version="17"`.
- Test framework correctly identified for JUnit 5/4/TestNG fixtures.

---

## Java adapter: build_env_plan (Maven + Gradle)

### ID mlang-302

### Priority P2

### Type task

### Labels multi-language, phase-3, java

### Dependencies

- mlang-301

### Description

- Implement `build_env_plan()` for Java: choose `mvn` or `./gradlew` commands based on detected build tool.

### Design

- Maven:
  - `pre_install = []`.
  - `install = ["mvn -B -DskipTests install"]`.
  - `build = []`.
  - `test_cmd_base = "mvn -B test"`.
- Gradle:
  - `pre_install = []`.
  - `install = ["./gradlew assemble"]`.
  - `build = []`.
  - `test_cmd_base = "./gradlew test"`.
- `runtime_version` from inspect.
- `language = "java"`.
- `strategy_name = f"{build_tool}:junit"` (or detected framework).
- `test_report_filename`: not a single file — for Maven it's `target/surefire-reports/*.xml`, for Gradle `build/test-results/test/*.xml`. The validate flow needs to glob, not look for one file. Add a new `test_report_glob` Protocol method that defaults to `f"./{test_report_filename}"` and overrides for Java to glob the surefire/test-results dirs.
- Gotcha: `./gradlew` requires the wrapper to be present and executable. Add a check in inspect: if `gradlew` exists but is not executable, add a warning and fall back to `gradle test` (assumes system gradle).
- Gotcha: Maven CI mode `-B` and dependency download can be slow. The eval loop will tolerate this; document expected runtime.

### Acceptance Criteria

- Maven plan: `install=["mvn -B -DskipTests install"]`, `test_cmd_base="mvn -B test"`.
- Gradle plan: `install=["./gradlew assemble"]`, `test_cmd_base="./gradlew test"`.

---

## Java adapter: parse_test_output (surefire/failsafe XML)

### ID mlang-303

### Priority P2

### Type task

### Labels multi-language, phase-3, java

### Dependencies

- mlang-301

### Description

- Implement `parse_test_output()` for Java by reusing `validation/junit_parser.py` with a Java-specific test-id canonicalizer that handles surefire/failsafe XML output.

### Design

- Surefire/Failsafe emit standard JUnit XML with classnames like `com.example.MyTest` and method names like `myMethodTest`. Canonical Java test id: `com.example.MyTest::myMethodTest`.
- Parameterized JUnit 5 tests have `name="myMethod(int)[1]"` style — preserve as-is.
- Reuse `parse_junit_xml` from `validation/junit_parser.py` with the new `canonicalize_test_id(classname, name, style="java")` extension added in mlang-203.
- Multiple report files: surefire writes one XML per test class. The adapter merges all XMLs in the report glob.
- Register parser name `"junit_java"` via `register_parser`.

### Acceptance Criteria

- Given a fixture surefire `TEST-com.example.FooTest.xml`, the parser returns `{"com.example.FooTest::testBar": "passed", ...}`.
- Multiple XML files are merged into one dict.

---

## Java adapter: file role rules

### ID mlang-304

### Priority P2

### Type task

### Labels multi-language, phase-3, java

### Dependencies

- mlang-300

### Description

- Provide `file_role_rules()` for Java.

### Design

- `prod_extensions = {".java", ".kt"}`.
- `test_filename_patterns = ["*Test.java", "*Tests.java", "*IT.java", "*IntegrationTest.java", "*Test.kt", "*Tests.kt"]`.
- `test_dir_names = {"src/test", "src/test/java", "src/test/kotlin", "src/integrationTest"}`.
- `config_build_filenames = {"pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "gradle.properties", "gradlew", "gradlew.bat", "checkstyle.xml", "spotbugs.xml"}`.
- `vendor_dir_names = {"target", "build", ".gradle", "out"}`.
- Gotcha: `target/` and `build/` are common across multiple ecosystems; the matching is by exact directory name, which is fine.

### Acceptance Criteria

- Classifications correct for `src/main/java/Foo.java` → prod, `src/test/java/FooTest.java` → test, `target/...` → generated_vendor.

---

## Java adapter: harness template vars

### ID mlang-305

### Priority P2

### Type task

### Labels multi-language, phase-3, java

### Dependencies

- mlang-303

### Description

- Provide `harness_template_vars()` for Java.

### Design

- `parser_import = "repogauge.lang.java.parse_java_junit"`.
- `parser_name = "junit_java"`.
- `ext = "java"` (or `"kt"` if Kotlin-primary; if both, `"java"`).
- `install_str_join = " && "`.

### Acceptance Criteria

- Generated adapter file imports `parse_java_junit` and sets `MAP_REPO_TO_EXT` correctly.

---

## Java adapter: unit tests

### ID mlang-306

### Priority P2

### Type task

### Labels multi-language, phase-3, java, tests

### Dependencies

- mlang-300
- mlang-301
- mlang-302
- mlang-303
- mlang-304
- mlang-305

### Description

- Add `tests/unit/test_lang_java.py` with fixtures for Maven and Gradle, all framework variants.

### Design

- Fixtures: `tests/fixtures/java_maven_junit5/`, `tests/fixtures/java_gradle_junit4/`, `tests/fixtures/java_gradle_kts/`.
- Surefire XML fixture: `tests/fixtures/java_surefire_TEST-FooTest.xml`.
- Tests cover: detect, inspect (Maven/Gradle/Kotlin DSL), env plan, surefire parser, file roles.
- No JVM dependency at test time.

### Acceptance Criteria

- `uv run python -m pytest tests/unit/test_lang_java.py -v` passes.

---

## Java adapter: e2e smoke test

### ID mlang-307

### Priority P3

### Type task

### Labels multi-language, phase-3, java, tests, e2e

### Dependencies

- mlang-306

### Description

- E2E mine→review→export against a tiny Java fixture. Skip eval if `mvn` not on PATH.

### Design

- Tiny Maven fixture under `tests/fixtures/java_maven_real/` with one test class.
- Subprocess-based mine/review/export, assertions on artifact contents.
- Eval phase skipped without `mvn`. Mark `@pytest.mark.slow`.

### Acceptance Criteria

- Test passes on machines with maven; skips eval otherwise.

---

## Rust adapter: detection (Cargo.toml, workspaces)

### ID mlang-400

### Priority P2

### Type task

### Labels multi-language, phase-4, rust

### Dependencies

- mlang-022

### Description

- Implement `detect()` for Rust in `repogauge/lang/rust.py`. Recognize Rust via `Cargo.toml` at root.

### Design

- New file: `repogauge/lang/rust.py`. `class RustAdapter` with `name() -> "rust"`.
- `detect(repo_root)`:
  - confidence 1.0 if `Cargo.toml` exists at root. Signals: `"Cargo.toml"`, plus `"workspace"` if `[workspace]` table present.
  - 0.0 otherwise.
- Workspace detection: parse `Cargo.toml` for `[workspace]` table and `members` list. Record under `language_hints.workspace_members`.

### Acceptance Criteria

- Detects single-crate and workspace fixtures with confidence 1.0.

---

## Rust adapter: inspector (rust-toolchain, edition)

### ID mlang-401

### Priority P2

### Type task

### Labels multi-language, phase-4, rust

### Dependencies

- mlang-400

### Description

- Parse `Cargo.toml` for crate name/version/edition and `rust-toolchain.toml` (or legacy `rust-toolchain`) for the channel.

### Design

- Use stdlib `tomllib` (Python 3.11+) or `tomli` (existing fallback in `mining/inspect.py:14-16`).
- Extract from `Cargo.toml`:
  - `[package].name` and `[package].version` → name and `repo_version`.
  - `[package].edition` → record as `language_hints.edition` (`"2018"`, `"2021"`).
  - For workspaces, version may be on `[workspace.package]` (Rust 1.64+); fall back gracefully.
- Channel from `rust-toolchain.toml`:
  - `[toolchain].channel = "stable"` or `"1.74"` or `"nightly"`.
- Legacy `rust-toolchain` (single-line): the file content is the channel.
- `runtime_version`: parsed channel; default `"stable"`.
- Gotcha: `rust-toolchain.toml` is TOML; the legacy `rust-toolchain` file is plain text. Try TOML first, fall back to plain.

### Acceptance Criteria

- For `Cargo.toml` with `edition = "2021"`, inspect records edition.
- For `rust-toolchain.toml` with `channel = "1.74"`, runtime_version is `"1.74"`.
- Default `"stable"` when toolchain file absent.

---

## Rust adapter: build_env_plan (cargo test)

### ID mlang-402

### Priority P2

### Type task

### Labels multi-language, phase-4, rust

### Dependencies

- mlang-401

### Description

- Implement `build_env_plan()` for Rust using `cargo`.

### Design

- `pre_install = []`.
- `install = ["cargo fetch"]` (downloads dependencies; idempotent and cache-friendly).
- `build = []` (`cargo test` builds).
- `test_cmd_base`:
  - Stable libtest JSON output is gated behind nightly's `-Z unstable-options --format json`. For MVP, use the human-readable output and parse it: `cargo test --no-fail-fast`.
  - Add a `--cargo-test-json` flag plumbing for users on nightly: when enabled, use `cargo test --no-fail-fast -- -Z unstable-options --format json`.
  - Default: human-readable.
- `runtime_version` from inspect.
- `language = "rust"`.
- `strategy_name = "cargo:cargo-test"`.
- `test_report_filename`: cargo doesn't write a single file. The adapter captures stdout/stderr and parses inline.
- `env_overrides(worktree)`: `{"CARGO_HOME": str(worktree / ".cargo"), "CARGO_TARGET_DIR": str(worktree / "target")}` to keep runs hermetic.
- Gotcha: `cargo test` on a workspace runs all member crates. The `--no-fail-fast` flag is critical to capture all failures, not just the first crate's.
- Gotcha: hermetic CARGO_HOME means downloading deps every run. Acceptable for MVP; document and offer a flag for shared cache later.

### Acceptance Criteria

- Plan returns `install=["cargo fetch"]`, `test_cmd_base="cargo test --no-fail-fast"`.
- `env_overrides` returns CARGO_HOME and CARGO_TARGET_DIR pointed at worktree.

---

## Rust adapter: parse_test_output (libtest output)

### ID mlang-403

### Priority P2

### Type task

### Labels multi-language, phase-4, rust

### Dependencies

- mlang-401

### Description

- Parse cargo's libtest output (human-readable) into `{test_id: outcome}`. Optionally support the JSON format when nightly is detected.

### Design

- New file: `repogauge/lang/_rust_test_parser.py`.
- Human-readable output parser:
  - Each crate emits `running N tests` then `test <test_id> ... ok` / `FAILED` / `ignored`.
  - Test ID format: `<module_path>::<test_name>`, e.g., `tests::add_one`.
  - For multi-crate workspace runs, prefix with crate name: `<crate>::<module_path>::<test_name>`. Cargo prefixes the section with `Running unittests <path>`; capture the crate name from there.
- JSON output parser (optional, gated on `cargo test -- --format json`):
  - Each line is a JSON object with `type` (`"test"`, `"suite"`), `event` (`"started"`, `"ok"`, `"failed"`, `"ignored"`), `name`.
- Outcome mapping: `ok` → pass, `FAILED` → fail, `ignored` → skip.
- Tolerate ANSI color codes by stripping with a small regex (`\x1b\[[0-9;]*m`).
- Tolerate truncated output.
- Register parser name `"cargo_human"` (and `"cargo_json"` if implemented) via `register_parser`.
- Gotcha: cargo interleaves crate output. Track current crate as state while parsing.
- Gotcha: doctest output is different (`test src/lib.rs - foo (line 12) ... ok`). Capture as `<file>::doctest_<line>`.

### Acceptance Criteria

- Given fixture human-readable output for 3 crates with various outcomes, parser returns correctly prefixed test IDs.
- ANSI codes stripped.
- Doctest cases captured.

---

## Rust adapter: file role rules

### ID mlang-404

### Priority P3

### Type task

### Labels multi-language, phase-4, rust

### Dependencies

- mlang-400

### Description

- Provide `file_role_rules()` for Rust.

### Design

- `prod_extensions = {".rs"}`.
- `test_filename_patterns = []` (Rust tests are inline `#[cfg(test)]` mods or under `tests/`).
- `test_dir_names = {"tests", "benches"}`.
- `config_build_filenames = {"Cargo.toml", "Cargo.lock", "rust-toolchain", "rust-toolchain.toml", "rustfmt.toml", "clippy.toml", ".cargo/config.toml"}`.
- `vendor_dir_names = {"target"}`.

### Acceptance Criteria

- Classifications: `src/lib.rs` → prod, `tests/integration.rs` → test, `target/...` → generated_vendor.

---

## Rust adapter: harness template vars

### ID mlang-405

### Priority P3

### Type task

### Labels multi-language, phase-4, rust

### Dependencies

- mlang-403

### Description

- Provide `harness_template_vars()` for Rust.

### Design

- `parser_import = "repogauge.lang._rust_test_parser.parse_cargo_human"`.
- `parser_name = "cargo_human"`.
- `ext = "rs"`.
- `install_str_join = " && "`.

### Acceptance Criteria

- Generated adapter file imports the cargo parser and sets ext to `rs`.

---

## Rust adapter: unit tests

### ID mlang-406

### Priority P2

### Type task

### Labels multi-language, phase-4, rust, tests

### Dependencies

- mlang-400
- mlang-401
- mlang-402
- mlang-403
- mlang-404
- mlang-405

### Description

- Add `tests/unit/test_lang_rust.py` with fixtures for single crate and workspace, plus libtest output samples.

### Design

- Fixtures: `tests/fixtures/rust_single_crate/`, `tests/fixtures/rust_workspace/`, `tests/fixtures/rust_test_output.txt` (captured `cargo test` output with pass/fail/ignored).
- Tests: detect, inspect, env plan, parse output, file roles.
- No `cargo` dependency at test time.

### Acceptance Criteria

- `uv run python -m pytest tests/unit/test_lang_rust.py -v` passes.

---

## Rust adapter: e2e smoke test

### ID mlang-407

### Priority P3

### Type task

### Labels multi-language, phase-4, rust, tests, e2e

### Dependencies

- mlang-406

### Description

- E2E mine→review→export against a tiny Rust crate fixture. Skip eval if `cargo` not on PATH.

### Design

- Tiny crate fixture under `tests/fixtures/rust_minimal_real/`.
- Subprocess-based mine/review/export.
- Eval phase skipped without `cargo`. Mark `@pytest.mark.slow`.

### Acceptance Criteria

- Test passes with cargo; skips eval otherwise.
