# JUnit Parser Contract

## Scope

RepoGauge uses JUnit XML as the canonical per-test result format in validation and grading. When JUnit output is available, it is treated as authoritative and parser output drives PASS/FAIL/SKIP determination.

## Invocation requirements

- Validation must force JUnit XML generation for pytest execution.
- The targeted test command must preserve any pre-existing JUnit argument already present.
- The expected pytest flag forms are:
  - `--junitxml=<path>` (legacy/explicit)
  - `--junit-xml=<path>` (supported alias)

## Canonical test id mapping

- A test identifier is canonicalized to match dataset row IDs.
- Dataset/test IDs are represented as `{nodeid}` strings.
- IDs from parser outputs are normalized before set comparison.

## Outcome policy

- `fail` and `error` outcomes are treated as failures.
- `pass` outcomes are treated as passes.
- `skipped`, `xfail`, and `xpass` outcomes are treated as skips.
- `xpass`/`xfail` are explicitly non-failures for FAIL_TO_PASS derivation.

## Parse failures

- Missing or malformed JUnit XML is a hard failure.
- RepoGauge should fail deterministically rather than silently fallback to alternate parsing.
- This prevents benchmark drift and makes validation outcomes explainable.
