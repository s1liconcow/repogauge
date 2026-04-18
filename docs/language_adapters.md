# RepoGauge language adapter contract

## Status

Canonical.

This document is the source of truth for the `LanguageAdapter` protocol used by
RepoGauge's multi-language migration. It complements
[ADR-0002](ADRs/0002-language-adapter-registry.md), which records the registry
invariants and dispatch rules.

The contract is intentionally transport-agnostic. Adapters are plain in-process
Python objects. Nothing here assumes JSON-RPC, subprocess boundaries, or any
particular harness calling convention.

## Purpose

RepoGauge routes every language-sensitive decision through a single adapter
instance selected from the process-wide language registry:

- repository detection
- repository inspection
- environment planning
- test output parsing
- file-role classification
- harness registration template rendering
- environment-signature labeling

The adapter contract keeps those seams deterministic and keeps language-specific
logic out of shared modules.

## Canonical Terms

- **Language**: the primary adapter identity for a repository, for example
  `python`, `go`, `javascript`, `java`, or `rust`.
- **Primary language**: the single language chosen by detection after applying
  the confidence tie-break rule.
- **Language hints**: the language-specific inspection payload returned by an
  adapter. This is the authoritative language-specific view of the repo.
- **Legacy Python hints**: the historical `python_hints` / `python_version`
  keys. They remain populated for compatibility when the primary language is
  Python, but they are aliases, not the source of truth.
- **Detection entry**: one adapter's detection result, recorded for later
  inspection and debugging.

## Core Dataclasses

### `DetectionResult`

```python
@dataclass
class DetectionResult:
    language: str
    confidence: float
    signals: list[str]
    runtime_version: Optional[str] = None
```

Semantics:

- `language` is the adapter name that won the detection pass.
- `confidence` is a value in `[0.0, 1.0]`.
- `signals` explains why the adapter matched, using short stable strings.
- `runtime_version` is the language runtime version the adapter believes is
  active, if one can be identified confidently.

### `FileRoleRules`

```python
@dataclass
class FileRoleRules:
    prod_extensions: set[str]
    test_filename_patterns: list[str]
    test_dir_names: set[str]
    config_build_filenames: set[str]
    vendor_dir_names: set[str]
```

Semantics:

- `prod_extensions` are source-file extensions that should classify as
  production code.
- `test_filename_patterns` are filename patterns that should classify as tests.
- `test_dir_names` are directory names that imply test-only paths.
- `config_build_filenames` are config, build, and tooling files that should
  classify as `config_build`.
- `vendor_dir_names` are generated, vendored, or cache directories that should
  classify as `generated_vendor`.

## Language Recording

Every inspected repository records the selected language in the repo profile.
The canonical fields are:

- `language`: primary language name
- `language_version`: primary runtime version
- `language_hints`: authoritative language-specific inspection data
- `language_detection`: ordered list of all adapter detection results

For Python repositories, the profile also carries:

- `python_hints`: exact alias of `language_hints`
- `python_version`: exact alias of `language_version`

The profile may contain additional non-language fields such as `repo_name`,
`repo_version`, `install_hints`, `test_runner_hints`, warnings, and metadata.
Those are part of the broader RepoGauge data model, not the language contract
itself.

Each `language_detection` entry is a serialized detection result with at least:

- `name`
- `language`
- `confidence`
- `signals`
- `runtime_version`

### Primary-language recording rule

If more than one adapter reports a non-zero confidence:

1. Choose the highest confidence.
2. If two adapters tie on confidence, choose the lexicographically smaller
   `name()`.
3. Record every adapter's detection result in `language_detection` so the
   caller can inspect the full decision set.

That rule is what keeps mixed-language repos deterministic.

## `LanguageAdapter` Protocol

An adapter must implement the following methods and hooks.

### `name() -> str`

Returns the stable adapter identifier.

