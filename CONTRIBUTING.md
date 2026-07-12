# Contributing

This repository is an autonomy system first (see [`docs/VISION.md`](docs/VISION.md)); its parts are reusable skills. Changes are judged against the vision's principles — most often *one source of truth per contract*, *design for the weakest model in the loop*, and *an honest threat model, stated where it matters*.

## Vision Governance

`docs/VISION.md` is deliberately timeless and authoritative. A change that contradicts it needs either a revert or a deliberate, explicit vision revision in the same change — never a silent divergence. When a document and the vision disagree, the vision wins until it is revised.

## Source-of-Truth Map

Each contract lives in exactly one place; everything else points at it. Before editing guidance, check you are editing its home:

| Contract | Home |
|---|---|
| Why the repo exists, principles, personas, autonomy ladder | `docs/VISION.md` |
| Human-facing skill index, quickstart, glossary | `README.md` (top level) |
| Mode A launchers (checkpointed default and autonomous alternate usage) | `skills/implementation-plan/SKILL.md` → "Next Chat Prompt Format" |
| Mode B launcher and MC operating path | `skills/master-controller/SKILL.md` → "Launcher", "Default Operating Path" |
| Handoff resume prompt | Derived from the checkpointed Mode A launcher per `skills/handoff/SKILL.md` — never restated |
| Plan format and machine-consumed fields | `skills/implementation-plan/SKILL.md` |
| MC trust boundary and safety rules | `skills/master-controller/SKILL.md` → "Safety Rules" |
| Run-state schema | `skills/master-controller/references/run-state-schema.md` |
| Orchestrator and repair prompt contracts | `skills/master-controller/references/orchestrator-prompt.md` |
| Harness adapter/profile contract | `skills/master-controller/references/harness-adapter-contract.md` |
| Worker policy/request contract and access modes | `skills/ai-orchestrator/references/worker-contract.md` |
| Per-harness CLI capabilities | `skills/ai-orchestrator/references/<harness>.md` |
| Privacy and data flows, artifact sensitivity | `skills/master-controller/README.md` → "Privacy and Data Flows" |
| Maintainer guides | `skills/ai-orchestrator/AGENTS.md`, `skills/master-controller/AGENTS.md` |

Each skill's `SKILL.md` is the source of truth for its own triggers, workflow, and output format; the top-level README only indexes them.

## Tests

- Master Controller: `python3 -m unittest discover -s skills/master-controller/tests -p 'test_*.py'`. Tests needing `tmux` self-skip when it is absent; no test needs a real coding CLI (runtime tests inject fake harnesses). Themed modules and what belongs where: see `skills/master-controller/AGENTS.md` → "Test Matrix".
- AI Orchestrator: `python3 -m unittest discover -s skills/ai-orchestrator/tests -p 'test_*.py'`.
- CI runs both suites plus compile checks on every push and pull request using the minimum supported MC runtime, Python 3.13. Keep them green; never weaken a failing test to make it pass — a failing test is evidence of a real problem.
- New behavior lands with a regression test pinned beside it. Keep tests boundary-focused rather than permutation-heavy.

## Change Conventions

- **Fail closed by default.** Ambiguity stops a run; new paths must not accept work from narration, pane text, or hints.
- **Fix the owning layer.** Root-cause first; strengthen the contract layer that owns the problem (one definition, all harnesses) rather than patching a symptom where it appeared. Do not migrate slice-orchestrator responsibilities into MC.
- **Replace, don't shadow.** When a stronger deterministic contract replaces an older path, remove the obsolete path and update tests and docs in the same change.
- **Be honest about enforcement.** Every documented guarantee names its layer: mechanical, evidence-checked, or heuristic. Overclaiming is a defect.
- **Never bypass hooks** (`--no-verify` is off the table); fix the issue and commit again.
- **Archive, don't delete.** Superseded files move to `archive/` (gitignored) rather than being removed outright.
- **Commit messages** carry a short imperative summary plus a body listing every changed file with its reason, grouped logically.

## Releases

Tag from `main` using SemVer; update `CHANGELOG.md` (Keep a Changelog format) in the same change. v1.0.0 marks the first stable contract for the skills, the MC CLI, and the plan format — until then, minor versions may adjust contracts with a changelog entry.
