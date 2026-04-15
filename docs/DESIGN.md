Here’s the v1 I would ship:

A **Python-first, local-first mining tool** that turns one repo into a **private SWE-bench-style dataset plus a tiny generated harness adapter**. That adapter is the important part: the current SWE-bench harness is Docker-based, the main local entry point is `swebench.harness.run_evaluation`, and it can load a local JSON/JSONL/Parquet dataset path plus a predictions file. But for unseen repos, the harness code still looks up repo/version/language/parser in internal maps like `MAP_REPO_VERSION_TO_SPECS`, `MAP_REPO_TO_EXT`, and `MAP_REPO_TO_PARSER`, so a dataset file by itself is not enough for arbitrary repositories. ([SWE-bench][1])

The official dataset shape you want to target already exists: instances carry fields like `instance_id`, `repo`, `base_commit`, `problem_statement`, `version`, `patch`, `test_patch`, `FAIL_TO_PASS`, and `PASS_TO_PASS`; predictions are JSONL objects with `instance_id`, `model_name_or_path`, and `model_patch`. The harness also accepts `FAIL_TO_PASS` and `PASS_TO_PASS` as arrays or JSON-encoded strings, which makes local export simpler. ([SWE-bench][3])

## 1. What the first OSS release should do

Ship one CLI, no service, no database, no web UI.

Core commands:

```bash
repogauge mine /path/to/repo --out ./out
repogauge review ./out/candidates.jsonl
repogauge export ./out/reviewed.jsonl --dataset ./out/dataset
repogauge eval ./out/dataset/dataset.jsonl --gold
```

What `repogauge mine` should produce:

* `candidates.jsonl`: all scanned commits with heuristic and LLM scores
* `review.html` or `review.md`: human-readable shortlist
* `dataset.jsonl`: SWE-bench-style local dataset
* `predictions.gold.jsonl`: predictions file built from each instance’s gold `patch`
* `adapter/`: generated harness registration code for this repo
* `validation.jsonl`: per-instance validation evidence and failure reasons
* `logs/`: raw test/build output

The key UX goal is: **point it at a repo, get back a local dataset that can be evaluated with the official harness through the generated wrapper**.

## 2. Scope the first release aggressively

I would keep v1 to this scope:

1. **Python repos only**
2. **Single-parent commits and squash-merge style fixes only**
3. **Default branch history only**
4. **Commits that already include regression tests or obvious test-support changes**
5. **Local repo input first**, optional GitHub API enrichment second

I would explicitly not do, in v1:

* multi-language generality
* multi-commit PR reconstruction
* synthetic test generation
* automatic benchmark publishing
* cloud orchestration

That keeps the project small enough to be genuinely self-runnable while matching how the current harness expects repo/version-specific execution instructions. The official harness also uses separate base, environment, and instance image layers, so keeping the problem narrow lets you reuse images aggressively instead of rebuilding everything per task. ([SWE-bench][1])

## 3. End-to-end pipeline

### Stage A: inspect the repo

Input:

* local path
* optional branch
* optional commit range
* optional max commits to inspect

Detect:

* default branch
* package manager (`pyproject.toml`, `setup.py`, `requirements*.txt`, `tox.ini`, `noxfile.py`)
* likely test runner (`pytest`, `unittest`, `tox`, `nox`)
* likely Python version from CI files, `requires-python`, `.python-version`, or tox config

Output:

* `repo_profile.json`

This stage should be 100% heuristic and deterministic.

### Stage B: discover candidate commits

Walk recent history and score commits. Good first-pass filters:

Hard rejects:

* merge commits
* reverts
* docs-only changes
* formatting-only changes
* dependency-only bumps
* vendored or generated file changes
* huge refactors
* commits that only rename/move files

Strong positives:

* touches both `src/`-like files and `tests/`-like files
* commit message contains `fix`, `bug`, `regression`, `error`, `handle`, `crash`, `fails`, `incorrect`
* small to medium diff
* linked issue/PR exists
* test additions include new assertions or new test functions

Good default thresholds:

* 1–8 production files changed
* 1–5 test files changed
* under ~600 total changed lines
* under ~12 total changed hunks
* no more than 2 config/build files unless they are test-support files

