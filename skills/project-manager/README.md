# Project Manager (Mode B) — Operator Guide

Mode B runs a frozen implementation plan autonomously under a supervising PM agent: one fresh Developer session per slice, a mechanical floor of eight non-waivable checks, a recorded PM assessment for every decided slice, independent reviews commissioned by PM where risk warrants, and a durable audit trail. The PM's operating contract is [SKILL.md](SKILL.md); this file covers the toolkit, layout, privacy, and a verify-your-setup trial.

## Launcher

To start a Mode B run, paste the launcher prompt from [SKILL.md](SKILL.md#launcher) — the single authoritative copy — into a fresh PM-capable session.

## Requirements

- Python ≥ 3.13 (`PurePosixPath.full_match` drives authorized-surface matching; `pm.py` refuses older interpreters)
- `git`, `tmux`
- At least one supported coding CLI for the Developer seat: `codex`, `claude`, `copilot`, `opencode`, or `qwen` (or any command via `--harness-command`)
- Optionally a reviewer CLI (`codex`, `claude`, `copilot`, `opencode`, `qwen`) for PM-commissioned reviews

All five supported harnesses are equally eligible for either seat; the operator chooses what fits the plan. Profiles encode factual CLI differences only, and the two seats differ because they launch different commands. OpenCode and Qwen expose no interactive effort override, so `--effort` fails closed for the Developer seat rather than being silently ignored. Reviews run one-shot, where `opencode run` does accept effort as `--variant` — so `review --effort` works for OpenCode but still fails closed for Qwen. OpenCode does not validate the variant name itself — an unknown one runs at the model's default — so `review` verifies it against the model's inventory first, refusing an unsupported variant, an unnamed model, or an unreadable inventory.

Every **Developer** seat launches at its harness's fullest autonomy (codex `--dangerously-bypass-approvals-and-sandbox`, claude `--permission-mode bypassPermissions`, copilot `--allow-all`, opencode `--auto`, qwen `--yolo`). An unattended Developer has nobody to answer a permission prompt, so a prompt is a silent stall, not a safety feature. Reviewer seats are the deliberate exception: a reviewer emits a report rather than edits, so codex, claude, and opencode reviewers keep their read-only modes.

**Run PM inside real process and filesystem isolation** — a container or VM. The harnesses no longer constrain what a Developer can touch outside the repository, and the floor cannot see it either (see *Trust model*), so a separate checkout is not containment: nothing stops a Developer reaching the rest of the host.

## CLI

All commands: `python3 skills/project-manager/scripts/pm.py <command> …`, run from inside the target repository (except `check-plan`/`init`, which take paths). Mutating commands need the run capability token (`--token` or `PM_RUN_TOKEN` in your environment).

| Command | Purpose |
|---|---|
| `check-plan --plan P [--repo R]` | "Is this plan runnable?" — errors fail closed; also runs automatically at init |
| `init --repo R --plan P --harness H [--model M] [--effort E] [--branch B \| --create-branch B] [--attest "Slice 1,…"] [--max-attempts N] [--reviewer-tools T,…] [--reviewer-model M] [--reviewer-effort E] [--harness-command CMD]` | set up the run; freezes the plan digest; prints the token once (refuses main/master by implicit default — pass `--branch`/`--create-branch`) |
| `status [--report] [--run ID]` | where are we? prints the run's `recorded event span` (last recorded activity, not wall-clock now — `status` logs no event); `--report` regenerates `run-report.md`, whose header carries total run time with its endpoints |
| `approve --slice ID --reason TEXT` | record a **human** approval for a plan-gated slice |
| `start-slice [--model M] [--effort E] [--risk elevated] [--reviewer-tools T,…] [--harness-command CMD]` | launch (or relaunch) the next eligible slice in a fresh tmux session |
| `observe [--wait N]` | evidence: liveness, pane tail, result presence, hard-stop markers; a wait returns early only on session death, `result.json` appearing, or a hard-stop marker (never a mere pane change), and reports elapsed wait time. Every call waits at least 120s, whatever `--wait` asks for, so a controller looping on `observe` cannot spend a model round-trip per second. The floor is charged for silence only: an already-signalled slice returns immediately, and the wait holds no state between calls |
| `send --text T --reason R` | one-line nudge into the live session (refused over hard prompts; costs nothing) |
| `finalize` | run the eight-fact floor and collect evidence (decides nothing) |
| `finalize --accept "reasoning" \| --steer "correction" \| --stop "reason" [--risk elevated]` | PM's recorded decision; accept requires a passing floor (+ both fresh reviews when elevated); steer costs an attempt |
| `review --slice ID --skill drift-audit\|code-review [--tool T] [--model M] [--effort E] [--timeout N] [--adjudicated TEXT …]` | commission an independent review pinned to `before_head..HEAD` (`--tool` ∈ codex/claude/copilot/opencode/qwen); prints the report path, stderr path, and reviewer process-group id at launch, before waiting. Blocks until the reviewer exits; `--timeout` (default 3600s) kills its process group and fails closed, as a backstop against a hang rather than a cadence — raise it for a slow cold local model. `--adjudicated` (repeatable) names rulings PM already settled so a reviewer stops re-raising them — it bounds attention only, still gets a full dissenting finding from a reviewer that disagrees, and the rendered prompt is persisted for audit |
| `notes --append TEXT \| --set TEXT [--run ID]` | update the run's curated `notes.md` — writes the state-dir original then re-mirrors; never hand-edit the `.pm/` mirror |
| `rate --text TEXT [--run ID]` | record the run's harness/model performance rating once, per [references/model-performance-rubric.md](references/model-performance-rubric.md) — writes `model-performance.md` the same way as `notes`, included verbatim in `run-report.md` |
| `stop --reason R [--slice-status stopped] [--scavenge]` | end the run preserving evidence; `--scavenge` sweeps sessions even with state destroyed |

The attempt budget defaults to 10 per slice — the initial launch plus ten steers or relaunches. Lower it at `init` (`--max-attempts N`) for a strong Developer model where you want autonomy measured tightly; a slice that needs more than 10 rounds is itself the finding, and the human should see it rather than have more budget granted. Weak or unproven Developer models fail by exhausting the budget, not by shipping bad code. `status` and `finalize` print attempts against the ceiling so the PM can pace steering decisions without reading `run.json`.

Exit codes: 0 success; 1 = a `finalize` refusal — a floor fact failed, or `--accept` was refused for another recorded reason (e.g. a missing or stale mandatory review on an elevated slice); 2 = error/refusal (integrity failures are prefixed `INTEGRITY:` and are terminal — start a new run).

If a harness displays a directory-trust or permission prompt, the PM stops and leaves that approval to the human. The human may configure trust through the harness's own supported mechanism, then rerun `start-slice`; the PM must not acknowledge the dialog with `tmux send-keys` or change user-global harness configuration itself. Autonomy flags do not clear a folder-trust dialog — Copilot still asks on a folder it has not been told to remember — so that one stays a human decision.

## Layout: who owns what

- **`<git-dir>/pm/<run-id>/`** — authoritative state (`run.json`, HMAC-authenticated) and every PM-authored original (assessments, reviews, notes, performance rating, report — plain files, protected by living outside the worktree, not by the MAC). See [references/run-state.md](references/run-state.md).
- **`<repo>/.pm/runs/<run-id>/`** — the human-facing mirror of PM artifacts plus Developer-authored evidence (`result.json`, `validation.md`, pane captures, diffs, prompts). Self-ignoring via `.pm/.gitignore`. The boundary, precisely: PM's records and decisions live in the controller originals and are never read back from this mirror — but Developer-authored evidence here (`result.json`, `validation.md`) *is* input to the floor and to PM's assessment. Vandalizing it damages the Developer's own case and fails the slice closed (floor fact 4); it can never forge an acceptance or alter PM state.
- Per slice: `prompt.md` (the rendered authorization), `steer-attempt-<n>.md` (each `finalize --steer` correction, verbatim — PM injects only a one-line pointer to it, so a long or multi-line correction cannot be truncated or split by a harness TUI), `pane-live.txt`/`pane.txt`, `status-before/after.txt`, `diff.patch`, `validation.md`, `result.json`, `attempt-<n>/` for superseded launches, `assessment.md` + `review-*.md` mirrors, and `review-*-prompt.md` — the exact prompt each reviewer was commissioned with, so any PM adjudication that narrowed a review is visible to the human rather than inferable only from what the report omits.

## Trust model, honestly

Mechanical and non-waivable: the eight floor facts (frozen plan digest; repo/branch identity; recorded approvals; result presence/identity; changed files ⊆ frozen surface; commit ancestry and branch head; clean worktree; no visible hard-stop prompt). Everything semantic — is the change good, is the evidence sufficient — is the PM agent's recorded judgement; read the assessments.

Known limits, inherited and stated: the floor sees final Git-visible worktree state only (ignored files, Git hooks/metadata, write-then-revert effects, and anything a Developer writes outside the repository escape it — containment is your sandbox around the run, not the harness); dependency/license/side-effect stops are heuristic (pane markers + prompt prohibitions + plan-level surface exclusion); role authority is capability-token-raised, not OS-enforced — a same-user process that steals the token or subverts the PM agent is outside the threat model; `attested` slices are operator narration; PM-seat quality is load-bearing — a weak model in the PM seat weakens the judgement layer itself.

## Privacy & sensitive artifacts

Everything stays local; the toolkit phones nowhere. But captured artifacts can still contain secrets your repo or shell exposed:

| Artifact | May contain |
|---|---|
| `pane*.txt` | anything printed in-session: code, env values, echoed secrets |
| harness-side transcripts (e.g. Claude Code's own session files — the toolkit passes `--session-id` but does not copy them into `.pm/`) | full session content, stored under the harness's home directory |
| `diff.patch`, `review-*.md`, `review-*-prompt.md` | repository code, including sensitive files inside the surface; the prompt copies also embed the plan's slice contract and the review skill's bundle |
| `validation.md`, `result.json` | command output the Developer chose to record |
| `prompt.md`, `steer-attempt-<n>.md` | the plan's slice contract, and whatever PM wrote into a correction |

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
python3 $PM init --repo . --plan ../trial-plan.md --harness fake --create-branch pm-trial --harness-command "sh ../fake-dev.sh"
export PM_RUN_TOKEN=<the token line init printed>
python3 $PM start-slice
python3 $PM observe --wait 30
python3 $PM finalize                       # expect: eight PASS lines
python3 $PM finalize --accept "Trial slice: diff creates hello.txt exactly per contract; validation output shows the expected content; floor 8/8."
python3 $PM rate --text "Developer (fake harness):
Process discipline: 5/5 — no incidents.
Reporting reliability: 5/5 — result matched.
Output quality: 5/5 — trial slice exactly per contract."
python3 $PM status --report                # then read .pm/runs/<id>/run-report.md
```

You should see the floor pass 8/8, the acceptance land with an assessment, and a run report you can read end-to-end. `stop --scavenge --reason cleanup` tears down anything left.

## Maintainer map

`scripts/pm.py` (entry) → `pm_lib/`: `cli` (parsing/dispatch) · `plan` (parser, lint, risk derivation) · `state` (lite-1 authenticated state, events, report) · `git_ops` (facts + surface matching) · `floor` (the eight facts) · `sessions` (all tmux contact + hard-stop markers) · `profiles` (harness table) · `slice_ops` (command orchestration) · `review` (PM-commissioned reviewers) · `prompts` (template rendering). Tests in `tests/` use fake harnesses via `--harness-command`; tmux-dependent tests skip when tmux is absent. `pm_test_helpers` owns the shared fixtures — `PlanTestCase` (plain temp directory), `PmTestCase` (adds a git repo), `TmuxRunTestCase` (adds the session reaper and `init`/wait helpers), and the fake-harness builders — and pins `PM_TMUX_SOCKET` so each test process drives its own tmux server. Modules are independent and safe to run in parallel.
