# Fix prompt: codex delegate continuations fail at launch

Copy everything in the block below into a fresh chat in this repository.

---

```md
In this repository (`ai-agent-coder`), every `orchestrator` delegate continuation on the `codex` harness fails at launch. Please fix it.

## Symptom

A validated schema-v3 continuation request (`parent_label` set, `-r1` label) rejects immediately with:

    error: unexpected argument '--sandbox' found
    tip: to pass '--sandbox' as a value, use '-- --sandbox'
    Usage: codex exec resume --model <MODEL> --config <key=value> <SESSION_ID> <PROMPT>

Observed with codex-cli 0.145.0. It affects both access modes, so the continuation feature is unusable for codex.

## Cause

`skills/orchestrator/scripts/delegate_contract.py`, the `codex` branch of `compose_delegate_command` (around lines 798-810): the function switches the base command to `["codex", "exec", "resume", resume_session_id, prompt]` when resuming, but then appends `--sandbox <mode>`, `--skip-git-repo-check`, and `-C <repo>` unconditionally, exactly as it does for a fresh `codex exec`. `codex exec resume` does not accept `--sandbox` or `-C`.

The installed skill at `~/.claude/skills/orchestrator/` is byte-identical to the repo copy, so fixing the repo fixes both.

## Ground truth already verified (no need to re-derive)

- `codex exec resume --help` accepts: `-c/--config <key=value>`, `-m/--model`, `--skip-git-repo-check`, `--last`, `--all`, `--ephemeral`, `--json`, `-o/--output-last-message`, `--dangerously-bypass-approvals-and-sandbox`. It does **not** accept `--sandbox` or `-C/--cd`.
- The offending flags on the resume path are therefore `--sandbox <mode>` and `-C <repo>` only; `--skip-git-repo-check` is fine to keep.
- `-C` is redundant on this path: `delegate_jobs.py` (around line 660) launches every delegate through a wrapper invoked with `--cwd <cwd>`, so the child process already starts in the policy repository.
- `codex` config exposes sandboxing as a config value, so `-c sandbox_mode="read-only"` is the likely equivalent of `--sandbox read-only` on resume — **verify this against the installed codex before relying on it**, including that it actually constrains a resumed session rather than being silently accepted.

## The real decision, which is not just deleting flags

For a fresh codex delegate, `--sandbox read-only` versus `--sandbox workspace-write` is how the access mode is *mechanically* enforced — `references/codex.md` advertises the Codex read-only sandbox and `workspace-write` confinement as the harness's actual boundary, and it is the strongest enforcement any supported harness offers. If a resumed session cannot be given a sandbox flag, then decide and document which of these is true:

1. `-c sandbox_mode=...` genuinely constrains the resumed session — then use it, and the advertised boundary still holds for continuations.
2. It does not, and a resumed session inherits whatever sandbox its parent recorded — then say so honestly in `references/codex.md`, and consider whether a `read-only` continuation of a `read-write` parent (or vice versa) must be refused rather than launched with an unverified boundary.
3. Neither can be established — then fail closed: refuse codex continuations with a clear error rather than launching them with unknown sandboxing.

`CONTRIBUTING.md`'s change conventions apply directly here: *fail closed by default*, *be honest about enforcement* (every documented guarantee names its layer: mechanical, evidence-checked, or heuristic — overclaiming is a defect), and *fix the owning layer*. Do not let a continuation silently acquire broader write access than its request authorized.

## Test gap worth closing in the same change

`skills/orchestrator/tests/test_delegate_contract.py::test_harness_resume_commands_use_only_the_explicit_parent_session_id` passes today. It asserts only the command *prefix* and the absence of `--session-id`/`--last`/`--continue` — it never asserts that the composed resume command is one the CLI would accept. That is why the suite is 77/77 green while every real codex continuation fails.

Add a boundary-focused assertion that the codex resume command carries no flag the resume subcommand rejects (at minimum: no `--sandbox`, no `-C`). Keep it boundary-focused, not a permutation matrix. Also check the other four harnesses' resume paths for the same class of defect — claude (`--resume` + `--permission-mode`), copilot (`--resume=`), opencode (`--session`), qwen (`--resume`) — and fix any that compose flags their resume mode rejects.

## Constraints

- Light touch. The correct fix is likely a handful of lines plus one or two assertions plus a documentation sentence. Do not restructure the harness command table.
- Never weaken a failing test to make it pass; never use `--no-verify`.
- Run both suites before reporting: `python3 -m unittest discover -s skills/orchestrator/tests -p 'test_*.py'` and `python3 -m unittest discover -s skills/project-manager/tests -p 'test_*.py'`. Run them one at a time — concurrent runs share a tmux server and produce spurious timing failures.
- Update `CHANGELOG.md` under `[Unreleased]` and ask before committing.

## Definition of done

A real codex continuation launches successfully end to end (a fresh `read-only` delegate, then a `-r1` continuation of it via `delegate_jobs.py launch`), the access-mode boundary for a resumed session is either enforced or honestly documented as unenforced, and a regression test would catch reintroduction of a resume-rejected flag.
```
