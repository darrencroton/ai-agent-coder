# Project Manager (Mode B) — Operator Guide

Mode B runs a frozen implementation plan autonomously under a supervising PM agent: one fresh Developer session per slice, a mechanical floor of eight non-waivable checks, a recorded PM assessment for every decided slice, independent reviews commissioned by PM where risk warrants, and a durable audit trail. The PM's operating contract is [SKILL.md](SKILL.md); this file covers the toolkit, layout, privacy, and a verify-your-setup trial.

## Requirements

- Python ≥ 3.13 (`PurePosixPath.full_match` drives authorized-surface matching; `pm.py` refuses older interpreters)
- `git`, `tmux`
- At least one supported coding CLI for the Developer seat: `codex`, `claude`, `copilot`, or `opencode` (or any command via `--harness-command`)
- Optionally a reviewer CLI (`codex`, `claude`, `copilot`, `opencode`, `qwen`) for PM-commissioned reviews

## CLI

All commands: `python3 skills/project-manager/scripts/pm.py <command> …`, run from inside the target repository (except `check-plan`/`init`, which take paths). Mutating commands need the run capability token (`--token` or `PM_RUN_TOKEN` in your environment).

| Command | Purpose |
|---|---|
| `check-plan --plan P [--repo R]` | "Is this plan runnable?" — errors fail closed; also runs automatically at init |
| `init --repo R --plan P --harness H [--model M] [--effort E] [--branch B \| --create-branch B] [--attest "Slice 1,…"] [--max-attempts N] [--reviewer-tools T,…] [--reviewer-model M] [--reviewer-effort E] [--harness-command CMD]` | set up the run; freezes the plan digest; prints the token once |
| `status [--report] [--run ID]` | where are we? `--report` regenerates `run-report.md` |
| `approve --slice ID --reason TEXT` | record a **human** approval for a plan-gated slice |
| `start-slice [--model M] [--effort E] [--risk elevated] [--reviewer-tools T,…] [--harness-command CMD]` | launch (or relaunch) the next eligible slice in a fresh tmux session |
| `observe [--wait N]` | evidence: liveness, pane tail, result presence, hard-stop markers; a wait returns early only on session death, `result.json` appearing, or a hard-stop marker (never a mere pane change), and reports elapsed wait time |
| `send --text T --reason R` | one-line nudge into the live session (refused over hard prompts; costs nothing) |
| `finalize` | run the eight-fact floor and collect evidence (decides nothing) |
| `finalize --accept "reasoning" \| --steer "correction" \| --stop "reason" [--risk elevated]` | PM's recorded decision; accept requires a passing floor (+ both fresh reviews when elevated); steer costs an attempt |
| `review --slice ID --skill drift-audit\|code-review [--tool T] [--model M] [--effort E] [--timeout N]` | commission an independent review pinned to `before_head..HEAD`; prints the report path, stderr path, and reviewer process-group id at launch, before waiting |
| `stop --reason R [--slice-status stopped] [--scavenge]` | end the run preserving evidence; `--scavenge` sweeps sessions even with state destroyed |

Exit codes: 0 success; 1 = a `finalize` refusal — a floor fact failed, or `--accept` was refused for another recorded reason (e.g. a missing or stale mandatory review on an elevated slice); 2 = error/refusal (integrity failures are prefixed `INTEGRITY:` and are terminal — start a new run).

## Layout: who owns what

- **`<git-dir>/pm/<run-id>/`** — authoritative state (`run.json`, HMAC-authenticated) and every PM-authored original (assessments, reviews, notes, report — plain files, protected by living outside the worktree, not by the MAC). See [references/run-state.md](references/run-state.md).
- **`<repo>/.pm/runs/<run-id>/`** — the human-facing mirror of PM artifacts plus Developer-authored evidence (`result.json`, `validation.md`, pane captures, diffs, prompts). Self-ignoring via `.pm/.gitignore`. The boundary, precisely: PM's records and decisions live in the controller originals and are never read back from this mirror — but Developer-authored evidence here (`result.json`, `validation.md`) *is* input to the floor and to PM's assessment. Vandalizing it damages the Developer's own case and fails the slice closed (floor fact 4); it can never forge an acceptance or alter PM state.
- Per slice: `prompt.md` (the rendered authorization), `pane-live.txt`/`pane.txt`, `status-before/after.txt`, `diff.patch`, `validation.md`, `result.json`, `attempt-<n>/` for superseded launches, `assessment.md` + `review-*.md` mirrors.

