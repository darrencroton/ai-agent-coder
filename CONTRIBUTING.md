# Contributing

This repository is an autonomy system first (see [`docs/VISION.md`](docs/VISION.md)); its parts are reusable skills. Changes are judged against the vision's principles — most often *one source of truth per contract*, *mechanise the floor; judge the rest*, *minimise the whole system*, and *an honest threat model, stated where it matters*.

## Vision Governance

`docs/VISION.md` is deliberately timeless and authoritative. A change that contradicts it needs either a revert or a deliberate, explicit vision revision in the same change — never a silent divergence. When a document and the vision disagree, the vision wins until it is revised.

## Source-of-Truth Map

Each contract lives in exactly one place; everything else points at it. Before editing guidance, check you are editing its home:

| Contract | Home |
|---|---|
| Why the repo exists, principles, personas, autonomy ladder | `docs/VISION.md` |
| Human-facing skill index, quickstart, glossary | `README.md` (top level) |
| Mode A launchers (checkpointed default and autonomous alternate usage) | `skills/implementation-plan/SKILL.md` → "Next Chat Prompt Format" |
| Mode B launcher and PM operating contract | `skills/project-manager/SKILL.md` → "Launcher", "Workflow" |
| Handoff resume prompt | Derived from the checkpointed Mode A launcher per `skills/handoff/SKILL.md` — never restated |
| Plan format and machine-consumed fields | `skills/implementation-plan/SKILL.md` |
| PM charter, floor facts, and always-stop rules | `skills/project-manager/SKILL.md` → "Charter", "The floor", "Always stop" |
| PM trust model and known limits | `skills/project-manager/README.md` → "Trust model, honestly" |
| Run-state layout and authority model | `skills/project-manager/references/run-state.md` |
| Developer and Reviewer prompt contracts | `skills/project-manager/references/developer-prompt.md`, `…/reviewer-prompt.md` |
| Harness profile table | `skills/project-manager/scripts/pm_lib/profiles.py` |
| Reviewer request contract and read-only semantics | `skills/orchestrator/references/reviewer-contract.md` |
| Per-harness CLI capabilities | `skills/orchestrator/references/<harness>.md` |
| Privacy and artifact sensitivity | `skills/project-manager/README.md` → "Privacy & sensitive artifacts" |
| Maintainer guides | `skills/orchestrator/AGENTS.md`; `skills/project-manager/README.md` → "Maintainer map" |

Each skill's `SKILL.md` is the source of truth for its own triggers, workflow, and output format; the top-level README only indexes them.

## Tests

- Project Manager: `python3 -m unittest discover -s skills/project-manager/tests -p 'test_*.py'`. Tests needing `tmux` self-skip when it is absent; no test needs a real coding CLI (runtime tests inject fake harnesses). Module layout: `skills/project-manager/README.md` → "Maintainer map".
- Orchestrator: `python3 -m unittest discover -s skills/orchestrator/tests -p 'test_*.py'`.
- CI runs both suites plus compile checks on every push and pull request using the minimum supported PM runtime, Python 3.13. Keep them green; never weaken a failing test to make it pass — a failing test is evidence of a real problem.
- New behavior lands with a regression test pinned beside it. Keep tests boundary-focused rather than permutation-heavy.

## Change Conventions

- **Fail closed by default.** Ambiguity stops a run; new paths must not accept work from narration, pane text, or hints.
- **Fix the owning layer.** Root-cause first; strengthen the contract layer that owns the problem (one definition, all harnesses) rather than patching a symptom where it appeared. Do not migrate slice Developer responsibilities into PM.
- **Replace, don't shadow.** When a stronger deterministic contract replaces an older path, remove the obsolete path and update tests and docs in the same change.
- **Be honest about enforcement.** Every documented guarantee names its layer: mechanical, evidence-checked, or heuristic. Overclaiming is a defect.
- **Never bypass hooks** (`--no-verify` is off the table); fix the issue and commit again.
- **Archive, don't delete.** Superseded files move to `archive/` (gitignored) rather than being removed outright.
- **Commit messages** carry a short imperative summary plus a body listing every changed file with its reason, grouped logically.

## Releases

Tag from `main` using SemVer; update `CHANGELOG.md` (Keep a Changelog format) in the same change. v1.0.0 marks the first stable contract for the skills, the PM CLI, and the plan format — until then, minor versions may adjust contracts with a changelog entry.
