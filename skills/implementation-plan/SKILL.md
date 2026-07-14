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

`project-manager` parses these plan fields mechanically, so keep their labels and shapes exact:

- Slice headings must be `## Slice <N>: <name>` with unique numbers, and each slice must include all seven `###` sections above verbatim. Heading text beginning with the standalone word `Slice` is reserved for these machine-consumed headings, except the optional `Slice Batches` heading. Never place slice-like headings inside fenced code blocks — the parser reads headings literally, so `check-plan` rejects fenced slice headings and unclosed fences as ambiguous.
- `Files allowed to change:` must list each authorized repository-relative path as an indented sub-bullet. Empty, absolute, `.`/`..`, `./`-prefixed, empty-segment, and backslash-separated paths are invalid, as are paths with unwrapped whitespace — to annotate an entry, backtick-wrap the path itself (`` `src/app.py` `` (new file)) so only the path is matched. Entries are matched segment-aware: a plain path matches exactly and never matches beneath a directory (add a trailing `/` to authorize a subtree), and `*`/`?` match within one segment. A lone `*` covers top-level paths only; use `/` separators and `**` for a recursive glob (`docs/**/*.md`).
- `Approval needed before implementation:` must be an exact `no` to run unattended. Anything else (`yes`, `not yet decided`, `none`, blank) stops the run for a human. An explicit `yes` can later be cleared at runtime with PM's `approve` command without editing the plan; anything unclear cannot.
- `Independent audit required:` is optional and lives in the `Risk Flags` section, a sibling of `Approval needed before implementation:`. Using a separate Reviewer for drift audit and code review is a degradable preference by default, so this defaults to off: absent, blank, or anything that is not an exact `yes` leaves it off. An exact `yes` opts the slice in to mechanical enforcement under `project-manager` (Mode B) — PM then requires separate validated Reviewer evidence for both `drift-audit` and `code-review`, and fails closed if no Reviewer was made available. It has no effect in Mode A, where the human or Developer judges independence directly. On default slices, Developer self-audit is valid when no Reviewer is configured or available, but the final report must identify the self-audit and its fallback context explicitly.
- Slice batches (`Batch A: Slices 1-2`) apply to Mode A runs only (either usage). `project-manager` (Mode B) executes atomic slices in plan order and ignores batch groupings, so a plan destined for PM should make each slice independently gateable rather than relying on a batch sharing one review.
- `project-manager`'s `check-plan` command validates all of the above across every slice before a run begins (and again automatically at `init`), plus lint warnings for dependency/license-shaped authorized files, whole-repo globs, plain entries that name existing directories (when run with repo context), and batch groupings. Running it against a fresh plan is the fast way to confirm the plan is PM-ready.

## Execution Modes

The same plan file serves two run modes — Mode A (one agent session, checkpointed by default or autonomous as an alternate usage) and Mode B (supervised autonomy under `project-manager`) — but not every plan feature binds in both:

- **Atomic slices** are safe everywhere — they are the unit both modes gate on.
- **Batches** bind in Mode A only (either usage); Mode B ignores them (each slice is gated alone).
- **`Approval needed before implementation:`** must be an exact `no` for any slice expected to run unattended (Mode A autonomous usage, or Mode B). An explicit `yes` stops an autonomous Mode A run and stops Mode B until the operator records approval; anything else is a planning defect that blocks Mode B entirely.
- **Risky-surface control is plan-level in both modes**: gates verify file authorization mechanically, but dependency/license/side-effect stops are heuristic — the plan is where those surfaces are kept out of unattended slices.
- **`Independent audit required:` binds in Mode B only.** Both modes prefer a separate read-only Reviewer for drift audit and code review, degrading to Developer self-audit when no Reviewer is configured or available. In Mode A that preference is judged by the human or Developer. In Mode B a slice marked `Independent audit required: yes` additionally makes PM mechanically verify separate successful Reviewer contracts for both audits; a default slice is accepted with either Reviewer evidence or explicitly reported Developer self-audit.

## Output Rules

- Keep plans specific to files, symbols, and observable behaviour.
- Prefer one slice that can be completed and reviewed independently over a broad multi-concern pass.
- Do not list files as authorized just because they might be convenient; only authorize files the implementation is expected to touch.
- Do not put dependency manifests, lockfiles, or license files in the authorized surface of a slice that runs unattended. Runtime dependency/license stops are heuristic, not diff inspection, so the plan is the real control: isolate such changes into their own slice and mark it `Approval needed before implementation: yes`.
- If discovery shows the planned surface is too broad, recommend a smaller first slice.
- If the repository state is unclear or dirty in relevant files, call that out before finalising the plan.
- Include a final `Next Chat Prompt` using the format below. Pick the run mode that fits the plan, set the plan file path and slice selection, and reference the plan file rather than pasting receipts so the launcher stays lean.

## Next Chat Prompt Format

End the plan with a copyable launcher for the next chat. The skill chain is the same in every mode (`scoped-implementation` → `drift-audit` → `code-review` → `commit`, with `orchestrator` for read-only Reviewer delegation and `handoff` at boundaries); the modes differ only in who holds the gates and when handoff happens.

Choose the launcher for the plan:

- **Mode A, checkpointed (default)** — when slices are risky, touch flagged surfaces, or you want a checkpoint between them. You stay in the loop, approve before risky slices, review findings, and approve each commit. One slice (or a few tightly-coupled slices) per chat, then a handoff to the next session.
- **Mode A, autonomous session (alternate usage)** — the same Developer session runs in a loop over all remaining slices, for when the plan is straightforward, the selected models are suitable, the work fits one session, and the user does not want to stand up an external supervisor. The Developer prefers a read-only Reviewer for the hostile drift-audit skill and independent code-review pass, recovers from findings itself, and commits each slice that clears all gates. If the launcher omits Reviewer configuration, the Developer self-audits and records that provenance explicitly. You assess at the end.
- **Mode B (Supervised autonomy)** — when the plan is long, the run is unattended, models are weaker or cheaper, or the user wants external verification with a durable audit trail. Do not embed a launcher for this mode: end the plan with a pointer to the single authoritative Mode B launcher in `project-manager`'s `SKILL.md` ("Launcher") with the plan file path filled into its first line. A plan destined for Mode B must keep every slice independently gateable — PM ignores batches — and should pass `check-plan` cleanly.

Every launcher keeps two non-negotiables: a slice whose Risk Flags mark approval-needed pauses (checkpointed Mode A) or stops the run (autonomous Mode A and Mode B), and each slice reports its authorization-gate result before quality review.

### Mode A — Assisted run (default, checkpointed)

```md
Plan file: <path>
Slices or batch this session: <e.g. Slice 2, Slices 2–3, or Batch A>

Read the full plan file first. If a selected slice or batch receipt is incomplete or the plan state is unclear, stop and tell me before coding.

Work on the current feature branch for this plan; if none exists, create one and tell me the name.

Use orchestrator as the controlling skill. Act as the Developer: keep implementation, validation, Git operations, and commits local. Use a read-only Reviewer only for investigation, evidence gathering, the hostile drift-audit skill, and an independent code-review skill pass. If no Reviewer is configured or available, perform Developer self-audit and record that provenance explicitly.

For each selected slice or batch, in plan order:
1. Restate the frozen contract (authorized surface + non-goals) from the plan.
2. If any included slice's Risk Flags mark approval-needed, stop and get my approval before coding.
3. apply the scoped-implementation skill against the selected contract.
4. apply the drift-audit skill using a read-only Reviewer when available; otherwise perform Developer self-audit. Report the authorization gate result and who performed it before any quality review.
5. If the gate passes, apply the code-review skill using a read-only Reviewer when available; otherwise perform Developer self-audit through the code-review skill. Record who performed it. If the drift gate fails, fix the drift and re-audit.
6. Surface drift and review findings to me, fix them, then re-run the relevant gate. If consecutive reviews return only minor findings and have clearly converged record residuals in the slice summary and proceed.
7. Ask me before committing. On my approval, commit the selected slice or batch with the commit skill.

After the selected slice(s) or batch are committed, use the handoff skill to record state, audit provenance (Reviewer tool/label or Developer self-audit and fallback context), and the next slice or batch to resume from. Do not continue past the selected scope.

Confirm before starting: plan file read, selected slice(s) or batch, branch, and the first slice.
```

### Mode A — Autonomous session (alternate usage)

```md
Plan file: <path>
Scope: all remaining slices, in plan order.

Read the full plan file first. If the plan is incomplete or its state is unclear, stop and report instead of improvising.

Act as the Developer per the orchestrator skill. You own the full run — implement, validate, gate, recover, commit, and make the accept/reject call. Prefer a read-only Reviewer for investigation, evidence gathering, the hostile drift-audit skill, and an independent code-review skill pass. Keep implementation, test execution, Git operations, and commits local. If no Reviewer is configured or available, perform Developer self-audit on default slices and record that provenance and fallback context explicitly.

Setup: create a new branch for this run, switch to it, and report the name.

For each slice or approved batch, in plan order:
1. Restate the frozen contract (authorized surface + non-goals).
2. If any included slice's Risk Flags mark approval-needed, STOP the run and report — do not self-approve a slice the plan gated for a human.
3. apply the scoped-implementation skill against the selected contract.
4. apply the drift-audit skill using a read-only Reviewer when available; otherwise perform Developer self-audit. Record the authorization gate result and who performed it.
5. If the gate fails, fix the drift inside the contract and re-audit. If it can't be fixed inside the contract, STOP and report.
6. On a passing gate, apply the code-review skill using a read-only Reviewer when available; otherwise perform Developer self-audit through the code-review skill. Record who performed it. Fix findings, then re-run the relevant gate. If consecutive reviews return only minor findings and have clearly converged record residuals in the slice summary and proceed.
7. When the slice passes validation, the drift-audit gate, and the code-review gate, use the commit skill. This prompt is explicit approval to commit each slice that has cleared all three gates — and only those.

Stop the run early on: an approval-gated slice, a blocker, an unapproved scope change, a gate/validation failure unfixable inside the contract, or context pressure. On any stop, use the handoff skill to record current state and the next slice or batch to resume from.

When all slices are complete, use the report skill to write a final report covering slices committed, gate results and audit provenance per slice, validation, and every residual or post-plan consideration left for me to assess. Identify each Reviewer tool/label used and every Developer self-audit with its fallback context. Do not lose a non-blocking observation merely because it did not belong in the frozen plan.

Confirm before starting: plan file read, branch name, the ordered slice list you'll execute, and the first slice.
```