Output:

* `scan.jsonl` with one row per commit and a numeric `heuristic_score`

### Stage C: optional LLM triage

Only send the top heuristic candidates to a model.

Ask the model for strict JSON:

```json
{
  "is_bugfix_eval": true,
  "confidence": 0.86,
  "reason": "Short bugfix with regression tests",
  "prod_files": ["package/module.py"],
  "test_files": ["tests/test_module.py"],
  "test_support_files": ["pytest.ini"],
  "problem_statement_draft": "...",
  "environment_hints": {
    "python": "3.11",
    "install": ["pip install -e .", "pip install pytest"],
    "test_cmd": "pytest"
  }
}
```

Use the LLM for:

* commit classification
* file-role classification
* issue-style problem statement synthesis
* environment fallback hints when heuristics fail

Do **not** let the LLM be the source of truth for whether an instance is valid. It only proposes; the validator proves.

For integration, prefer CLI-first in v1: the current official non-interactive paths are `codex exec` for Codex and `claude -p` for Claude Code. Claude also has official Python and TypeScript Agent SDKs, while Codex has a TypeScript SDK and its Python SDK is currently marked experimental and requires a local Codex checkout, which makes the CLI path the simpler first release. Claude print mode also supports structured JSON output and JSON Schema validation. ([OpenAI Developers][3])

## 4. How each instance should be built

For each accepted candidate commit:

### Base commit

Use the candidate commit’s parent as `base_commit`.

### Gold patch split

Split the fixing commit into:

* `patch`: production-code diff only
* `test_patch`: tests plus test-support diff only

File classes for `test_patch`:

* `tests/**`
* `test/**`
* `*_test.py`
* `test_*.py`
* fixtures under test directories
* test config such as `pytest.ini`, `tox.ini`, `conftest.py`, only if needed for the regression test to run

Everything else stays in `patch`.

### Problem statement

Priority order:

1. linked issue title/body
2. PR title/body
3. commit message + diff summary
4. LLM-generated issue-style restatement

Good rule: the exported `problem_statement` should read like a GitHub issue, not like a commit message.

### Version key

For private tasks, make `version` a stable environment key, for example:

```text
0.9.2__py311__pytest__reqhash_4f2c1e2b
```

That keeps the field compatible with the harness expectation that `version` selects installation instructions. SWE-bench’s own versioning docs make clear that version information is used to determine the correct setup/install path for evaluation. ([SWE-bench][4])

### Instance id

Use something stable and collision-resistant:

```text
owner__repo-rg-<shortsha>
```

Example:

```text
pallets__flask-rg-a1b2c3d
```

## 5. How to derive `FAIL_TO_PASS` and `PASS_TO_PASS`

This is the most important part of the tool.

The validator should perform four runs:

### Run 1: base sanity

Checkout `base_commit`, install env, run the targeted test command.
Purpose: ensure the repo can run at all.

### Run 2: base + test_patch

Apply only `test_patch`.
Run targeted tests.
Collect failing and passing test case IDs.

### Run 3: base + patch + test_patch

Apply both `patch` and `test_patch`.
Run the same targeted tests.
Collect failing and passing IDs again.

### Run 4: rerun flake check

Repeat Run 2 and Run 3 at least once more.
Reject candidates with unstable outcomes.

Then compute:

* `FAIL_TO_PASS` = tests that fail in Run 2 and pass in Run 3
* `PASS_TO_PASS` = tests that pass in both Run 2 and Run 3

Acceptance rule for export:

* at least one `FAIL_TO_PASS`
* no `PASS_TO_PASS` regressions
* identical outcomes across reruns
* `patch` and `test_patch` both apply cleanly
* full gold validation resolves in harness

This matches the official grading idea: fail-to-pass measures resolution, pass-to-pass measures regression safety, and an instance is fully resolved only when both are perfect. ([SWE-bench][5])

## 6. The harness-wrapper design

This is the cleanest v1 architecture.

### Why a wrapper is needed

For unseen repos, the harness currently builds `TestSpec` by reading:

