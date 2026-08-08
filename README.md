# AI Agent Coder

A set of skills for working with AI coding agents, from plan to commit, so that changes stay in scope, are checked for quality, and are verified against the repository rather than taken from the agent's own report.

The skills form a chain — plan → implement → lint → drift audit → code review → commit — and can be used at three levels of independence: individually, as a supervised session, or as an unattended multi-slice run under a Project Manager. The chain is the same at every level; what changes is who holds the gates.

Background, motivation, and design principles: [`docs/VISION.md`](docs/VISION.md).

## Requirements

- **Atomic skills** — nothing beyond the Markdown files.
- **`lint`** — whichever linters your repository needs. `skills/lint/scripts/lint.py detect` reports what is missing and prints install commands; it never installs anything.
- **Mode B (Project Manager)** — Python 3.13+, `git`, `tmux`, and at least one supported coding CLI on the machine running PM.

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
| [`implementation-plan`](skills/implementation-plan/) | Breaks a request into auditable slices with frozen contracts: acceptance criteria, authorized surface, validation, risk flags, and a copyable launcher for the next chat. |
| [`scoped-implementation`](skills/scoped-implementation/) | Implements one frozen slice without expanding scope; prepares the receipt for drift audit. |
| [`lint`](skills/lint/) | Runs the project's linters and reports only findings this change introduced. A missing linter is reported as uncovered, never as a pass. |
| [`code-health`](skills/code-health/) | Measures codebase composition, duplication, complexity, dependencies, and optional Git churn; the agent investigates the evidence rather than grading the repository. |
| [`drift-audit`](skills/drift-audit/) | Answers one question: was the implementation authorized? Runs before any quality review. |
| [`code-review`](skills/code-review/) | Quality review after drift audit passes: correctness, edge cases, tests, error handling, domain-specific risks. |
| [`commit`](skills/commit/) | Stages by name, never skips hooks, writes a message listing every file with reasons. |
| [`project-manager`](skills/project-manager/) | Supervises execution of an existing plan one slice at a time: durable run state, plan sanity check, fresh tmux-backed session per slice, mechanical floor, recorded assessments, commissioned independent reviews. |
| [`orchestrator`](skills/orchestrator/) | Delegates bounded read-only or read-write work through validated contracts, with session tracking and continuation. The Developer retains verification, gates, commits, and final responsibility. |
| [`code-simplifier`](skills/code-simplifier/) | Behaviour-preserving clarity pass over working code. A separate cleanup step, not part of the default chain. |
| [`handoff`](skills/handoff/) | Compact continuation state for the next session: status, blockers, frozen contract, exact next action. |
| [`report`](skills/report/) | Evidence-backed written synthesis when explicitly requested. Outside the gate chain. |

Call skills explicitly. Do not rely on the model to guess which workflow applies.

## The workflow chain

The default flow for feature or bug work, at every level:

1. **Plan** — `implementation-plan`: define slices, freeze contracts, flag risky surfaces.
2. **Implement** — `scoped-implementation` against one frozen slice, in a fresh session.
3. **Lint** — `lint`, differential against the starting commit so pre-existing debt cannot block. Runs before the reviews because its findings are unarguable and free.
4. **Audit scope** — `drift-audit`: was what happened authorized? Always before quality review.
5. **Measure structure** (optional) — `code-health`, for broad, architectural, or maintainability-sensitive changes; supplies evidence, never a gate verdict.
6. **Review quality** — `code-review`, after the authorization gate passes.
7. **Simplify** (optional) — `code-simplifier`, as a separate pass over working code.
8. **Hand off** (if needed) — `handoff` before ending an unfinished session.
9. **Commit** — `commit`, only with explicit approval.

Mode B reverses one step: the slice is committed *before* the reviews. The Developer commits, then PM runs the mechanical floor and commissions `drift-audit` and `code-review` against the committed diff before deciding acceptance. Committing per slice makes the reviewed state exact and any mistake one revert away.

Launcher templates live in exactly one place each: both Mode A launchers in `implementation-plan`'s SKILL.md, the Mode B launcher in `project-manager`'s SKILL.md. The handoff resume prompt derives from the checkpointed Mode A launcher, as described in `handoff`'s SKILL.md. Generated plans end with the right launcher already filled in.

## Levels of independence

The three levels are independent — start wherever the task is.

### 1. Standalone skills

Any skill, in any harness, with no infrastructure. In your coding assistant: *"Use the code-review skill on the diff on this branch."* You get a severity-ranked review with `file:line` findings and an explicit verdict. Every skill works this way, one explicit request each.

### 2. Mode A — assisted (one agent session)

You supervise a run slice by slice. The agent restates each frozen contract, implements, audits, and reviews; you approve risky slices before coding and every commit after gates pass.

- Chat 1: *"Use the implementation-plan skill: &lt;describe the change&gt;."* You get a plan with frozen slices and a launcher prompt at the end.
- Chat 2: paste the launcher into a fresh session. The agent implements one slice, audits its own authorization, reviews quality, and asks before committing. Repeat per slice.

