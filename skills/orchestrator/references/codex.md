# Codex CLI Reference

## Eligibility

Codex is eligible as Developer or delegate, in either access mode. The user, plan, or launcher chooses the role, model, and effort. This reference does not rank its capability.

## Read-only delegate launch

Write schema-v3 policy/request JSON as documented in [delegate-contract.md](delegate-contract.md), then use `delegate_jobs.py launch`. The launcher owns `codex exec`, model/reasoning flags, sandbox, repository directory, prompt, and capture.

Read-only command shape:

```text
codex exec <prompt> [-m <model>] [-c model_reasoning_effort="<effort>"] --sandbox read-only --skip-git-repo-check -C <repo>
```

The read-only sandbox is the strongest mechanical read-only boundary among the current profiles. This is an enforcement fact, not a suitability ranking. The same no-edit, no-mutation, no-commit, and no-redelegation prompt applies.

## Read-write delegate launch

Only valid against a policy whose `required_access` includes `read-write`. The launcher composes the same base command with `--sandbox workspace-write` instead of `read-only`:

```text
codex exec <prompt> [-m <model>] [-c model_reasoning_effort="<effort>"] --sandbox workspace-write --skip-git-repo-check -C <repo>
```

`codex exec` runs non-interactively with `approval: never`, so there is no interactive approval loop to stall: a smoke test in this repository confirmed a `workspace-write` run creates and corrects a file end-to-end unattended. The `workspace-write` sandbox mechanically confines filesystem writes to the working directory (and `/tmp`) — the strongest mechanical write boundary among the current profiles — but it does not mechanically restrict writes to the request's specific `authorized_surface`; that finer-grained boundary is prompt-enforced and is meant to be checked afterward with drift-audit against the actual diff.

## Lifecycle

Codex does not accept a caller-set session ID at first launch. The helper captures one only from launch output or a rollout JSONL record correlated by prompt, repository, and start time; unresolved ownership remains `null`. Owned transcript activity and assistant output drive health and extraction, with captured output as the fallback. Preserve vendor transcript fields such as `role: assistant`; they are external transcript schema, not orchestrator roles.

Use `delegate_jobs.py activity`, `wait`, `extract`, and `cancel`. A validated continuation composes `codex exec resume <captured-id>` from a fresh same-run request with `parent_label` and an advancing `-rN` label. The shared parent-identity and policy rules are defined in [delegate-contract.md](delegate-contract.md#validated-continuation); do not invoke raw resume commands.

## Authentication and configuration

Use the caller-supplied Codex environment and authentication. Do not redirect `CODEX_HOME` or invent credentials. Explicit model/effort values are passed through without ranking; report unsupported selections as launch failures.
