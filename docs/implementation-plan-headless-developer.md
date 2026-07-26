# Implementation Plan — PM headless Developer (retire tmux)

## Purpose

Move the `project-manager` (Mode B) **Developer** from a persistent interactive tmux TUI to a **headless, resumable** invocation, still launched and supervised by PM. This is a simplifying refactor: it deletes readiness-banner detection, keystroke injection, and pane screen-scraping, and replaces the "one named tmux session per slice" model with a detached background process whose captured stdout (the *outfile*) is the universal, harness-agnostic progress signal. All five harnesses become first-class by construction, because the outfile and the `result.json` completion signal are identical for every tool.

No backwards compatibility with tmux is retained; there is no dual runtime path once the cutover lands.

## Relationship to the orchestrator-resume plan (connected, not coupled)

This plan is a **sibling** of `docs/implementation-plan-orchestrator-resume-tracking.md`. They share design *philosophy* but by explicit decision **share no code**: PM gets its own thin launcher fitted to its single-serialised-Developer + mechanical-floor + Developer-commits model, rather than importing the orchestrator's multi-job substrate. The orchestrator's delegate contract is role-incompatible with PM's Developer (a delegate *never commits*; a PM Developer *owns the slice commit* — floor fact 6), so PM cannot route its Developer through it. The single deliberately-duplicated artifact is the per-harness *launch + resume command syntax*; PM owns its own copy (frozen in Slice 2 below), matching how `review.py` already re-specs the orchestrator's command table as "behavioural evidence, shares no code, never imports."

The orchestrator-resume plan has been completed (commit 88f0e55..58fe16f).

## The core mapping (tmux → headless)

| tmux concept | headless replacement |
|---|---|
| `tmux new-session` + readiness banner + keystroke injection | detached `Popen(start_new_session=True)`, prompt passed as the `-p`/`exec` argument; no readiness wait, no injection |
| `capture-pane` → `pane.txt`/`pane-live.txt` | tail of the captured **outfile** → `session-output.txt` |
| `has-session` liveness | `os.kill(pid, 0)` + a captured start-time identity (guards PID reuse) |
| pane-diff "active" | outfile mtime/size growth |
| `send-keys` steer into a live pane | **session resume**: a new detached turn (`--resume <id>` / `codex exec resume <id>`), after the prior turn is confirmed dead |
| `kill-session` / `pm-*` prefix sweep | terminate the tracked process group after an identity check; a `developer.pid` sidecar (PID+PGID+identity+run/slice) for state-less scavenge |
| `scan_hard_stop(pane_text)` | `scan_hard_stop(outfile_text)` — logic unchanged |

## Semantic changes (accepted by the owner)

- **Steering is turn-based.** A headless `-p`/`exec` turn runs to completion (writes `result.json`) and exits; PM then *resumes* with a follow-up turn. There is no mid-turn keystroke injection.
- **The free `send` nudge is removed.** There is no live pane to nudge; every follow-up is a resume turn via `finalize --steer`, counted against the attempt budget. The `send` subcommand is deleted.
- **Readiness/banner detection is deleted.** OpenCode's model is validated at launch by `query_model_identity` (inventory), which needs no pane; the pane-based display check is removed with the rest of the TUI path.

## Resume/quiescence invariants (from review)

- **Use a launch-bound session id; block on capture failure.** claude/copilot set the id at launch (bound by construction); codex/opencode/qwen capture it post-launch by correlating the store record to *this* launch (exact stdout id, or a record matched by prompt/cwd/start-time) — never a bare "most recent" query, which even under PM's serial execution could pick up an unrelated same-harness session. If no id can be bound to this launch, `finalize --steer` refuses with a clear error rather than blind-resuming; PM never guesses "the last session."
- **Quiesce before resume.** `result.json` appearing does not prove the harness process exited. Before any resume turn, PM confirms the prior process is dead (terminating and reaping its identity-checked process group if necessary), so a resume can never race a still-flushing or still-acting prior turn.

## Current-state facts this plan relies on (verify during review)

- tmux contact is almost entirely in `sessions.py` (it owns launch/capture/injection/liveness), consumed by `slice_ops.py`; `floor.py` fact 8 takes a pane-text string; `cli.py` prints pane/session strings; `state.py`/`run-state.md` carry `current_slice.tmux_session`. One exception: `slice_ops.py:301`/`slice_ops.py:374` independently refuse `init` when the `tmux` executable is absent — that check must be removed at cutover.
- `review.py` already composes headless one-shot commands for all five harnesses; the reviewer is already a non-tmux detached subprocess. `scan_hard_stop` is pure text parsing and reusable verbatim.
- All five harnesses support headless launch + resume (exact syntax frozen in Slice 2). claude/copilot allow setting the session id at launch; codex/opencode/qwen capture it post-launch.
- Test coupling (approximate, to be confirmed by test collection during implementation): the tmux-coupled tests are concentrated in `test_sessions.py` (the epicentre), with live-session scenarios spread across `test_slice_ops.py` and `test_finalize.py`; `test_floor.py`/`test_state.py`/`test_review.py` are largely unaffected (fact 8 takes a plain string; the reviewer is already headless). Fake harnesses are runtime `#!/bin/sh` scripts; a headless fake is a script that writes `result.json` and exits, and on resume commits/appends.

## Slicing rationale

The tmux→headless cutover cannot be split into separately-green commits at the runtime layer: deleting tmux while callers still use it, or rewiring callers before the headless runner exists, leaves the tree broken between commits. So the plan is **additive first, then one atomic cutover, then cleanup**: Slices 1–2 add the headless runner and composer *alongside* the untouched tmux path (tree stays green, `review.py` safely adopts the shared composer); Slice 3 is the single atomic cutover; Slice 4 deletes the now-dead tmux code; Slices 5–6 finish docs and CI.

