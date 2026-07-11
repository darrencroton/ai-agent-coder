---
name: implementation-plan
description: Create a narrow, auditable implementation plan with frozen acceptance slices. Use only when the user explicitly asks for this skill, an implementation plan, or a plan-first workflow before coding.
---

# Implementation Plan

Use this skill to produce the plan-first artifact for a later implementation chat. Do not implement code while using this skill unless the user explicitly changes the task.

## Purpose

Create a plan that makes each agent loop narrow, boring, and auditable. The output should be good enough that a new chat can implement one slice without needing the original discussion.

## Workflow

1. Inspect the codebase enough to understand the requested change and the relevant conventions.
2. Identify the likely implementation model/profile if the user supplied one. If not supplied, default to conservative atomic slices but include batching guidance for stronger models when adjacent slices can safely share one review.
3. Define the smallest useful acceptance slices. If the request has multiple concerns, split it into ordered slices, but do not split purely mechanical setup/docs/runtime work so finely that the plan becomes harder to execute than the change.
4. Group adjacent slices into optional implementation batches when a stronger model could reasonably implement them together under one drift audit and code review. Never group slices that cross an approval-needed gate, mix unrelated risky surfaces, or would make rollback unclear.
5. For each slice, freeze the contract before proposing implementation detail.
6. Identify risky surfaces: auth, billing, permissions, persistence, database schema, migrations, shared types, API contracts, routing, global state, concurrency, generated files, public CLI flags, or release/deployment config.
7. If a slice touches a risky surface, mark it as requiring explicit approval or split it until the risk is isolated.
8. Define validation before coding: tests to add/update, targeted checks to run, and behaviours that must not regress.
9. End with a copyable implementation prompt for the next chat.

## Slice Granularity

Choose slice size based on risk, coupling, rollback, and expected implementer strength.

- **Frontier model / senior human profile**: prefer one to three substantial slices for a coherent feature when the change is internally coupled, low-to-medium risk, and can be reviewed with one clear diff per slice. Use optional batches so the implementer can run multiple atomic contracts together when that improves coherence.
- **Standard strong model profile**: prefer smaller slices with one main runtime concern per slice and explicit validation after each. Keep batching optional, not required.
- **Weaker or less trusted model profile**: prefer narrower atomic slices, more checkpoints, and less cross-file autonomy.

Do not create extra slices just to separate every file or every documentation step. Split when a boundary improves authorization, reviewability, rollback, or human approval, not because smaller is automatically better.

When useful, include a short `Implementation Profiles` section before the slice receipts:

```md
## Implementation Profiles

- Recommended for frontier/senior implementer: run Batch A, then Batch B.
- Recommended for standard implementer: run slices individually unless the implementer explicitly confirms the batch contract.
- Recommended for weaker implementer: run atomic slices one at a time.

## Slice Batches

- Batch A: Slices 1-2 — <why these can share one implementation/review loop>
- Batch B: Slices 3-4 — <why these can share one implementation/review loop>
```

## Planning Receipt

Use this shape for every implementation slice:

```md
## Slice <N>: <short name>

### Intended Change
- ...

### Acceptance Criteria
- Inputs:
- Outputs:
- User-visible behaviour:
- Behaviour that must not change:

### Authorized Surface
- Files allowed to change:
- Functions/classes/components allowed to change:
- Tests allowed or expected to change:

### Explicit Non-Goals
- ...

### Risk Flags
- Risky surfaces touched:
- Approval needed before implementation:

### Validation Plan
- Tests to add/update:
- Commands to run:
- Manual checks:

### Rollback Path
- ...
```

## Machine-Consumed Fields

`master-controller` parses these plan fields mechanically, so keep their labels and shapes exact:

- Slice headings must be `## Slice <N>: <name>` with unique numbers, and each slice must include all seven `###` sections above verbatim.
- `Files allowed to change:` must list each authorized path as an indented sub-bullet. Entries are matched segment-aware: a plain path matches exactly, a trailing `/` matches a directory subtree, and `*`/`?` match within one segment — use `**` for a recursive glob (`docs/**/*.md`).
- `Approval needed before implementation:` must be an exact `no` to run unattended. Anything else (`yes`, `not yet decided`, `none`, blank) stops the run for a human. An explicit `yes` can later be cleared at runtime with MC's `approve` command without editing the plan; anything unclear cannot.
- Slice batches (`Batch A: Slices 1-2`) apply to Mode A and Mode B runs only. `master-controller` (Mode C) executes atomic slices in plan order and ignores batch groupings, so a plan destined for MC should make each slice independently gateable rather than relying on a batch sharing one review.
- `master-controller`'s `check-plan` command validates all of the above across every slice before a run begins (and again automatically at `init`), plus lint warnings for dependency/license-shaped authorized files, whole-repo globs, and batch groupings. Running it against a fresh plan is the fast way to confirm the plan is MC-ready.

## Execution Modes

The same plan file serves three run modes, but not every plan feature binds in every mode:

- **Atomic slices** are safe everywhere — they are the unit all three modes gate on.
- **Batches** bind in Modes A and B only; Mode C ignores them (each slice is gated alone).
- **`Approval needed before implementation:`** must be an exact `no` for any slice expected to run unattended (Mode B or C). An explicit `yes` stops Mode B and stops Mode C until the operator records approval; anything else is a planning defect that blocks Mode C entirely.
- **Risky-surface control is plan-level in every mode**: gates verify file authorization mechanically, but dependency/license/side-effect stops are heuristic — the plan is where those surfaces are kept out of unattended slices.

## Output Rules

