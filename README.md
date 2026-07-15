# AI Engineering Skills

**One safety chain for AI coding agents, applied at increasing levels of independence — from a single standalone review to a fully unattended multi-slice run.**

AI coding agents are strong implementers and unreliable narrators: left unsupervised they expand scope, grade their own work generously, and report success that can diverge from what actually happened in the repository. This repository moves trust out of the model and into contracts, evidence, and role separation. What "authorized" means is frozen before coding; authorization is audited separately from quality; and acceptance rests on repository evidence, never on the agent's say-so. The agent moves fast inside the lane — it just doesn't get to redraw it.

The system is graduated: use one skill standalone (a single code review), run a plan slice-by-slice with checkpoints, or hand a whole plan to an external supervisor and come back to committed, verified slices. The safety chain — **plan → scoped implementation → validation → drift audit → code review → commit** — never changes; only who holds the gates does.

Why it exists, who it serves, and the principles that govern design decisions live in [`docs/VISION.md`](docs/VISION.md).

## Installation

Each skill is a self-contained directory under [`skills/`](skills/) with a standard `SKILL.md` entry point, so any coding-agent harness that supports skill directories (Claude Code, Codex CLI, OpenCode, GitHub Copilot CLI, …) can use them.

- **Copy or symlink individual skills** into your harness's skills directory (for example `~/.claude/skills/<skill-name>`). Skills are modular — take only what you use.
- **Clone the repo and symlink every skill:**

  ```bash
  git clone git@github.com:darrencroton/ai-engineering-skills.git
  for s in ai-engineering-skills/skills/*/; do
    ln -s "$(realpath "$s")" ~/.claude/skills/"$(basename "$s")"
  done
  ```

- **Compose through a bootstrap repo.** If you maintain a private agent-home repo that composes skills from several sources into one canonical catalogue, register this repo there and let its setup script clone it and create the symlinks.

The atomic skills need nothing beyond the Markdown files. Supervised autonomy (Mode B) additionally needs Python 3.13 or newer, `git`, `tmux`, and at least one supported coding CLI on the machine that runs Project Manager.

## The Autonomy Ladder

Three rungs, one skill chain; each rung moves the gatekeeper further from the keyboard. Each rung is independent — start wherever your task is.

**1. Standalone skills.** Every skill is independently useful in any harness with no infrastructure. This is the entry point and the graceful-degradation floor.

In your coding assistant: *"Use the code-review skill on the diff on this branch."* You get a senior-level, severity-ranked review with `file:line` findings and an explicit verdict. Every skill works this way — `drift-audit`, `commit`, `handoff`, and the rest are one explicit request each.

**2. Mode A — Assisted (one agent session).** You supervise an orchestrated run slice by slice: the agent restates each frozen contract, implements, audits, and reviews; you approve risky slices before coding and every commit after gates pass. The same mode has an **autonomous alternate usage**: pointed at all remaining slices with standing authorization to commit whatever clears every gate, it loops through the plan in one session — right when the plan is straightforward, the models are strong, and you don't want to stand up an external supervisor. In that usage the gates are promises kept in-session: disciplined, but not externally verified.

- Chat 1: *"Use the implementation-plan skill: <describe the change>."* You get a plan with frozen slices and a copyable launcher prompt at the end.
- Chat 2: paste the launcher into a fresh session. The agent implements one slice, audits its own authorization, reviews quality, and asks you before committing. Repeat per slice.
- Both launchers (checkpointed and autonomous): [`skills/implementation-plan/SKILL.md`](skills/implementation-plan/SKILL.md) → "Next Chat Prompt Format".

**3. Mode B — Supervised autonomy (Project Manager).** The gatekeeper moves outside the implementing agent. PM keeps durable run state, sanity-checks the whole plan before starting, launches a fresh session per slice (the context reset that makes long plans tractable), verifies gates from git and artifact evidence, steers bounded repairs, and stops for a human on anything outside policy. Supervision is a dial, not a fork: by default a supervising model stays in the loop for operational judgment (usage resets, stalls, transient interruptions); if you can't or don't want to provide one, PM's fail-closed batch style runs unattended and stops at the first ambiguity.

- Verify your machine once with the tmux-backed trial in [`skills/project-manager/README.md`](skills/project-manager/README.md) → "Verify Your Setup".
- Sanity-check the plan: `python3 skills/project-manager/scripts/pm.py check-plan --plan <plan.md>` (also runs automatically at `init` — a defective plan stops before any harness launches).
- Start the run with the Mode B launcher in [`skills/project-manager/SKILL.md`](skills/project-manager/SKILL.md) → "Launcher".

Choosing a rung is about the task, not your skill level:

| Situation | Use |
|---|---|
| One-off review, audit, commit, or handoff | Call the skill directly |
| Risky or unfamiliar surfaces; you want a checkpoint between slices | Mode A — checkpointed |
| Straightforward plan, strong models, fits in one session, no third model available to supervise | Mode A — autonomous usage |
| Long plan, unattended time, weaker/cheaper/local models, or you want external verification and a durable audit trail | Mode B — Project Manager |

