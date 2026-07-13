# Changelog

Notable changes to this repository. Format follows [Keep a Changelog](https://keepachangelog.com/); versions follow [SemVer](https://semver.org/) once tagged. Releases are tagged from `main`; v1.0.0 will mark the first stable contract for the skills, the Master Controller CLI, and the plan format.

## [Unreleased]

## [0.5.0] — 2026-07-13

### Added

- Mode B residual-finding propagation: every passing orchestrator result carries a structured `residual_findings` ledger, MC writes a per-slice `slice-summary.md`, and every run-state update refreshes an aggregate `run-report.md` so non-blocking post-plan considerations survive fresh sessions and repair rounds.

### Changed

- **Breaking:** Master Controller durable state and orchestrator results now use schema version 2. MC supports only the current complete state shape; runs missing the frozen plan digest, supervision/event state, repair state, worker posture, or recorded Git boundary must be reinitialized instead of being inferred or migrated.
- MC opt-in independent audits now require separate validated worker contracts for `drift-audit` and `code-review`; prompts require the drift verdict before code review launches and preserve the original delegation posture during repair.
- MC slice prompts embed a compact 615-word delegation contract instead of the full transitive `ai-orchestrator` skill and every harness reference.
- Mode A handoff/final-report guidance and Mode B reporting now preserve the same residual post-plan considerations without using a repo-root `HANDOFF.md` as Mode B continuation state.

### Fixed

- MC's prompt now matches its exact-`PASS` code-review gate and distinguishes reportable pre-existing or unrelated observations from material slice-caused defects that must still stop the run.

## [0.2.0] — 2026-07-13

### Added

- `docs/VISION.md`: the repository's timeless vision — problem, commitments, autonomy ladder, roles, personas, design principles, non-goals.
- `check-plan`: whole-plan pre-run sanity check in Master Controller (also runs automatically at `init`, failing closed on errors). Validates every slice's required sections, authorized surface, and approval flag, and lints for dependency/license-shaped authorized files, whole-repo surfaces, and Mode A-only batch groupings.
- "Privacy and Data Flows" in the master-controller README: per-seat data visibility, fully-local configurations, and an artifact sensitivity map.
- `skills/master-controller/AGENTS.md`: maintainer guide with file roles, working rules, test matrix, and change checklists.
- CI (GitHub Actions): compile checks plus both unit suites, including tmux-backed runtime tests with fake harnesses.
- `CONTRIBUTING.md`: source-of-truth map, test matrix, and change conventions.
- `archive/docs/pre-schema-v2-20260713/repo-review-vision-rubric-20260712.md`: repository-wide code-review and simplification assessment measured against the vision's eight design principles (archived after the schema-v2 reset).

### Changed

- Top-level README reframed around the autonomy ladder (Rung 0 → Mode A → Mode B) with a quickstart, decision table, glossary, and validation note; identity is now explicitly "autonomy system first".
- Mode taxonomy simplified from four labels (A / B / C1 / C2) to two modes: the former Mode B (autonomous single session) is now the documented autonomous alternate usage of Mode A — same session, same launcher family, standing commit authorization — and supervised autonomy under Master Controller is Mode B, model-supervised by default with a fail-closed unattended batch fallback (the former C1/C2 fork). No CLI behavior changed.
- Launcher templates single-sourced: both Mode A launchers (checkpointed and autonomous) live in `implementation-plan`'s SKILL.md, the Mode B launcher lives in `master-controller`'s SKILL.md, and the handoff resume prompt is derived from the checkpointed Mode A launcher instead of restating it.
- Master Controller SKILL.md: headline verification claim aligned with the documented trust boundary; new "Roles and Topology" section naming all four seats (supervising model, MC deterministic tools, slice orchestrator, worker) and what each may decide.
- `implementation-plan`: new "Execution Modes" section stating which plan features bind in which mode; Mode B (Master Controller) added to the launcher choices as a pointer; output rule keeping dependency/license files out of unattended authorized surfaces.
- `ai-orchestrator` SKILL.md: MC-specific requirements consolidated into one "Under Master Controller" section; skill map trimmed to skills that exist in this repository.
- `ai-orchestrator` worker management split by responsibility: vendor-specific session discovery, activity interpretation, and transcript extraction now live in `worker_sessions.py`, while `worker_jobs.py` retains contract launch, tracked-process lifecycle, and the CLI.
- `stop-with-evidence` now fails closed when the frozen plan changed mid-run; the plan-independent `stop` command remains available for cancellation.
- `code-simplifier` rewritten in the repository's contract style: ecosystem-neutral (standards discovered from the target project), no model pin.
- `master-controller` test suite split from one 4,456-line monolith into seven themed modules plus a shared fixtures module (`mc_test_helpers.py`); test count and coverage unchanged.

### Fixed

- Flaky runtime-test fixture: the hard-prompt-at-repair fake harness now exposes a Codex-ready marker after terminal setup and waits for MC's initial prompt injection before showing its trust prompt, removing a startup race that could time the test out under system load.
- `check-plan` now rejects malformed slice-like headings instead of silently omitting their work, and rejects authorized entries that cannot match repository-relative git paths.
- `check-plan` closes three more silent-mismatch shapes: authorized entries with unwrapped whitespace (usually a trailing annotation like `README.md (new file)`) are rejected — backtick-wrap the path to annotate it; slice-like headings inside fenced code blocks, and unclosed fences, are rejected as ambiguous; and a plain entry that names an existing directory draws a warning when repo context is available (automatic at `init`, and via `check-plan --repo`, default the current directory). The `Slice Batches` lint now fires at any heading level.
- CI now declares a read-only token boundary and does not persist checkout credentials; MC's Python 3.13 minimum is documented consistently with the version CI validates.
- Model-supervised startup failures now retain the underlying exception in run state instead of recording only a generic launch failure.
- Master Controller's known unattended launch commands are derived from harness profiles, authorized-surface matcher parity is regression-tested across MC and ai-orchestrator, and literal tmux sends terminate option parsing safely.

## [0.1.0] — 2026-07-10

Initial public import of ten modular AI engineering skills from the private bootstrap repository, including the master-controller supervision runtime, the ai-orchestrator semantic worker launcher, and the plan/implementation/audit/review/commit skill chain.