* `MAP_REPO_VERSION_TO_SPECS[repo][version]`
* `MAP_REPO_TO_EXT[repo]`

and grading selects

* `MAP_REPO_TO_PARSER[repo]`

So the OSS tool should generate a tiny adapter module that registers those entries at runtime, then calls the official harness. ([GitHub][6])

### What the adapter should generate

For each repo/version key:

```python
MAP_REPO_TO_EXT["owner/repo"] = "py"

MAP_REPO_VERSION_TO_SPECS["owner/repo"][version_key] = {
    "docker_specs": {"python_version": "3.11"},
    "pre_install": [...],
    "install": [...],
    "build": [...],      # optional
    "test_cmd": ["python -m pytest --junitxml=/tmp/rg-junit.xml"]
}
MAP_REPO_TO_PARSER["owner/repo"] = parse_repogauge_junit
```

### Best parser strategy for v1

Do not try to parse every repo’s native stdout format.

Instead:

* force pytest to emit JUnit XML
* print the XML to stdout or a delimited block in logs
* parse that with one generic parser

That gives you one parser for almost every Python repo.

### Wrapper entrypoint

`repogauge eval` should:

1. import the generated adapter
2. register repo mappings
3. optionally generate gold predictions if `--gold`
4. dispatch to `swebench.harness.run_evaluation`

That gives users “official harness underneath” with minimal glue.

## 7. Recommended heuristics in detail

### File-role classification

Use path heuristics first:

Production:

* `src/**`
* package dirs
* non-test Python modules

Tests:

* `tests/**`
* `test/**`
* `*_test.py`
* `test_*.py`

Test-support:

* `conftest.py`
* fixtures
* `pytest.ini`
* `tox.ini`
* test-only helper modules

Everything ambiguous goes to LLM classification.

### Candidate score formula

Start with something like:

* `+4` touches both prod and tests
* `+3` small/medium patch
* `+3` bugfix-like message
* `+2` linked issue or PR
* `+2` test additions
* `-3` large refactor
* `-4` docs/chore/release/dependency-only
* `-4` no test changes
* `-5` validation instability
* `-5` env install failure

Then define:

* `score >= 8`: auto-shortlist
* `5 <= score < 8`: LLM review queue
* `< 5`: reject

### Commit shapes to prefer

Best v1 candidates are:

* single-commit bugfixes
* squash merges that include the regression test
* localized changes
* test failures reproducible in one test file or one package

### Commit shapes to avoid

* broad API renames
* migration-heavy changes
* flaky integration tests
* network-dependent tests
* snapshot churn
* style or lint-only changes
* changes where the “test” is actually benchmark/demo code

## 8. Environment generation strategy

Use a ladder, not one-shot guessing.

### Level 1: deterministic heuristics

Infer:

* Python version
* install command
* test command

Examples:

* `pyproject.toml` + pytest → `pip install -e . && pip install pytest`
* `requirements-dev.txt` → `pip install -r requirements-dev.txt`
* `tox.ini` but no easy pytest path → use tox only if pytest direct invocation fails

### Level 2: dry-run correction

Run install and a tiny smoke test on HEAD.
If it fails, adjust with deterministic fallbacks:

* upgrade pip/setuptools/wheel
* install `.[test]`, `.[dev]`, or `.[tests]`
* fallback to `python -m pytest`

### Level 3: LLM rescue

If heuristics fail, ask Claude/Codex for a minimal install recipe from:

* `pyproject.toml`
* CI config
* tox/nox config
* error logs

Keep the accepted result after a successful dry run.

### Cache by environment signature

Group instances by identical environment signature so many tasks share one generated `version` key. That aligns with the harness image layering and keeps builds cheap. ([SWE-bench][1])

## 9. Dataset schema to export

Use the official field names plus extra metadata.