- It must be lowercase and deterministic.
- It must be unique within the registry.
- It is used for tie-breaks, registry lookups, and user-facing diagnostics.

Recommended canonical names:

- `python`
- `go`
- `javascript`
- `java`
- `rust`

### `detect(repo_root: Path) -> DetectionResult`

Returns the adapter's detection score for a repository root.

Requirements:

- Never mutate the repository.
- Return the same result for the same filesystem state.
- Use short, stable `signals` strings that explain the match.
- Return `confidence=0.0` when the repository does not look like this
  language.

Interpretation:

- `1.0` means a strong, manifest-level match.
- Lower values mean weaker but still plausible evidence.
- The exact scoring rubric is adapter-specific, but the score must be stable
  and monotonic with evidence quality.

### `inspect(repo_root: Path) -> dict`

Produces the authoritative inspection payload for the repository.

The returned mapping must include:

- `language`
- `language_version`
- `language_hints`
- `install_hints`
- `test_runner_hints`
- `repo_version`

The returned mapping should also include:

- `language_detection` when the inspector records the full candidate set
- `python_hints` and `python_version` when `language == "python"`

Semantics:

- `language_hints` is the authoritative language-specific block.
- `install_hints` and `test_runner_hints` are advisory but deterministic.
- `repo_version` is the repository version used in dataset/signature material.
- The method should return JSON-serializable data, even though the contract is
  in-process.

Python back-compat rule:

- When `language == "python"`, `python_hints` must equal `language_hints`.
- When `language == "python"`, `python_version` must equal `language_version`.

### `build_env_plan(profile: dict) -> EnvPlan`

Turns an inspected profile into the concrete execution plan consumed by the
validation pipeline.

The returned plan is the contract object defined in
`repogauge.validation.env_detect.EnvPlan`.

Core fields:

- `python_version`: the runtime version slot used by the current validator
  pipeline
- `pre_install`: commands that must run before installation
- `install`: install commands
- `build`: build commands
- `test_cmd_base`: the base test command
- `strategy_name`: a stable label for the chosen plan
- `confidence`: a numeric confidence score for the plan choice
- `provenance`: a list of deterministic explanation tokens

Requirements:

- Deterministic for the same `profile`.
- Preserve Python behavior byte-for-byte during the Phase 0 transition.
- Keep command ordering stable.

### `parse_test_output(report: object, test_spec: object | None) -> dict[str, str]`

Maps test-runner output to `{test_id: outcome}`.

Requirements:

- Return stable test IDs.
- Use outcome strings from RepoGauge's shared outcome constants.
- Accept the concrete report payloads produced by the validator for that
  language.
- Stay transport-agnostic. The contract does not require a particular wire
  format.

### `file_role_rules() -> FileRoleRules`

Returns the adapter's file-role classification rules.

The file-role names are shared across the project and must use the canonical
values from `repogauge/mining/file_roles.py`:

- `prod`
- `test`
- `test_support`
- `config_build`
- `docs`
- `generated_vendor`
- `unknown`

Language adapters contribute their own extension, filename, and directory
rules; the shared classifier unions those rules when needed.

### `harness_template_vars(spec: dict) -> dict`

Returns the variables needed to render the generated harness registration
module.

Required keys:

- `parser_import`: dotted import path to the parser callable
- `parser_name`: registry name used in `AdapterSpec`
- `ext`: file-extension suffix for the generated adapter mapping
- `install_str_join`: join string used when serializing install commands

The generated adapter module is still a Python file, but its contents must come
from the active language adapter.

### `signature_labels(profile: dict) -> dict`

Returns the labels used by the environment-signature builder.

Required keys:

- `runtime_label`
- `test_label`
- `package_label`

Semantics:

- `runtime_label` summarizes the detected runtime version(s).
- `test_label` summarizes the chosen test runner(s).
- `package_label` summarizes the package manager(s).
- The returned labels must be deterministic for the same profile.

## Supporting Hooks

