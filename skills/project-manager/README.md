# Project Manager (Mode B) — Operator Guide

Mode B runs a frozen implementation plan autonomously under a supervising PM agent: one fresh Developer session per slice, a mechanical floor of seven non-waivable checks, a recorded PM assessment for every decided slice, independent reviews commissioned by PM where risk warrants, and a durable audit trail. The PM's operating contract is [SKILL.md](SKILL.md); this file covers the toolkit, layout, privacy, and a verify-your-setup trial.

## Launcher

To start a Mode B run, paste the launcher prompt from [SKILL.md](SKILL.md#launcher) — the single authoritative copy — into a fresh PM-capable session.

## Requirements

- Python ≥ 3.13 (`PurePosixPath.full_match` drives authorized-surface matching; `pm.py` refuses older interpreters)
- `git` 2.31 or newer (`init` records the repository name with `git rev-parse --path-format=absolute`), `tmux`
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
| `init --repo R --plan P --harness H [--model M] [--effort E] [--model-tag TEXT] [--run-tag TEXT] [--branch B \| --create-branch B] [--attest "Slice 1,…"] [--max-attempts N] [--reviewer-tools T,…] [--reviewer-model M] [--reviewer-effort E] [--harness-command CMD]` | set up the run; freezes the plan digest; prints the token once (refuses main/master by implicit default — pass `--branch`/`--create-branch`). Pass full model ids (for example `claude-opus-5-5`), never aliases: the ledger groups exact strings and an alias's meaning drifts. `--model-tag` is the run's default Developer tag and `--run-tag` labels the whole run (see [Ledger](#ledger)); a blank or multi-line tag, or one containing `=`, is refused. |
| `status [--report] [--run ID]` | where are we? prints the run's `recorded event span` (last recorded activity, not wall-clock now — `status` logs no event); `--report` regenerates `run-report.md`, whose header carries total run time with its endpoints |
| `approve --slice ID --reason TEXT` | record a **human** approval for a plan-gated slice |
| `grant --slice ID --path PATH --evidence TEXT [--run ID] [--token T]` | extend the in-flight slice's effective authorized surface by one exact file path on recorded evidence; ratchets the slice to elevated **and stales every review already recorded for it**, so both mandatory reviews must be re-commissioned after the last grant. Refused, not warned, on a directory, glob, or dependency/lockfile/license/whole-repo-shaped path — isolate those into their own approval-gated slice |
| `start-slice [--model M] [--effort E] [--model-tag TEXT] [--risk elevated] [--reviewer-tools T,…] [--harness-command CMD]` | launch (or relaunch) the next eligible slice in a fresh tmux session (`--model-tag` overrides `init`'s for this launch only; a later launch without it falls back to the run default); a relaunch of a live slice, including resume after `stop`, is refused by the judgment gate until the current submission is judged (see [SKILL.md](SKILL.md#developer-and-reviewer-judgments)). Pass full model ids (for example `claude-opus-5-5`), never aliases: the ledger groups exact strings and an alias's meaning drifts. |
| `observe [--wait N]` | evidence: liveness, pane tail, result presence, and any interactive-dialog marker on the **visible** pane; a wait returns early only on session death, `result.json` appearing, or such a marker (never a mere pane change), and reports elapsed wait time. The marker is a signal for PM to weigh, not a verdict — see [SKILL.md](SKILL.md) *Reading the pane* |
| `send --text T --reason R` | one-line nudge into the live session (refused while an interactive dialog marker is on the visible pane, so a blind Enter cannot answer a real dialog; a pre-typing refusal may be re-issued once ordinary output clears, but `TypedNotSubmitted` means the line is already in the input and must not be re-issued). Costs nothing |
| `finalize` | run the seven-fact floor, collect evidence, and print the captured pane tail (decides nothing) |
| `finalize --accept "reasoning" \| --steer "correction" \| --stop "reason" --cause plan\|developer\|environment [--risk elevated]` | PM's recorded decision; accept requires a passing floor (+ both fresh reviews when elevated) and prints the pane tail, since the acceptance reasoning must state what it showed; steer costs an attempt. All three are refused by the judgment gate, naming every gap, until the current submission has a current Developer judgment, a rating for every intact review of it, and an order for every code panel (see [SKILL.md](SKILL.md#developer-and-reviewer-judgments)) |
| `review --slice ID --skill drift-audit\|code-review [--tool T] [--model M] [--effort E] [--model-tag TEXT] [--timeout N] [--adjudicated TEXT …] [--adjudicated-file PATH]` | commission an independent review pinned to `before_head..HEAD` (`--tool` ∈ codex/claude/copilot/opencode/qwen); prints the report path, stderr path, and reviewer process-group id at launch, before waiting. Blocks until the reviewer exits; `--timeout` (default 3600s) kills its process group and fails closed, as a backstop against a hang rather than a cadence — raise it for a slow cold local model. Pass full model ids (for example `claude-opus-5-5`), never aliases: the ledger groups exact strings and an alias's meaning drifts. `--model-tag` labels this commission's Reviewer configuration in the ledger. `--adjudicated` (repeatable) names rulings PM already settled so a reviewer stops re-raising them — it bounds attention only, still gets a full dissenting finding from a reviewer that disagrees, and the rendered prompt is persisted for audit. `--adjudicated-file PATH` reads the same kind of ruling one per line (blank/`#` lines skipped) and combines with any `--adjudicated` flags, file lines first — reuse one file byte-for-byte across a panel's commissions instead of retyping a ruling per call, since `judge-reviews`' panel comparison refuses a ranking the moment two commissions' adjudication text differs by even a word |
| `judge-reviews --file INPUT [--run ID] [--token T]` | record PM's 0–2 rating for a drift or code report, or a separate code-panel order/ties in signed state; see [references/review-judgments.md](references/review-judgments.md) |
| `judge-developer --file INPUT [--run ID] [--token T]` | record PM's 0–2 rating for the current Developer submission before its decision, retaining submission identity and evidence context; corrections explicitly supersede earlier judgments |
| `notes --append TEXT \| --set TEXT [--run ID]` | update the run's curated `notes.md` — writes the state-dir original then re-mirrors; never hand-edit the `.pm/` mirror |
| `stop --reason R [--slice-status stopped] [--scavenge]` | end the run preserving evidence; `--scavenge` sweeps sessions even with state destroyed, but with no run id and state that cannot be read it kills every `pm-*` session on the tmux server, including a live Developer's, so it is for a run whose state is gone, never for cleanup inside another run |
| `ledger render [--out PATH] [--run-tag TEXT …]` | render every per-run ledger file under the ledger directory (every `<host>/*.json`) into one markdown leaderboard, by default `leaderboard.md` in the ledger directory; `--run-tag` (repeatable) keeps only runs carrying one of the tags; needs no run and no token; exits 2 when there are no files, or no run carries any of the given tags, naming the directory searched |
| `ledger tag RUN_ID [--run-tag TEXT] [--model-tag OLD=NEW …]` | retag a finished run's per-run ledger files in place, every host copy at once: `--run-tag` sets the run tag, and each `--model-tag` (repeatable; all renames apply together, so a swap works) renames a model tag on every Developer and review, `untagged` as OLD selecting those with no tag; needs at least one flag, no run and no token; refuses before writing anything on an unknown run, an OLD that matches nothing, a malformed form or a host folder it cannot list, and restores the copies already rewritten if a write fails. See [references/model-ledger.md](references/model-ledger.md) |

The attempt budget defaults to 10 per slice — the initial launch plus ten steers or relaunches. Lower it at `init` (`--max-attempts N`) for a strong Developer model where you want autonomy measured tightly; a slice that needs more than 10 rounds is itself the finding, and the human should see it rather than have more budget granted. Weak or unproven Developer models fail by exhausting the budget, not by shipping bad code. `status` and `finalize` print attempts against the ceiling so the PM can pace steering decisions without reading `run.json`.

Exit codes: 0 success; 1 = a `finalize` refusal — a floor fact failed, or `--accept` was refused for another recorded reason (e.g. a missing or stale mandatory review on an elevated slice); 2 = error/refusal, or the judgment gate (integrity failures are prefixed `INTEGRITY:` and are terminal — start a new run).

If a harness displays a directory-trust or permission prompt, the PM stops and leaves that approval to the human. The human may configure trust through the harness's own supported mechanism, then rerun `start-slice`; the PM must not acknowledge the dialog with `tmux send-keys` or change user-global harness configuration itself. Autonomy flags do not clear a folder-trust dialog — Copilot still asks on a folder it has not been told to remember — so that one stays a human decision.

## Optional: the poll guard (Claude Code PM seat)

`hooks/pm-poll-guard.py` is an optional cost guard for a PM seat running in Claude Code. A PM session backgrounds long commands — a Developer wait, a commissioned review, a test run — then goes looking to see whether they finished. Claude Code already re-invokes the agent when a background command exits, so the looking is redundant, and each look costs a full model round-trip that resends the entire conversation. Measured on one 1391-turn session supervising two PM runs: about 26% of turns were exactly this. These are harness calls, not PM commands, so the toolkit itself cannot see or bound them; the guard has to live in the harness.

It denies two shapes, one per tool. Both require `.pm/` in the session's working directory, so nothing outside a PM run is affected, and it fails open on any unexpected input or error — it bounds spend, and can never strand a run.

**`Read`** — a re-read byte-identical to the file's previous read: provably no new information. The first read of a file, and any read returning new content, still pass. The path must match Claude Code's scratchpad task-output layout (`.../claude-<uid>/<project>/<session-uuid>/tasks/<id>.output`), which a repository's own `tasks/` directory cannot. This branch did the heavy lifting on the measured session: 273 of 294 repeat reads.

**`Bash`** — a **backgrounded** command that waits and then inspects a PM artifact. Backgrounding it is itself the waste: it schedules a second completion notification for a target that already has one coming. The measured forms are hand-rolled waiters:

```sh
until [ -s .../tasks/<id>.output ]; do sleep 30; done; ...
until grep -qi "^## Verdict" .git/pm/<run-id>/slices/slice-002/review-4-drift-audit-opencode.md; do sleep 60; done
until [ -f .pm/runs/<run-id>/slices/slice-003/result.json ]; do sleep 60; done
```

The rule keys on the wait and the target, never on the inspector — across the 32 measured polls the inspectors were `grep` (17), `cat` (13), `head` (8), `git log` (7), `test -s` (5), `test -f` (5), `tail` (3) and `ls` (2), so an allowlist of "reading" commands would have missed most of them. Commands invoking `pm.py` are exempt: `observe --wait` and `review` are the toolkit's own legitimate waiters, and `review` launches the very reviewer it waits on.

A **foreground** wait is always allowed. It blocks the turn but spawns no extra wake, and it is the right tool when no notification is genuinely coming — after a session resume, say. The deny message says so, so the escape hatch is always one edit away.

Install:

```sh
cp skills/project-manager/hooks/pm-poll-guard.py ~/.claude/hooks/
```

then add to `~/.claude/settings.json` (merging with any existing `hooks` block):

```json
"hooks": {
  "PreToolUse": [
    {
      "matcher": "Read",
      "hooks": [
        { "type": "command", "command": "python3 ~/.claude/hooks/pm-poll-guard.py", "timeout": 10 }
      ]
    },
    {
      "matcher": "Bash",
      "hooks": [
        { "type": "command", "command": "python3 ~/.claude/hooks/pm-poll-guard.py", "timeout": 10 }
      ]
    }
  ]
}
```

Installing only the `Read` matcher is supported and still worth doing; the `Bash` matcher is what closes the hand-rolled-waiter route around it. Use an absolute path if your harness does not expand `~`. The guard keeps a per-session digest stamp under `~/.claude/hooks/.pm-poll-guard/`; delete that directory any time. Other harnesses in the PM seat have no equivalent, and the run works without the guard — it costs more.

### The pane is PM's read, not a fact

No floor fact examines the pane, so the toolkit's job is to make PM's own read unavoidable and its result durable: `finalize` prints the captured tail on every path that records an assessment — the bare evidence run, `--accept`, and `--stop` — and [SKILL.md](SKILL.md) requires the acceptance reasoning to state what it showed. That sentence in `assessment.md` is the audit trace — a PM that skipped the read is visible in the record instead of merely unmeasured. Deliberately absent is any check that the reasoning *mentions* the pane: scanning PM's prose for a keyword would be the same defect as the fact this replaced.

The one scan that remains guards PM's own keystrokes, and is not a verdict on the run. `send_prompt`/`send_line` send text, then Enter, then a second Enter after a settle — and if the first Enter surfaces a trust or credential dialog, the second would answer it. That window is sub-second and inside one call, so no PM turn can interpose and it has to be code. It matches literal dialog strings on the **visible** pane only, names what it matched, and is not overridable: a marker literal cannot distinguish a log line from a real dialog, so no comparison is allowed to authorize a keystroke (the CHANGELOG records the override that was tried and removed). A failed safety capture also refuses the Enter instead of reading as a clear pane. If a false match happens before typing, visible-pane scoping lets ordinary output scroll away and PM re-issue; after `TypedNotSubmitted`, the text is already in the input and the safe routes are relaunch, stop, or human cleanup — never a blind retry.

### What the toolkit does instead of guarding

Wait *length* is the PM's judgement, not the hook's business, so the toolkit advises rather than refuses: when `observe --wait` elapses with no session-death, result, or dialog-marker signal, it prints a note suggesting a longer wait or an action instead. It never changes the exit code. Measured across two real runs, the wasteful pattern was not re-polling an answered question but repeating a too-short wait — `20/20/20/20` against a Developer that needed 85 minutes — so the note nudges toward waiting once, longer.

## Layout: who owns what

- **`<git-dir>/pm/<run-id>/`** — authoritative state (`run.json`, HMAC-authenticated) and every PM-authored original (assessments, reviews, notes, report — plain files, protected by living outside the worktree, not by the MAC). See [references/run-state.md](references/run-state.md).
- **`<repo>/.pm/runs/<run-id>/`** — the human-facing mirror of PM artifacts plus Developer-authored evidence (`result.json`, `validation.md`, pane captures, diffs, prompts). Self-ignoring via `.pm/.gitignore`. The boundary, precisely: PM's records and decisions live in the controller originals and are never read back from this mirror — but Developer-authored evidence here (`result.json`, `validation.md`) *is* input to the floor and to PM's assessment. Vandalizing it damages the Developer's own case and fails the slice closed (floor fact 4); it can never forge an acceptance or alter PM state.
- Per slice: `prompt.md` (the rendered authorization), `steer-attempt-<n>.md` (each `finalize --steer` correction, verbatim — PM injects only a one-line pointer to it, so a long or multi-line correction cannot be truncated or split by a harness TUI), `pane-live.txt`/`pane.txt`, `status-before/after.txt`, `diff.patch`, `validation.md`, `result.json`, `attempt-<n>/` for superseded launches, `assessment.md` + `review-*.md` mirrors, and `review-*-prompt.md` — the exact prompt each reviewer was commissioned with, so any PM adjudication that narrowed a review is visible to the human rather than inferable only from what the report omits.

## Ledger

Each run writes one ledger file, `ledger/<host>/<run-id>.json` beside this README or under `PM_LEDGER_DIR` (an absolute path outside the supervised repository; the test helpers and the walkthrough below always set it, so trial runs never reach the real ledger). The file is derived from signed state after every command that carries the run token, is never edited by hand except through `ledger tag`, and a failed write is loud but never blocks a command. `ledger render` turns every run's file, from every host, into one leaderboard. How the models are scored, what the columns mean, tags, sharing a ledger between machines and the common situations are in [references/model-ledger.md](references/model-ledger.md); the file shape is in [references/run-state.md](references/run-state.md).

## Trust model, honestly

Mechanical and non-waivable: the seven floor facts (frozen plan digest; repo/branch identity; recorded approvals; result presence/identity; changed files ⊆ effective surface (frozen plan surface + recorded grants); commit ancestry and branch head; clean worktree). Every one is a property of repository state. Everything semantic — is the change good, is the evidence sufficient, does what the pane shows mean this run must stop — is the PM agent's recorded judgement; read the assessments.

Known limits, inherited and stated: the floor sees final Git-visible worktree state only (ignored files, Git hooks/metadata, write-then-revert effects, and anything a Developer writes outside the repository escape it — containment is your sandbox around the run, not the harness); dependency/license/side-effect stops rest on the Developer contract's explicit prohibition (`references/developer-prompt.md`), plan-level surface exclusion, and the operator's sandbox, with PM's pane read catching only a harness that actually prompts — and Developer seats launch at full autonomy, so often none does; role authority is capability-token-raised, not OS-enforced — a same-user process that steals the token or subverts the PM agent is outside the threat model; `attested` slices are operator narration; PM-seat quality is load-bearing — a weak model in the PM seat weakens the judgement layer itself.

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
- [ ] hello.txt contains the single word hello

### Authorized Surface
- Files allowed to change:
  - hello.txt

### Explicit Non-Goals
- Nothing else.

### Risk Flags
- Risky surfaces touched: none
- Approval needed before implementation: no
- Difficulty: easy

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
export PM_LEDGER_DIR=$PWD/../trial-ledger   # keep the trial out of the real ledger
export PM_TMUX_SOCKET=pm-trial   # a private tmux server, so nothing here can touch a real run
python3 $PM start-slice
python3 $PM observe --wait 30
python3 $PM finalize                       # expect: seven PASS lines
cat > ../developer-judgment.json <<'JUDGE'
{"schema_version": 1, "slice": "Slice 1", "origin_event_index": 1, "score": 2, "criteria_met": 1,
 "defects": {"P0": 0, "P1": 0, "P2": 0, "P3": 0},
 "reason": "Trial slice: hello.txt matches the contract and the floor passed 7/7."}
JUDGE
python3 $PM judge-developer --file ../developer-judgment.json   # origin_event_index: the launch event status names
python3 $PM finalize --accept "Trial slice: diff creates hello.txt exactly per contract; validation output shows the expected content; floor 7/7; pane tail clear, no dialog or usage message."
python3 $PM status --report                # then read .pm/runs/<id>/run-report.md
```

You should see the floor pass 7/7, the acceptance land with an assessment, a run report you can read end-to-end, and the run's ledger file under `../trial-ledger/<host>/`. End the trial with `python3 $PM stop --reason cleanup`, which kills only this run's own sessions (it matches the run's id, never the `pm-` prefix), and then `tmux -L pm-trial kill-server` to remove the private tmux server.

## Maintainer map

`scripts/pm.py` (entry) → `pm_lib/`: `cli` (parsing/dispatch) · `plan` (parser, lint, risk derivation) · `state` (lite-2 authenticated state, events, report) · `git_ops` (facts + surface matching) · `floor` (the seven facts) · `sessions` (all tmux contact + interactive-dialog markers) · `profiles` (harness table) · `slice_ops` (command orchestration) · `review` (PM-commissioned reviewers) · `judgments` (signed Developer/reviewer ratings and panel comparisons) · `code_metrics` (line and complexity deltas stored at accept, via code-health) · `ledger` (per-window rows and the per-run ledger file) · `leaderboard` (reads every per-run file into the markdown leaderboard, and retags a finished run's files) · `prompts` (template rendering). Tests in `tests/` use fake harnesses via `--harness-command`; tmux-dependent tests skip when tmux is absent. `pm_test_helpers` owns the shared fixtures — `PlanTestCase` (plain temp directory), `PmTestCase` (adds a git repo), `TmuxRunTestCase` (adds the session reaper and `init`/wait helpers), and the fake-harness builders — and pins `PM_TMUX_SOCKET` and `PM_LEDGER_DIR` so each test process drives its own tmux server and writes its own ledger. Modules are independent and safe to run in parallel.