## Implementation Profiles

- Recommended for frontier/senior implementer: Batch A (Slices 1–2), then Slice 3, then Slice 4, then Slices 5–6.
- Recommended for standard implementer: run slices individually with validation after each; Slice 3 is the behavioural core.
- Recommended for weaker implementer: atomic slices one at a time, in order.

## Slice Batches

- Batch A: Slices 1–2 — the additive headless runner and the additive composer are independent of the cutover and review well as one diff.

## Slice 1: Add the headless runner to sessions.py (additive)

### Intended Change
- Add headless process-contact functions to `pm_lib/sessions.py` *alongside* the existing tmux functions (which are untouched here): launch a detached background process (`Popen(start_new_session=True)`, stdin `DEVNULL`, stdout+stderr → an outfile in the slice artifact dir), recording pid, pgid, and a captured start-time identity; read the outfile tail; check liveness (`os.kill(pid,0)` + identity match); terminate-and-reap the identity-checked process group; a quiescence helper that confirms a prior process is dead before a resume; and a `developer.pid` sidecar writer/reader carrying PID+PGID+identity+run/slice ownership.
- Keep `scan_hard_stop` (and all regexes) and `session_name` unchanged.
- Do not delete or modify any tmux function, and do not rewire any caller — this slice only adds.

### Acceptance Criteria
- Inputs: a launch command string, a repo, an env map without `PM_RUN_TOKEN`, an artifact dir.
- Outputs: a running detached process writing to a known outfile; pid/pgid/identity recorded; helpers to read the outfile tail, check liveness, confirm death/quiesce, terminate+reap by identity, and read/write the sidecar.
- User-visible behaviour: none yet (functions are unwired); the existing tmux path is unchanged.
- Behaviour that must not change: every existing tmux function; the `PM_RUN_TOKEN`-never-in-env assertion; `scan_hard_stop` results for every existing fixture.

### Authorized Surface
- Files allowed to change:
  - `skills/project-manager/scripts/pm_lib/sessions.py`
  - `skills/project-manager/tests/pm_test_helpers.py`
  - `skills/project-manager/tests/`
- Functions/classes/components allowed to change: new headless-runner functions in `sessions.py` (additive only); the fake-harness helper (add a headless fake that writes `result.json`, exits, and on resume commits/appends).
- Tests allowed or expected to change: new headless-runner tests under `skills/project-manager/tests/` (leaving the existing tmux tests untouched until Slice 4).

### Explicit Non-Goals
- No caller rewiring (Slice 3); no tmux deletion (Slice 4).
- No per-harness command syntax here (Slice 2).
- No import of orchestrator code.

### Risk Flags
- Risky surfaces touched: none
- Approval needed before implementation: no

### Validation Plan
- Tests to add/update: launch a headless fake, read its outfile, detect completion/liveness, confirm quiescence, resume it, terminate+reap by identity, exercise the sidecar; confirm the env-token assertion and `scan_hard_stop` fixtures still pass; confirm the existing tmux tests still pass unchanged.
- Commands to run: `python3 -m pytest skills/project-manager/tests/`
- Manual checks: launch a trivial headless fake and confirm it survives one `pm.py` process exit and is observable by the next.

### Rollback Path
- Revert the slice commit; `sessions.py` loses the additive functions with no caller affected.

## Slice 2: Add the unified resumable headless composer (additive)

### Intended Change
- Add to `pm_lib/profiles.py` a headless command composer serving both seats (`mode=developer|reviewer`) plus a resume-command composer, *alongside* the existing tmux `compose_command` (untouched here). The developer mode composes a read-write, autonomous, resumable launch; session-id-set flags are included for claude/copilot.
- Point `review.py` at the shared composer (`mode=reviewer`), deleting `review.compose_reviewer_command`'s private table — reviewer command shapes are behaviour-preserving.
- **Freeze PM's own copy of the per-harness command syntax** (the seam artifact; must stay factually consistent with the orchestrator plan but shares no code). The developer launch / resume shapes the composer produces and the tests assert:
  - claude — launch `claude -p <pointer> [--model M] [--effort E] --permission-mode acceptEdits --session-id <uuid> --add-dir <repo>`; resume `claude -p <correction> --resume <uuid> --permission-mode acceptEdits --add-dir <repo>`
  - codex — launch `codex exec <pointer> [-m M] [-c model_reasoning_effort="E"] --sandbox workspace-write --skip-git-repo-check -C <repo> [--add-dir <git-dir>]`; resume `codex exec resume <session-id> <correction> --sandbox workspace-write --skip-git-repo-check -C <repo> [--add-dir <git-dir>]` (the resume turn keeps the same commit-time `--add-dir <git-dir>` the launch used when commits are required, so a steered turn in a linked worktree can still commit)
  - copilot — launch `copilot -p <pointer> [--model M] [--effort E] --allow-all-tools --autopilot --session-id <uuid> --add-dir <repo>`; resume `copilot -p <correction> --resume=<id> --allow-all-tools --autopilot --add-dir <repo>`
  - opencode — launch `opencode run <pointer> [-m M] --agent build --auto --dir <repo>`; resume `opencode run <correction> --session <id> --agent build --auto --dir <repo>`
  - qwen — launch `qwen --prompt <pointer> [--model M] --sandbox --output-format text`; resume `qwen --prompt <correction> --resume <id> --sandbox --output-format text`
