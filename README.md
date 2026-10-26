# AI Agent Coder

A set of skills for working with AI coding agents, from plan to commit, so that changes stay in scope, are checked for quality, and are verified against the repository rather than taken from the agent's own report.

The skills form a chain — plan → implement → lint → drift audit → code review → commit — and every skill works on its own. Run as a system, the chain has three modes that differ in who holds the gates: **Mode A**, a human-managed plan; **Mode B**, a PM-managed plan under a Project Manager; **Mode C**, a CM-managed campaign in which a Campaign Manager plans and runs many Mode B runs against an owner-approved brief. The chain is the same in every mode; what changes is who holds the gates and how far the human stands from the work.

Background, motivation, and design principles: [`docs/VISION.md`](docs/VISION.md).

## Requirements

- **Standalone skills** — nothing beyond the Markdown files.
- **`lint`** — whichever linters your repository needs. `skills/lint/scripts/lint.py detect` reports what is missing and prints install commands; it never installs anything.
- **`code-health`** — Python 3.13+ and `git` only; Lizard is an optional multi-language complexity collector that `skills/code-health/scripts/health.py detect` reports as present or missing and never installs.
- **Mode B (Project Manager)** — Python 3.13+, `git`, `tmux`, and at least one supported coding CLI on the machine running PM.
- **Mode C (campaign)** — everything Mode B needs, plus `git worktree`, a second git repository for the dossier, a worktree parent directory trusted in each harness, and the `implementation-plan` and `orchestrator` skills installed beside `project-manager` in the Campaign Manager's harness; when the brief sandboxes the children, macOS and Anthropic's `srt` on `PATH` ([`skills/project-manager/references/sandbox.md`](skills/project-manager/references/sandbox.md)).

## Installation

Each skill is a self-contained directory under [`skills/`](skills/) with a `SKILL.md` entry point, usable by any harness that supports skill directories (Claude Code, Codex CLI, OpenCode, GitHub Copilot CLI, and others).

Copy or symlink individual skills into your harness's skills directory (for example `~/.claude/skills/<skill-name>`), or symlink all of them:

```bash
git clone git@github.com:darrencroton/ai-agent-coder.git
for s in ai-agent-coder/skills/*/; do
  ln -s "$(realpath "$s")" ~/.claude/skills/"$(basename "$s")"
done
```

If you maintain a private agent-home repo that composes skills from several sources, register this repo there and let its setup script create the symlinks.

## Skills

Each skill's `SKILL.md` is the source of truth for trigger conditions, workflow, and output format. This table is the index.

| Skill | What it does |
|-------|-------------|
| [`implementation-plan`](skills/implementation-plan/) | Breaks a request into auditable slices with frozen contracts: acceptance criteria, authorized surface, validation, risk flags, and the Mode A and Mode B launchers for the next chat. |
| [`scoped-implementation`](skills/scoped-implementation/) | Implements one frozen slice without expanding scope; prepares the receipt for drift audit. |
| [`lint`](skills/lint/) | Runs the project's linters and reports only findings this change introduced. A missing linter is reported as uncovered, never as a pass. |
| [`code-health`](skills/code-health/) | Measures codebase composition, duplication, complexity, dependencies, and optional Git churn; the agent investigates the evidence rather than grading the repository. |
| [`drift-audit`](skills/drift-audit/) | Answers one question: was the implementation authorized? Runs before any quality review. |
| [`code-review`](skills/code-review/) | Quality review after drift audit passes: correctness, edge cases, tests, error handling, domain-specific risks. |
| [`commit`](skills/commit/) | Stages by name, never skips hooks, writes a message listing every file with reasons. |
| [`project-manager`](skills/project-manager/) | Mode B: supervises execution of an existing plan one slice at a time — durable run state, plan sanity check, fresh tmux-backed session per slice, mechanical floor, recorded assessments, commissioned independent reviews. Mode C: as Campaign Manager, plans and runs many such plans against an owner-approved brief. |
| [`orchestrator`](skills/orchestrator/) | Delegates bounded read-only or read-write work through validated contracts, with session tracking and continuation. The Developer retains verification, gates, commits, and final responsibility. |
| [`code-simplifier`](skills/code-simplifier/) | Behaviour-preserving clarity pass over working code. A separate cleanup step, not part of the default chain. |
| [`style-guide`](skills/style-guide/) | Draft a project style guide from a portable baseline, audit code against it, or write code following it. `write` mode folds into `scoped-implementation` when available; not a gate anywhere. |
| [`handoff`](skills/handoff/) | Compact continuation state for the next session: status, blockers, frozen contract, exact next action. |
| [`report`](skills/report/) | Evidence-backed written synthesis when explicitly requested. Outside the gate chain. |

