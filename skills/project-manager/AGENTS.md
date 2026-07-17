# Project Manager Repo Guide

## Purpose

This directory defines the `project-manager` skill: deterministic supervision of implementation-plan execution, one slice at a time, with evidence-based gates and a bounded repair loop. This file is the maintainer's map; user-facing behavior is documented in `SKILL.md` and `README.md`.

## File Roles

- `SKILL.md`: source of truth for the operating contract — roles/topology, workflow, default operating path, the Mode B launcher, long-running command discipline, safety rules, and the trust boundary. Other documents (including the repo's top-level README) point here; do not restate the launcher or operating path elsewhere.
- `README.md`: human-facing overview — CLI examples, run-state layout, plan eligibility, profiles, privacy and data flows, and the "Verify Your Setup" trial. Defers to `SKILL.md` for the operating path.
- `references/run-state-schema.md`: durable `run.json` and slice-state semantics. Update alongside any state-shape change in `state.py`/`commands.py`.
- `references/developer-prompt.md`: the rendered slice Developer prompt and repair-prompt contracts. Any `{placeholder}` change must match `render_developer_prompt`/`render_repair_prompt` in `prompts.py`; literal braces must be escaped. The prompt embeds only `orchestrator/references/pm-slice-contract.md`, not the full transitive skill bundle.
- `references/harness-adapter-contract.md`: adapter responsibilities, tmux requirements, profile composition, validated-harness evidence, and residual coverage gaps.
- `scripts/pm.py`: thin CLI entrypoint; keep it free of logic.
- `scripts/pm_lib/`: the implementation, grouped by responsibility — `cli.py` (argument parsing), `commands.py` (command handlers), `plan.py` (plan parsing, eligibility, check-plan), `state.py` (run state I/O and audit provenance reports), `gates.py` (deterministic gate and normalized Reviewer-evidence verification), `git_ops.py` (git evidence and authorized-surface matching), `runner.py` (batch slice execution and the repair loop), `runtime.py` (slice runtime: environment/paths, Reviewer policy/credentials, artifact capture), `hints.py` (operational-hint extraction from live harness evidence), `prompts.py` (developer/repair prompt rendering), `context.py` (prior-slice-context generation and integrity), `tmux_adapter.py` (session control and capture), `observation.py` (operational hints wiring for observe/send/wait), `profiles.py` (mechanical harness profiles), `constants.py` (shared vocabulary), `models.py` (dataclasses).
- `tests/`: themed test modules split from one monolith; `pm_test_helpers.py` carries shared fixtures, fake harnesses, and the `PmTestCase` base class every module subclasses.

## Working Rules

- Deterministic acceptance is the identity of this skill: model-supervised primitives provide operational control and evidence, never acceptance. Do not add a path that accepts a slice from narration, pane text, or hints.
- Fixes strengthen the layer that owns the problem. Do not move slice-developer responsibilities (self-correction, semantic verification of reviewer output) into PM; PM makes failures visible and enforces gates.
- When a stronger deterministic contract replaces an older path, remove the obsolete path and update tests/docs together rather than preserving ambiguous compatibility.
- Keep docs honest about enforcement layers: mechanical (recomputed file authorization, commit ancestry, clean worktree), evidence-checked (drift/review/validation verdict fields plus non-empty artifacts), and heuristic (pane-marker stops). Never attribute a guarantee to the wrong layer.
- Shared vocabulary (access modes, failure signatures, plan-lint patterns) lives in the contract layer that owns it, defined once.

## Test Matrix

- Python 3.13 is the minimum supported runtime and the CI test version; authorization glob matching relies on `PurePath.full_match`.
- `python3 -m unittest discover -s tests -p 'test_*.py'` runs everything. Tests marked `@unittest.skipUnless(shutil.which("tmux"), ...)` need `tmux` on PATH; the rest are pure-Python and safe anywhere. No test needs a real coding CLI — runtime tests inject fake harnesses via `--harness-command`.
- Themed modules: `test_plan_state.py` (parsing, eligibility, approval, init, check-plan, run state), `test_prompts.py` (prompt/repair-prompt rendering), `test_harness_adapters.py` (tmux adapter, profiles, readiness, preflight, credentials), `test_observation_hints.py` (observe/send, operational hints, hard prompts), `test_gates_verification.py` (gates and reviewer evidence), `test_runtime_batch.py` (run-next/run-remaining/reconcile), `test_supervision_repair.py` (model-supervised primitives, repair state).
- Add regression tests beside the behavior they pin; keep them boundary-focused rather than permutation-heavy.

## When Changing Behavior

- State shape or generated slice/run reports → `state.py`/`commands.py` + `references/run-state-schema.md` + tests.
- Gate semantics → `gates.py` + the trust-boundary paragraph in `SKILL.md` ("Safety Rules") + tests.
- Prompt contracts → `prompts.py` render functions + `references/developer-prompt.md` + `test_prompts.py`.
- Harness support → `constants.py` profiles + `references/harness-adapter-contract.md` + `test_harness_adapters.py`; readiness/hard-prompt markers need direct observation evidence, not assumption.
- Plan parsing or lint → `plan.py` + "Plan Eligibility" in `README.md` + `implementation-plan`'s "Machine-Consumed Fields" section (that skill must keep emitting what this parser consumes).

## Verification

- `python3 -m py_compile scripts/pm.py scripts/pm_lib/*.py` after code changes.
- Full suite before any commit; the tmux-dependent tests are part of the contract, not optional extras.
- For workflow-level changes, run the "Verify Your Setup" trial in `README.md` end-to-end.
