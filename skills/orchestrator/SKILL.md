---
name: orchestrator
description: Routes read-only investigation, drift-audit, and code-review tasks to external AI CLI tools (Claude Code, Codex CLI, GitHub Copilot CLI, and OpenCode) while the current Developer retains implementation, verification, and final responsibility. Use when the user asks to delegate to another model, names one of these CLIs, or wants independent review evidence.
---

# Orchestrator

The orchestrator is the workflow. The assistant directly handling the user's request is the **Developer**. The Developer owns session management, planning, implementation, testing, verification, commits, acceptance decisions, and the final user-facing deliverable.

A delegated **Reviewer** is a read-only evidence source. Reviewers inspect code and artifacts, gather evidence, verify plans, perform drift audits, and perform code reviews. They never edit files, run mutation-prone commands, perform Git or GitHub mutations, commit, re-delegate, or make the final acceptance decision. The Developer must critically evaluate every Reviewer result.

Use [scripts/reviewer_jobs.py](scripts/reviewer_jobs.py) for every Reviewer launch. Read [references/reviewer-contract.md](references/reviewer-contract.md) before writing a request. The helper validates schema-v2 semantic requests, embeds required skills, composes the selected harness command, records normalized Reviewer/read-only evidence, manages process lifecycle, and extracts results.

Reviewer artifacts default to `.orchestrator/runs/`. Override that root with `ORCHESTRATOR_ARTIFACT_ROOT`. Existing `.ai-orchestrator/` evidence is historical and must not be read, migrated, or reinterpreted.

## Execution Checklist

For a non-trivial orchestration task, keep a short operational checklist:

- local Developer work and any bounded Reviewer investigations
- exact Reviewer labels and required skills
- frozen contract and drift-audit handoff for implementation work
- launch, monitoring, extraction, and evidence steps
- audit provenance, including any Developer self-audit
- synthesis and final response

Before replying, complete, defer, or explicitly cancel every item.

## Roles

| Role | Responsibilities | Hard limits |
|---|---|---|
| **Developer** | Own the user session; plan, code, test, verify, fix, gate, commit when approved, synthesize, and deliver | Must retain implementation and acceptance responsibility; may self-audit only under the rules below |
| **Reviewer** | Read-only investigation, evidence gathering, plan verification, drift audit, and code review | No workspace changes, mutation-prone commands, Git/GitHub mutations, commits, re-delegation, implementation, or final acceptance |

There are no senior/junior tiers. Task scope, model, and effort are explicit request choices, not role variants.

## Tool Selection

Claude, Codex, Copilot, and OpenCode are equally eligible to act as Developer or Reviewer. The user, plan, or launcher chooses the tool, model, and effort. Do not rank tools, infer a capability tier from a vendor name, or reject a Reviewer because its harness has weaker mechanical isolation.

Equal eligibility does not imply identical enforcement. Read the selected tool reference and report the actual boundary:

| Tool | Reviewer launch boundary | Reference |
|---|---|---|
| Claude Code | Plan-mode restrictions; command execution still requires Reviewer discipline | [references/claude.md](references/claude.md) |
| Codex CLI | Read-only sandbox | [references/codex.md](references/codex.md) |
| GitHub Copilot CLI | Prompt-enforced read-only behavior | [references/copilot.md](references/copilot.md) |
| OpenCode CLI | Plan agent denies edit tools; shell discipline remains prompt-enforced | [references/opencode.md](references/opencode.md) |

Harness enforcement is evidence, not model-ranking policy. The Developer and, under Project Manager, repository mutation gates remain the backstop.

## Skill and Tool Coordination

Name every required skill explicitly in the request. The launcher embeds each named skill's complete local Markdown bundle, including linked Markdown resources. A missing skill or resource rejects the launch.

Common Reviewer skills are:

- `drift-audit` for authorization review against a frozen contract
- `code-review` for correctness, safety, maintainability, and test review
- `implementation-plan` only when reviewing an existing plan or gathering read-only planning evidence
- `report` only for a requested evidence synthesis

Do not request `scoped-implementation`, `code-simplifier`, `commit`, or any workflow requiring edits or external mutation from a Reviewer.

## Reviewer Request Discipline

Write one self-contained `reviewer-request.json` per bounded task. Include the exact task, tool/model/effort, relevant files and context, required skills, constraints, and output contract. For correctness-critical work, name every file, directory, schema, or artifact that materially affects the conclusion. Require `path:line` evidence for material claims.

Do not put `role` or `access` in a request. The launcher owns both constants and records `role: reviewer` and `access: read-only` in normalized launch evidence. Schema v2 rejects those retired fields, old Worker fields, unknown extensions, and schema v1.

Never invoke a harness directly. Launch through:

```bash
run_dir="$(python3 scripts/reviewer_jobs.py init --prefix reviewers)"
python3 scripts/reviewer_jobs.py launch \
  --run-dir "$run_dir" \
  --policy /absolute/path/to/reviewer-policy.json \
  --request /absolute/path/to/reviewer-request.json
```

Use lowercase kebab-case labels shaped as `<nn>-<tool>-<subtask-slug>[-rN]`. Use `activity`, `wait`, `extract`, and `cancel` for lifecycle management. A rejected request must be corrected from its feedback artifact; never bypass rejection with a raw command.

## Developer and Reviewer Workflow

1. **Preflight** — confirm the selected CLI is installed and authenticated.
2. **Plan** — keep implementation with the Developer; identify bounded read-only work that benefits from independent evidence.
3. **Freeze** — capture the acceptance slice, allowed implementation surface, non-goals, risky areas, and validation before coding.
4. **Request** — copy identity/tool/model/effort fields from policy and write a schema-v2 Reviewer request using [references/templates.md](references/templates.md).
5. **Launch** — use `reviewer_jobs.py launch`; inspect rejection feedback instead of weakening the contract.
6. **Monitor** — use `activity` on a calm cadence. Silence alone is not failure while the process remains healthy.
7. **Extract** — read the clean result and verify it against the requested output contract.
8. **Gate** — after implementation, perform drift audit before code review. Fix authorized defects or stop for scope approval.
9. **Test** — the Developer runs tests and owns interpretation and fixes. Reviewers may inspect existing results but must not run commands likely to mutate the workspace.
10. **Synthesize** — record who performed each audit and deliver the Developer's validated conclusion.

## Audit Provenance and Self-Audit

Prefer an independent Reviewer for drift audit and code review. If no Reviewer is configured, a selected Reviewer cannot launch, or the launcher explicitly selects self-audit, the Developer may self-audit a default slice. The final report must explicitly identify each audit as `reviewer` or `developer-self-audit` and explain fallback context when applicable.

A slice marked `Independent audit required: yes` never permits self-audit. It requires separate validated Reviewer launches for `drift-audit` and `code-review`, in that order, with exact `PASS` verdict evidence.

Under Project Manager, follow [references/pm-slice-contract.md](references/pm-slice-contract.md). PM validates process evidence and mutation gates; the Developer remains responsible for semantic sufficiency and the final gate decision.

## Reviewer Summary

When Reviewers were used, summarize each tool briefly:

- bounded task performed
- whether the evidence was sufficient
- factual enforcement boundary used
- rough effectiveness score and estimated share of the review work

Do not present this operating feedback as an objective model ranking.
