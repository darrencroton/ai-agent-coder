# GitHub Copilot CLI Reference

## Eligibility

Copilot is eligible as Developer or delegate, in either access mode, on the same terms as every supported harness. The user, plan, or launcher chooses the role, model, and effort. There is no junior/senior default.

## Read-only delegate launch

Write schema-v3 policy/request JSON as documented in [delegate-contract.md](delegate-contract.md), then use `delegate_jobs.py launch`. The launcher owns prompt/model/effort, autopilot, capture, and repository directory flags.

Command shape:

```text
copilot [--model <model>] [--effort <effort>] -p <prompt> --allow-all-tools --autopilot --silent --add-dir <repo>
```

Copilot currently has no tested mechanical read-only launch flag. Read-only isolation is therefore prompt-enforced. The command's tool availability must not be mistaken for authorization: the embedded delegate contract forbids edits, mutation-prone commands, Git/GitHub mutations, commits, and re-delegation. The Developer and repository mutation gates are the backstop.

This weaker mechanical boundary is reported by `delegate_jobs.py profiles`; it does not make Copilot ineligible.

## Read-write delegate launch

Only valid against a policy whose `required_access` includes `read-write`. The composed command is identical to the read-only command above — `--allow-all-tools --autopilot` already grants Copilot the tool access it needs to edit files, so nothing mechanical changes between access modes. The delegate prompt is what changes: it instructs a read-write delegate to stay inside `authorized_surface` and forbids Git/GitHub mutations and commits exactly as it forbids edits entirely in read-only mode. Because there is no mechanical distinction between the two modes for this harness, the Developer's own diff review and drift-audit are the entire backstop, not a supplement to one.

## Lifecycle

Copilot receives a launch-bound UUID through `--session-id`. The helper persists that ID and uses its owned `events.jsonl` transcript as the preferred activity signal, with captured output as the fallback.

Use `delegate_jobs.py activity`, `wait`, `extract`, and `cancel`. A validated continuation composes `copilot --resume=<captured-id>` from a fresh same-run request with `parent_label` and an advancing `-rN` label. The shared parent-identity and policy rules are defined in [delegate-contract.md](delegate-contract.md#validated-continuation); do not invoke raw resume commands.

## Authentication and configuration

Use the caller's signed-in session and configuration. Do not add GitHub toolsets or request Git/GitHub operations for a delegate, in either access mode. Explicit model/effort values are passed through without capability ranking; report unsupported selections as launch failures.