```json
{
  "instance_id": "owner__repo-rg-a1b2c3d",
  "repo": "owner/repo",
  "issue_id": null,
  "base_commit": "abc123...",
  "problem_statement": "Calling foo() with bar=None crashes when ...",
  "version": "0.9.2__py311__pytest__reqhash_4f2c1e2b",
  "issue_url": null,
  "pr_url": "https://github.com/owner/repo/pull/1234",
  "patch": "diff --git ...",
  "test_patch": "diff --git ...",
  "created_at": "2026-04-15T00:00:00Z",
  "FAIL_TO_PASS": ["tests/test_foo.py::test_none_regression"],
  "PASS_TO_PASS": ["tests/test_foo.py::test_existing_behavior"],
  "metadata": {
    "source_commit": "a1b2c3d...",
    "heuristic_score": 11,
    "llm_confidence": 0.88,
    "install_strategy": "pyproject+pytest",
    "test_strategy": "pytest-junit",
    "flake_runs": 2
  }
}
```

Extra fields are fine for your local workflow because the harness code only reads the fields it needs to build the `TestSpec`; additional metadata can ride along for debugging and curation. ([GitHub][6])

## 10. OSS repo layout

```text
repogauge/
  pyproject.toml
  README.md
  repogauge/
    cli.py
    config.py
    git_scan.py
    scoring.py
    classify.py
    synthesize.py
    split_patch.py
    env_detect.py
    validate.py
    dataset_export.py
    harness_adapter.py
    parsers/
      junit.py
    llm/
      base.py
      claude_cli.py
      claude_sdk.py
      codex_cli.py
      codex_sdk.py
  examples/
    config.yaml
  tests/
    test_scoring.py
    test_patch_split.py
    test_junit_parser.py
    test_dataset_export.py
```

## 11. Milestones

### Milestone 1: deterministic miner

Deliver:

* repo inspection
* commit scan
* heuristic scoring
* human-readable shortlist

Exit criterion:

* users can point at a repo and get a ranked candidate list

### Milestone 2: instance materializer

Deliver:

* patch/test patch split
* problem statement synthesis
* dataset export
* gold predictions export

Exit criterion:

* tool emits syntactically valid SWE-bench-style dataset rows

### Milestone 3: validator

Deliver:

* env detection
* targeted test runs
* `FAIL_TO_PASS` / `PASS_TO_PASS` extraction
* flake filtering

Exit criterion:

* exported tasks have validated test semantics

### Milestone 4: harness wrapper

Deliver:

* generated repo/version specs
* generic JUnit parser
* `repogauge eval --gold`

Exit criterion:

* gold predictions resolve every exported instance through the official harness wrapper

### Milestone 5: model-assisted mode

Deliver:

* Claude/Codex classification
* env rescue
* better problem statements
* response caching

Exit criterion:

* better recall without losing determinism in final validation

## 12. Acceptance criteria for the first release

I would treat v1 as successful if it guarantees all of this:

* every exported instance has at least one `FAIL_TO_PASS`
* no exported instance shows flakiness across repeated validation runs
* the generated gold predictions resolve every exported instance through `repogauge eval`
* a clean machine with git, Docker, Python, and optional model CLI access can run the tool end-to-end
* users can disable all LLM calls and still get a useful deterministic pipeline
* the tool never sends repository contents externally unless the user explicitly enables a model provider

## 13. One design choice I would make immediately

I would **not** try to make “dataset only” the artifact for arbitrary repos.

I would make the artifact pair be:

1. `dataset.jsonl`
2. `adapter/repogauge_<repo>.py`

That is the smallest honest unit that actually runs with today’s harness for previously unsupported repositories. The dataset stays SWE-bench-shaped, and the adapter keeps you off a hard fork.

If you want, the next useful step is turning this plan into a concrete command spec and module-by-module implementation checklist.

[1]: https://www.swebench.com/SWE-bench/reference/harness/ "https://www.swebench.com/SWE-bench/reference/harness/"
[2]: https://www.swebench.com/SWE-bench/guides/datasets/ "https://www.swebench.com/SWE-bench/guides/datasets/"
[3]: https://developers.openai.com/codex/noninteractive "https://developers.openai.com/codex/noninteractive"
[4]: https://www.swebench.com/SWE-bench/reference/versioning/ "https://www.swebench.com/SWE-bench/reference/versioning/"
[5]: https://www.swebench.com/SWE-bench/api/harness/ "https://www.swebench.com/SWE-bench/api/harness/"
[6]: https://github.com/SWE-bench/SWE-bench/blob/main/swebench/harness/test_spec/test_spec.py "https://github.com/SWE-bench/SWE-bench/blob/main/swebench/harness/test_spec/test_spec.py"

