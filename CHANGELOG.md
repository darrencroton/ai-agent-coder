# Changelog

Notable changes to this repository. Format follows [Keep a Changelog](https://keepachangelog.com/); versions follow [SemVer](https://semver.org/) once tagged. Releases are tagged from `main`; v1.0.0 will mark the first stable contract for the skills, the Master Controller CLI, and the plan format.

## [Unreleased]

### Added

- `docs/VISION.md`: the repository's timeless vision — problem, commitments, autonomy ladder, roles, personas, design principles, non-goals.
- `check-plan`: whole-plan pre-run sanity check in Master Controller (also runs automatically at `init`, failing closed on errors). Validates every slice's required sections, authorized surface, and approval flag, and lints for dependency/license-shaped authorized files, whole-repo surfaces, and Mode A-only batch groupings.
- "Privacy and Data Flows" in the master-controller README: per-seat data visibility, fully-local configurations, and an artifact sensitivity map.
- `skills/master-controller/AGENTS.md`: maintainer guide with file roles, working rules, test matrix, and change checklists.
- CI (GitHub Actions): compile checks plus both unit suites, including tmux-backed runtime tests with fake harnesses.
- `CONTRIBUTING.md`: source-of-truth map, test matrix, and change conventions.

### Changed

- Top-level README reframed around the autonomy ladder (Rung 0 → Mode A → Mode B) with a quickstart, decision table, glossary, and validation note; identity is now explicitly "autonomy system first".
- Mode taxonomy simplified from four labels (A / B / C1 / C2) to two modes: the former Mode B (autonomous single session) is now the documented autonomous alternate usage of Mode A — same session, same launcher family, standing commit authorization — and supervised autonomy under Master Controller is Mode B, model-supervised by default with a fail-closed unattended batch fallback (the former C1/C2 fork). No CLI behavior changed.
- Launcher templates single-sourced: both Mode A launchers (checkpointed and autonomous) live in `implementation-plan`'s SKILL.md, the Mode B launcher lives in `master-controller`'s SKILL.md, and the handoff resume prompt is derived from the checkpointed Mode A launcher instead of restating it.
- Master Controller SKILL.md: headline verification claim aligned with the documented trust boundary; new "Roles and Topology" section naming all four seats (supervising model, MC deterministic tools, slice orchestrator, worker) and what each may decide.
- `implementation-plan`: new "Execution Modes" section stating which plan features bind in which mode; Mode B (Master Controller) added to the launcher choices as a pointer; output rule keeping dependency/license files out of unattended authorized surfaces.
- `ai-orchestrator` SKILL.md: MC-specific requirements consolidated into one "Under Master Controller" section; skill map trimmed to skills that exist in this repository.
- `code-simplifier` rewritten in the repository's contract style: ecosystem-neutral (standards discovered from the target project), no model pin.
- `master-controller` test suite split from one 4,456-line monolith into seven themed modules plus a shared fixtures module (`mc_test_helpers.py`); test count and coverage unchanged.

### Fixed

- Flaky runtime-test fixture: the hard-prompt-at-repair fake harness now exposes a Codex-ready marker after terminal setup and waits for MC's initial prompt injection before showing its trust prompt, removing a startup race that could time the test out under system load.
- `check-plan` now rejects malformed slice-like headings instead of silently omitting their work, and rejects authorized entries that cannot match repository-relative git paths.
- CI now declares a read-only token boundary and does not persist checkout credentials; MC's Python 3.13 minimum is documented consistently with the version CI validates.
- Model-supervised startup failures now retain the underlying exception in run state instead of recording only a generic launch failure.

## [0.1.0] — 2026-07-10

Initial public import of ten modular AI engineering skills from the private bootstrap repository, including the master-controller supervision runtime, the ai-orchestrator semantic worker launcher, and the plan/implementation/audit/review/commit skill chain.