Call skills explicitly. Do not rely on the model to guess which workflow applies.

## The workflow chain

The default flow for feature or bug work, in every mode:

1. **Plan** — `implementation-plan`: define slices, freeze contracts, flag risky surfaces.
2. **Implement** — `scoped-implementation` against one frozen slice, in a fresh session. When `style-guide` is available, its `write` mode loads the applicable style standard here too — the project's own drafted guide, or its portable baseline if none exists yet.
3. **Lint** — `lint`, differential against the starting commit so pre-existing debt cannot block. Runs before the reviews because its findings are unarguable and free.
4. **Audit scope** — `drift-audit`: was what happened authorized? Always before quality review.
5. **Measure structure** (optional) — `code-health`, for broad, architectural, or maintainability-sensitive changes; supplies evidence, never a gate verdict.
6. **Review quality** — `code-review`, after the authorization gate passes.
7. **Simplify** (optional) — `code-simplifier`, as a separate pass over working code.
8. **Hand off** (if needed) — `handoff` before ending an unfinished session.
9. **Commit** — `commit`, only with explicit approval.

Mode B reverses one step: the slice is committed *before* the reviews. The Developer commits, then PM runs the mechanical floor and commissions `drift-audit` and `code-review` against the committed diff before deciding acceptance. Committing per slice makes the reviewed state exact and any mistake one revert away. Mode C adds a step above that: each change or evidence node runs the Mode B chain in its own worktree, and the Campaign Manager merges an accepted change node into the integration branch only after judging it from the child's evidence.

Three launchers start a run, one per mode. The Mode A and Mode B launchers live in `implementation-plan`'s SKILL.md → "Next Chat Prompt Format", and a generated plan ends with both already filled in; the Mode C launcher lives in `project-manager`'s [`references/campaign-mode.md`](skills/project-manager/references/campaign-mode.md). The handoff resume prompt derives from the Mode A launcher, as `handoff`'s SKILL.md → "Plan Workflow Handoff" describes.

## Modes: who holds the gates

Start wherever the task is; no mode requires the one below it.

### Standalone skills

Any skill, in any harness, with no infrastructure. In your coding assistant: *"Use the code-review skill on the diff on this branch."* You get a severity-ranked review with `file:line` findings and an explicit verdict. Every skill works this way, one explicit request each.

### Mode A — human-managed plan

One agent session acts as the Developer over the slices you choose — one, several, or all — and you hold the gates. The agent restates each frozen contract, implements, audits its own authorization, and reviews quality; the launcher's `Supervision:` line sets how present you are.

- **Checkpointed** (default): you approve before any approval-flagged slice and before every commit. Chat 1: *"Use the implementation-plan skill: &lt;describe the change&gt;."* You get a plan with frozen slices and the launcher at the end. Chat 2: paste the launcher into a fresh session for one slice or a few, then paste the handoff's resume prompt into the next. Repeat until the plan is done.
- **Autonomous**: the same launcher with scope set to all remaining slices and standing authorization to commit whatever clears every gate. The agent loops through the plan in one session and stops for anything that needs you. The gates are then promises kept in-session rather than externally verified; you assess at the end.

Launcher: [`skills/implementation-plan/SKILL.md`](skills/implementation-plan/SKILL.md) → "Next Chat Prompt Format".

### Mode B — PM-managed plan

The gatekeeper moves outside the implementing agent. A deterministic toolkit owns durable run state, fresh tmux-backed sessions (one per slice, which is the context reset that makes long plans tractable), artifact capture, and a seven-fact mechanical floor. The PM agent owns everything semantic: it assesses each completed slice from the diff, commit, and validation evidence; records its reasoning in a durable assessment; commissions independent drift-audit and code-review sessions where risk warrants; steers bounded corrections; and stops for a human on anything the plan or the floor reserves for one.