## Experiment runner

The cleanest shape is to keep model names as plain config strings, not hardcoded enums, because the providers already expose them that way. Anthropic’s current public aliases include `claude-opus-4-6` and `claude-sonnet-4-6`; OpenAI currently recommends `gpt-5.4` as the default and `gpt-5.4-mini` as the smaller variant, and Codex config accepts a `model` string such as `gpt-5.4`; OpenCode runs models as `provider/model`; and Kimi K2.5 exposes an OpenAI-compatible API. ([Claude API Docs][1])

## What I’d add to the OSS tool to enable running experiments

I’d turn the project into four top-level commands:

```bash
repogauge mine /path/to/repo --out ./artifact
repogauge run ./matrix.yaml --dataset ./artifact/dataset.jsonl --run-id q2_baseline
repogauge analyze ./runs/q2_baseline
repogauge train-router ./runs/q2_baseline/router_train.parquet
```

That keeps the original mining workflow, but adds a repeatable experiment system for “run these N solvers against these M mined tasks, then judge all patches with the SWE-bench harness, then produce cost/quality reports.”

## The one architectural decision that matters

Do **not** combine “agent solving” and “SWE-bench judging” into one monolithic worker.

Make them two separate queues:

1. **Solver queue**: `(instance_id, solver_id, seed)` → produce `model_patch`
2. **Judge queue**: `(instance_id, solver_id, model_patch)` → run official harness evaluation

That separation matters because the harness is Docker-heavy and parallelized independently via `--max_workers`; the official docs also recommend substantial local resources and caution against overprovisioning workers. Keeping solver concurrency and judge concurrency separate prevents your expensive Docker layer from becoming the bottleneck for model throughput. ([SWE-bench][2])

## Proposed system layout

I’d split the codebase into these subsystems:

### 1. Miner

Exactly what we already discussed:

* scan repo history
* identify likely single-commit bugfix evals
* materialize SWE-bench-style instances
* generate harness adapter
* validate gold patches

### 2. Matrix runner

New subsystem:

* reads `matrix.yaml`
* expands into jobs `(instance × solver × repeat)`
* schedules jobs with per-provider rate limits and per-solver budgets
* writes normalized predictions files per solver

### 3. Judge

New subsystem:

* consumes predictions
* runs the generated repo adapter plus `swebench.harness.run_evaluation`
* stores official per-instance results and logs

### 4. Analyzer

New subsystem:

* merges attempt telemetry with harness results
* computes cost / latency / resolution tradeoffs
* emits HTML + CSV + Parquet + router training data

## Recommended command/config model

I’d use one config file for the whole matrix:

```yaml
dataset:
  path: ./artifact/dataset.jsonl
  adapter: ./artifact/adapter
  split: private
  instance_filter: null

execution:
  workspace_root: ./.repogauge/workspaces
  runs_root: ./runs
  solver_workers: 10
  judge_workers: 4
  retries: 1
  shuffle: true
  repeat_seeds: [0]
  timeout_minutes_per_attempt: 45

fairness:
  prompt_template: swebench_like_v1
  expose_hidden_tests: false
  allow_network: false
  max_turns: 40
  same_repo_snapshot_per_solver: true
  same_tool_policy_per_solver_family: true

providers:
  anthropic:
    kind: anthropic_api
    api_key_env: ANTHROPIC_API_KEY
  openai:
    kind: openai_responses
    api_key_env: OPENAI_API_KEY
  codex_local:
    kind: codex_cli
  opencode_local:
    kind: opencode_server
    base_url: http://127.0.0.1:4096
  moonshot:
    kind: openai_compatible
    api_key_env: MOONSHOT_API_KEY
    base_url: https://api.moonshot.ai/v1

solvers:
  - id: claude_sonnet
    adapter: claude_agent_sdk
    provider: anthropic
    model: claude-sonnet-4-6
    reasoning: medium
    budget:
      max_cost_usd: 2.50
      max_input_tokens: 200000
      max_output_tokens: 32000

  - id: claude_opus
    adapter: claude_agent_sdk
    provider: anthropic
    model: claude-opus-4-6
    reasoning: medium
    budget:
      max_cost_usd: 8.00
      max_input_tokens: 200000
      max_output_tokens: 64000

  - id: codex_5_4
    adapter: codex_cli
    provider: codex_local
    model: gpt-5.4
    reasoning_effort: medium
    verbosity: low
    budget:
      max_cost_usd: 3.00

  - id: codex_5_4_mini
    adapter: codex_cli
    provider: codex_local
    model: gpt-5.4-mini
    reasoning_effort: low
    verbosity: low
    budget:
      max_cost_usd: 1.00

  - id: opencode_kimi
    adapter: opencode_server
    provider: opencode_local
    model: moonshot/kimi-k2.5
    agent: build
    budget:
      max_cost_usd: 1.50
```

