# Project Manager — Operator Guide

In Mode B the Project Manager runs a frozen implementation plan autonomously under a supervising PM agent: one fresh Developer session per slice, a mechanical floor of seven non-waivable checks, a recorded PM assessment for every decided slice, independent reviews commissioned by PM where risk warrants, and a durable audit trail. The PM's operating contract is [SKILL.md](SKILL.md); this file covers the toolkit, layout, privacy, how an owner starts a campaign (Mode C), and two model-free setup checks.

## Launcher

To start a Mode B run, paste the Mode B launcher in `implementation-plan`'s SKILL.md → "Next Chat Prompt Format" — its only copy, beside the Mode A launcher — into a fresh PM-capable session. Plans written with that skill end with it already filled in.

## Campaign mode (Mode C)

A Campaign Manager is a PM seat in campaign mode (Mode C): it turns an owner-approved `brief.md` into a validated outcome across many small node plans, each supervised by an ordinary child PM in its own worktree. Its operating contract and the campaign launcher, its only copy, are in [references/campaign-mode.md](references/campaign-mode.md); the dossier it keeps is templated in [references/dossier-template.md](references/dossier-template.md). The toolkit touchpoints:

- `--policy FILE` on `check-plan` and `init`: a protected-paths policy file. A plan surface that reaches one of its entries is refused at plan time and at `init`, and `grant` refuses a path under one; floor fact 5 refuses acceptance of a slice that changes one, which is mechanical but sees Git-visible files only. A plain entry naming a directory covers everything below it. Commits a child inherits from a stopped head, and the Campaign Manager's own merges, are outside fact 5, so they are the Campaign Manager's judgement.
- `campaign-preflight` (see [CLI](#cli)): the checks a Campaign Manager makes first at every reconcile and an owner makes before the first launch, from the brief's pins and the product repository, with no run token.
- A private tmux socket per child (`--socket`, default `pm-child-<b>-<hex>` with `<b>` cut to 32 characters), shared by the child PM and every Developer it launches, so `stop-child` ends the child and its Developer sessions together and a human can `tmux -L <socket> attach` to watch both.
- The question channel: a child PM writes `.pm/campaign/cm-question.md`, `wait-child` returns on it, and `answer-child` answers it, as `observe` and `answer` do for a Developer's `question.md`.
- An optional OS sandbox (`--sandbox FILE` on `launch-child`, and on `init` for Mode B; macOS, `srt`): the child's tmux server, and everything it starts, runs under the owner's pinned `srt` policy. [references/sandbox.md](references/sandbox.md) is its operator's guide and contract.

### Start a campaign

The owner's steps; everything after the paste is the Campaign Manager's, as [references/campaign-mode.md](references/campaign-mode.md) says.

1. Commit a protected-paths policy file in the product repository (`{"schema": 1, "protected_paths": [...]}`; a plain entry naming a directory covers its subtree) and record its SHA-256 (`shasum -a 256 <path>`) for the brief.
2. Create the dossier: an empty git repository outside the product repository holding `brief.md`, `work.md`, `evidence.md` and `decisions.md` from [references/dossier-template.md](references/dossier-template.md), committed. When node worktrees need git-ignored scaffolding (data links, a virtualenv link), write it as `setup.sh` there too and pin its SHA-256 in the brief's *Worktree setup* section.
3. Create the integration branch and its checkout at the campaign base the brief pins: `git -C <product> worktree add -b <integration branch> <checkout> <base>`.
4. Trust the node worktree parent directory the brief names in every harness the campaign uses, so a new worktree launches without a trust dialog.
5. To sandbox the children, write the `srt` base policy as [references/sandbox.md](references/sandbox.md) *Producing your policy* says, commit it in the dossier as `sandbox.json`, and pin its SHA-256 in the brief's `## Sandbox` section; otherwise write `- none` there. `campaign-preflight` checks the pin, and `launch-child` refuses an unsandboxed launch while it is in place (both mechanical toolkit checks).
6. Install `implementation-plan` and `orchestrator` beside `project-manager` in the Campaign Manager's harness: it writes node plans with the first and runs task nodes through the second.
7. Run [Verify campaign mode](#verify-campaign-mode-no-real-model-2-minutes) once on this machine.
8. Run `campaign-preflight` (see [CLI](#cli)) with the dossier, the product repository, the integration branch and checkout, and the child PM's and each child seat's harness; fix every `FAIL` it names and commit the dossier. The Campaign Manager runs the same command first at every reconcile.
9. Approve the brief and take its digest with `shasum -a 256 brief.md`. The Campaign Manager needs that digest at every start and resume, and stops on a mismatch.
10. Fill the campaign launcher from `references/campaign-mode.md` and paste it into a fresh PM-capable session. Resume the same way with the same digest: the Campaign Manager reconciles from the dossier.

## Requirements

- Python ≥ 3.13 (`PurePosixPath.full_match` drives authorized-surface matching; `pm.py` refuses older interpreters)
- `git` 2.31 or newer (`init` records the repository name with `git rev-parse --path-format=absolute`), `tmux`
- At least one supported coding CLI for the Developer seat: `codex`, `claude`, `copilot`, `opencode`, or `qwen` (or any command via `--harness-command`)
- Optionally a reviewer CLI (`codex`, `claude`, `copilot`, `opencode`, `qwen`) for PM-commissioned reviews
- A repository without submodules: repositories with submodules (gitlinks) are not supported, and every host-side `status` refuses them (toolkit check), because `git status` executes a submodule's configuration on the host; see [references/sandbox.md](references/sandbox.md)
- Optionally, for a sandboxed run (`init --sandbox`, `launch-child --sandbox`): macOS, Anthropic's `srt` (`npm install -g @anthropic-ai/sandbox-runtime`) on `PATH`, Node 22.12 or newer to run it, and ripgrep, which `srt`'s README lists for macOS; see [references/sandbox.md](references/sandbox.md)

All five supported harnesses are equally eligible for either seat; the operator chooses what fits the plan. Profiles encode factual CLI differences only, and the two seats differ because they launch different commands. OpenCode and Qwen expose no interactive effort override, so `--effort` fails closed for the Developer seat rather than being silently ignored. Reviews run one-shot, where `opencode run` does accept effort as `--variant` — so `review --effort` works for OpenCode but still fails closed for Qwen. OpenCode does not validate the variant name itself — an unknown one runs at the model's default — so `review` verifies it against the model's inventory first, refusing an unsupported variant, an unnamed model, or an unreadable inventory.

Every **Developer** seat launches at its harness's fullest autonomy (codex `--dangerously-bypass-approvals-and-sandbox`, claude `--permission-mode bypassPermissions`, copilot `--allow-all`, opencode `--auto`, qwen `--yolo`). An unattended Developer has nobody to answer a permission prompt, so a prompt is a silent stall, not a safety feature. Reviewer seats are the deliberate exception: a reviewer emits a report rather than edits, so in an unsandboxed run codex, claude, and opencode reviewers keep their read-only modes. Inside a sandbox a harness's own sandbox cannot start, so `review` turns it off (Codex at `danger-full-access`, Claude Code's sandbox disabled); there a Mode B reviewer is kept read-only by the OS reviewer policy, and a Mode C reviewer is as the Mode C reviewer caveat in [references/sandbox.md](references/sandbox.md) says (see its *What enforces what* table).

**Run PM inside real process and filesystem isolation** — a container or VM, or, on macOS, the optional sandbox around the run's tmux server ([references/sandbox.md](references/sandbox.md)), which confines what the processes the run launches can write and reach. The harnesses no longer constrain what a Developer can touch outside the repository, and the floor cannot see it either (see *Trust model*), so a separate checkout is not containment: nothing stops a Developer reaching the rest of the host.

## CLI

All commands: `python3 skills/project-manager/scripts/pm.py <command> …`, run from inside the target repository (except `check-plan`/`init` and the four campaign commands `launch-child`/`wait-child`/`answer-child`/`stop-child`, which take paths). Mutating commands need the run capability token (`--token` or `PM_RUN_TOKEN` in your environment).

| Command | Purpose |
|---|---|
| `check-plan --plan P [--repo R] [--policy FILE]` | "Is this plan runnable?" — errors fail closed (and end the output, so no summary follows an `ERROR:` line); otherwise it prints each slice's derived risk and why (`Slice 1: risk elevated (Risky surfaces touched: 'none. the slice adds one report')`, the value as normalised and cut at 60 characters, which shows that prose after `none` reads as touched); also runs automatically at init. `--policy` names a protected-paths policy file (see [run-state.md](references/run-state.md)): an authorized entry reaching a protected path is an error. `--repo` must be a git repository: it is resolved to the repository toplevel with `git rev-parse`, as `init` does, so a subdirectory works; `--policy` without `--repo` prints `WARNING: --policy without --repo: the policy file is not checked for self-protection` (a warning never changes the exit code; plan defects still exit 2) |
| `init --repo R --plan P --harness H [--model M] [--effort E] [--model-tag TEXT] [--run-tag TEXT] [--branch B \| --create-branch B] [--attest "Slice 1,…"] [--max-attempts N] [--reviewer-tools T,…] [--reviewer-model M] [--reviewer-effort E] [--harness-command CMD] [--policy FILE] [--sandbox FILE]` | set up the run; freezes the plan digest; prints the token once (refuses main/master by implicit default — pass `--branch`/`--create-branch`). Pass full model ids (for example `claude-opus-5-5`), never aliases: the ledger groups exact strings and an alias's meaning drifts. `--model-tag` is the run's default Developer tag and `--run-tag` labels the whole run (see [Ledger](#ledger)); a tag is trimmed, and a blank or multi-line tag, one containing `=`, or the reserved `untagged` is refused. `--policy FILE` freezes a protected-paths policy file's entries into run state: `init` refuses a plan whose authorized surface reaches one (as `check-plan --policy` does), `grant` refuses one, and floor fact 5 fails on a change to one; the file is never re-read. `--sandbox FILE` (macOS, `srt` on `PATH`) makes the run sandboxed: it validates the `srt` base policy and derives the run's policies, refusing one that defeats the boundary before any run directory or branch exists (`--create-branch` leaves none), records it in `run.json` with its SHA-256 and the run's socket `pm-run-<run id>`, writes the derived policies and record to the run's control directory, and prints `sandbox: control dir <path>` before the token line and `export PM_TMUX_SOCKET=pm-run-<run id>` after it; every command that drives the run's tmux server refuses until that export is in place ([references/sandbox.md](references/sandbox.md) *Mode B: a sandboxed run* lists them), `start-slice` runs the Developers on that server under `srt`, `review` wraps each reviewer in its own `srt`, and the run end tears the server down. See [references/sandbox.md](references/sandbox.md) for the refusals, the derived policies and the teardown. |
| `status [--report] [--run ID]` | where are we? prints the run's `recorded event span` (last recorded activity, not wall-clock now — `status` logs no event); `--report` regenerates `run-report.md`, whose header carries total run time with its endpoints |
| `approve --slice ID --reason TEXT` | record a **human** approval for a plan-gated slice |
| `grant --slice ID --path PATH --evidence TEXT [--run ID] [--token T]` | extend the in-flight slice's effective authorized surface by one exact file path on recorded evidence; ratchets the slice to elevated **and stales every review already recorded for it**, so both mandatory reviews must be re-commissioned after the last grant. Refused, not warned, on a directory, glob, or dependency/lockfile/license/whole-repo-shaped path — isolate those into their own approval-gated slice |
| `start-slice [--model M] [--effort E] [--model-tag TEXT] [--risk elevated] [--reviewer-tools T,…] [--harness-command CMD]` | launch (or relaunch) the next eligible slice in a fresh tmux session (`--model-tag` overrides `init`'s for this launch only; a later launch without it falls back to the run default); a relaunch of a live slice, including resume after `stop`, is refused by the judgment gate until the current submission is judged (see [SKILL.md](SKILL.md#developer-and-reviewer-judgments)). Pass full model ids (for example `claude-opus-5-5`), never aliases: the ledger groups exact strings and an alias's meaning drifts. |
| `observe [--wait N]` | evidence: liveness, pane tail, result presence, and any interactive-dialog marker on the **visible** pane; a wait returns early only on session death, `result.json` appearing, `question.md` (the Developer's open question) appearing, or such a marker (never a mere pane change), and reports elapsed wait time. An open question's text is printed between `--- question ---` and `--- end question ---` lines, each line prefixed with `\|` and a space and stripped of C0 control characters other than tab, capped at 40 lines and 4096 bytes with a truncation marker line. The marker is a signal for PM to weigh, not a verdict — see [SKILL.md](SKILL.md) *Reading the pane* |
| `send --text T --reason R` | one-line nudge into the live session (refused while an interactive dialog marker is on the visible pane, so a blind Enter cannot answer a real dialog; a pre-typing refusal may be re-issued once ordinary output clears, but `TypedNotSubmitted` means the line is already in the input and must not be re-issued). Costs nothing |
| `answer --text TEXT [--run ID] [--token T]` | answer the Developer's open question (`question.md` in the slice artifact directory, which `observe --wait` returns on): writes `answer-<n>.md` with the text verbatim, archives the question as `question-<n>.md`, and sends a one-line pointer to the answer into the live session behind `send`'s dialog guard. Costs nothing and records an `answer` event with the full text. Refused when no question is open, once `result.json` exists (use `finalize --steer`), with no live session, on blank text, and after the attempt budget is exhausted. On `TypedNotSubmitted` both files stay in place and the `answer` event is still appended, marked `delivered: false`; on any other send failure the question is restored (never over a fresh `question.md`), the answer file is removed, and any cleanup leftover is named in the error |
| `finalize` | run the seven-fact floor, collect evidence, and print the captured pane tail (decides nothing) |
| `finalize --accept "reasoning" \| --steer "correction" \| --stop "reason" --cause plan\|developer\|environment [--risk elevated]` | PM's recorded decision; accept requires a passing floor (+ both fresh reviews when elevated) and prints the pane tail, since the acceptance reasoning must state what it showed; steer costs an attempt. All three are refused by the judgment gate, naming every gap, until the current submission has a current Developer judgment, a rating for every intact review of it, and an order for every code panel (see [SKILL.md](SKILL.md#developer-and-reviewer-judgments)) |
| `review --slice ID --skill drift-audit\|code-review [--tool T] [--model M] [--effort E] [--model-tag TEXT] [--timeout N] [--adjudicated TEXT …] [--adjudicated-file PATH]` | commission an independent review pinned to `before_head..HEAD` (`--tool` ∈ codex/claude/copilot/opencode/qwen); prints the report path, stderr path, and reviewer process-group id at launch, before waiting. Blocks until the reviewer exits; a timeout, a non-zero exit or an empty (or whitespace-only) report is a failed review, recorded as `review-failed` with its reason and no review, so read the stderr and re-commission; `--timeout` (default 3600s) kills its process group and fails closed, as a backstop against a hang rather than a cadence — raise it for a slow cold local model. Pass full model ids (for example `claude-opus-5-5`), never aliases: the ledger groups exact strings and an alias's meaning drifts. `--model-tag` labels this commission's Reviewer configuration in the ledger. `--adjudicated` (repeatable) names rulings PM already settled so a reviewer stops re-raising them — it bounds attention only, still gets a full dissenting finding from a reviewer that disagrees, and the rendered prompt is persisted for audit. `--adjudicated-file PATH` reads the same kind of ruling one per line (blank/`#` lines skipped) and combines with any `--adjudicated` flags, file lines first — reuse one file byte-for-byte across a panel's commissions instead of retyping a ruling per call, since `judge-reviews`' panel comparison refuses a ranking the moment two commissions' adjudication text differs by even a word |
| `judge-reviews --file INPUT [--run ID] [--token T]` | record PM's 0–2 rating for a drift or code report, or a separate code-panel order/ties in signed state; see [references/review-judgments.md](references/review-judgments.md) |
| `judge-developer --file INPUT [--run ID] [--token T]` | record PM's 0–2 rating for the current Developer submission before its decision, retaining submission identity and evidence context; corrections explicitly supersede earlier judgments |
| `notes --append TEXT \| --set TEXT [--run ID]` | update the run's curated `notes.md` — writes the state-dir original then re-mirrors; never hand-edit the `.pm/` mirror |
| `stop --reason R [--slice-status stopped] [--scavenge]` | end the run preserving evidence; `--scavenge` sweeps sessions even with state destroyed, but with no run id and state that cannot be read it kills every `pm-*` session on the tmux server, including a live Developer's, so it is for a run whose state is gone, never for cleanup inside another run |
| `launch-child --repo R --base REF --branch B --worktree PATH --prompt-file FILE --harness H [--model M] [--effort E] [--socket NAME] [--harness-command CMD] [--setup FILE] [--sandbox FILE]` | campaign mode: create node branch `B` at the commit `REF` names as a new linked worktree at `PATH`, copy the prompt file to `PATH/.pm/campaign/launcher.md`, run `--setup` when given (`sh FILE` inside the new worktree, in its own process group and without `PM_RUN_TOKEN`, output kept in `PATH/.pm/campaign/setup.log`; a non-zero exit, a ten-minute hang, which kills everything still in the script's process group, or a worktree left dirty outside `.pm/` fails the launch before any session exists, naming the branch and worktree left behind), and launch a child PM there in a tmux session named `campaign-<b>-<hex>` on its own private socket (`--socket`, default `pm-child-<b>-<hex>`, where `<b>` is the branch with every character outside `[A-Za-z0-9_-]` replaced by `-` and cut to 32 characters), then inject a one-line pointer to the launcher, and print `watch: tmux -L <socket> attach -t <session>` (a bare `attach` lands on the most recent session, usually a Developer). It appends the root-anchored line `/HANDOFF.md` to the worktree's git exclude file (`git rev-parse --git-path info/exclude`, which in a linked worktree is the product repository's `info/exclude`, so the line persists after the campaign) when neither `HANDOFF.md` nor `/HANDOFF.md` is already listed, then verifies with `git check-ignore` that the root HANDOFF.md is ignored, so the child PM's HANDOFF.md cannot fail floor fact 7; when an ignore rule overrides the exclusion (a committed `!HANDOFF.md`, say) the launch fails at setup. The child's environment carries `PM_TMUX_SOCKET` (so the Developers it launches share that server), `PM_CM_QUESTION_PATH` and `PM_CHILD_RESULT_PATH`, never `PM_RUN_TOKEN`; as soon as the session exists its tmux server is marked with `PM_CHILD_WORKTREE=<worktree>` in its global environment (the ownership mark `stop-child` verifies) and the manifest `PATH/.pm/campaign/child.json` is written. Refused before anything is created on an unresolvable repo or base, a base commit that tracks HANDOFF.md (a child PM's handoff must be untracked), an existing branch or path, an unreadable or blank prompt file, an unsupported harness without `--harness-command`, a blank `--socket`, one containing whitespace or `/`, one longer than 64 characters, or `default` (the operator's own unscoped server), `tmux` or the harness executable not on `PATH`, a command that does not compose, or a socket (explicit or default) whose tmux server already hosts a session or cannot be listed (any tmux error other than no server on that socket), since `stop-child` kills the whole server. A later failure names the branch and worktree left behind (remove them by hand with `git worktree remove` and `git branch -D`), and once the session has started it is best effort: it captures the child's pane to `.pm/campaign/pane.txt` when it can, kills the server and reports whether the kill was confirmed, and keeps any `child.json` already written. With `--sandbox FILE` (macOS, `srt` on `PATH`), after `--setup` it starts the child's tmux server under `srt` with the policy derived from that base policy, verifies it with two probes, and creates the child's session through it; the manifest gains a `sandbox` block, policy warnings print as `WARNING:` lines, and the `watch:` line becomes the attach-policy form ([references/sandbox.md](references/sandbox.md) *The boundary*), the only safe way to attach to a sandboxed child (operator discipline). When the prompt file's dossier brief pins a policy in `## Sandbox`, `--sandbox` is required and must match the pin's SHA-256; the refusals and the boundary are in [references/sandbox.md](references/sandbox.md). Takes no run and no token |
| `campaign-preflight --dossier PATH --repo PATH --integration-branch NAME [--worktree PATH] [--digest HEX] [--harness NAME …]` | campaign mode: the mechanical half of reconcile, with no run and no token, for the Campaign Manager at every start and for the owner before the first launch. Checks the dossier (a git repository of its own, outside the product, with `brief.md` and `setup.sh` committed and unchanged; `brief.md` with no `<placeholder>` left outside campaign vocabulary and matching `--digest`), the product repository (the integration branch exists, descends from the brief's pinned campaign base and tracks no HANDOFF.md at its head; the integration checkout, when given, belongs to the repository, sits at that head and is clean), the brief's pins (the committed policy file's SHA-256, hashed from the blob's bytes; `setup.sh` present, non-blank and matching its SHA-256, then run in a throwaway detached worktree of the integration head with the code `launch-child --setup` uses, which must leave the tree clean, and only after every integrity check passed), the node worktree parent (absolute, present, writable), the brief's `## Sandbox` section (`ok sandbox: none` when it is absent or `none`; otherwise `sandbox.json` tracked and unchanged, matching its pin, a valid base policy with each warning a `note`, and `srt <version> on PATH`), and `tmux` and each `--harness` on PATH. Prints one `ok`, `FAIL` (with what to do) or `note` line per check and exits 1 on any failure; harness trust is a note, since only a human can see the dialog |
| `wait-child --worktree PATH [--wait N]` | campaign mode: evidence on the child PM, never a decision: liveness, `child-result.json` (with its `status`), an open `cm-question.md` (printed between `--- question ---` and `--- end question ---` lines, each line prefixed with `\|` and a space and stripped of C0 control characters other than tab, capped at 40 lines and 4096 bytes with a truncation marker line), any dialog marker on the visible pane, one `child run <id>: status=… stop_reason=… (unverified: tokenless read)` line per `run.json` under the worktree's state root (`status=unreadable` for a bad file), and the 40-line tail of the visible pane, which each poll writes to `.pm/campaign/pane-live.txt`. A wait polls every 2 seconds and returns early only on session death, the result file, the question file, or a dialog marker, never on a pane change. Exits 0 once the manifest loads |
| `answer-child --worktree PATH --text TEXT` | campaign mode: answer the child PM's open `cm-question.md`, as `answer` does for a Developer: writes `cm-answer-<n>.md` verbatim, archives the question as `cm-question-<n>.md`, and sends the one-line answer pointer behind `send`'s dialog guard. Refused with no live child session, no open question, `child-result.json` present, or blank text; on `TypedNotSubmitted` both files stay in place, and on any other send failure the question is restored (never over a fresh `cm-question.md`), the answer file is removed and any cleanup leftover is named in the error |
| `stop-child --worktree PATH --reason TEXT` | campaign mode: capture the child's full pane to `.pm/campaign/pane.txt`, print any reviewer process groups its `run.json` files record (read without a token; they run outside the child's tmux server, so verify and kill them by hand — `stop-child` never signals them), then kill the child's tmux server, which ends the child and every Developer session on it, and print the reason for the CM to record. The manifest is child-writable, so ownership is checked against the server itself: `launch-child` marks the child's server with `PM_CHILD_WORKTREE=<worktree>`, and a live server without this worktree's mark (one launched for another worktree, such as a later child's on a reused socket) is refused (mechanical; the mark guards against stale manifests and reused sockets, not a hostile child, since any process on that server can rewrite it). The mark belongs to the server, so it outlives the child's own session and a dead child's orphaned `pm-*` Developers can still be cleaned up. It refuses (exit 2) and kills nothing on a mark mismatch, when the manifest's socket is `default`, or when the server hosts any session not named `campaign-*` or `pm-*` (a bystander, such as an operator's own session). It prints `killed tmux server <socket>` only when a re-probe confirms no server remains (and then removes the server's socket file, best effort, since tmux itself never does), otherwise `tmux server <socket> still has sessions: …` and exits 2. Safe to re-run: with the session already gone, an earlier `pane.txt` is kept. For a sandboxed child it also tears the sandbox down and exits 2 when it cannot; what it removes and prints is in [references/campaign-mode.md](references/campaign-mode.md) *Sandbox*, and the exit conditions and the retry rules are in [references/sandbox.md](references/sandbox.md) *Teardown* |
| `ledger render [--out PATH] [--run-tag TEXT …]` | render every per-run ledger file under the ledger directory (every `<host>/*.json`) into one markdown leaderboard, by default `leaderboard.md` in the ledger directory; `--run-tag` (repeatable) keeps only runs carrying one of the tags; needs no run and no token; exits 2 when there are no files, or no run carries any of the given tags, naming the directory searched |
| `ledger tag RUN_ID [--run-tag TEXT] [--model-tag OLD=NEW …]` | retag a finished run's per-run ledger files in place, every host copy at once: `--run-tag` sets the run tag, and each `--model-tag` (repeatable; all renames apply together, so a swap works) renames a model tag on every Developer and review, `untagged` as OLD selecting those with no tag; needs at least one flag, no run and no token; refuses before writing anything on an unknown run, an OLD that matches nothing, a malformed form or a host folder it cannot list, and restores the copies already rewritten if a write fails. See [references/model-ledger.md](references/model-ledger.md) |

The attempt budget defaults to 10 per slice — the initial launch plus ten steers or relaunches. Lower it at `init` (`--max-attempts N`) for a strong Developer model where you want autonomy measured tightly; a slice that needs more than 10 rounds is itself the finding, and the human should see it rather than have more budget granted. Weak or unproven Developer models fail by exhausting the budget, not by shipping bad code. `status` and `finalize` print attempts against the ceiling so the PM can pace steering decisions without reading `run.json`.

Exit codes: 0 success; 1 = a `finalize` refusal — a floor fact failed, or `--accept` was refused for another recorded reason (e.g. a missing or stale mandatory review on an elevated slice); 2 = error/refusal, or the judgment gate (integrity failures are prefixed `INTEGRITY:` and are terminal — start a new run).

If a harness displays a directory-trust or permission prompt, the PM stops and leaves that approval to the human. The human may configure trust through the harness's own supported mechanism, then rerun `start-slice`; the PM must not acknowledge the dialog with `tmux send-keys` or change user-global harness configuration itself. Autonomy flags do not clear a folder-trust dialog — Copilot still asks on a folder it has not been told to remember — so that one stays a human decision.

## Optional: the poll guard (Claude Code PM seat)

`hooks/pm-poll-guard.py` is an optional cost guard for a PM seat running in Claude Code. A PM session backgrounds long commands — a Developer wait, a commissioned review, a test run — then goes looking to see whether they finished. Claude Code already re-invokes the agent when a background command exits, so the looking is redundant, and each look costs a full model round-trip that resends the entire conversation. These are harness calls, not PM commands, so the toolkit itself cannot see or bound them; the guard has to live in the harness.

It denies two shapes, one per tool. Both require `.pm/` in the session's working directory, so nothing outside a PM run is affected, and it fails open on any unexpected input or error — it bounds spend, and can never strand a run.

**`Read`** — a re-read byte-identical to the file's previous read: provably no new information. The first read of a file, and any read returning new content, still pass. The path must match Claude Code's scratchpad task-output layout (`.../claude-<uid>/<project>/<session-uuid>/tasks/<id>.output`), which a repository's own `tasks/` directory cannot.

**`Bash`** — a **backgrounded** command that waits and then inspects a PM artifact. Backgrounding it is itself the waste: it schedules a second completion notification for a target that already has one coming. The forms it refuses are hand-rolled waiters:

```sh
until [ -s .../tasks/<id>.output ]; do sleep 30; done; ...
until grep -qi "^## Verdict" .git/pm/<run-id>/slices/slice-002/review-4-drift-audit-opencode.md; do sleep 60; done
until [ -f .pm/runs/<run-id>/slices/slice-003/result.json ]; do sleep 60; done
```

The rule keys on the wait and the target, never on the inspector — the inspectors vary (`grep`, `cat`, `head`, `git log`, `test`, `tail`, `ls`), so an allowlist of "reading" commands would miss most of them. Commands invoking `pm.py` are exempt: `observe --wait` and `review` are the toolkit's own legitimate waiters, and `review` launches the very reviewer it waits on.

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

The one scan that remains guards PM's own keystrokes, and is not a verdict on the run. `send_prompt`/`send_line` send text, then Enter, then a second Enter after a settle — and if the first Enter surfaces a trust or credential dialog, the second would answer it. That window is sub-second and inside one call, so no PM turn can interpose and it has to be code. It matches literal dialog strings on the **visible** pane only, names what it matched, and is not overridable: a marker literal cannot distinguish a log line from a real dialog, so no comparison is allowed to authorize a keystroke. A failed safety capture also refuses the Enter instead of reading as a clear pane. If a false match happens before typing, visible-pane scoping lets ordinary output scroll away and PM re-issue; after `TypedNotSubmitted`, the text is already in the input and the safe routes are relaunch, stop, or human cleanup — never a blind retry.

### What the toolkit does instead of guarding

Wait *length* is the PM's judgement, not the hook's business, so the toolkit advises rather than refuses: when `observe --wait` elapses with no session-death, result, `question.md` (the Developer's open question), or dialog-marker signal, it prints a note suggesting a longer wait or an action instead. It never changes the exit code. The wasteful pattern is repeating a too-short wait rather than re-polling an answered question, so the note nudges toward waiting once, longer.

## Layout: who owns what

- **`<git-dir>/pm/<run-id>/`** — authoritative state (`run.json`, HMAC-authenticated) and every PM-authored original (assessments, reviews, notes, report — plain files, protected by living outside the worktree, not by the MAC). See [references/run-state.md](references/run-state.md).
- **`<repo>/.pm/runs/<run-id>/`** — the human-facing mirror of PM artifacts plus Developer-authored evidence (`result.json`, `validation.md`, pane captures, diffs, prompts). Self-ignoring via `.pm/.gitignore`; the host's reads and writes there are guarded against planted links ([references/run-state.md](references/run-state.md)). The boundary, precisely: PM's records and decisions live in the controller originals and are never read back from this mirror — but Developer-authored evidence here (`result.json`, `validation.md`) *is* input to the floor and to PM's assessment. Vandalizing it damages the Developer's own case and fails the slice closed (floor fact 4); it can never forge an acceptance or alter PM state.
- Per slice: `prompt.md` (the rendered authorization), `steer-attempt-<n>.md` (each `finalize --steer` correction, verbatim — PM injects only a one-line pointer to it, so a long or multi-line correction cannot be truncated or split by a harness TUI), `pane-live.txt`/`pane.txt`, `status-before/after.txt`, `diff.patch`, `validation.md`, `result.json`, `question.md` (the Developer's open question, which rotates into `attempt-<n>/` with the attempt that asked it), `question-<n>.md` and `answer-<n>.md` (each answered question and `pm answer`'s verbatim answer, numbered across the slice), `attempt-<n>/` for superseded launches, `assessment.md` + `review-*.md` mirrors, and `review-*-prompt.md` — the exact prompt each reviewer was commissioned with, so any PM adjudication that narrowed a review is visible to the human rather than inferable only from what the report omits.

## Ledger

Each run writes one ledger file, `ledger/<host>/<run-id>.json` beside this README or under `PM_LEDGER_DIR` (an absolute path outside the supervised repository; the test helpers and the walkthrough below always set it, so trial runs never reach the real ledger). The file is derived from signed state after every command that carries the run token, is never edited by hand except through `ledger tag`, and a failed write is loud but never blocks a command. `ledger render` turns every run's file, from every host, into one leaderboard. How the models are scored, what the columns mean, tags, sharing a ledger between machines and the common situations are in [references/model-ledger.md](references/model-ledger.md); the file shape is in [references/run-state.md](references/run-state.md).

## Trust model, honestly

Mechanical and non-waivable: the seven floor facts (frozen plan digest; repo/branch identity; recorded approvals; result presence/identity; changed files ⊆ effective surface (frozen plan surface + recorded grants), and no change to a frozen protected path; commit ancestry and branch head; clean worktree). Every one is a property of repository state. Everything semantic — is the change good, is the evidence sufficient, does what the pane shows mean this run must stop — is the PM agent's recorded judgement; read the assessments.

Known limits, inherited and stated: the floor sees final Git-visible worktree state only (ignored files, Git hooks/metadata, write-then-revert effects, and anything a Developer writes outside the repository escape it — containment is your sandbox around the run, not the harness); dependency/license/side-effect stops rest on the Developer contract's explicit prohibition (`references/developer-prompt.md`), plan-level surface exclusion, and the operator's sandbox, with PM's pane read catching only a harness that actually prompts — and Developer seats launch at full autonomy, so often none does; role authority is capability-token-raised, not OS-enforced — Developer sessions launch with `PM_RUN_TOKEN` unset and a tmux server PM starts never carries it in its global environment, but a same-user process that steals the token or subverts the PM agent is outside the threat model; `attested` slices are operator narration; PM-seat quality is load-bearing — a weak model in the PM seat weakens the judgement layer itself.

With a sandbox (`init --sandbox`, `launch-child --sandbox`), the containment line moves from "your sandbox around the run" to the OS boundary [references/sandbox.md](references/sandbox.md) describes: writes outside the effective policy's grants (the derived and operator-granted paths, plus `srt`'s implicit `~/.npm/_logs` and `~/.claude/debug`), reads of the operator's `denyRead` paths, and egress to unlisted hosts are refused by the kernel and `srt`'s proxy for every process the run's tmux server starts. What stays outside it: reads not on the operator's `denyRead` list, which stay allowed; what a harness sends to its model provider; the host-side PM, Campaign Manager, setup script and task delegates, which run unsandboxed; and whether the operator's grants are sensible. That file's enforcement table and limitations name the layer for each, and the rest of what stays outside.

## Privacy & sensitive artifacts

Everything stays local; the toolkit phones nowhere. But captured artifacts can still contain secrets your repo or shell exposed:

| Artifact | May contain |
|---|---|
| `pane*.txt` | anything printed in-session: code, env values, echoed secrets |
| harness-side transcripts (e.g. Claude Code's own session files — the toolkit passes `--session-id` but does not copy them into `.pm/`) | full session content, stored under the harness's home directory |
| `diff.patch`, `review-*.md`, `review-*-prompt.md` | repository code, including sensitive files inside the surface; the prompt copies also embed the plan's slice contract and the review skill's bundle |
| `validation.md`, `result.json` | command output the Developer chose to record |
| `prompt.md`, `steer-attempt-<n>.md` | the plan's slice contract, and whatever PM wrote into a correction |
| `.pm/campaign/pane*.txt`, `.pm/campaign/launcher.md` (in a campaign child's worktree) | the child PM's pane, which shows its own `PM_RUN_TOKEN=` line from its `init` (authorizing only that child's run state), and whatever the Campaign Manager wrote into the launcher |
| `validation/<node>/` in a campaign dossier (copies of a child's pane, questions, answers and `child-result.json`) | the same content as the originals; the pane shows the child's `PM_RUN_TOKEN=` line, a low exposure because that run's state root is deleted with the worktree |

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

## Verify campaign mode (no real model, ~2 minutes)

A model-free check of the four child commands and a protected instrument; the child PM is a fake. From an empty scratch directory:

```sh
git init -q -b main product && cd product
mkdir instruments && echo 'print("T1")' > instruments/check_t1.py   # any content; the trial never runs it
echo '{"schema": 1, "protected_paths": ["instruments/"]}' > campaign-policy.json
git add -A && git commit -q -m "instrument and policy"
{ echo '## Slice 1: edit the instrument'   # a node plan whose surface reaches the instrument
  for s in "Intended Change" "Explicit Non-Goals" "Validation Plan" "Rollback Path"; do printf '### %s\n- n/a\n' "$s"; done
  printf '### Acceptance Criteria\n- [ ] edited\n### Authorized Surface\n- Files allowed to change:\n  - instruments/check_t1.py\n'
  printf '### Risk Flags\n- Risky surfaces touched: none\n- Approval needed before implementation: no\n- Difficulty: easy\n'; } > ../node-plan.md
cat > ../fake-child.sh <<'FAKE'
echo "fake child PM"; sleep 3; echo "T1 first, or T2? I suggest T1." > "$PM_CM_QUESTION_PATH"
until [ -e "${PM_CM_QUESTION_PATH%/*}/cm-answer-1.md" ]; do sleep 1; done
echo '{"status":"complete","run_id":null,"summary":"fake child done"}' > "$PM_CHILD_RESULT_PATH"
sleep 3600
FAKE
PM=<path-to>/skills/project-manager/scripts/pm.py
python3 $PM check-plan --plan ../node-plan.md --repo . --policy campaign-policy.json   # expect: exit 2, ERROR ... reaches protected path 'instruments/'
launch=$(python3 $PM launch-child --repo . --base main --branch campaign/t/n1 --worktree ../n1 --prompt-file ../node-plan.md --harness fake --harness-command "sh $PWD/../fake-child.sh")
echo "$launch"; SOCK=$(echo "$launch" | grep -o 'socket=[^ ]*' | head -1 | cut -d= -f2)   # the socket, from the launch line (a sandboxed launch prints extra lines); to watch an unsandboxed child, tmux -L $SOCK attach
python3 $PM wait-child --worktree ../n1 --wait 30      # expect: question present: yes
python3 $PM answer-child --worktree ../n1 --text "T1 first."
python3 $PM wait-child --worktree ../n1 --wait 30      # expect: result present: True (status=complete)
python3 $PM stop-child --worktree ../n1 --reason "trial done"
tmux -L $SOCK ls                                       # expect: no server running
```

The policy file protects itself too, so `check-plan` reports `protected paths: 2`. Remove the scratch directory by hand; the node branch and worktree live in its `product` repository.

## Maintainer map

`scripts/pm.py` (entry) → `pm_lib/`: `cli` (parsing/dispatch) · `plan` (parser, lint, risk derivation) · `state` (lite-2 authenticated state, events, report) · `git_ops` (facts + surface matching) · `floor` (the seven facts) · `sessions` (all tmux contact + interactive-dialog markers) · `profiles` (harness table) · `slice_ops` (command orchestration) · `review` (PM-commissioned reviewers) · `judgments` (signed Developer/reviewer ratings and panel comparisons) · `code_metrics` (line and complexity deltas stored at accept, via code-health) · `ledger` (per-window rows and the per-run ledger file) · `leaderboard` (reads every per-run file into the markdown leaderboard, and retags a finished run's files) · `prompts` (template rendering) · `campaign` (child PM launch, watch, answer and stop). Tests in `tests/` use fake harnesses via `--harness-command`; tmux-dependent tests skip when tmux is absent. `pm_test_helpers` owns the shared fixtures — `PlanTestCase` (plain temp directory), `PmTestCase` (adds a git repo), `TmuxRunTestCase` (adds the session reaper and `init`/wait helpers), and the fake-harness builders — and pins `PM_TMUX_SOCKET` and `PM_LEDGER_DIR` so each test process drives its own tmux server and writes its own ledger, and it prepends a directory of stub `codex`, `claude`, `copilot`, `opencode` and `qwen` executables to `PATH` so no test can launch a real coding CLI. Modules are independent and safe to run in parallel.