- Preserve the OpenCode inventory validation via `query_model_identity`.
- **Freeze the `--harness-command` override resume protocol** (needed by Slice 3's steer test and any custom harness): a launch runs the override command with the launch pointer as its final argument and `PM_DEVELOPER_RESUME_SESSION_ID` unset in the env; a resume re-runs the same override command with the correction as its final argument and `PM_DEVELOPER_RESUME_SESSION_ID` set to the captured id, which a custom/fake harness honours to continue its prior session. An override that captured no session id blocks on `finalize --steer`, consistent with the block-on-capture-failure rule.

### Acceptance Criteria
- Inputs: harness name, model, effort, mode (developer/reviewer), optional session id / correction.
- Outputs: the launch and resume commands above for each of the five harnesses; reviewer command shapes identical in behaviour to today's.
- User-visible behaviour: reviewer behaviour unchanged; the Developer launch path is not yet wired (Slice 3).
- Behaviour that must not change: reviewer command shapes asserted by `test_review.py`; the fail-closed effort/model handling for opencode/qwen; the tmux `compose_command` (still present for the pre-cutover Developer path).
- Behaviour to verify during implementation: each developer-mode permission level (`acceptEdits` / `workspace-write` / `--allow-all-tools --autopilot` / `--agent build` / `--sandbox`) is sufficient for the Developer to edit, run its validation, and **git-commit** headlessly without hanging; if a harness hangs awaiting a permission a headless run cannot supply, treat that as a launch-config defect to resolve for that harness, not a reason to broaden the mode blindly.

### Authorized Surface
- Files allowed to change:
  - `skills/project-manager/scripts/pm_lib/profiles.py`
  - `skills/project-manager/scripts/pm_lib/review.py`
  - `skills/project-manager/tests/test_profiles.py`
  - `skills/project-manager/tests/test_review.py`
- Functions/classes/components allowed to change: new headless + resume composer in `profiles.py` (additive; tmux `compose_command` untouched); `review.py` command-building to consume the shared composer.
- Tests allowed or expected to change: `test_profiles.py` (developer + resume composition assertions per the frozen shapes above), `test_review.py` (reviewer via the shared composer).

### Explicit Non-Goals
- No lifecycle rewiring (Slice 3); no tmux composer deletion (Slice 4).
- No behavioural change to reviewer output; only the source of its command table moves.

### Risk Flags
- Risky surfaces touched: none
- Approval needed before implementation: no

### Validation Plan
- Tests to add/update: per-harness developer launch + resume command shapes exactly as frozen above; reviewer shapes preserved through the shared composer; opencode/qwen effort fail-closed retained.
- Commands to run: `python3 -m pytest skills/project-manager/tests/test_profiles.py skills/project-manager/tests/test_review.py`
- Manual checks: composed developer/resume commands for each tool read correctly by eye and match the frozen shapes.

### Rollback Path
- Revert the slice commit; `review.py` regains its private table and `profiles.py` loses the additive composer.

## Slice 3: Cutover — the slice lifecycle runs headless (atomic)

### Intended Change
- Rewire `pm_lib/slice_ops.py` to the headless runner: `start_slice` launches the headless Developer detached (via the Slice 2 composer), captures/records a **launch-bound** session id (set for claude/copilot; correlated to this launch for codex/opencode/qwen, `null` if ownership cannot be established), pid, pgid, identity, and outfile in `current_slice`, and writes the sidecar; drop the readiness-wait and pointer-injection (pass the launch pointer as the `-p`/`exec` argument); remove the `slice_ops.py` `init`-time tmux-executable check.
- `observe` reads the outfile tail + `result.json` + liveness + `scan_hard_stop(outfile)`; `--wait` exits early on process death, `result.json`, or a hard-stop marker (never on mere output churn).
- `finalize --steer` resumes the session as a new budgeted turn: it first quiesces (confirms the prior process is dead, reaping the identity-checked group if needed), then requires a captured session id (blocking with a clear error if none), then launches the resume turn.
- accept/stop/scavenge terminate the tracked process group by identity (not a tmux prefix sweep); `stop --scavenge` reads the `developer.pid` sidecar and validates identity before signalling; remove the `send` code path.
- Update `floor.py` fact 8 to be fed the outfile text (reword its `detail` strings from "captured pane" to "captured session output"; logic identical) and `_collect_finalize_evidence`/`stop` to snapshot the outfile as `session-output.txt` instead of `pane.txt`.
- Rename `current_slice.tmux_session` → `session` and add `session_id`, `pid`, `pgid`, `outfile` in `state.py`/`slice_ops.py`; update `references/run-state.md` (schema, scavenge wording, `PM_RUN_TOKEN`-unset wording, and the explicit limitation that state-less scavenge relies on the sidecar — if both the run state and the sidecar are gone, global discovery is impossible, unlike tmux's global session list).
- Update `cli.py`: remove the `send` subcommand/handler and change output strings (pane→output, "in tmux session"→"as headless session", session/liveness lines).
- Migrate the tmux-coupled scenarios in `test_slice_ops.py`, `test_finalize.py`, `test_floor.py`, and `test_state.py` to the headless fake.

### Acceptance Criteria
- Inputs: a run with a frozen plan; `start-slice`, `observe --wait`, `finalize --steer/--accept/--stop`, `stop --scavenge`.
- Outputs: a launched detached Developer; observation from the outfile/result; a quiesced, id-checked resume turn on steer; clean identity-checked termination on accept/stop; sidecar-based scavenge; floor fact 8 evaluating the outfile.
- User-visible behaviour: the supervise loop (launch → observe → steer/accept/stop) behaves equivalently to tmux, minus the free `send` nudge; `finalize --steer` blocks clearly when no session id was captured.
- Behaviour that must not change: attempt-budget accounting; the never-`PM_RUN_TOKEN` guarantee; artifact rotation on relaunch/steer; the eight-fact floor's pass/fail logic; MAC-authenticated state writes.

### Authorized Surface
- Files allowed to change:
  - `skills/project-manager/scripts/pm_lib/slice_ops.py`
  - `skills/project-manager/scripts/pm_lib/floor.py`
  - `skills/project-manager/scripts/pm_lib/state.py`
  - `skills/project-manager/scripts/pm_lib/cli.py`
  - `skills/project-manager/references/run-state.md`
  - `skills/project-manager/tests/test_slice_ops.py`
  - `skills/project-manager/tests/test_finalize.py`
  - `skills/project-manager/tests/test_floor.py`
  - `skills/project-manager/tests/test_state.py`
- Functions/classes/components allowed to change: `start_slice`, `observe`, `finalize_steer`, `finalize_accept`, `finalize_stop`, `stop`, `stop_scavenge_sweep`, `send` (removal), `_collect_finalize_evidence`, `_rotate_prior_attempt`, session/pid helpers in `slice_ops.py`; `_fact_hard_stop_scan` wording/input in `floor.py`; `current_slice` fields in `state.py`; the `send` subparser and print paths in `cli.py`; the schema/scavenge/token wording in `run-state.md`.
- Tests allowed or expected to change: `test_slice_ops.py`, `test_finalize.py`, `test_floor.py`, `test_state.py`.

### Explicit Non-Goals
- No deletion of the now-dead tmux functions from `sessions.py`/`profiles.py` yet (Slice 4) — they simply stop being called here, keeping this commit's diff focused on the rewiring.
- No prose-doc changes beyond `run-state.md` (Slice 5).
- No orchestrator import.

### Risk Flags
- Risky surfaces touched: the mechanical floor (fact 8 input), the persisted run-state schema (field rename + additions), the live supervise/steer control flow, process lifecycle, and a public CLI command removal (`send`)
- Approval needed before implementation: no
- Independent audit required: yes

### Validation Plan
- Tests to add/update: launch/observe/steer(resume)/accept/stop/scavenge with headless fakes; `observe --wait` early-exit on death/result/hard-stop; steer quiesces then resumes and rotates the stale result; steer blocks when no launch-bound session id was captured; a newer *unrelated* same-harness session is not mis-captured (provenance test); budget exhaustion kills the process and closes steer/accept; fact 8 passes/fails on outfile text exactly as it did on pane text (reuse the wrapping/normalisation fixtures); state round-trips the new fields; scavenge validates identity before signalling.
- Commands to run: `python3 -m pytest skills/project-manager/tests/test_slice_ops.py skills/project-manager/tests/test_finalize.py skills/project-manager/tests/test_floor.py skills/project-manager/tests/test_state.py`
- Manual checks: a full fake-harness run through the loop, including a steer/resume and a `stop --scavenge` with state deleted.

### Rollback Path
- Revert the slice commit; the lifecycle returns to the tmux path (still present via Slices 1–2 being additive and `sessions.py`/`profiles.py` tmux code still intact). Per-slice commits keep this one revert away.

## Slice 4: Delete the dead tmux code (cleanup)

### Intended Change
- Delete the now-unused tmux functions from `sessions.py` (`_run_tmux`, `_tmux_or_raise`, `start_session`, `pane_text`, `capture_to`, `session_exists`, `sessions_with_prefix`, `detect_activity`, `request_stop`, `force_stop`, `wait_until_ready` and its readiness helpers, `_verify_opencode_model_display`, `send_prompt`, `send_line`, `send_correction`), keeping `scan_hard_stop`, `session_name`, and the headless runner.
- Delete the tmux `compose_command` and `_tmux_present` from `profiles.py`/`slice_ops.py` if any dead remnant remains after the cutover.
- Rewrite `test_sessions.py` to cover only the headless runner and `scan_hard_stop` (removing the tmux `TmuxSessionTestCase` hierarchy and its `skipUnless`).

### Acceptance Criteria
- Inputs: the codebase after the Slice 3 cutover.
- Outputs: no tmux subprocess call remains in `pm_lib/`; `test_sessions.py` has no tmux dependency and does not skip.
- User-visible behaviour: unchanged from the end of Slice 3.
- Behaviour that must not change: the headless runner behaviour; `scan_hard_stop` results.

### Authorized Surface
- Files allowed to change:
  - `skills/project-manager/scripts/pm_lib/sessions.py`
  - `skills/project-manager/scripts/pm_lib/profiles.py`
  - `skills/project-manager/scripts/pm_lib/slice_ops.py`
  - `skills/project-manager/tests/test_sessions.py`
  - `skills/project-manager/tests/test_profiles.py`
- Functions/classes/components allowed to change: removal of the listed dead tmux functions only; drop the tmux-`compose_command` assertions from `test_profiles.py`.
- Tests allowed or expected to change: `test_sessions.py` (rewritten for the headless runner, no `skipUnless`); `test_profiles.py` (remove the deleted-`compose_command` tmux assertions, keeping the headless/resume composition tests added in Slice 2).

### Explicit Non-Goals
- No behavioural change (the cutover already happened in Slice 3).
- No new functionality.

### Risk Flags
- Risky surfaces touched: none
- Approval needed before implementation: no

### Validation Plan
- Tests to add/update: `test_sessions.py` covers the headless runner + `scan_hard_stop` only.
- Commands to run: `python3 -m pytest skills/project-manager/tests/` and `rg -n "tmux|capture-pane|send-keys" skills/project-manager/scripts` returns nothing.
- Manual checks: none beyond the suite.

### Rollback Path
- Revert the slice commit; the dead tmux code returns (harmless, unused).

## Slice 5: Documentation (PM + top-level, incl. README)

### Intended Change
- Rewrite the tmux/pane prose in `skills/project-manager/README.md` (drop the tmux prerequisite; pane tail → output tail; `pane*.txt` → `session-output.txt`; maintainer map; trial recipe fake harness), `skills/project-manager/SKILL.md` (workflow loop: launch/observe/resume-steer/terminate; remove free-`send` guidance), `references/developer-prompt.md` (the Launch Pointer is passed as the `-p` argument; the Steer template is a resume follow-up), and the `prompts.py` docstrings that still describe TUI injection / live-session correction.
- Update the top-level `README.md` (Mode B prerequisites and "fresh session per slice" description — drop tmux), `CHANGELOG.md` (headless-Developer entry), and `CONTRIBUTING.md` (drop the "tests needing tmux self-skip" note; keep the "no work from narration" principle, reworded from "pane text" to "session output").

### Acceptance Criteria
- Inputs: the docs after Slices 1–4.
- Outputs: no source doc describes the Developer as tmux/pane-based; the headless model is documented in one authoritative place each.
- User-visible behaviour: a reader can operate PM headlessly from the docs; the operator trial recipe works with a headless fake.
- Behaviour that must not change: the floor description, tool-equality framing, privacy guidance (reworded, not removed).

### Authorized Surface
- Files allowed to change:
  - `skills/project-manager/README.md`
  - `skills/project-manager/SKILL.md`
  - `skills/project-manager/references/developer-prompt.md`
  - `skills/project-manager/scripts/pm_lib/prompts.py`
  - `skills/project-manager/tests/test_prompts.py`
  - `README.md`
  - `CHANGELOG.md`
  - `CONTRIBUTING.md`
- Functions/classes/components allowed to change: `prompts.py` docstrings and any Launch-Pointer/Steer template wording; documentation only elsewhere.
- Tests allowed or expected to change: `test_prompts.py` (comment/doc expectations, if any assert the old wording).

### Explicit Non-Goals
- No edit to `docs/VISION.md` (already harness-neutral).
- No behavioural code change (only `prompts.py` docstrings/templates).

### Risk Flags
- Risky surfaces touched: none
- Approval needed before implementation: no

### Validation Plan
- Tests to add/update: `test_prompts.py` still passes with reworded templates.
- Commands to run: `python3 -m pytest skills/project-manager/tests/test_prompts.py` and `rg -n -i "tmux|capture-pane|pane\.txt|pane-live" skills/project-manager README.md CONTRIBUTING.md` returns nothing **outside the enumerated allowlist below**.
  - **Amended (see Amendment 2026-07-26, item B).** As originally written this check was unsatisfiable: two classes of match inside the searched paths are deliberate and must survive, so no slice could ever make the bare command return nothing. The check is therefore "no match **across the command's listed paths** outside this allowlist":
    - `tests/test_state.py` — `tmux_session` appears in **negative** assertions pinning that the field does not survive the cutover. Deleting them would delete the regression guard.
    - `references/run-state.md` — two deliberately contrastive sentences ("the Developer runs as a detached headless process, not an interactive tmux session"; "no global discovery path (unlike tmux's global session list)"). Both read correctly post-cutover and name tmux only to contrast with it.
  - `CHANGELOG.md` needs no allowlist entry: it is **outside the command's search paths** (`skills/project-manager`, `README.md`, `CONTRIBUTING.md`), so it can never match this invocation. Its historical entries legitimately record the prior tmux-era behaviour and are deliberately preserved; slices only *add* a headless entry. This is exclusion by scope, not by allowlist — the original Slice 5 wording conflated the two.
  - Slice 5 satisfied the bare command only over its own eight authorized files. Amended Slice 6 clears the remaining non-allowlisted matches (`run-state.md:74` and the stale test comments), at which point the allowlisted form is genuinely true across those paths. It is **not** a repository-wide claim: this plan file and other historical documents legitimately discuss tmux throughout.
- Manual checks: follow the README trial recipe end-to-end with a headless fake harness.

### Rollback Path
- Revert the slice commit; docs return to describing tmux.

## Amendment 2026-07-26 — closeout scope (owner-approved)

Slice 6 as originally frozen authorized only `.github/workflows/ci.yml`. Closing this body of work out requires resolving carry-forward items that all sit outside that surface, so the owner walked the closeout register and decided each item. Every item is **fixed**; nothing is closed unfixed. The decisions are split across two slices because exactly one of them changes behaviour on a safety-critical path:

- **Amended Slice 6** (below) — CI, plus items **A** (stale wording no slice authorized), **B** (the unsatisfiable Slice 5 validation check, fixed in the plan text above), **C** (OpenCode reasoning-effort parity via `--variant`), **D3** (fake-harness helper duplication), and **E** (two pre-existing PM README defects). All of it is wording, a profile-table flag, test-helper relocation, and docs.
- **New Slice 7** (below) — item **D1**, the accept-path floor TOCTOU window. Separated because it is the only behavioural change to PM's accept decision path; a separate commit lets the drift audit and the code review each reason about one thing, and lets it be reverted without losing the closeout.

Two register items are closed **as already-correct, with no code change**, and are recorded here so they are not rediscovered as open:

- **D2 — fact 8 scans the final 128 KiB of the outfile** (`sessions.read_output_tail(max_bytes=128*1024)`). Closed: this is a strict *increase* in coverage over the ~50-line tmux pane capture it replaced. A hard-stop marker buried under >128 KiB of later output would be missed, but fixing that means streaming the whole outfile — new machinery for a hypothetical, which VISION principle 9 discourages.
- **D4 — the opencode/qwen effort guard dropped in Slice 4.** Closed as correct and **must not be restored**: the hardcoded `harness in {"opencode","qwen"}` refusal is superseded by the table-driven "no effort flag means fail closed" rule. Restoring it would have kept raising for opencode even after item C adds `effort_flag`, silently overriding the fix.

Two register claims were checked against the repo and found **overstated**; the corrected facts are what the slices below are scoped to:

- Item B's "fixing group A makes the check genuinely true" is wrong — `test_state.py`'s negative assertions and `run-state.md`'s two contrastive sentences must survive. Hence the allowlist form above.
- Item D3's "~14 of the same helpers" is wrong — the actual overlap is three (`_write_result_cmd` and `_idle_body` byte-identical; `_commit_and_result_body` differing only in signature formatting and a docstring).

## Slice 6: CI, closeout wording, OpenCode effort parity, and README defects

### Intended Change
- **CI (original Slice 6).** Update `.github/workflows/ci.yml`: remove the tmux install step and the "tmux-backed runtime tests / tmux self-skip" comments; runtime tests use headless fakes and need no tmux. Confirm the entire `project-manager` suite passes with no tmux present.
- **Item A — stale wording no slice ever authorized.** Reword "pane markers" → "session-output markers" in `plan.py:32` (comment) and `plan.py:276` (**inside a user-facing `check-plan` warning string**, so this is visible output, not just a comment). Drop the stale tmux parenthetical from `references/run-state.md:74` ("the headless replacement for diffing against the tmux path's `pane-live.txt`"), which now disagrees with the reworded comment in `slice_ops.py`. Reword the stale test comments at `tests/pm_test_helpers.py:36` ("in tmux-gated slice_ops tests" — nothing is gated now), `tests/test_slice_ops.py:17`, `tests/test_review.py:244`, `tests/test_floor.py:3` and `:477`. **Leave `run-state.md:66`/`:72` and all of `tests/test_state.py` alone** — allowlisted above as deliberate.
  - *Addendum, found during implementation:* a repo-wide sweep for references to names the cutover deleted turned up exactly one more instance of this same class — `tests/pm_test_helpers.py:32` still lists the deleted `send` subcommand in its command enumeration, inside the very comment block line 36 sits in. It is fixed with line 36 rather than left dangling, since leaving a known stale reference is what this closeout exists to prevent. Recorded here so the change is authorized rather than drift.
- **Item C — OpenCode reasoning-effort parity.** `opencode run --variant` is documented by the installed CLI as *"model variant (provider-specific reasoning effort, e.g., high, max, minimal)"*, re-verified at this commit. Give opencode `"effort_flag": "--variant"` in `HARNESS_PROFILES` so `--effort` maps to it instead of failing closed, and update the profile comment. **The mechanical trap this must avoid:** `profiles.py` currently calls `_append_headless_effort([], profile, effort, harness)` for opencode and qwen — a *throwaway list*, used deliberately as a pre-flight "raise before building the command". Adding `effort_flag` alone would append `--variant` to a discarded list and the flag would never reach the argv, a silent no-op that still looks fixed. Opencode's calls must append to the real `command`; unify all five call sites on `_append_headless_effort(command, …)` so the idiom cannot regress. **Qwen is unchanged and stays fail-closed** — its full option list contains nothing reasoning-related. Effort values stay free-form pass-through (PM validates them for no harness), so no new validation is introduced. `skills/project-manager/README.md:16` ("OpenCode and Qwen expose no tested headless effort override") becomes false and must be corrected in the same commit; add a `CHANGELOG.md` entry.
- **Item D3 — fake-harness helper duplication.** Move the three helpers duplicated between `tests/test_slice_ops.py` and `tests/test_finalize.py` (`_write_result_cmd`, `_idle_body`, `_commit_and_result_body`) into `tests/pm_test_helpers.py`, their correct home, and import them in both files. Behaviour-preserving: the consolidated `_commit_and_result_body` must keep both call sites' semantics exactly (the two copies differ only in signature formatting and a docstring).
  - **They lose the leading underscore on the way** (`write_result_cmd`, `idle_body`, `commit_and_result_body`), matching `pm_test_helpers.py`'s existing convention that everything it exports is public (`parse_init_output`, `write_fake_harness`, `render_slice`). A module-private name on a deliberately shared helper would misdescribe it. This renames the call sites in both consumer suites — mechanical, and the suite is the check that every one resolved.
- **Item E — pre-existing PM README defects.** Document `review --reviewer-command` in the CLI table (genuinely absent from the README). Document the `--token`/`--run` pair that the closeout register named as undocumented. **Corrected during round-1 drift audit:** this bullet originally asserted that `--run` and `--token` were "already covered in the prose above the table". That is true of `--token` (`README.md:20`) but **false of `--run`**, which appeared in exactly two table rows (`status`, `notes`) and was defined nowhere — exactly the gap the register recorded. Rather than document `--run` per-row on eight commands, state the shared `--token`/`--run` availability once beneath the intro paragraph, and **drop the two now-redundant per-row `[--run ID]` mentions** so the table and that paragraph agree rather than leaving two rows looking exceptional. Also add `-c user.name`/`-c user.email` to the trial recipe's first `git commit --allow-empty` (`README.md:70`) so the documented recipe works on a machine with no global Git identity — the fake Developer's own commit already does this correctly, so the recipe is internally inconsistent as well as broken.

### Frozen Command Shapes (amending Slice 2's table for opencode only)
- opencode developer launch — `opencode run <pointer> [-m M] [--variant E] --agent build --auto --dir <repo>`
- opencode reviewer launch — `opencode run <pointer> [-m M] [--variant E] --agent plan --auto --dir <repo>`
- opencode resume — **unchanged**: `opencode run <correction> --session <id> --agent build --auto --dir <repo>`. No harness carries effort on a resume turn; opencode stays consistent with the other four rather than diverging.
- The other four harnesses' launch and resume shapes are **unchanged** from Slice 2.

### Acceptance Criteria
- Inputs: the CI config, the full test suite, and the closeout files above.
- Outputs: CI installs no tmux; the full suite passes without tmux; no non-allowlisted tmux/pane wording remains in `skills/project-manager`; `--effort` composes `--variant` for opencode in both modes and still fails closed for qwen; the three duplicated test helpers have exactly one definition; the README documents `--reviewer-command` and its trial recipe runs without a global Git identity.
- User-visible behaviour: CI is simpler and green; an OpenCode seat now accepts an effort override instead of erroring, in each of the three places one can be supplied — `init --harness opencode --effort E`, `start-slice --effort E` on a run whose harness is opencode (`start-slice` takes no `--harness`; it inherits the run's), and `review --tool opencode --effort E` (`review` selects its harness with `--tool`, not `--harness`); the `check-plan` dependency warning says "session-output markers"; README is accurate.
- Behaviour that must not change: test coverage of the supervise loop, floor, state, and reviewer; **qwen's fail-closed effort handling**; every other harness's composed argv in both modes, byte-for-byte; every resume shape including opencode's; the eight-fact floor's logic; the `check-plan` warning's *trigger conditions* (only its wording changes).

### Authorized Surface
- Files allowed to change:
  - `.github/workflows/ci.yml`
  - `skills/project-manager/scripts/pm_lib/plan.py`
  - `skills/project-manager/scripts/pm_lib/profiles.py`
  - `skills/project-manager/references/run-state.md`
  - `skills/project-manager/README.md`
  - `skills/project-manager/tests/pm_test_helpers.py`
  - `skills/project-manager/tests/test_profiles.py`
  - `skills/project-manager/tests/test_slice_ops.py`
  - `skills/project-manager/tests/test_finalize.py`
  - `skills/project-manager/tests/test_review.py`
  - `skills/project-manager/tests/test_floor.py`
  - `CHANGELOG.md`
  - `docs/implementation-plan-headless-developer.md` (this amendment and item B's plan-text fix)
- Functions/classes/components allowed to change: the tmux install/skip steps and comments in `ci.yml`; the two "pane markers" strings in `plan.py`; opencode's `HARNESS_PROFILES` entry, its comment, the `_append_headless_effort` call sites in `compose_headless_command`, and **`_append_headless_effort`'s own docstring** in `profiles.py`; the stale wording listed in item A; the three helper definitions moved in item D3; the README's harness-difference sentence, CLI table row, shared-flag intro paragraph, and trial recipe.
  - *The docstring is a required consequence of item C, not optional polish:* it read "OpenCode and Qwen Code expose no effort/reasoning flag", which item C makes **false**. Leaving it would be precisely the documentation-truth defect this closeout exists to remove. Qwen's own profile comment is deliberately **not** in this surface — it was already accurate before and after item C, so a round-1 drift finding correctly flagged its rewrite as unrecorded scope and it was reverted to its committed text.
- Tests allowed or expected to change: `test_profiles.py` (opencode effort assertions in both modes; qwen still fail-closed), `test_slice_ops.py`/`test_finalize.py`/`pm_test_helpers.py` (helper relocation + comment), `test_review.py`/`test_floor.py` (comments only).

### Explicit Non-Goals
- **No change to the accept path** — item D1 is Slice 7.
- No restoration of the Slice 4 opencode/qwen effort guard (register item D4).
- No change to qwen's effort handling, to any resume shape, or to the other four harnesses' composed argv.
- No deletion of `test_state.py`'s `tmux_session` negative assertions or `run-state.md:66`/`:72`.
- No new CI jobs; no widening of the `check-plan` warning's trigger conditions.
- No streaming rewrite of the fact-8 outfile scan (register item D2, closed above).

### Risk Flags
- Risky surfaces touched: the frozen per-harness command table (opencode's entry) and the fail-closed effort path — the reason the Developer seat runs at `high` effort for this slice and the code review at `high`.
- Approval needed before implementation: **yes — this amendment**, granted 2026-07-26.
- Independent audit required: yes (two separate read-only delegates — drift-audit then code-review; see HANDOFF.md "Seat Assignment For The Final Session").

### Validation Plan
- Tests to add/update: `test_profiles.py` gains opencode `--variant` assertions for developer and reviewer mode and keeps qwen's fail-closed assertion; the relocated helpers are exercised by the existing `test_slice_ops.py`/`test_finalize.py` suites unchanged.
- Commands to run: the full suite `uv run --python 3.13 --with pytest python -m pytest skills/project-manager/tests/`, passing with nothing skipped, run **unsandboxed** (under a sandbox `test_review.py::TestReviewTimeout::test_slow_reviewer_times_out_kills_process_and_fails_closed` fails with `PermissionError` on `os.killpg` — the sandbox denying the signal, not a defect) and additionally with `tmux` removed from `PATH` to prove the no-tmux claim rather than assume it.
- Manual checks: the CI YAML has no tmux reference; `rg -n -i "tmux|capture-pane|pane\.txt|pane-live" skills/project-manager README.md CONTRIBUTING.md` matches only the allowlist; composed opencode argv read by eye against the frozen shapes above.

### Rollback Path
- Revert the slice commit; CI reinstalls tmux, opencode returns to failing closed on `--effort`, and the wording/helper/README changes revert. No other slice depends on it.

## Slice 7: Close the accept-path floor TOCTOU window

### Intended Change
- `finalize_accept` currently evaluates the eight-fact floor (`slice_ops.py:1638`) and reads HEAD (`:1654`) while the Developer is **still live**, terminating it only afterwards (`:1669`). A harness that commits or edits in that window produces an ACCEPTED assessment describing a tree that is no longer current, and a commit that never faced the surface-authorization check. Close the window by **re-evaluating the floor after quiescing**: once `_terminate_current(current)` has succeeded (so the Developer is provably dead), call `_collect_finalize_evidence` again, re-read HEAD, and record the acceptance only if the floor still passes and HEAD is unchanged. If either moved, refuse the acceptance with a clear message naming the race, and write no assessment.
- The post-quiesce report and HEAD become the authoritative ones recorded in the assessment: they are the only pair evaluated against a repo no process can still mutate.
- **Preserve the existing ordering decision that termination comes after the review-freshness gate** (documented in the comment at `slice_ops.py:1669`): a *refused* acceptance must leave the Developer alive so the operator can still steer it. So the fix adds a check after termination rather than moving termination earlier — a refusal caused by a floor failure or a stale review still leaves the Developer running, exactly as today.
- `_collect_finalize_evidence` is already idempotent and never mutates or saves state (it rewrites `status-after.txt` and `diff.patch` and re-evaluates the floor), so calling it twice needs no new machinery — which is what keeps this consistent with VISION principle 9.
- Document the guarantee in `references/run-state.md` alongside the existing accept/attempt semantics: acceptance is recorded only against a quiesced repository.

### Acceptance Criteria
- Inputs: a run with a current slice and a Developer that has written `result.json`; `finalize --accept "reasoning"`.
- Outputs: on the normal path, an ACCEPTED assessment whose floor report and recorded commit were evaluated after the Developer was confirmed dead. On a raced path, no assessment, a non-zero exit, and a message identifying that the Developer acted during acceptance.
- User-visible behaviour: unchanged for every non-raced acceptance. A raced acceptance, which previously succeeded with stale evidence, now refuses and tells the operator to re-run `finalize`.
- Behaviour that must not change: the floor-failed and reviews-stale refusal paths, **including that both still leave the Developer alive**; termination failures still raise and refuse the acceptance outright; no assessment is ever left on disk announcing an acceptance the state never recorded; the run-completion/`regenerate_report` closing sequence; attempt-budget accounting; MAC-authenticated state writes.
- `cli.py` needs no change: `cli.py:403-408` renders any non-`accepted` outcome kind generically from `outcome.message` and exits 1.

### Authorized Surface
- Files allowed to change:
  - `skills/project-manager/scripts/pm_lib/slice_ops.py`
  - `skills/project-manager/tests/test_finalize.py`
  - `skills/project-manager/references/run-state.md`
- Functions/classes/components allowed to change: `finalize_accept` and the `AcceptOutcome` dataclass (a new refusal `kind`) in `slice_ops.py`; the accept-semantics paragraph in `run-state.md`.
- Tests allowed or expected to change: `test_finalize.py` (a raced-acceptance test plus confirmation that ordinary acceptance is unaffected and that the floor passing twice in a row is not spuriously refused).

### Explicit Non-Goals
- No change to `finalize --steer`, `finalize --stop`, bare `finalize`, `stop`, or `stop --scavenge`.
- No reordering of the safety-critical `finalize_steer` sequence (capture id → quiesce → hard-stop scan → re-correlate → require id → rotate → launch).
- No move of termination earlier than the review-freshness gate.
- No `cli.py` change; no new CLI flag or subcommand.
- No change to the floor's eight facts or their logic.

### Risk Flags
- Risky surfaces touched: the accept decision path — the point at which PM records an irreversible assessment.
- Approval needed before implementation: **yes — this amendment**, granted 2026-07-26.
- Independent audit required: yes.

### Validation Plan
- Tests to add/update: a fake harness that writes `result.json`, commits, then keeps committing until killed, so HEAD provably differs between the two evaluations → acceptance refuses with the race message and writes no assessment; an ordinary accept still succeeds (proving the second floor evaluation is not spuriously failing — note `_collect_finalize_evidence` writes into the artifact dir on both runs, so this test is the guard against the second run's own writes flipping a fact); floor-failed and reviews-stale refusals still leave the Developer alive.
- Commands to run: the full suite `uv run --python 3.13 --with pytest python -m pytest skills/project-manager/tests/`, unsandboxed, nothing skipped.
- Manual checks: read `finalize_accept` end to end and confirm the ordering comment still describes the code.

### Rollback Path
- Revert the slice commit; `finalize_accept` returns to a single pre-termination floor evaluation. Slice 6 is unaffected.

## Next Chat Prompt

```md
Plan file: docs/implementation-plan-headless-developer.md
Slices or batch this session: Slice 1 (or Batch A: Slices 1–2)

Read the full plan file first. If a selected slice receipt is incomplete or the repo state is unclear, stop and tell me before coding.

Work on the current feature branch (feature/headless-developer).

Use orchestrator as the controlling skill. Act as the Developer: keep implementation, validation, Git operations, and commits local.

For each selected slice, in plan order: restate the frozen contract; apply scoped-implementation; apply drift-audit and report the gate result; on a passing gate apply code-review; surface findings, fix, re-gate; then ask before committing.

Slice 3 (the cutover) is marked "Independent audit required: yes": commission independent read-only drift-audit and code-review delegates for it, and if none can be launched, STOP and report rather than self-audit it. Slices 1, 2, 4, 5, and 6 are standard: Developer self-audit is acceptable when no Reviewer is available, recorded as such.

Confirm before starting: plan file read, selected slice(s), branch, and the first slice.
```
