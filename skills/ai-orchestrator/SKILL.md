---
name: ai-orchestrator
description: Routes coding and analysis tasks to external AI CLI tools (e.g. Claude Code, Codex CLI, GitHub Copilot CLI). Use when the user wants to delegate a task to an external AI agent, mentions "claude", "codex", or "copilot" explicitly, asks to "use another model", or wants to spread work across multiple models while keeping one orchestrator responsible for quality and direction.
---

# AI Orchestrator

Only the assistant directly handling the user's request may act as the orchestrator and use this skill for delegation (standalone, this is the *orchestrating assistant*; inside a Master Controller run, the per-slice session — the *slice orchestrator* — fills the same seat for one slice, with MC as its boss; see "Under Master Controller" below). Delegated workers are never orchestrators. If the current assistant is not marked as orchestrator-capable in the model table below, it must not orchestrate with this skill.

The orchestrator owns context, planning, delegation, verification, testing, and final responsibility. It delegates selectively when a worker will improve quality, speed, independence, or context management. It keeps work local when the slice is small, prompt construction would cost more than the task, delegation would weaken correctness, or the orchestrator needs to preserve tight control of the acceptance boundary. The orchestrator is the finisher: workers produce inputs, evidence, drafts, and implementation, but the orchestrator must retain the final user-facing deliverable, authorization decisions, accept-or-reject decisions, and correctness-critical judgment.

Use [scripts/worker_jobs.py](scripts/worker_jobs.py) to validate semantic worker requests against an authoritative policy, compose tested harness commands, create a unique run directory, track worker artifacts, wait safely, check lightweight worker activity, cancel cleanly, and extract outputs for every worker run. Read [references/worker-contract.md](references/worker-contract.md) before launching workers.
The helper writes worker artifacts to `.ai-orchestrator/runs/` in the current project by default. Override with `AI_ORCHESTRATOR_ARTIFACT_ROOT`. Use `--run-dir current` to reference the latest run without knowing the timestamped path.
When using the helper, worker labels must use lowercase kebab-case in the form `<nn>-<tool>-<subtask-slug>[-rN]` (for example `01-codex-trace-login`). The helper writes `<label>-out.txt`, `<label>-err.txt`, and `<label>-status.json` inside the per-run directory and rejects bad labels before launch.
Use `worker_jobs.py activity --run-dir "$run_dir" --label <label>` as the health check. If it reports `healthy=yes`, keep waiting on cadence.

## Execution Checklist

At the start of each orchestration task, write a short checklist or todo list and keep it updated. Keep it operational, not narrative.

- planned worker split and labels
- required skills per worker, or `none`
- frozen contract / drift-audit handoff for implementation tasks
- launch and extraction steps
- any promised follow-up reviewer
- synthesis rubric for judgment-heavy outputs
- synthesis and final response

Before replying, every checklist item must be completed, deferred, or explicitly cancelled with a reason.

## Roles

| Role | Purpose | Typical tasks | Hard limits |
|---|---|---|---|
| **Orchestrator** | Human-facing owner and finisher | Planning, context packaging, verification, testing, final synthesis, final answer/report/recommendation | Only the assistant directly handling the user may do this; must retain the final user-facing deliverable, the acceptance decision, and correctness-critical judgment |
| **Senior worker** | Deep technical worker | Multi-file edits, refactors, complex logic, plan review, implementation drafts, evidence gathering | Self-contained prompt only; no re-delegation; outputs are inputs or drafts for orchestrator review, not the final deliverable; if unavailable, keep the task local |
| **Junior worker** | Tactical worker | Surgical edits, approved git/GitHub operations, low-stakes web research, codebase mapping, non-critical summarising, support-text drafts | Escalate when scope, context depth, or importance grows; never own correctness-critical decisions; outputs are inputs or drafts for orchestrator review, not the final deliverable |

## Available Models

Role fit is a functional judgment, not a fixed property baked into a CLI's name. Decide per task using the role definitions above plus each reference file's own notes on what its currently configured model has actually demonstrated — the same CLI can be a strong orchestrator with one configured model and unsuitable for anything past junior-worker with another. Do not treat this table as a hardcoded capability gate; treat it as a starting default to override when the reference file's notes, or direct evidence from this session, say otherwise.

