---
name: orchestrator
description: Routes bounded work to external AI CLI tools (Claude Code, Codex CLI, GitHub Copilot CLI, OpenCode, and Qwen Code) while the current Developer retains final responsibility. Covers read-only investigation, drift-audit, and code-review delegates, and read-write implementation delegates that create and edit files inside an explicit authorized surface. Use when the user asks to delegate to another model or harness, names one of these CLIs, or wants independent review evidence.
---

# Orchestrator

The orchestrator is a workflow, not a person or a model tier. Any supported harness can run it; any supported harness can be launched by it. The assistant directly handling the user's request is the **Developer** for that session. The Developer owns session management, planning, implementation, testing, verification, commits, acceptance decisions, and the final user-facing deliverable — that ownership never transfers to a delegate, no matter which harness is doing the delegating or which harness is launched.

A **delegate** is an external harness session the Developer launches to do bounded work outside the current session. Every delegate launches in one of two access modes, chosen by the request and constrained by policy:

- **`read-only`** — an evidence source. It inspects code and artifacts, gathers evidence, verifies plans, performs drift audits, and performs code reviews. It never edits files, runs mutation-prone commands, performs Git or GitHub mutations, commits, or re-delegates.
- **`read-write`** — a bounded implementer. It may create, edit, and run commands to complete a task, but only inside the request's explicit `authorized_surface` and never past its `non_goals`. Like a read-only delegate, it never performs Git or GitHub mutations, never commits, and never re-delegates: the Developer reviews its diff and commits it, exactly as it would for its own edits.

The Developer must critically evaluate every delegate result — a read-only delegate's report before trusting it as evidence, and a read-write delegate's diff before accepting it as if it were the Developer's own work. Running `drift-audit` against a read-write delegate's diff is the expected next step, the same as after any other implementation.

Use [scripts/delegate_jobs.py](scripts/delegate_jobs.py) for every delegate launch. Read [references/delegate-contract.md](references/delegate-contract.md) before writing a request. The helper validates schema-v3 semantic requests, embeds required skills, composes the selected harness command, records normalized access evidence, manages process lifecycle, and extracts results.

Delegate artifacts default to `.orchestrator/runs/`. Override that root with `ORCHESTRATOR_ARTIFACT_ROOT`. Existing `.ai-orchestrator/` evidence is historical and must not be read, migrated, or reinterpreted.

## Execution Checklist

For a non-trivial orchestration task, keep a short operational checklist:

- local Developer work and any bounded delegate work (read-only or read-write)
- exact delegate labels, access modes, and required skills
- frozen contract and drift-audit handoff for any implementation work, delegated or not
- launch, monitoring, extraction, and evidence steps
- audit provenance, including any Developer self-audit
- synthesis and final response

Before replying, complete, defer, or explicitly cancel every item.

## Roles

| Role | Responsibilities | Hard limits |
|---|---|---|
| **Developer** | Own the user session; plan, code, test, verify, fix, gate, commit when approved, synthesize, and deliver | Must retain implementation and acceptance responsibility for every change, including a delegate's; may self-audit only under the rules below |
| **Delegate (read-only)** | Investigation, evidence gathering, plan verification, drift audit, and code review | No workspace changes, mutation-prone commands, Git/GitHub mutations, commits, re-delegation, implementation, or final acceptance |
| **Delegate (read-write)** | A bounded implementation task inside an explicit authorized surface | No changes outside the authorized surface, no Git/GitHub mutations, no commits, no re-delegation, no final acceptance |

There are no senior/junior tiers and no harness ranking. Task scope, access mode, tool, model, and effort are explicit request choices, not role variants.

## Tool Selection

Claude, Codex, Copilot, OpenCode, and Qwen Code are equally eligible to act as Developer or delegate, in either access mode. The user, plan, or launcher chooses the tool, model, and effort. Do not rank tools, infer a capability tier from a vendor name, or reject a delegate because its harness has weaker mechanical isolation.

Equal eligibility does not imply identical enforcement. Read the selected tool reference and report the actual boundary for the access mode you launched:

| Tool | Read-only boundary | Read-write boundary | Reference |
|---|---|---|---|
| Claude Code | Plan-mode restrictions; command execution still requires delegate discipline | `acceptEdits` auto-approves file edits; prompt-enforced beyond that | [references/claude.md](references/claude.md) |
| Codex CLI | Read-only sandbox | `workspace-write` sandbox mechanically confines writes to the working directory and `/tmp` | [references/codex.md](references/codex.md) |
| GitHub Copilot CLI | Prompt-enforced read-only behavior | Prompt-enforced; same command as read-only | [references/copilot.md](references/copilot.md) |
| OpenCode CLI | Plan agent denies edit tools; shell discipline remains prompt-enforced | Build agent grants unrestricted tool permissions; prompt-enforced | [references/opencode.md](references/opencode.md) |
| Qwen Code | Prompt-enforced repository read-only behavior; launcher requests sandboxing | Prompt-enforced; same command as read-only | [references/qwen.md](references/qwen.md) |

Harness enforcement is evidence, not model-ranking policy. No profile mechanically restricts a read-write delegate to its specific `authorized_surface` — that boundary is prompt-enforced for every harness. The Developer's own review and drift-audit are the backstop either way.

## Skill and Tool Coordination

Name every required skill explicitly in the request. The launcher embeds each named skill's complete local Markdown bundle, including linked Markdown resources. A missing skill or resource rejects the launch.

For a **read-only** delegate, common skills are:

