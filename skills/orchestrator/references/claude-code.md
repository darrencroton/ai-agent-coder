# Claude Code CLI Reference

## Eligibility

Claude is eligible as Developer or delegate, in either access mode. The user, plan, or launcher chooses the role, model, and effort. This reference does not rank its capability.

## Read-only delegate launch

Write schema-v3 policy/request JSON as documented in [delegate-contract.md](delegate-contract.md), then use `delegate_jobs.py launch`. The launcher owns `claude -p`, model/effort flags, `--permission-mode plan`, output format, repository directory, prompt, and capture.

Read-only command shape:

```text
claude -p <prompt> [--model <model>] [--effort <effort>] --permission-mode plan --output-format text --add-dir <repo>
```

Plan mode is the closest tested read-only configuration. It restricts direct editing behavior, but command execution may still depend on harness/model behavior. Treat its read-only boundary as partial and rely on the delegate prompt, Developer verification, and repository mutation gates.

## Read-write delegate launch

Only valid against a policy whose `required_access` includes `read-write`. The launcher composes the same base command with `--permission-mode acceptEdits` instead of `plan`:

```text
claude -p <prompt> [--model <model>] [--effort <effort>] --permission-mode acceptEdits --output-format text --add-dir <repo>
```

`acceptEdits` auto-approves file edit tool calls headlessly; a smoke test in this repository confirmed it creates a file non-interactively without hanging. It does not mechanically confine writes to the request's `authorized_surface` — that boundary is prompt-enforced, exactly like the read-only prompt's no-mutation instruction. Other tool permissions (for example Bash commands not already allowed by the caller's own settings) may still require approval that a headless run cannot supply; treat an unattended stall as evidence the task needs different constraints, not as a reason to switch to a broader permission mode.

## Lifecycle

Claude receives a launch-bound UUID through `--session-id`. The helper persists that ID, resolves its owned JSONL transcript, and uses transcript timestamps plus assistant activity as health signals; captured output remains the fallback. Empty stdout while the process runs is not evidence of failure.

Use `delegate_jobs.py activity`, `wait`, `extract`, and `cancel`. A validated continuation composes `claude --resume <captured-id>` from a fresh same-run request with `parent_label` and an advancing `-rN` label. The shared parent-identity and policy rules are defined in [delegate-contract.md](delegate-contract.md#validated-continuation); do not invoke raw resume commands.

## Authentication and configuration

Use the environment and authentication supplied by the caller. Do not redirect Claude home/configuration or invent credential variables. If an explicit model or effort is selected, the launcher passes it without capability ranking; report unsupported selections as launch failures.
