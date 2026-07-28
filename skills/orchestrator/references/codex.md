# Codex CLI Reference

## Eligibility

Codex is eligible as Developer or delegate, in either access mode. The user, plan, or launcher chooses the role, model, and effort. This reference does not rank its capability.

## Read-only delegate launch

Write schema-v3 policy/request JSON as documented in [delegate-contract.md](delegate-contract.md), then use `delegate_jobs.py launch`. The launcher owns `codex exec`, model/reasoning flags, sandbox, repository directory, prompt, and capture.

Read-only command shape:

```text
codex exec <prompt> [-m <model>] [-c model_reasoning_effort="<effort>"] -c sandbox_mode="read-only" -c approval_policy="never" --skip-git-repo-check -C <repo>
```

The read-only sandbox is the strongest mechanical read-only boundary among the current profiles. This is an enforcement fact, not a suitability ranking. The same no-edit, no-mutation, no-commit, and no-redelegation prompt applies.

That boundary holds only while approvals cannot escalate out of the sandbox, so the launcher pins `approval_policy="never"` rather than inheriting the caller's policy. Under an `on-request` policy — a caller policy the harness inherited before this was pinned, and a common setting in a personal `~/.codex/config.toml` — a delegate whose command is blocked by the sandbox may request escalated permissions, and an automated approvals reviewer can grant them with no human present. Verified against codex-cli 0.145.0: under `on-request` a `read-only` delegate escalated and wrote a file outside the repository; under `never` the same command was refused and the sandbox held.

## Read-write delegate launch

Only valid against a policy whose `required_access` includes `read-write`. The launcher composes the same base command with `sandbox_mode="workspace-write"` instead of `read-only`:

```text
codex exec <prompt> [-m <model>] [-c model_reasoning_effort="<effort>"] -c sandbox_mode="workspace-write" -c approval_policy="never" --skip-git-repo-check -C <repo>
```

The pinned `approval_policy="never"` also removes any interactive approval loop that could stall an unattended delegate: a sandbox-blocked command fails and is returned to the model instead of waiting on an approval a headless run cannot supply. A smoke test in this repository confirmed a `workspace-write` run creates and corrects a file end-to-end unattended. The `workspace-write` sandbox mechanically confines filesystem writes to the working directory, `/tmp`, and `$TMPDIR` — the strongest mechanical write boundary among the current profiles — but it does not mechanically restrict writes to the request's specific `authorized_surface`; that finer-grained boundary is prompt-enforced and is meant to be checked afterward with drift-audit against the actual diff.

## Lifecycle

Codex does not accept a caller-set session ID at first launch. The helper captures one only from launch output or a rollout JSONL record correlated by prompt, repository, and start time; unresolved ownership remains `null`. Owned transcript activity and assistant output drive health and extraction, with captured output as the fallback. Preserve vendor transcript fields such as `role: assistant`; they are external transcript schema, not orchestrator roles.

Use `delegate_jobs.py activity`, `wait`, `extract`, and `cancel`. A validated continuation composes `codex exec resume <captured-id>` from a fresh same-run request with `parent_label` and an advancing `-rN` label. The shared parent-identity and policy rules are defined in [delegate-contract.md](delegate-contract.md#validated-continuation); do not invoke raw resume commands.

Continuation command shape:

```text
codex exec resume <captured-id> <prompt> [-m <model>] [-c model_reasoning_effort="<effort>"] -c sandbox_mode="<mode>" -c approval_policy="never" --skip-git-repo-check
```

`codex exec resume` has a smaller flag set than `codex exec`: it rejects `--sandbox` and `-C` outright, and composing either fails the launch before the delegate starts. This is why both launch paths express the sandbox through `sandbox_mode` — one spelling that both subcommands accept. Dropping `-C` costs nothing, because the launcher already starts every delegate process in the policy repository.

A continuation is sandboxed by its own request's access mode, not by whatever its parent ran under: the pinned `sandbox_mode` overrides the resumed session's recorded value, verified in both directions against codex-cli 0.145.0. A `read-only` continuation of a `read-write` parent is therefore genuinely read-only, and needs no refusal.

## Authentication and configuration

Use the caller-supplied Codex environment and authentication. Do not redirect `CODEX_HOME` or invent credentials. Explicit model/effort values are passed through without ranking; report unsupported selections as launch failures.