Mode A has an **autonomous usage**: pointed at all remaining slices with standing authorization to commit whatever clears every gate, the agent loops through the plan in one session. The gates are then promises kept in-session rather than externally verified.

Both launchers: [`skills/implementation-plan/SKILL.md`](skills/implementation-plan/SKILL.md) → "Next Chat Prompt Format".

### 3. Mode B — supervised autonomy (Project Manager)

The gatekeeper moves outside the implementing agent. A deterministic toolkit owns durable run state, fresh tmux-backed sessions (one per slice, which is the context reset that makes long plans tractable), artifact capture, and an eight-fact mechanical floor. The PM agent owns everything semantic: it assesses each completed slice from the diff, commit, and validation evidence; records its reasoning in a durable assessment; commissions independent drift-audit and code-review sessions where risk warrants; steers bounded corrections; and stops for a human on anything the plan or the floor reserves for one.

The PM seat is a model you choose, including a local one.

1. Verify your machine once with the tmux-backed trial in [`skills/project-manager/README.md`](skills/project-manager/README.md) → "Verify your setup".
2. Sanity-check the plan: `python3 skills/project-manager/scripts/pm.py check-plan --plan <plan.md>`. This also runs automatically at `init`, so a defective plan stops before any harness launches.
3. Start the run with the launcher in [`skills/project-manager/SKILL.md`](skills/project-manager/SKILL.md) → "Launcher".

If the PM seat runs in Claude Code, consider installing the optional poll guard (`skills/project-manager/hooks/pm-poll-guard.py`). It blocks two ways a PM burns turns learning what the harness would have told it for free: a re-read of a background task's output identical to the previous read, and a backgrounded command that sleeps and then inspects a PM artifact — both redundant, since Claude Code already re-invokes the agent when a background command exits, and expensive, since each one resends the whole conversation. It fails open everywhere, and a foreground wait is always allowed. Setup: [`skills/project-manager/README.md`](skills/project-manager/README.md) → "Optional: the poll guard".

### Choosing a level

| Situation | Use |
|---|---|
| One-off review, audit, commit, or handoff | Call the skill directly |
| Risky or unfamiliar surfaces; you want a checkpoint between slices | Mode A — checkpointed |
| Straightforward plan, strong models, fits in one session | Mode A — autonomous usage |
| Long plan, unattended time, weaker or local models, or you want external verification and a durable audit trail | Mode B — Project Manager |

## Privacy and data flows

Run state, artifacts, transcripts, and review evidence stay on your machine. What leaves the machine is determined entirely by which models you place in which seats. The artifact sensitivity map is in [`skills/project-manager/README.md`](skills/project-manager/README.md) → "Privacy & sensitive artifacts".

## Glossary

- **Slice** — the unit of work: one narrow, independently reviewable change with its own frozen contract.
- **Frozen contract** — a slice's authorization, fixed before coding: acceptance criteria, authorized surface, non-goals, validation plan, rollback path.
- **Authorized surface** — the files (and functions/tests) a slice may touch; everything else is drift.
- **Drift audit** — the authorization gate: compares actual changes against the frozen contract, before any quality judgment.
- **Differential lint** — `lint`'s default question: not "is this code clean?" but "does this change introduce a finding that was not there before?"
- **Gate** — a check that must pass before work advances. In Mode A these are the in-session chain steps (validation, drift audit, code review, commit evidence); in Mode B there are exactly three: the mechanical floor, PM assessment, and human approval.
- **Floor** — the eight mechanical, non-waivable facts checked at finalize: plan digest, repo/branch identity, approvals, result identity, frozen surface, commit ancestry, clean worktree, hard-stop scan. Any failure blocks acceptance.
- **Harness** — a coding-agent CLI (Codex CLI, Claude Code, OpenCode, Copilot CLI, and others) that PM or you run a session in.
- **Developer** — the context-rich agent that owns implementation, validation, session management, gates, commits, and delivery. Under PM it is the supervised per-slice session and has no authority above PM.
- **Delegate** — orchestrator's term for a harness session the Developer launches: **read-only** (investigation, evidence gathering, drift audit, code review — a synonym for Reviewer) or **read-write** (a bounded implementer confined to an explicit authorized surface). Neither commits, mutates Git/GitHub state, or re-delegates; the Developer reviews and accepts its output either way.
- **Reviewer** — a read-only helper. Owns no gates, never mutates the repository, never commits, never re-delegates.
- **PM seat** — in Mode B, the model that drives Project Manager's commands and judgement.
- **Project Manager (PM)** — the accountable supervisor: the toolkit owns state, sessions, and the floor; the PM agent owns assessment, review depth, steering, and stop decisions.
- **Run state** — authenticated state and PM-authored originals under the repo's git directory, mirrored with per-slice artifacts under `.pm/` in the target repo; the audit trail.

## License

[MIT](LICENSE)