## Trust model, honestly

Mechanical and non-waivable: the eight floor facts (frozen plan digest; repo/branch identity; recorded approvals; result presence/identity; changed files ⊆ frozen surface; commit ancestry and branch head; clean worktree; no visible hard-stop prompt). Everything semantic — is the change good, is the evidence sufficient — is the PM agent's recorded judgement; read the assessments.

Known limits, inherited and stated: the floor sees final Git-visible worktree state only (ignored files, Git hooks/metadata, and write-then-revert effects escape it); dependency/license/side-effect stops are heuristic (pane markers + prompt prohibitions + plan-level surface exclusion); role authority is capability-token-raised, not OS-enforced — a same-user process that steals the token or subverts the PM agent is outside the threat model; `attested` slices are operator narration; PM-seat quality is load-bearing — a weak model in the PM seat weakens the judgement layer itself.

## Privacy & sensitive artifacts

Everything stays local; the toolkit phones nowhere. But captured artifacts can still contain secrets your repo or shell exposed:

| Artifact | May contain |
|---|---|
| `pane*.txt` | anything printed in-session: code, env values, echoed secrets |
| harness-side transcripts (e.g. Claude Code's own session files — the toolkit passes `--session-id` but does not copy them into `.pm/`) | full session content, stored under the harness's home directory |
| `diff.patch`, `review-*.md` | repository code, including sensitive files inside the surface |
| `validation.md`, `result.json` | command output the Developer chose to record |

Clean up with your normal tools when a run is done; `.pm/` and `<git-dir>/pm/` are plain directories. Never commit `.pm/` (it self-ignores) and never share the run token — it authorizes state writes.

## Verify your setup (no real model, ~1 minute)

From an empty scratch directory:

```sh
git init -q -b main trial && cd trial && git commit --allow-empty -q -m base
cat > ../trial-plan.md <<'PLAN'
## Slice 1: hello file

### Intended Change
- Create hello.txt containing "hello".

### Acceptance Criteria
- Outputs: hello.txt with the single word hello

### Authorized Surface
- Files allowed to change:
  - hello.txt

### Explicit Non-Goals
- Nothing else.

### Risk Flags
- Risky surfaces touched: none
- Approval needed before implementation: no

### Validation Plan
- Commands to run: cat hello.txt

### Rollback Path
- Revert the commit.
PLAN
cat > ../fake-dev.sh <<'FAKE'
#!/bin/sh
echo "fake developer starting"; sleep 3
echo hello > hello.txt && git add hello.txt
git -c user.name=dev -c user.email=dev@local commit -q -m "Slice 1: hello file"
echo "ran: cat hello.txt -> $(cat hello.txt)" > "$PM_SLICE_ARTIFACT_DIR/validation.md"
printf '{"slice":"%s","status":"done","summary":"created hello.txt","notes":"trial run; nothing to carry forward"}\n' "$PM_SLICE_ID" > "$PM_RESULT_PATH"
cat -
FAKE
PM=<path-to>/skills/project-manager/scripts/pm.py
python3 $PM init --repo . --plan ../trial-plan.md --harness fake --harness-command "sh ../fake-dev.sh"
export PM_RUN_TOKEN=<the token line init printed>
python3 $PM start-slice
python3 $PM observe --wait 30
python3 $PM finalize                       # expect: eight PASS lines
python3 $PM finalize --accept "Trial slice: diff creates hello.txt exactly per contract; validation output shows the expected content; floor 8/8."
python3 $PM status --report                # then read .pm/runs/<id>/run-report.md
```

You should see the floor pass 8/8, the acceptance land with an assessment, and a run report you can read end-to-end. `stop --scavenge --reason cleanup` tears down anything left.

## Maintainer map

`scripts/pm.py` (entry) → `pm_lib/`: `cli` (parsing/dispatch) · `plan` (parser, lint, risk derivation) · `state` (lite-1 authenticated state, events, report) · `git_ops` (facts + surface matching) · `floor` (the eight facts) · `sessions` (all tmux contact + hard-stop markers) · `profiles` (harness table) · `slice_ops` (command orchestration) · `review` (PM-commissioned reviewers) · `prompts` (template rendering). Tests in `tests/` use fake harnesses via `--harness-command`; tmux-dependent tests skip when tmux is absent.