## Skills

This README is the maintained human-facing skill index. Each skill's own `SKILL.md` remains the source of truth for trigger conditions, detailed workflow, and output format.

| Skill | What it does |
|-------|-------------|
| [`implementation-plan`](skills/implementation-plan/) | Breaks a request into auditable slices with frozen contracts: acceptance criteria, authorized surface, validation, risk flags, and a copyable launcher for the next chat. |
| [`project-manager`](skills/project-manager/) | Supervises execution of an existing plan one slice at a time: durable run state, whole-plan sanity check, fresh tmux-backed session per slice, evidence-based gates, bounded repair loop. |
| [`scoped-implementation`](skills/scoped-implementation/) | Implements one frozen slice without expanding scope; prepares the receipt for drift audit. |
| [`drift-audit`](skills/drift-audit/) | Answers one question: was the implementation authorized? Runs before any quality review. |
| [`code-review`](skills/code-review/) | Senior-level quality review after drift audit passes: correctness, edge cases, tests, error handling, domain-specific risks. |
| [`orchestrator`](skills/orchestrator/) | Manages read-only Reviewer delegation through validated semantic contracts; the Developer retains implementation, verification, gates, commits, and final responsibility. |
| [`code-simplifier`](skills/code-simplifier/) | Behaviour-preserving clarity pass over working code; a separate cleanup step, not part of the default chain. |
| [`handoff`](skills/handoff/) | Compact continuation state for the next session: status, blockers, frozen contract, exact next action. |
| [`commit`](skills/commit/) | Disciplined commits: stage by name, never skip hooks, message lists every file with reasons. |
| [`report`](skills/report/) | Evidence-backed written synthesis when explicitly requested; optional, outside the gate chain. |

Use explicit skill calls. Do not rely on the model to guess which workflow applies.

## The Workflow Chain

The default flow for feature or bug work, at every rung:

1. **Plan** — `implementation-plan`: define slices, freeze contracts, flag risky surfaces.
2. **Implement** — `scoped-implementation` against one frozen slice, in a fresh session.
3. **Audit scope** — `drift-audit`: was what happened authorized? Always before quality review.
4. **Review quality** — `code-review` after the authorization gate passes.
5. **Simplify** (optional) — `code-simplifier` as a separate pass over working code.
6. **Hand off** (if needed) — `handoff` before ending an unfinished session.
7. **Commit** — `commit`, only with explicit approval (yours in checkpointed Mode A; the plan's standing authorization in autonomous Mode A and Mode B after all gates pass).

Each launcher template lives in exactly one place: both Mode A launchers (checkpointed and autonomous) in `implementation-plan`'s SKILL.md, the Mode B launcher in `project-manager`'s SKILL.md, and the handoff resume prompt derives from the checkpointed Mode A launcher as described in `handoff`'s SKILL.md. Generated plans end with the right launcher already filled in.

## Privacy and Data Flows

Everything the system produces — run state, artifacts, transcripts, Reviewer evidence — stays on your machine, and local/self-hosted models are first-class citizens at every rung. What leaves the machine is determined entirely by which models you place in which seats; the per-mode breakdown, the fully-local configurations, and the artifact sensitivity map are in [`skills/project-manager/README.md`](skills/project-manager/README.md) → "Privacy and Data Flows".

## Glossary

- **Slice** — the unit of work: one narrow, independently reviewable change with its own frozen contract.
- **Frozen contract** — a slice's authorization, fixed before coding: acceptance criteria, authorized surface, non-goals, validation plan, rollback path.
- **Authorized surface** — the files (and functions/tests) a slice may touch; everything else is drift.
- **Drift audit** — the authorization gate: compares actual changes against the frozen contract, before any quality judgment.
- **Gate** — a check that must pass before work advances: validation, drift audit, code review, commit evidence, clean worktree.
- **Harness** — a coding-agent CLI (Codex CLI, Claude Code, OpenCode, Copilot CLI, …) that PM or you run a session in.
- **Orchestrator** — the skill and workflow that coordinates Developer execution with optional read-only Reviewer evidence.
- **Developer** — the context-rich agent that owns implementation, validation, session management, gates, commits, and delivery. Under PM it is the supervised per-slice session and has no authority above PM.
- **Reviewer** — a read-only helper for investigation, evidence gathering, drift audit, and code review. It owns no gates, never mutates the repository, never commits, and never re-delegates.
- **Supervising model** — in Mode B, the model that drives PM's commands and handles operational judgment; it never decides acceptance.
- **Project Manager (PM)** — the deterministic supervisor: run state, gates, repair loop, stop authority.
- **Repair loop** — PM's bounded self-correction: a fixable gate failure is steered back into the live session and re-verified at full rigor; budgets and a circuit breaker cap it.
- **Run state** — durable JSON plus per-slice artifacts under `.ai-pm/` in the target repo; the audit trail.

## License

[MIT](LICENSE)
