---
name: handoff
description: Create a compact, high-signal handoff for continuing in another chat or coding agent. Use when the user says "handoff".
---

## Principles

Preserve momentum — the next agent must know exactly what to do first. Prefer facts over narrative. Separate confirmed facts from assumptions. Record failed attempts so the next agent does not repeat them. Never include secrets or credentials.

## Output

Write or update `HANDOFF.md` in the project root unless the user requests a different file. If it already exists, update in place — keep still-relevant context, remove stale items. Do not commit the handoff unless told or already part of the repo.

## Modes

Choose the lightest mode that preserves continuity.

**Quick** — use when near a hard usage limit. Target: 10–25 lines. Include only: objective, current status, next action, blockers, files touched, critical warnings.

**Full** — use for complex or multi-step tasks with significant history. Target: 25–100 lines. Use all sections from the template below.

## What To Capture

The template below names the structure — objective, task list, status, blockers, files, validation, next action, and so on. Capture facts under whichever heading fits. Beyond that structure, always capture:

- frozen contract, authorized surface, and authorization gate status when the task used implementation-plan or scoped-implementation
- residual findings and post-plan considerations that were intentionally not fixed in the current frozen slice, including why they did not block and what later follow-up should consider

Never:
- chatty narrative or repeated background
- speculative details that do not affect next steps
- secrets or credential values

## Plan Workflow Handoff

When all of the following are true, populate `Resume Prompt` using the full next-chat launcher below — not the generic resume prompt:

- an implementation plan file exists and was the source of the current work
- you just finished implementing one or more slices or a named batch from that plan
- you are stopping at a slice or batch boundary so the user can review before the next session begins (i.e. you are **not** running all remaining slices autonomously without user checkpoints)

Build the resume prompt from the Mode A launcher in `implementation-plan`'s `SKILL.md` → "Next Chat Prompt Format" — that file is the single source for launcher text; do not reproduce a stale copy from memory. Copy the launcher, then make exactly these handoff-specific modifications:

1. Set `Plan file:` and set `Slices or batch this session:` to the next unstarted slice, slice range, or named batch.
2. Extend the "Read the full plan file first" line to require reading **both** the plan file **and** `HANDOFF.md`, stating that the handoff is part of the run contract and records what has been committed and what remains.
3. Add `HANDOFF.md read` to the final "Confirm before starting" list.

For all other handoffs — including autonomous Mode A full-plan runs, Mode B/PM runs (PM's run state and `run-report.md` under the repo's git directory are the continuation record), and non-plan work — omit the Resume Prompt section entirely.

## Template

Use this structure. For Quick Mode, omit sections that do not apply.

```md
# HANDOFF

## Objective
- What the user wants, in one or two sentences.

## Task List
- The full list of tasks to achieve the objective
- Extract from the plan if one was provided, otherwise from your own planning
- Be brief, but include all relevant context not captured elsewhere

## Current Status
- What is complete.
- What is in progress.
- What is not started.

## Decisions Made
- Confirmed decisions the next agent should not re-litigate unless new evidence appears.

## Frozen Contract
- Intended slice, authorized files/functions, tests, non-goals, risky surfaces, and rollback path if a scoped implementation contract exists.

## Failed or Rejected Approaches
- Attempts that did not work and why.

## Active Blockers
- Missing information, failing checks, unresolved bugs, or external dependencies.

## Files That Matter
- Paths to files changed, created, or heavily inspected, with a brief reason each matters.

## Developer / Reviewer State (only if called as part of an orchestrator workflow)
- Active run dir, Developer session, Reviewer runs and tool/labels, audit provenance, and any planned Reviewer work

## Validation
- Tests, lint, builds, or manual checks already run, with results.
- Checks still needed.

## Authorization Gate
- Actual changed surface versus authorized surface.
- Drift status: none, fixed, approved, or unresolved.

## Next Action
- The exact first thing the next agent should do.

## Resume Prompt
*(Only when handing off at a slice or batch boundary with user checkpoints between sessions — build it as described in the Plan Workflow Handoff section above. Omit this section for all other handoffs.)*
```

## Next Action Standard

The `Next Action` must be specific enough that the next agent can start immediately.

Good:
- `Update src/auth/session.ts to guard null refresh tokens, then rerun the auth test suite.`
- `Open package.json and align the Vite version with the plugin peer dependency before retrying install.`

Weak:
- `Continue working on it.`
- `Fix the bug.`

## Quality Checks

Before finishing, verify:
- the todo list is clear
- blockers are explicit
- the handoff can be scanned in under two minutes

## Writing Style

- Short bullets. Prefer file paths and concrete nouns.
- Label uncertain items as `Assumption:` or `Unverified:`.
- Each bullet should be independently useful.

## Optional Enhancements

Add only when they provide real value:
- `Branch:` line if git state matters
- `Commands:` section for critical reproducible commands
- `Risks:` section for sensitive refactors or migrations
- `Open Questions:` if user input is needed
- Note the repo or working directory per item if the task spans multiple repos or tools

## Example

```md
# HANDOFF

## Objective
- Migrate the app from legacy auth middleware to token-based session validation without breaking admin routes.

## Task List
- [x] Create new token validator and wire for standard user routes.
- [ ] Audit admin routes in `src/routes/admin.ts` for legacy middleware dependencies.
- [ ] Run full integration test suite to confirm no regressions.

## Current Status
- New token validator exists and is wired for standard user routes.
- Admin routes still reference legacy middleware.

## Decisions Made
- Keep the existing session cookie format for backward compatibility.

## Failed or Rejected Approaches
- Replacing all middleware in one pass caused admin authorization regressions.

## Active Blockers
- Need to inspect admin route coverage before removing fallback logic.

## Files That Matter
- `src/auth/token-validator.ts`: new validation path.
- `src/routes/admin.ts`: still using legacy path.

## Validation
- Targeted auth unit tests passed. Admin integration tests not yet run.

## Next Action
- Update `src/routes/admin.ts` to use the new validator behind the existing fallback, then run `tests/auth/integration.test.ts`.
```