- `drift-audit` for authorization review against a frozen contract
- `code-review` for correctness, safety, maintainability, and test review
- `implementation-plan` only when reviewing an existing plan or gathering read-only planning evidence
- `report` only for a requested evidence synthesis

Do not request `scoped-implementation`, `code-simplifier`, `commit`, `orchestrator`, `project-manager`, or any workflow requiring edits, external mutation, or supervisory authority from a read-only delegate. The launcher rejects these regardless: `commit`, `orchestrator`, `project-manager`, and `scoped-implementation` may never be embedded in any delegate prompt, in either access mode — the first three because they edit, mutate, or supervise on the Developer's behalf, and `scoped-implementation` because its own text is written for the Developer's first-person use and instructs "never let a Reviewer edit files," which directly contradicts a read-write delegate's own task.

For a **read-write** delegate, `code-simplifier` is appropriate when the task is a cleanup pass; the task's own `authorized_surface` and `non_goals` already state the bounded surface, so most read-write requests need no embedded skill at all. `commit` is never appropriate for any delegate, in either access mode — the Developer always owns the commit.

## Delegate Request Discipline

Write one self-contained `delegate-request.json` per bounded task. Include the exact task, tool/model/effort, access mode, relevant files and context, required skills, constraints, and output contract. For correctness-critical work, name every file, directory, schema, or artifact that materially affects the conclusion. Require `path:line` evidence for material claims.

Do not put `role` in a request; it does not exist in the schema. Do put `access` in a request: it must be one of the policy's `required_access` values, exactly like `tool` must be one of `required_tools`. A `read-write` request additionally requires `authorized_surface` (the bounded set of files/functions/components this delegate may change) and `non_goals` (explicit exclusions); a `read-only` request must omit both. Schema v3 rejects retired fields, old Worker fields, unknown extensions, and schema v1/v2.

Never invoke a harness directly. Launch through:

```bash
run_dir="$(python3 scripts/delegate_jobs.py init --prefix delegates)"
python3 scripts/delegate_jobs.py launch \
  --run-dir "$run_dir" \
  --policy /absolute/path/to/delegate-policy.json \
  --request /absolute/path/to/delegate-request.json
```

Use lowercase kebab-case labels shaped as `<nn>-<tool>-<subtask-slug>[-rN]`. Use `activity`, `wait`, `extract`, and `cancel` for lifecycle management. A rejected request must be corrected from its feedback artifact; never bypass rejection with a raw command.

## Session Tracking and Continuation

Every managed launch records a `session_id` field and exposes it through `status` and `activity`. Claude and Copilot receive launch-bound IDs; Codex, OpenCode, and Qwen use launch-correlated discovery from launch output or session stores and leave the field `null` rather than guess when ownership cannot be established. `activity` reports a source-labelled health signal for every harness, using an owned transcript or session store when available and captured output as the universal fallback.

To continue a delegate, write a fresh schema-v3 request in the same managed run, set `parent_label` to the prior delegate, and give the new request a lineage-preserving `-rN` label. The launcher re-validates the entire request against policy, including tool, model, effort, and access; continuation does not inherit authorization. It blocks unless the parent is terminal, uses the same harness, owns a captured session ID, and belongs to the same advancing label lineage. There is no global “resume last” fallback. The new manifest entry records `parent_label`, `parent_session_id`, and `continuation_index`. See [references/delegate-contract.md](references/delegate-contract.md#validated-continuation) for the authoritative contract and [references/templates.md](references/templates.md#validated-continuation) for a request template.

## Developer and Delegate Workflow

1. **Preflight** — confirm the selected CLI is installed and authenticated.
2. **Plan** — keep implementation with the Developer by default; identify bounded work (read-only evidence, or a boundable implementation slice) that benefits from delegation.
3. **Freeze** — capture the acceptance slice, allowed implementation surface, non-goals, risky areas, and validation before coding, whether the Developer or a read-write delegate will implement it.
4. **Request** — copy identity/tool/model/effort fields from policy and write a schema-v3 delegate request using [references/templates.md](references/templates.md); for a continuation, also name the verified parent and advance its `-rN` lineage.
5. **Launch** — use `delegate_jobs.py launch`; inspect rejection feedback instead of weakening the contract.
6. **Monitor** — use `activity` on a calm cadence. Silence alone is not failure while the process remains healthy.
7. **Extract** — read the clean result and verify it against the requested output contract.
8. **Gate** — after any implementation, Developer-authored or delegated, perform drift audit before code review. Fix authorized defects or stop for scope approval.
9. **Test** — the Developer runs tests and owns interpretation and fixes, even for a read-write delegate's diff. Read-only delegates may inspect existing results but must not run commands likely to mutate the workspace.
10. **Synthesize** — record who performed each audit and each implementation, and deliver the Developer's validated conclusion.

## Audit Provenance and Self-Audit

Prefer an independent read-only delegate for drift audit and code review. If none is configured, none can launch, or the launcher explicitly selects self-audit, the Developer may self-audit a default slice. The final report must explicitly identify each audit as delegate-performed or Developer self-audit, and identify each implementation as Developer-authored or delegate-authored, explaining fallback context when applicable.

A plan that asks for independent review (`Independent audit required: yes`) deserves separate read-only delegate launches for `drift-audit` and `code-review`, in that order; if a delegate cannot be launched for such a slice, stop and report rather than self-audit.

## Delegate Summary

When delegates were used, summarize each briefly:

- access mode and bounded task performed
- whether the evidence, or the diff, was sufficient
- factual enforcement boundary used
- rough effectiveness score and estimated share of the work

Do not present this operating feedback as an objective model ranking.
