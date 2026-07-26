# Run State Reference (`lite-1`)

Authoritative run state is a **single copy outside the worktree**: `<worktree-git-dir>/pm/<run-id>/` (found via `git rev-parse --absolute-git-dir`, so each linked worktree gets its own state). `<worktree-git-dir>/pm/current` names the active run; every run-scoped command (all except `check-plan` and `init`) defaults to it and accepts `--run <id>`.

## Files in a run directory

| File | Written by | Purpose |
|---|---|---|
| `run.json` | toolkit only | the run's authoritative state (schema below) |
| `run.json.mac` | toolkit only | HMAC-SHA256 of `run.json`, keyed by the run capability token |
| `events.jsonl` | toolkit only | append-only log: `{ts, kind, slice, note, evidence?}` |
| `notes.md` | the PM agent (via `pm notes`) | curated run knowledge fed to each new Developer session (mirrored into `.pm/`) |
| `run-report.md` | toolkit | human-facing report, regenerated from controller-owned data only |
| `slices/slice-NNN/assessment.md` | toolkit (PM reasoning embedded) | the accountability record per decided slice |
| `slices/slice-NNN/review-*.md` | reviewer sessions via toolkit | independent review reports, sha256-recorded in state |

## Authority model

`init` mints a random capability token, prints it once, and stores only its SHA-256 in `auth.token_sha256`. Every mutating command (`approve`, `start-slice`, `finalize`, `review`, `notes`, `stop`) requires the token (`--token` or `PM_RUN_TOKEN` in the *controller's* environment — never a session's). Every state write is HMAC-signed with the token; every token-bearing read verifies. A `run.json` edited by anything not holding the token fails verification: an **integrity stop**, terminal by construction — the toolkit never re-signs unauthenticated bytes, so every later mutating command keeps failing closed and the tampered file survives as evidence. A *wrong* token is a plain error, not an integrity stop. One deliberate exception: `stop --scavenge` still terminates the tracked Developer process group — read from the per-run `developer.pid` sidecar and validated against its recorded start-time identity before any signal — when state is missing or unverifiable; it is cleanup of local processes, not a state write. Read-only commands (`status`, `observe`, `check-plan`) load without verification when tokenless — treat that output as unverified. When a token IS available (`--token` or `PM_RUN_TOKEN` — the PM agent's normal situation), `status` and `observe` verify the MAC and fail with `INTEGRITY:` on tampered state, and `status --report` never regenerates the report from unverified state. The token is never inherited by subordinate processes: the Developer's detached headless process launches with `PM_RUN_TOKEN` stripped from its environment, and reviewer subprocesses receive a sanitized environment.

Writes are atomic (temp file + rename) under an advisory `fcntl` lock (`.lock`); a held lock is reported after ~5 s and never stolen.

## `run.json` shape

```json
{
  "schema": "lite-1",
  "run_id": "20260718T090000Z",
  "created_at": "…", "updated_at": "…",
  "status": "active | needs-human | complete | stopped",
  "repo": "/abs/path", "branch": "feature/x",
  "plan": {"path": "/abs/plan.md", "sha256": "…", "slice_count": 5},
  "harness": {"name": "codex", "model": null, "effort": null, "command_override": null},
  "reviewer": {"tools": ["copilot"], "model": null, "effort": null},
  "policy": {"max_attempts": 3, "commit_required": true},
  "auth": {"token_sha256": "…"},
  "current_slice": {
    "id": "Slice 3", "artifact_dir": "…", "session": "pm-<run-id>-s03a0",
    "session_id": "<launch-bound resume id, or null>",
    "pid": 12345, "pgid": 12345, "identity": "<start-time identity>",
    "outfile": "…/session-output.txt", "command_override": null,
    "launch_pointer": "<exact prompt this launch sent>",
    "launch_cwd": "/abs/repo", "launch_started_at": 1750000000.0,
    "before_head": "…", "started_at": "…", "attempts": 0,
    "risk": "standard", "plan_risk": "standard",
    "wake_at": null, "reviewer_pids": []
  },
  "slices": [
    {"id": "Slice 1", "title": "…", "status": null,
     "risk": "standard", "plan_risk": "standard", "commit": null, "attempts": 0,
     "decision": "…", "reviews": [{"skill": "code-review", "tool": "…", "head": "…",
       "before_head": "…", "artifact": "…", "sha256": "…", "at": "…"}],
     "assessment": "<state-dir>/slices/slice-001/assessment.md", "summary": "…"}
  ],
  "approvals": {"Slice 4": {"at": "…", "reason": "…"}},
  "stop_reason": null
}
```

Validation is tolerant: only the fields PM reads are checked; unknown extras pass through. A different `schema` value is refused with no migration — runs are days long, not years.

## Semantics worth knowing

- **Slice statuses:** `null` = pending; `accepted` (PM's recorded decision), `attested` (operator-attested prior completion at `init --attest` — narration, not verification), `stopped` (any non-accepted end; reason in the entry and assessment).
- **Risk:** `plan_risk` is derived mechanically at parse time (approval `yes`, independent-audit `yes`, or risky-surfaces ≠ exact `none` ⇒ `elevated`) and never changes. `risk` starts equal and may only be **raised** (`--risk elevated` on `start-slice`/`finalize`); elevated slices cannot be accepted without both a fresh `drift-audit` and `code-review` review pinned to the exact final HEAD.
- **Session fields:** the Developer runs as a detached headless process, not an interactive tmux session. `current_slice` records `session` (PM's per-attempt label, `pm-<run-id>-sNNaN` — advanced to the new attempt's label on every relaunch and every steer resume), `session_id` (the harness's launch-bound resume handle, held stable across a resume), `pid`/`pgid`/`identity` (the tracked process group and a start-time identity that guards against PID reuse), `outfile` (the captured stdout/stderr at `<artifact_dir>/session-output.txt` — the universal, harness-agnostic progress signal), `command_override` (the `--harness-command` string, if any, so a resume re-runs the same custom harness), and the launch-correlation metadata `launch_pointer`/`launch_cwd`/`launch_started_at` used for a safe delayed re-correlation.
- **Launch-bound session id (never a bare newest session):** `session_id` is bound to *this* launch only. claude/copilot receive a launch-set UUID (bound by construction). A `--harness-command` override prints its own id on an exact `PM_DEVELOPER_SESSION_ID: <id>` line which PM reads from this launch's own output — PM never synthesizes one. codex/opencode/qwen are correlated to this launch's own harness-store record by matching the exact pointer, repo cwd, and a bounded start-time window, accepting a result only when a *single* record matches (codex also accepts its exact `session id:` stdout line); OpenCode's store is read read-only over SQLite. A harness emits its id shortly after launch, not synchronously with it, so `finalize --steer` first *gathers* the launch-owned id from the still-live prior turn with a bounded, fail-closed wait (it stops early once the id is found or the output shows a hard-stop marker), then *quiesces* that turn, then *requires* the id — quiescing always precedes requiring the id and launching the resume, and an id that only finalized as the turn exited is re-correlated once more after quiescing. Anything ambiguous, missing, or unverifiable stays `null`, and a steer then fails closed and directs a relaunch — PM never guesses "the last session" or a newest one, and never synthesizes an override id. A steer also refuses (before any increment/rotation/event) when the quiesced turn's captured output shows a hard-stop marker.
- **Launch environment:** the Developer's detached process launches with `PM_RUN_TOKEN` stripped and with `PM_DEVELOPER_RESUME_SESSION_ID` guaranteed unset on an initial launch even when the controller inherited one; only an override *resume* sets `PM_DEVELOPER_RESUME_SESSION_ID` to the captured id so a custom harness continues its prior session.
- **Attempts:** 0 on the initial launch; +1 per relaunch (`start-slice` again) and per steer (`finalize --steer`); pure observation is free. Steering is turn-based: a steer always quiesces the prior turn before resuming. Normally that turn has already run to completion and exited, so quiescing is a no-op; a turn still working when the steer arrives **is terminated** (its identity-checked group reaped) once its launch-owned session id has been captured, so the resume can never race it. There is no free mid-turn nudge, and a steer never interleaves with a live turn. `attempts > policy.max_attempts` forces a genuine stop: the tracked Developer process group is terminated, and `finalize --steer` and `finalize --accept` are refused for the slice — only `finalize --stop` (record the story) and `stop` remain. Persisted in the slice entry, so budgets survive process restarts. Known semantics to be aware of: re-running a slice that was explicitly stopped (`finalize --stop`, then `start-slice` after human review) starts a fresh budget — the reset is the recorded stop/re-run pair, visible in events and the assessment.
- **Review freshness:** each review records the HEAD it reviewed and the report's sha256. Any tree change after a mandatory review invalidates it for acceptance; re-commission against the new HEAD.
- **`wake_at`:** a reserved slot for a persisted resume time for whoever continues the run (PM agent or human). The toolkit initializes it and carries it in state; it has no setter command and no scheduler — multi-hour autonomous recovery depends on the PM harness's own scheduling, a declared dependency.
- **Recovery:** `run.json` + the artifact dir + git are sufficient. `status` reconstructs the situation and checks Developer-process liveness (`os.kill(pid, 0)` plus a start-time identity match). With state deleted or unreadable, `stop --scavenge --run <id>` reads the per-run `developer.pid` sidecar, validates its recorded identity, and terminates the tracked process group. The sidecar lives under the `.pm/` mirror (`.pm/runs/<run-id>/developer.pid`), so it survives a deleted state directory; it is rewritten on every launch and every resume. **Limitation:** state-less scavenge relies entirely on this sidecar — if *both* the run state and the sidecar are gone there is no global discovery path (unlike tmux's global session list), and `stop --scavenge` with no run id has nothing to act on.
- **Superseded attempts** live in `attempt-<n>/` subdirectories of the slice's `.pm/` artifact dir (the rotated `result.json` and `session-output.txt`) and in the event log — never as state rows.
- **`observe` progress signal:** "output changed" means the captured outfile has *grown since the previous observation*, tracked by a small `observe-cursor.txt` byte count beside the outfile. It is a progress signal only: `observe --wait` never ends early on output growth, just on process death, `result.json`, or a hard-stop marker.
