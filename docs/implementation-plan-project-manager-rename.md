# Project Manager Rename Implementation Plan

## Context

Rename the repository's deterministic Mode B supervision skill from `master-controller` (MC) to `project-manager` (PM) because Project Manager better describes how the skill is used. The rename must preserve the vision's constant chain, role boundaries, deterministic acceptance, durable evidence, bounded repair, and stop authority. This is a breaking vocabulary and path migration, not a behavioral redesign.

The active surface includes the skill directory and metadata, Python entry point and package, runtime paths and environment variables, test fixtures, CI, cross-skill contracts, repository documentation, and the active plans in the nested fixture repository. Dated reports, archived source, and historical run artifacts remain unchanged because rewriting their recorded commands and evidence paths would make the historical record inaccurate.

## Implementation Profiles

- Recommended for a frontier/senior implementer: execute Slices 1–2 in one session, validating each repository independently and reviewing the combined rename for stale active references.
- Recommended for a standard implementer: execute Slice 1 completely before updating the nested fixture worktree in Slice 2.
- Recommended for a weaker implementer: use the same atomic order and stop after any rename residue or failing test rather than adding compatibility aliases.

## Slice Batches

- Batch A: Slices 1–2 — both slices implement one vocabulary migration, but their Git histories, validation, and rollback remain independently inspectable.

## Slice 1: Rename the Main Skill and Runtime Vocabulary

### Intended Change

- Rename the `master-controller` skill to `project-manager` and consistently rename MC-specific active paths, commands, Python identifiers, environment variables, runtime directories, session prefixes, test helpers, cross-skill contracts, CI labels, and documentation to PM equivalents.
- Preserve the existing Mode B purpose, role boundaries, deterministic gates, state semantics, repair behavior, and supported harness behavior.
- Document the rename as a breaking change without retaining an ambiguous old-name compatibility path.

### Acceptance Criteria

- Inputs:
  - The current `master-controller` skill, its tests, repository documentation, and all active cross-skill references.
- Outputs:
  - A discoverable `project-manager` skill at `skills/project-manager/` with `name: project-manager`.
  - A `pm.py` entry point backed by `pm_lib`, PM-named Python identifiers, `PM_*` environment variables, `.ai-pm/` runtime state, `pm_` session prefixes, and PM-named test helpers.
  - Updated active repository documentation, CI, ignore rules, and cross-skill references.
- User-visible behaviour:
  - Users invoke `project-manager` / PM and its documented `pm.py` commands everywhere in the active repository.
  - Mode B supervision behaves exactly as before the rename.
- Behaviour that must not change:
  - Plan parsing, eligibility, deterministic gate decisions, role authority, Reviewer contracts, repair budgets, run-state schema semantics, CLI subcommands/options, harness profiles, and evidence requirements.

### Authorized Surface

- Files allowed to change:
  - `.github/workflows/ci.yml`
  - `.gitignore`
  - `CHANGELOG.md`
  - `CONTRIBUTING.md`
  - `README.md`
  - `docs/VISION.md`
  - `docs/implementation-plan-project-manager-rename.md` (new file)
  - `skills/handoff/SKILL.md`
  - `skills/implementation-plan/SKILL.md`
  - `skills/master-controller/**` (renamed source tree)
  - `skills/project-manager/**` (renamed destination tree)
  - `skills/orchestrator/**`
  - `skills/report/SKILL.md`
- Functions/classes/components allowed to change:
  - Skill metadata and prose, CLI/package imports, PM-specific exception and fixture names, runtime path constants, environment variable names, session prefixes, cross-skill contract markers, tests, and CI commands.
- Tests allowed or expected to change:
  - The complete renamed Project Manager suite and affected orchestrator tests.

### Explicit Non-Goals

- No controller behavior, plan schema, run-state schema shape, gate policy, role authority, harness support, or repair-policy redesign.
- No compatibility alias for `master-controller`, `mc.py`, `mc_lib`, `.ai-mc`, or `MC_*` names.
- No rewriting of files under `archive/`, prior Git history, or historical runtime artifacts.

### Risk Flags

- Risky surfaces touched:
  - Public skill name, public CLI path, runtime storage path, environment variable contract, cross-skill contract marker, and CI paths.
- Approval needed before implementation: no
- Independent audit required: yes

### Validation Plan

- Tests to add/update:
  - Rename and update the existing controller tests so every former MC-specific contract is asserted under PM names.