Two important choices here:

First, I would keep **providers** and **solvers** separate. A provider is “how to reach a backend.” A solver is “the exact behavioral config you want to benchmark.” That makes it easy to compare `codex_5_4` vs `codex_5_4_high_reasoning` without duplicating credentials.

Second, I would keep the matrix config **declarative**. Organizations will want to check these benchmark definitions into Git and rerun them later.

## Which adapters I would support first

For Anthropic, I would make the **Claude Agent SDK/API path** the primary production adapter, because Anthropic’s Agent SDK is programmable in Python and TypeScript and gives you the Claude Code agent loop as a library. I would still keep a `claude -p` convenience adapter for local experimentation, but not make it the gold path for analytics. Claude Code also supports OpenTelemetry export for usage, cost, and tool activity, but Anthropic documents those cost metrics as approximations and says official billing should come from the provider. ([Claude API Docs][3])

For Codex, I would make **`codex exec --json`** the first-class adapter. The official CLI emits JSONL events in non-interactive mode, and Codex supports OpenTelemetry with structured events that include token counts on `response.completed`. That gives you an auditable stream without scraping terminal text. ([OpenAI Developers][4])

For OpenCode, I would prefer **`opencode serve` + HTTP/SDK** for scaled runs, with `opencode run --format json` as the fallback CLI adapter. The docs explicitly recommend `opencode serve` for programmatic use, `opencode run` supports non-interactive execution with model selection and JSON output, and OpenCode can attach to an already-running server to avoid cold-start overhead. OpenCode also has built-in session stats and export commands for token usage, cost, and model breakdown. ([OpenCode][5])

For Kimi, I would support it two ways:

* directly via an OpenAI-compatible adapter using Moonshot’s base URL
* indirectly through OpenCode, if the org already standardizes on OpenCode as a multi-provider agent shell

Moonshot’s docs say Kimi K2.5 is OpenAI-SDK-compatible and supports a 256K context window. OpenCode’s provider system is also explicitly designed around configurable providers and base URLs. ([Moonshot AI][6])

## How one benchmark attempt should run

Each `(instance, solver)` job should look like this:

1. Create an isolated `git worktree` at `base_commit`
2. Materialize a standard agent instruction pack
3. Invoke the solver adapter
4. Capture either:

   * direct unified diff output, or
   * file edits + `git diff`
5. Normalize the patch
6. Run cheap local validity checks
7. Queue the patch for official harness judging

The agent should see:

* the checked-out repo
* the `problem_statement`
* the standard allowed tools
* an instruction to return or leave behind a patch

It should **not** see:

* the gold `patch`
* the gold `test_patch`
* hidden evaluation results

That keeps the evaluation aligned with the spirit of SWE-bench rather than accidentally turning it into supervised reconstruction.

## Fairness rules I would enforce

This is where benchmark projects usually get noisy.

For each solver family, I would fix:

* the same repo snapshot
* the same problem statement
* the same allowed tools, unless the experiment is explicitly “tool set ablation”
* the same timeout budget
* the same maximum turn budget
* the same patch extraction rules
* the same postprocessing