The PM seat is a model you choose, including a local one.

1. Verify your machine once with the tmux-backed check in [`skills/project-manager/README.md`](skills/project-manager/README.md) → "Verify your setup".
2. Sanity-check the plan: `python3 skills/project-manager/scripts/pm.py check-plan --plan <plan.md>`. This also runs automatically at `init`, so a defective plan stops before any harness launches.
3. Start the run with the Mode B launcher in [`skills/implementation-plan/SKILL.md`](skills/implementation-plan/SKILL.md) → "Next Chat Prompt Format". The PM keeps a git-ignored `HANDOFF.md` in the repo root as the restart entry point; paste the same launcher to resume.

If the PM seat runs in Claude Code, consider installing the optional poll guard (`skills/project-manager/hooks/pm-poll-guard.py`). It blocks two ways a PM burns turns learning what the harness would have told it for free: a re-read of a background task's output identical to the previous read, and a backgrounded command that sleeps and then inspects a PM artifact — both redundant, since Claude Code already re-invokes the agent when a background command exits, and expensive, since each one resends the whole conversation. It fails open everywhere, and a foreground wait is always allowed. Setup: [`skills/project-manager/README.md`](skills/project-manager/README.md) → "Optional: the poll guard".

### Mode C — CM-managed campaign

The human-approved contract moves up one level, from the plan to a brief. You write and approve `brief.md` — the goal, measurable targets with tolerances, the paths the campaign may touch, a protected-paths policy file, the decision classes reserved for you, and budgets — in a dossier that is a git repository of its own. A Campaign Manager, which is the `project-manager` seat in campaign mode, then writes small node plans inside the brief, runs each through an ordinary Mode B child PM in its own worktree and tmux session, judges each node from the child's evidence, merges accepted change nodes one at a time, and re-runs the cheap checks at each new integration head, the expensive ones at milestones and all of them at the final head. It never implements, never supervises a plan it wrote, and never changes the brief; routine nodes ask you nothing, and reserved decisions, human gates and the final sign-off come to you.

1. Prepare the product repository, the dossier and your harness as [`skills/project-manager/README.md`](skills/project-manager/README.md) → "Start a campaign" lists, including the one-time "Verify campaign mode" check.
2. Start or resume the Campaign Manager with the Mode C launcher in [`skills/project-manager/references/campaign-mode.md`](skills/project-manager/references/campaign-mode.md), supplying the brief's digest each time. The dossier is the campaign's whole state; a restart reconciles from it.

### Choosing a mode

| Situation | Use |
|---|---|
| One-off review, audit, commit, or handoff | Call the skill directly |
| Risky or unfamiliar surfaces; you want a checkpoint between slices | Mode A, checkpointed |
| Straightforward plan, strong models, fits in one session | Mode A, autonomous |
| Long plan, unattended time, weaker or local models, or you want external verification and a durable audit trail | Mode B |
| A goal met across many plans and days, measured against targets, with an owner-approved brief | Mode C |

## Privacy and data flows

Run state, artifacts, transcripts, and review evidence stay on your machine. What leaves the machine is determined entirely by which models you place in which seats. The artifact sensitivity map is in [`skills/project-manager/README.md`](skills/project-manager/README.md) → "Privacy & sensitive artifacts".

## Glossary