These hooks are used by later phases of the pipeline. They are part of the
contract surface even when an adapter falls back to a default implementation.

### `dependency_signature_inputs(repo_root: Path, profile: dict) -> list[str]`

Returns the normalized text chunks that feed the environment signature hash.

Requirements:

- Deterministic ordering.
- Only include language-appropriate dependency material.
- For Python, preserve the current requirements/packaging normalization so
  existing dataset signatures stay byte-identical.

### `env_overrides(worktree: Path) -> dict[str, str]`

Returns environment variables injected into the test runner.

Requirements:

- Default to `{}` when no extra variables are needed.
- Use it for hermetic caches or framework-specific env vars.
- Only include variables that are required for deterministic execution.

### `test_command_attempts(test_cmd_base: str) -> list[list[str]]`

Returns ordered argv attempts for running tests.

Requirements:

- The first attempt is the primary one.
- Later attempts are deterministic fallbacks.
- Commands are argv lists, not shell strings.

### `test_report_filename` or `test_report_glob`

Declares where the validator should look for the test report artifact.

Requirements:

- Use `test_report_filename` when the language runner writes one report file.
- Use `test_report_glob` when the language runner writes multiple files.
- Keep the value stable because it is referenced by generated artifacts and
  eval logic.

## Canonical Value Sets

### Package-manager strings

Use the following stable strings for package-manager identification.

| Language | Canonical values |
| --- | --- |
| Python | `pyproject`, `poetry`, `pep621`, `setuptools`, `requirements`, `pipenv`, `uv` |
| Go | `gomod` |
| JavaScript / TypeScript | `npm`, `pnpm`, `yarn`, `bun` |
| Java | `maven`, `gradle` |
| Rust | `cargo` |

Rules:

- Prefer the narrowest stable identifier that still describes the repo.
- Do not use raw file names as package-manager values.
- Do not invent aliases unless they are documented here first.

### Test-runner identifiers

Use the following stable strings for test-runner identification.

| Language | Canonical values |
| --- | --- |
| Python | `pytest`, `unittest`, `tox`, `nox` |
| Go | `go_json` |
| JavaScript / TypeScript | `junit_js` |
| Java | `junit_java` |
| Rust | `cargo_human`, `cargo_json` |

Rules:

- The identifier names the parser/runner contract, not the shell command.
- If a language has multiple supported frameworks, the identifier should
  reflect the framework family chosen by the adapter.

### Harness parser names

Parser names are the keys used in adapter specs and the parser registry.

| Language | Canonical parser names |
| --- | --- |
| Python | `junit` |
| Go | `go_json` |
| JavaScript / TypeScript | `junit_js` |
| Java | `junit_java` |
| Rust | `cargo_human`, `cargo_json` |

Rules:

- Parser names must be stable across releases because they appear in generated
  user-facing artifacts.
- The parser name is part of the contract, not an implementation detail.

## Back-compat rules for Python

Python is the compatibility anchor for the migration.

When the primary language is Python:

- `python_hints` must be present and must equal `language_hints`.
- `python_version` must be present and must equal `language_version`.
- The dataset signature and generated adapter artifacts must remain byte-
  identical to the pre-migration output except for the newly added language
  fields required by the Phase 0 beads.
- The eval pipeline must keep its current pytest/JUnit behavior.

Python-specific derived values must never become authoritative once the
language-aware fields exist.

### Generated `AdapterSpec` recording

Generated adapter specs carry the same primary-language information:

- `language`: primary language name
- `runtime_version`: primary runtime version

Python repos keep the legacy `python_version` payloads populated inside the
adapter spec and nested `docker_specs`, but those are compatibility aliases.

## Non-goals

This document does not define:

- the exact repository-scanning heuristics for every adapter implementation
- the CLI wiring for registry bootstrapping
- the on-disk database format for beads or agent mail
- the transport used to call adapter methods

Those concerns belong in the implementation beads and in the registry ADR.
