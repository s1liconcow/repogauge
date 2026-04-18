# ADR-0002: Language adapter registry for multi-language support

## Status

Accepted

## Context

RepoGauge is moving from a Python-only v1 posture toward structured multi-language
support. The current Python-first implementation has useful invariants, but language
dispatch is spread across several seams:

- repository inspection
- environment planning
- dataset signature generation
- file-role classification
- test-output parsing
- generated harness adapter rendering
- evaluation path selection

That spread creates two risks:

- ad hoc manifest sniffing can creep into the wrong module
- the same repository can be described differently at different seams

This ADR records the architectural boundary that keeps multi-language support
deterministic. The concrete contract surface itself is documented separately in
[../language_adapters.md](../language_adapters.md); this ADR only records the
invariants and the dispatch model.

## Decision

1. Introduce a process-wide language adapter registry.
   - The registry is the only place language dispatch happens.
   - No module may sniff for `pyproject.toml`, `go.mod`, `package.json`,
     `pom.xml`, or similar manifests outside its adapter.
2. Make every language-sensitive seam adapter-owned.
   - Each adapter owns detection, inspection, environment planning, file-role
     rules, test-output parsing, and harness-template hints.
3. Make language explicit in language-agnostic data structures.
   - `RepoProfile`, `AdapterSpec`, and `EnvPlan` carry a `language` field.
   - Python compatibility aliases such as `python_hints` and `python_version`
     remain populated for Python repos, but they are derived, not authoritative.
4. Preserve existing Python outputs during the transition.
   - For an existing Python repository, generated `specs.json` and
     `adapter_*.py` artifacts must remain byte-identical across Phase 0 except
     for the new language-aware fields.
5. Keep evaluation pathing language-specific.
   - Python repositories continue to use the official SWE-bench harness.
   - Non-Python evaluation bypasses swebench's Docker provisioning and runs
     through RepoGauge's local `validate.py` worktree path instead.
6. Keep dispatch deterministic.
   - If multiple adapters report the same confidence, the lexicographically
     smaller `name()` wins.

## Consequences

### Positive

- Language routing has one authoritative seam instead of many partial checks.
- Future adapters can be added without scattering new manifest logic through
  mining, export, and judge code.
- Existing Python repositories keep stable generated artifacts during the
  transition.
- Reviewers can answer "what stops a future PR from sniffing `pyproject.toml`
  directly inside `signature.py`?" with "the adapter-registry invariant in
  ADR-0002."

### Negative

- Some adapter-specific logic will be duplicated across languages by design.
- The registry adds another piece of shared infrastructure that must stay
  deterministic and covered by tests.
- Non-Python evaluation intentionally diverges from the official SWE-bench
  harness, so the local validation path must be maintained separately.
- Each adapter can drift slightly in behavior unless its contract is kept tight.

## Alternatives Considered

### Switch on language inside each module

Rejected. This spreads dispatch across the codebase and recreates the exact
coupling problem the registry is meant to remove.

### Keep the Python-only invariant

Rejected. That blocks the multi-language roadmap and leaves RepoGauge dependent
on one language's conventions for all future work.

### Centralize detection only

Rejected. Detection is only one seam; signature generation, export, and
validation also need authoritative language-specific behavior.

## Follow-up Items

These are explicitly out of MVP scope for this ADR:

- write the canonical adapter contract document in `docs/language_adapters.md`
- land concrete adapters for Python, Go, JavaScript/TypeScript, Java, and Rust
- generalize the official harness path for non-Python languages
- add multi-language monorepo discovery and nested-manifest support
- add cross-language policy routing or cost-aware adapter selection