- **Mode A, B, C** — the three ways to run the chain as a system: a human-managed plan (one agent session, checkpointed or autonomous), a PM-managed plan (a Project Manager supervises one plan), and a CM-managed campaign (a Campaign Manager plans and runs many Mode B runs under a brief). Standalone skill use has no mode letter.
- **Slice** — the unit of work: one narrow, independently reviewable change with its own frozen contract.
- **Frozen contract** — a slice's authorization, fixed before coding: acceptance criteria, authorized surface, non-goals, validation plan, rollback path.
- **Authorized surface** — the files (and functions/tests) a slice may touch; its *effective* surface is the frozen plan surface plus any PM-recorded grants (see Surface grant, below). Everything outside it is drift.
- **Surface grant** — a PM-recorded widening of a slice's effective authorized surface to one exact additional file path (never a directory or glob), on recorded evidence; it ratchets the slice to elevated risk. Authority and refusal rules live in `project-manager`'s `SKILL.md` → "Surface grants".
- **Drift audit** — the authorization gate: compares actual changes against the frozen contract, before any quality judgment.
- **Differential lint** — `lint`'s default question: not "is this code clean?" but "does this change introduce a finding that was not there before?"
- **Gate** — a check that must pass before work advances. In Mode A these are the in-session chain steps (validation, drift audit, code review, commit evidence); in Mode B there are exactly three: the mechanical floor, PM assessment, and human approval; in Mode C each child run keeps the Mode B gates and the campaign adds the Campaign Manager's judgement of every node and, at the end, a final evidence node, an independent audit and the owner's sign-off.
- **Floor** — the seven mechanical, non-waivable facts checked at finalize, enumerated in `project-manager`'s `SKILL.md` → "The floor". Any failure blocks acceptance.
- **Harness** — a coding-agent CLI (Codex CLI, Claude Code, OpenCode, Copilot CLI, and others) that PM or you run a session in.
- **Developer** — the context-rich agent that owns implementation, validation, session management, gates, commits, and delivery. Under PM it is the supervised per-slice session and has no authority above PM.
- **Delegate** — orchestrator's term for a harness session the Developer launches: **read-only** (investigation, evidence gathering, drift audit, code review — a synonym for Reviewer) or **read-write** (a bounded implementer confined to an explicit authorized surface). Neither commits, mutates Git/GitHub state, or re-delegates; the Developer reviews and accepts its output either way.
- **Reviewer** — a read-only helper. Owns no gates, never mutates the repository, never commits, never re-delegates.
- **PM seat** — the model that drives Project Manager's commands and judgement: in Mode B the PM of a plan, in Mode C the Campaign Manager and each child PM.
- **Project Manager (PM)** — the accountable supervisor: the toolkit owns state, sessions, and the floor; the PM agent owns assessment, review depth, steering, and stop decisions.
- **Run state** — authenticated state and PM-authored originals under the repo's git directory, mirrored with per-slice artifacts under `.pm/` in the target repo; the audit trail.
- **Campaign mode** — Mode C: a `project-manager` mode that turns an owner-approved goal into a validated outcome across many small plans and days: the Campaign Manager writes node plans inside the brief, runs each through an ordinary child PM in its own worktree and tmux session, and merges accepted change nodes one at a time. Contract and launcher: [`skills/project-manager/references/campaign-mode.md`](skills/project-manager/references/campaign-mode.md).
- **Campaign Manager (CM)** — the `project-manager` seat in Mode C. It plans, launches, answers, judges and stops child PMs and keeps the dossier; it never implements, never supervises a plan it wrote, and never changes the brief.
- **Owner** — the human behind a campaign: approves `brief.md` and supplies its digest, approves reserved decision classes, answers every human gate, and signs off acceptance.
- **Dossier and brief** — the dossier is the Campaign Manager's whole state: a git repository of its own, outside the product repository, laid out in [`skills/project-manager/references/dossier-template.md`](skills/project-manager/references/dossier-template.md). Its `brief.md` is the owner-approved, digest-pinned goal, targets, permitted paths, protected-paths policy file, reserved decision classes and budgets, and is the CM's only authority to plan.
- **Node** — one unit of a campaign: a *change* node (a product change toward a target, merged once accepted), an *evidence* node (a measurement at a recorded product commit, never merged), or a *task* node (a read-only investigation run through `orchestrator`). Change and evidence nodes each run as a child PM on a small node plan.
- **Protected path** — a path named in a protected-paths policy file, frozen into run state at `init`; a plain entry naming a directory covers its subtree. A plan surface that reaches one is refused at `check-plan` and `init`, `grant` refuses it, and floor fact 5 fails on a change to it (a rename counts on both sides). Contract: `project-manager`'s `references/run-state.md`.
- **Question** — a one-line decision a session asks its supervisor through a file, answered without ending the session: a Developer asks PM (`pm answer`), and a child PM asks the Campaign Manager (`answer-child`).

## License

[MIT](LICENSE)