| Model | Default fit | Best used for | Avoid for | Reference |
|---|---|---|---|---|
| **Claude Code** | Orchestrator, Senior worker | Complex edits, long-running coding, plan review, deep debugging | Low-value tactical chores when a junior worker is available | [references/claude.md](references/claude.md) |
| **Codex CLI** | Orchestrator, Senior worker | Complex edits, refactors, plan review, deep debugging | Low-value tactical chores when a junior worker is available | [references/codex.md](references/codex.md) |
| **GitHub Copilot CLI** | Junior worker by default; senior/orchestrator when a strong configured model earns it | Surgical edits, approved git/GitHub operations, low-stakes web research, codebase mapping, non-critical summarising | Multi-file refactors, correctness-critical judgement, or owning complex plans unless the configured model has demonstrated that reliability | [references/copilot.md](references/copilot.md) |
| **OpenCode CLI** | Depends entirely on the configured model (often local/self-hosted) | Local/offline iteration; complex work when a capable model is configured; tactical work when only a small model is configured | Correctness-critical judgement without a demonstrably capable configured model | [references/opencode.md](references/opencode.md) |

## Skill and Tool Coordination

The user's preferred operating mode is explicit skill use. Do not rely on the model or a delegated worker to infer that a skill should apply from context alone.

When a task needs a skill, name it explicitly in the orchestrator checklist and in any delegated prompt. Delegated prompts must include a `REQUIRED SKILLS` section:

- list exact skill names the worker must use, or `none`
- the launcher embeds each named skill's complete local Markdown instruction bundle, including referenced Markdown resources
- a missing skill or referenced resource is a launch rejection, not a prompt-only fallback; read the feedback, correct the request or install/fix the skill, and retry
- include the essential task contract in the semantic request as well, so the worker receives both its exact task and the reusable skill workflow

Core local skill map:

| Skill | Use in orchestration |
|---|---|
| `implementation-plan` | Plan-first chat; create frozen acceptance slices, authorized surfaces, validation plans, rollback paths, and next-chat prompts |
| `scoped-implementation` | Implementation chat; execute one frozen slice and prepare drift-audit input |
| `drift-audit` | Authorization gate after implementation and before quality review; checks whether actual changes stayed inside the frozen contract |
| `code-review` | Quality gate after drift audit, or standalone code review; when a frozen contract exists, still note authorization status |
| `code-simplifier` | Explicit separate simplification/refactor pass for working code; not part of the default scoped implementation loop |
| `handoff` | Preserve task state for another chat or agent, including frozen contract and authorization gate status |
| `commit` | Only after explicit user approval to commit; prepare/stage/commit with the required message discipline |
| `report` | Optional final human-facing synthesis across evidence or worker outputs; not planning, implementation receipts, authorization gates, quality review, handoff, MC summaries, or commit messages |

Any other locally installed skill can be named in `required_skills` the same way — the launcher embeds whatever named skill (and its linked Markdown resources) it finds installed, and rejects the launch when one is missing.

Tool responsibilities:

- Use `scripts/worker_jobs.py launch --policy <worker-policy.json> --request <worker-request.json>` for worker launch; never construct or invoke a worker harness command directly.
- Use the selected model reference for capability and role-fit decisions. The launcher owns CLI flags.
- Use shell/git commands locally for verification and diff inspection when that is faster and clearer than delegation.
- Use GitHub tools or `gh` only for GitHub tasks where repository/PR context matters and the user request authorizes that workflow.

## Role Selection

Choose a role first:

| Task type | Role |
|---|---|
| Multi-file edits, refactoring, complex logic | Senior worker |
| Correctness-sensitive code investigation, parity analysis, migration analysis, ordering analysis | Senior worker only; do not downgrade for speed |
| Second opinion / review of the orchestrator's plan | Senior worker (read-only) |
| Step-by-step plan verification against code | Senior worker (read-only) |
| Hostile drift audit against a frozen implementation contract | Senior worker (read-only) |
| Long-running agentic coding tasks | Senior worker |
| Single-file surgical edit, clear spec | Junior worker |
| Draft supporting text for orchestrator review | Junior worker |
| Execute an explicitly approved git or GitHub action | Junior worker |
| Low-stakes web research, documentation lookup | Junior worker |
| "Find where X happens" / execution trace / codebase map | Junior worker, only when non-critical |
| Summarise a large codebase or long document | Junior worker, only when the output is non-critical |