- Commands to run:
  - `python3 -m py_compile skills/project-manager/scripts/pm.py skills/project-manager/scripts/pm_lib/*.py`
  - `python3 -m unittest discover -s skills/project-manager/tests -p 'test_*.py'`
  - `python3 -m unittest discover -s skills/orchestrator/tests -p 'test_*.py'`
  - Active-tree residue scans for old names, excluding intentional historical and migration-context references.
- Manual checks:
  - Inspect skill metadata, launcher examples, top-level skill index, runtime tree documentation, CI paths, and one-hop cross-skill consumers.

### Rollback Path

- Revert the single rename commit, restoring the original directory names, active references, runtime vocabulary, tests, and CI configuration together.

## Slice 2: Rename the Nested Fixture Worktree and Active Plans

### Intended Change

- Rename the ignored nested `mc-test` worktree to `pm-test` and update its three reusable execution plans, live lessons/index guidance, and ignore rules to use Project Manager terminology and paths.
- Preserve dated reports, archived evidence, existing Git history, and old generated run-state directories verbatim as historical records.

### Acceptance Criteria

- Inputs:
  - The nested fixture repository and its three reusable execution plans.
- Outputs:
  - A `pm-test` fixture worktree whose active plans reference `project-manager`, `pm.py`, `.ai-pm`, PM terminology, and `pm-trial` branch examples.
  - Active fixture documentation links resolve after renaming `mc-lessons-learnt.md` to `pm-lessons-learnt.md`.
- User-visible behaviour:
  - A new PM trial can follow any active fixture plan without encountering an obsolete MC path, command, runtime directory, or role label.
- Behaviour that must not change:
  - Fixture application code, plan slice contracts, test difficulty, acceptance criteria, and the content of historical reports and archived run evidence.

### Authorized Surface

- Files allowed to change:
  - `mc-test/.gitignore` (renamed worktree source)
  - `mc-test/docs/implementation-plan-easy-pi-calculator-smoke.md` (renamed worktree source)
  - `mc-test/docs/implementation-plan-hard-pi-convergence.md` (renamed worktree source)
  - `mc-test/docs/implementation-plan-medium-pi-algorithms.md` (renamed worktree source)
  - `mc-test/docs/mc-lessons-learnt.md` (renamed file source)
  - `pm-test/.gitignore` (renamed worktree destination)
  - `pm-test/docs/implementation-plan-easy-pi-calculator-smoke.md`
  - `pm-test/docs/implementation-plan-hard-pi-convergence.md`
  - `pm-test/docs/implementation-plan-medium-pi-algorithms.md`
  - `pm-test/docs/pm-lessons-learnt.md` (renamed file destination)
- Functions/classes/components allowed to change:
  - Active fixture terminology, paths, commands, branch examples, and ignore entries only.
- Tests allowed or expected to change:
  - No fixture application tests; all three reusable plans must pass Project Manager `check-plan` against the fixture repository.

### Explicit Non-Goals

- No changes to fixture application code, the completed `implementation-plan-developer-reviewer-fixtures.md` migration plan, dated `report-mc-*` files, `archive/`, `.git/ai-mc-control`, prior `.ai-mc` evidence, or Git history.
- No retroactive renaming of historical test numbers, findings, session names, or recorded evidence paths.

### Risk Flags

- Risky surfaces touched:
  - Nested Git worktree path and active execution-plan commands.
- Approval needed before implementation: no
- Independent audit required: yes

### Validation Plan

- Tests to add/update:
  - None; fixture plan validation and residue scans are sufficient because application behavior is unchanged.
- Commands to run:
  - Run `pm.py check-plan --repo .` against each active implementation plan from `pm-test`.
  - Inspect `git -C pm-test status` and verify only intended active files changed.
  - Scan active fixture files for stale MC names while excluding dated reports, archives, and preserved historical artifacts.
- Manual checks:
  - Confirm all active plan target paths, launcher pointers, run-state exclusions, and suggested branches use PM vocabulary.

### Rollback Path

- Rename the fixture worktree and lessons file back and revert the active-plan vocabulary changes; historical evidence requires no rollback because it remains untouched.

## Next Chat Prompt

Plan file: `docs/implementation-plan-project-manager-rename.md`
Slices or batch this session: Batch A

Read the full plan file first and continue on the current branch. Apply `scoped-implementation` to Slices 1–2 in order, then perform an authorization check and an independent `code-review` pass. Preserve historical reports and archives exactly as recorded. Run the validation commands in each slice, stop on any unexplained old-name residue or behavioral regression, and use the `commit` skill only after every gate passes.
