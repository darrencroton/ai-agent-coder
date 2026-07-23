# Qwen Code CLI Reference

## Role and command

Qwen Code is eligible as Developer or delegate, in either access mode. The user, plan, or launcher chooses the role, model, and effort. This reference does not rank its capability.

Write schema-v3 policy/request JSON as documented in [delegate-contract.md](delegate-contract.md), then use `delegate_jobs.py launch`. The launcher owns Qwen's headless prompt, model flag, sandbox, text output, repository working directory, and capture. The tested Qwen Code command has no effort/variant flag, so a non-default effort request fails closed before launch.

```bash
qwen --prompt <prompt> [--model <model>] --sandbox --output-format text
```

The tracked launcher fixes the child working directory to the policy repository; Qwen Code has no separate repository-directory flag. The composed command is identical for both access modes: Qwen Code's CLI exposes no separate write-enabled flag, so what changes between a read-only and a read-write launch is entirely the rendered prompt.

## Read-only and read-write boundary

The launcher requests `--sandbox`, which isolates Qwen Code from the host when enabled. A caller-supplied `QWEN_SANDBOX` setting can override that flag, so the evidence profile records sandboxing as requested rather than guaranteed. On macOS, the default Seatbelt profile restricts writes outside the project directory; container providers mount the workspace. Neither mechanism makes the repository itself read-only, and neither confines a read-write delegate to its request's specific `authorized_surface`. The embedded delegate contract therefore remains the authority that forbids edits in read-only mode, and that forbids Git/GitHub mutations, commits, and re-delegation in both modes. This profile is prompt-enforced, not mechanically differentiated between access modes.

A live check against the currently installed CLI in this environment hung on any prompt requiring a tool call (file read or write), succeeding only for a no-tool-call prompt; this looks like an approval-mode setting that must be configured in the caller's own Qwen Code configuration for unattended tool use, not a flag this launcher can pass. Confirm your own Qwen Code configuration allows unattended tool calls before relying on a Qwen delegate for a task that requires one.

## Lifecycle and configuration

Qwen Code does not accept a caller-set session ID at first launch. The helper captures an ID only from a chat JSONL record correlated by prompt, repository, and start time; unresolved ownership remains `null`. An owned chat transcript is the preferred activity signal, with captured output as the fallback.

Use `delegate_jobs.py activity`, `wait`, `extract`, and `cancel`. A validated continuation composes `qwen --resume <captured-id>` from a fresh same-run request with `parent_label` and an advancing `-rN` label. The shared parent-identity and policy rules are defined in [delegate-contract.md](delegate-contract.md#validated-continuation); do not invoke raw resume commands.

Use the caller-supplied Qwen Code environment and authentication. Do not redirect its configuration or invent credentials. Discover exact model identifiers from the caller's Qwen Code configuration or `qwen --help`; do not guess. Pass explicit model choices through without capability ranking. Qwen Code effort must remain `default` because the tested command exposes no effort flag; unsupported selections fail before launch.