I would also store a **prompt pack version** so every run is reproducible:

* `solver_prompt_version`
* `instruction_pack_hash`
* `tool_policy_hash`

That prevents “Sonnet beat Opus” from secretly meaning “Sonnet got a better prompt.”

## Parallel scheduling plan

The scheduler should be provider-aware, not just thread-aware.

I’d implement:

* a **global ready queue**
* **per-provider semaphores**
* **per-provider rate limiters**
* **per-solver budget guards**
* **separate judge queue**

A good default is:

* `solver_workers`: as many as your local CPU/network can handle
* `judge_workers`: much smaller, because SWE-bench Docker evaluation is the expensive part

The harness already supports `--max_workers`, cache levels, and heavy Docker reuse, so the judge queue should batch predictions by solver and reuse the same dataset adapter and image cache instead of spawning one-off harness processes per patch. ([SWE-bench][2])

## Artifacts I would persist

Per run:

* `runs/<run_id>/matrix.yaml`
* `runs/<run_id>/attempts.parquet`
* `runs/<run_id>/jobs.jsonl`
* `runs/<run_id>/predictions/<solver>.jsonl`
* `runs/<run_id>/eval/<solver>/results.json`
* `runs/<run_id>/eval/<solver>/instance_results.jsonl`
* `runs/<run_id>/eval/<solver>/run_logs/...`
* `runs/<run_id>/report.html`
* `runs/<run_id>/router_train.parquet`

Per attempt row:

* instance metadata
* solver config
* start/end timestamps
* exit reason
* patch validity
* patch size
* files changed
* tool count
* latency
* token usage
* estimated cost
* official harness outcome

## How I’d collect usage and cost

This is worth being strict about:

Use **provider-reported usage whenever possible**. OpenAI’s Responses API returns structured `usage` including input, output, total, and reasoning tokens; Anthropic documents reported `usage` metrics for API responses; Codex emits token counts in its telemetry; OpenCode exposes session stats and exports. ([OpenAI Developers][7])

My rule would be:

* **Tier 1**: exact provider usage from API response or telemetry
* **Tier 2**: exact CLI/session telemetry
* **Tier 3**: fallback estimated cost from local pricing catalog

And I would store both:

* `usage_source = exact_api | exact_cli | estimated`
* `cost_source = exact_provider | estimated_catalog`

That lets you later say, honestly, which numbers are authoritative and which are approximations.

## The report the org actually wants

The first HTML report should answer six questions:

1. **Raw resolution**
   “How often did each solver resolve the task?”

2. **Cost efficiency**
   “What was cost per resolved issue?”

3. **Latency efficiency**
   “How long did each solver take per resolved issue?”

4. **Marginal value of expensive models**
   “What did Opus solve that Sonnet missed?”

5. **By-cluster differences**
   “Which classes of tasks actually need the expensive model?”

6. **Routing opportunities**
   “How much spend could we save with a simple escalation policy?”

The most useful derived metrics are:

* `coverage_vs_expensive(cheap, expensive) = resolved_by_cheap / resolved_by_expensive`
* `exclusive_expensive_rate = resolved_by_expensive_and_not_cheap / total_instances`
* `marginal_cost_per_extra_resolve = (total_cost_expensive - total_cost_cheap) / extra_resolves`
* `pareto_frontier` over `(resolution, cost, latency)`

That gives you statements like:

* “Sonnet captured 83% of Opus’s wins.”
* “Only 6% of tasks were Opus-exclusive.”
* “The extra resolve cost for Opus over Sonnet was $41 per additional solved change.”

That is exactly the input you need before you even try a router.

## What to cluster tasks by

Do not just report one global scoreboard. Break results down by task features the organization can act on.

I’d cluster by:

* repo or subsystem
* bug category
* environment complexity
* likely edit breadth
* context size
* test/reproduction complexity
* static localization ambiguity

The features should be **available before solving**, or at worst after a cheap probe, so they can feed a future router.

Useful leak-free features:

* repo size and package layout
* test runner and env complexity
* problem statement length
* stack trace present / absent
* number of likely files from static retrieval
* number of symbols matching error terms
* cheap model probe outcome
* whether the cheap model finds a plausible target file quickly
* whether the cheap model emits a syntactically valid patch
* whether the cheap model’s patch applies cleanly

Do **not** use gold-derived features like true changed-file count from the historical fix, because those leak the answer.

## The router plan

I would not start with a fancy multi-model router. I would stage it.

### Stage 1: full matrix collection

Run the full solver matrix on a representative sample of private tasks.

Goal:

* learn the real cost/quality frontier
* build clean training data

### Stage 2: simple policy baselines

Before ML, compare:

* always cheap
* always expensive
* cheap then escalate on failure
* cheap then escalate on invalid patch / timeout / no-progress

You will often find that a simple escalation policy already captures most of the savings.

### Stage 3: supervised router

Train a classifier on leak-free pre-attempt features to predict one of:

* `cheap_is_enough`
* `needs_expensive`
* `likely_unsolved`

Start with gradient-boosted trees, not a neural net. The tabular features here are usually strong.

### Stage 4: policy evaluation

Evaluate policies offline on the full matrix:

* resolve rate
* average cost
* p95 latency
* regret vs oracle
* abstain/escalation rate

### Stage 5: deployed router with exploration

In production, do:

* route most tasks
* keep 10–20% exploration / shadow evaluations
* retrain periodically

That prevents the router from drifting as repos, prompts, and model versions change.

## One extra layer I would add for organizations

I’d add an optional **gateway mode**.

Instead of every adapter talking directly to Anthropic/OpenAI/Moonshot, let the tool optionally route through an internal LLM gateway that:

* centralizes auth
* logs usage
* enforces spend limits
* applies policy
* redacts secrets

This is especially attractive because both Codex and OpenCode support custom provider/base URL configuration, so you can standardize observability without forking each client. ([OpenAI Developers][8])

## Milestones I’d ship

### Milestone A

Keep your existing miner/exporter plan, but add:

* `repogauge run`
* solver abstraction
* predictions per solver
* harness judging per solver

Exit criterion:

* one config file runs Sonnet, Opus, Codex, and OpenCode/Kimi across one mined dataset

### Milestone B

Add telemetry normalization:

* exact usage capture
* cost estimation
* attempt-level parquet logs
* HTML report

Exit criterion:

* you can answer “which solver is best under budget X?”

### Milestone C

Add cluster analytics:

* task stratification
* marginal-win analysis
* cheap-vs-expensive upgrade report

Exit criterion:

* you can answer “when do we actually need Opus?”

### Milestone D

Add router training and offline policy eval

Exit criterion:

* you can compare “always Sonnet” vs “Sonnet→Opus escalate” vs learned router on historical tasks

## My strongest recommendation

For the first serious benchmark release, I would make the public story:

**“RepoGauge mines private SWE-bench-style tasks from your repo, runs a configurable matrix of coding agents against them, evaluates every patch with the official SWE-bench harness, and produces the spend/quality data needed to decide when premium models are worth it.”**

That framing is much stronger than “dataset miner,” because it gets you all the way to the org decision you actually care about.

The next useful thing to do is turn this into a concrete `matrix.yaml` schema plus the exact attempt/result parquet schemas.

[1]: https://docs.anthropic.com/en/docs/about-claude/models "Models overview - Claude API Docs"
[2]: https://www.swebench.com/SWE-bench/reference/harness/ "The Harness - SWE-bench"
[3]: https://docs.anthropic.com/en/docs/claude-code/sdk?utm_source=chatgpt.com "Agent SDK overview - Claude Code Docs"
[4]: https://developers.openai.com/codex/noninteractive "Non-interactive mode – Codex | OpenAI Developers"
[5]: https://opencode.ai/docs/server/ "Server | OpenCode"
[6]: https://platform.moonshot.ai/docs/guide/kimi-k2-5-quickstart "Kimi K2.5 - Kimi API Platform"
[7]: https://developers.openai.com/api/reference/resources/responses/methods/create/ "Create a model response | OpenAI API Reference"
[8]: https://developers.openai.com/codex/config-advanced "Advanced Configuration – Codex | OpenAI Developers"

