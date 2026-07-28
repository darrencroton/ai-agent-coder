# Implementation Plan — PM headless Developer (retire tmux)

## Purpose

Move the `project-manager` (Mode B) **Developer** from a persistent interactive tmux TUI to a **headless, resumable** invocation, still launched and supervised by PM. This is a simplifying refactor: it deletes readiness-banner detection, keystroke injection, and pane screen-scraping, and replaces the "one named tmux session per slice" model with a detached background process whose captured stdout (the *outfile*) is the universal, harness-agnostic progress signal. All five harnesses become first-class by construction, because the outfile and the `result.json` completion signal are identical for every tool.

No backwards compatibility with tmux is retained; there is no dual runtime path once the cutover lands.

## Relationship to the orchestrator-resume plan (connected, not coupled)

This plan is a **sibling** of `docs/implementation-plan-orchestrator-resume-tracking.md`. They share design *philosophy* but by explicit decision **share no code**: PM gets its own thin launcher fitted to its single-serialised-Developer + mechanical-floor + Developer-commits model, rather than importing the orchestrator's multi-job substrate. The orchestrator's delegate contract is role-incompatible with PM's Developer (a delegate *never commits*; a PM Developer *owns the slice commit* — floor fact 6), so PM cannot route its Developer through it. The single deliberately-duplicated artifact is the per-harness *launch + resume command syntax*; PM owns its own copy (frozen in Slice 2 below), matching how `review.py` already re-specs the orchestrator's command table as "behavioural evidence, shares no code, never imports."

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
  - codex — launch `codex exec <pointer> [-m M] [-c model_reasoning_effort="E"] -c sandbox_mode="workspace-write" -c approval_policy="never" --skip-git-repo-check -C <repo> [--add-dir <git-dir>]`; resume `codex exec resume <session-id> <correction> [-m M] -c sandbox_mode="workspace-write" -c approval_policy="never" --skip-git-repo-check` (`codex exec resume` accepts a smaller flag set than `codex exec` and rejects `--sandbox`, `-C`, and `--add-dir` outright, so the sandbox is expressed as config on both paths; `-C` is unnecessary because the launcher already starts the process in the repository, and the commit-time extra writable root a steered turn needs in a linked worktree is expressed as `-c sandbox_workspace_write.writable_roots='["<git-dir>"]'`, so a steered turn in a linked worktree can still commit. `approval_policy="never"` is what makes the sandbox a boundary and also removes any approval prompt a headless turn cannot answer. All three facts verified against codex-cli 0.145.0.)
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
- Commands to run: `python3 -m pytest skills/project-manager/tests/test_prompts.py` and `rg -n -i "tmux|capture-pane|pane\.txt|pane-live" skills/project-manager README.md CONTRIBUTING.md` returns nothing. (`CHANGELOG.md` is excluded from the zero-match check: its historical entries legitimately record the prior tmux-era behaviour; this slice only *adds* a new headless entry and does not rewrite that history.)
- Manual checks: follow the README trial recipe end-to-end with a headless fake harness.

### Rollback Path
- Revert the slice commit; docs return to describing tmux.

## Slice 6: CI and full-suite green

### Intended Change
- Update `.github/workflows/ci.yml`: remove the tmux install step and the "tmux-backed runtime tests / tmux self-skip" comments; runtime tests now use headless fakes and need no tmux.
- Confirm the entire `project-manager` test suite passes with no tmux present.

### Acceptance Criteria
- Inputs: the CI config and the full test suite.
- Outputs: CI installs no tmux; the full suite passes without tmux.
- User-visible behaviour: CI is simpler and green.
- Behaviour that must not change: test coverage of the supervise loop, floor, state, and reviewer.

### Authorized Surface
- Files allowed to change:
  - `.github/workflows/ci.yml`
- Functions/classes/components allowed to change: the tmux install/skip steps and comments.
- Tests allowed or expected to change: none (migration completed in earlier slices).

### Explicit Non-Goals
- No test rewrites here (done in Slices 1–4).
- No new CI jobs.

### Risk Flags
- Risky surfaces touched: none
- Approval needed before implementation: no

### Validation Plan
- Tests to add/update: none.
- Commands to run: `python3 -m pytest skills/project-manager/tests/` (locally, with no tmux on PATH if possible).
- Manual checks: the CI YAML has no tmux reference.

### Rollback Path
- Revert the slice commit; CI reinstalls tmux.

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
