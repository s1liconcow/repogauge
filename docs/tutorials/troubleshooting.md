# Troubleshooting

Common startup and execution issues and fast checks.

## `repogauge mine` exits with zero candidates

- Ensure the target path contains a git repository and enough commits.
- Increase `--max-commits` or use a broader `--commit-range`.
- Confirm Python test files are detected under your project’s test tree.

## Matrix run fails to parse providers

- Check provider keys and solver references:
  - every `solver.provider` must match a key under `providers`
  - provider `kind` must be one of: `mock`, `local`, `openai_responses`,
    `anthropic_api`, `claude_cli`, `openai_compatible`, `codex_cli`,
    `opencode_server`
- Validate YAML indentation and list formatting around `solvers` and `dataset`.

## `repogauge run` reuses stale outputs unexpectedly

- Use `--resume` only when inputs are unchanged.
- If you changed matrix options, dataset path, or solver behavior, delete
  `out/run` and rerun.
- `run_id` changes in the matrix are also treated as a behavioral boundary.

## Manifest says failed early

- Inspect `out/<command>/manifest.json` and `out/<command>/events.jsonl`.
- Every command writes step markers for `bootstrap`, `inspect`, `execute`, and
  `finish` plus terminal `command.finish`.
- If `artifact_paths` contain missing paths, rerun with a clean output directory.

## Performance / cache concerns

- `mine --resume` only reuses a prior run when the scan inputs match exactly.
  Changing `--commit-range`, `--max-commits`, or the output directory invalidates
  the resume path.
- Environment-signature caches are keyed from repository and toolchain signals,
  so they should invalidate when the repo topology, Python version, test runner
  shape, or dependency set changes.
- Model-response caches are provider-specific. Invalidate them when the model
  name, provider kind, prompt template, or repository snapshot changes.
- For expensive provider calls, switch matrix profiles between `mock` and real
  providers rather than reusing stale provider config.