- Keep plans specific to files, symbols, and observable behaviour.
- Prefer one slice that can be completed and reviewed independently over a broad multi-concern pass.
- Do not list files as authorized just because they might be convenient; only authorize files the implementation is expected to touch.
- Do not put dependency manifests, lockfiles, or license files in the authorized surface of a slice that runs unattended. Runtime dependency/license stops are heuristic, not diff inspection, so the plan is the real control: isolate such changes into their own slice and mark it `Approval needed before implementation: yes`.
- If discovery shows the planned surface is too broad, recommend a smaller first slice.
- If the repository state is unclear or dirty in relevant files, call that out before finalising the plan.
- Include a final `Next Chat Prompt` using the format below. Pick the run mode that fits the plan, set the plan file path and slice selection, and reference the plan file rather than pasting receipts so the launcher stays lean.

## Next Chat Prompt Format

End the plan with a copyable launcher for the next chat. The skill chain is the same in every mode (`scoped-implementation` → `drift-audit` → `code-review` → `commit`, with `ai-orchestrator` for delegation and `handoff` at boundaries); the modes differ only in who holds the gates and when handoff happens.

Choose the mode for the plan:

- **Mode A (Assisted)** — when slices are risky, touch flagged surfaces, or you want a checkpoint between them. You stay in the loop, approve before risky slices, review findings, and approve each commit. One slice (or a few tightly-coupled slices) per chat, then a handoff to the next session.
- **Mode B (Autonomous session)** — when the plan is straightforward, the implementing and reviewing models are strong, the work fits one session, and the user does not want to stand up an external supervisor. The orchestrator runs all remaining slices, delegates the hostile drift-audit skill and an independent code-review skill pass per slice, recovers from findings itself, and commits each slice that clears all gates. You assess at the end.
- **Mode C (Supervised autonomy)** — when the plan is long, the run is unattended, models are weaker or cheaper, or the user wants external verification with a durable audit trail. Do not embed a launcher for this mode: end the plan with a pointer to the single authoritative Mode C launcher in `master-controller`'s `SKILL.md` ("Launcher") with the plan file path filled into its first line. A plan destined for Mode C must keep every slice independently gateable — MC ignores batches — and should pass `check-plan` cleanly.

Every mode keeps two non-negotiables: a slice whose Risk Flags mark approval-needed pauses (Mode A) or stops the run (Modes B and C), and each slice reports its authorization-gate result before quality review.

### Mode A — Assisted run

```md
Plan file: <path>
Slices or batch this session: <e.g. Slice 2, Slices 2–3, or Batch A>

Read the full plan file first. If a selected slice or batch receipt is incomplete or the plan state is unclear, stop and tell me before coding.

Work on the current feature branch for this plan; if none exists, create one and tell me the name.

Use ai-orchestrator as the controlling skill. Keep the implementation local; delegate per that skill's guidance when independence or context economy helps — primarily the hostile drift-audit skill, an independent code-review skill pass, and long-running tests.

For each selected slice or batch, in plan order:
1. Restate the frozen contract (authorized surface + non-goals) from the plan.
2. If any included slice's Risk Flags mark approval-needed, stop and get my approval before coding.
3. apply the scoped-implementation skill against the selected contract.
4. apply the drift-audit skill. Report the authorization gate result before any quality review.
5. If the gate passes, apply the code-review skill. If it fails, fix the drift and re-audit.
6. Surface drift and review findings to me, fix them, then re-run the relevant gate. If consecutive reviews return only minor findings and have clearly converged record residuals in the slice summary and proceed.
7. Ask me before committing. On my approval, commit the selected slice or batch with the commit skill.

After the selected slice(s) or batch are committed, use the handoff skill to record state and the next slice or batch to resume from. Do not continue past the selected scope.

Confirm before starting: plan file read, selected slice(s) or batch, branch, and the first slice.
```

### Mode B — Autonomous full-loop driver

```md
Plan file: <path>
Scope: all remaining slices, in plan order.

Read the full plan file first. If the plan is incomplete or its state is unclear, stop and report instead of improvising.

Act as the orchestrator per the ai-orchestrator skill. You own the full run — implement, gate, recover, and make the accept/reject call. Delegate to other models for independence and context economy per that skill: at minimum the hostile drift-audit skill and an independent code-review skill pass per slice, plus long-running tests.

Setup: create a new branch for this run, switch to it, and report the name.

For each slice or approved batch, in plan order:
1. Restate the frozen contract (authorized surface + non-goals).
2. If any included slice's Risk Flags mark approval-needed, STOP the run and report — do not self-approve a slice the plan gated for a human.
3. apply the scoped-implementation skill against the selected contract.
4. apply the drift-audit skill (delegate a hostile audit). Record the authorization gate result.
5. If the gate fails, fix the drift inside the contract and re-audit. If it can't be fixed inside the contract, STOP and report.
6. On a passing gate, apply the code-review skill (delegate for independence). Fix findings, then re-run the relevant gate. If consecutive reviews return only minor findings and have clearly converged record residuals in the slice summary and proceed.
7. When the slice passes validation, the drift-audit gate, and the code-review gate, use the commit skill. This prompt is explicit approval to commit each slice that has cleared all three gates — and only those.

Stop the run early on: an approval-gated slice, a blocker, an unapproved scope change, a gate/validation failure unfixable inside the contract, or context pressure. On any stop, use the handoff skill to record current state and the next slice or batch to resume from.

When all slices are complete, write a final summary: slices committed, gate results per slice, and anything left for me to assess.

Confirm before starting: plan file read, branch name, the ordered slice list you'll execute, and the first slice.
```