## Model Selection

After choosing a role, choose a model from the table above:

1. Follow the user's explicit model preference unless it conflicts with a hard limit or approval rule.
2. The orchestrator is the current assistant, but only if that model is marked orchestrator-capable.
3. For worker roles, prefer a non-orchestrator model that is marked suitable for the role and best matches the task.
4. For planning or architecture tasks, prefer one senior worker to map the code and another senior worker to critique the synthesized plan when multiple senior tools are available.
5. For workplan verification, use parallel code-mapping workers only when the codebase splits cleanly. Otherwise prefer one senior investigation and keep the second senior worker for plan review after a first synthesis draft.
6. Do not launch the same tool as a worker from inside itself. Choose another worker model or keep that part local.
7. If no suitable worker is available, keep the task with the orchestrator rather than forcing delegation.

## Delegation Discipline

Every worker request must be self-contained. Encode the role template's semantic fields in `worker-request.json`; the launcher deterministically renders the worker-mode prompt and embeds requested skills. Include: specific task, relevant code or file paths, constraints, approval state for any state-changing git/GitHub action, and expected output format.
For implementation work, include the frozen contract: intended slice, allowed files/functions, expected tests, explicit non-goals, risky surfaces, and validation plan. Workers may implement inside the contract or audit against it, but they must not expand it or approve drift.
For correctness-critical investigations, explicitly name the evidence scope the worker must check before concluding: the files, directories, docs, configs, schemas, or artifacts that materially affect the answer.

Every delegated prompt must also place the receiver in worker mode: it is not the orchestrator, it must not invoke `ai-orchestrator`, and it must not re-delegate to another model. If blocked, it should report the blocker instead of bouncing the task onward. (Under Master Controller this boundary is also mechanical — see "Under Master Controller" below.)
If two workers must edit overlapping files, serialise them or refactor the scope split — do not run them in parallel.
Use absolute file paths when practical. For analysis and investigation prompts, require `path:line` evidence for every material claim. Inside shell-quoted prompts, use `SECTION: NAME` markers rather than Markdown headings that start with `#`. Keep worker outputs compact and high-signal.

## Under Master Controller

When this skill runs inside a Master Controller slice session, that session is the *slice orchestrator*: the same discipline as the standalone orchestrating assistant, with MC holding the gates. Everything in this section is verified mechanically by MC, not just expected by convention:

- `worker-policy.json`, written by MC, is authoritative for run/slice identity, the frozen plan digest, repository, worker artifact root, required tools, model, effort, permitted roles/access, and authorized files. Copy `slice_id` and `plan_sha256` from it into every request; never infer or retype them.
- Delegating the drift-audit and code-review to the available worker for independence is the preferred posture but a degradable preference: by default a locally self-audited slice is a valid pass, and MC reports worker delegation rather than gating on it. Only when the slice is marked `Independent audit required: yes` must every available tool in the policy complete through its own validated launch before the slice can pass; on such a slice the configured tools are requirements, not alternatives, and no worker made available at all fails the slice closed.
- Workers own no acceptance gates and never create the slice commit — those belong to the slice orchestrator, and MC verifies them, including mechanical evidence that each required worker actually ran (validated contract matching MC's stored policy, real process, real artifacts, successful completion).
- MC's worker gate is process-level; semantic verification of worker output is the orchestrator's job. A worker that exited cleanly without its contracted output — a refusal, a question instead of an answer, a missing `RESULT:`/`SECTION:` marker — is an incomplete delegation: write a corrected follow-up request with an `-rN` label and relaunch through the same contract. Never cite the failed attempt as worker evidence.
- MC sets the worker artifact root, slice temp directory, and tool-home environment for the slice. Use them as given; never set, unset, or redirect tool home or credential variables yourself, and report worker authentication failures as blockers instead of working around them.

## Workflow

Each new task requires a fresh role selection decision — do not carry forward a prior delegation choice.

1. **Preflight** — confirm the chosen CLI is installed, authenticated if needed, and allowed by user approval constraints; load user config defaults as a starting point when the model reference requires it
2. **Plan** — determine what needs doing, which role owns each piece, and whether delegation is worth the overhead
3. **Freeze contract** — for implementation tasks, capture the intended slice, allowed surface, non-goals, tests, risky surfaces, and rollback path before coding
4. **Checklist** — write a short execution checklist with worker labels, launch/extract steps, drift-audit handoff, any promised follow-up reviewer, and the final synthesis step
5. **Select role and model** — use the role matrix, model table, and any user directive
6. **Load contract** — read [references/worker-contract.md](references/worker-contract.md), [references/templates.md](references/templates.md), and the selected model reference for capability guidance
7. **Write request** — write a semantic `worker-request.json` carrying the task, role, access, files, constraints, required skills, context, and output contract (under MC, copy identity fields from the policy — see "Under Master Controller")
8. **Run** — invoke `worker_jobs.py launch` with the policy and request. The helper validates policy, embeds complete required-skill bundles, composes the harness command, forces the child working directory to the policy repository, and records launch evidence. If rejected, read `<label>-request-feedback.md`, correct only the named request fields, and retry; never bypass rejection with a raw command
9. **Monitor** — use a calm cadence. For senior tasks, wait 5 minutes, then run `worker_jobs.py activity --run-dir "$run_dir" --label <label> --max-idle 900` and re-check every 3 minutes. For simpler tasks, wait 3 minutes then re-check every 2 minutes. Three rules govern the cancellation decision: (1) `healthy=yes` → keep waiting; (2) `healthy=no` + process still running → re-check, do not cancel; require at least 3 consecutive `healthy=no` readings before considering early termination, and only when there is no prior evidence of work at all; (3) `healthy=no` + process not running → check status and extract. Do not infer failure from empty stdout/stderr alone while the process is running — a worker may produce no output for 10–15 minutes while thinking or running tools. Any worker with prior evidence of work may run up to 30 minutes before being treated as hung.
10. **Stay in role** — while workers run, do orchestration-only work such as monitoring status, updating the checklist, preparing the synthesis shell, or drafting a follow-up review prompt. Do not independently re-read or solve the same delegated investigation in parallel. A targeted local tie-break read is allowed only after worker outputs are back and there is a real conflict or missing evidence that materially affects the synthesis.
11. **Compress** — after completing a worker batch, summarise completed work in two to three lines and drop the raw worker output from active context to keep the session lean
12. **Check** — use `worker_jobs.py extract` when you need the clean final answer or section filtering; use `worker_jobs.py extract --json` when you need to see which artifact provided the extracted text. Inspect stderr only when extraction is still empty or clearly malformed after completion; never reuse a differently named old file from another run; do not launch probe commands or retries while an equivalent worker is still running normally
13. **Drift audit** — for implementation tasks, explicitly run or request `drift-audit` before normal quality review; unapproved drift must be fixed or explicitly escalated
14. **Test** (when appropriate) — the orchestrator runs tests via shell, interprets failures, and delegates follow-up fixes only when that helps quality
15. **Set the synthesis rubric** — before any final output that makes recommendations or other judgment calls, write down the two or three criteria or rules you will apply and use them consistently in the synthesis

When a worker needs to stop, use `worker_jobs.py cancel --run-dir "$run_dir" --label <label>` so the helper records the final cancelled state cleanly.

For tasks that ask to verify a plan or workplan, return a compact step matrix:

- Step
- Evidence (`path:line`)
- Confidence
- Blocker or divergence

End with:

- Recommended next actions
- Non-blocking gaps or follow-up debt

Before replying:

- Remove duplicated sections
- If the checklist promised a follow-up worker or reviewer, either run it or say explicitly why it was skipped
- If the output is judgment-heavy, confirm the synthesis used the stated rubric rather than ad hoc reasoning
- If a cheap missing file would materially change confidence, inspect it locally or with one targeted read-only follow-up before finalizing
- Do not mark a step `High` confidence when the blocker says more files or code paths are still needed for full verification

## Orchestrator Summary

After orchestration, summarize each model actually used with:

- What it did
- Brief feedback on how effective it was
- A rough score out of 10
- An estimated percentage of total work

Treat scores and percentages as rough operating feedback, not objective metrics. Keep the summary short and useful.
