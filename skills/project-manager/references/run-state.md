# Run State Reference (`lite-1`)

Authoritative run state is a **single copy outside the worktree**: `<worktree-git-dir>/pm/<run-id>/` (found via `git rev-parse --absolute-git-dir`, so each linked worktree gets its own state). `<worktree-git-dir>/pm/current` names the active run; every run-scoped command (all except `check-plan` and `init`) defaults to it and accepts `--run <id>`.

## Files in a run directory

| File | Written by | Purpose |
|---|---|---|
| `run.json` | toolkit only | the run's authoritative state (schema below) |
| `run.json.mac` | toolkit only | HMAC-SHA256 of `run.json`, keyed by the run capability token |
| `events.jsonl` | toolkit only | append-only log: `{ts, kind, slice, note, evidence?}` |
| `notes.md` | the PM agent | curated run knowledge fed to each new Developer session (mirrored into `.pm/`) |
| `run-report.md` | toolkit | human-facing report, regenerated from controller-owned data only |
| `slices/slice-NNN/assessment.md` | toolkit (PM reasoning embedded) | the accountability record per decided slice |
| `slices/slice-NNN/review-*.md` | reviewer sessions via toolkit | independent review reports, sha256-recorded in state |

## Authority model

`init` mints a random capability token, prints it once, and stores only its SHA-256 in `auth.token_sha256`. Every mutating command (`approve`, `start-slice`, `send`, `finalize`, `review`, `stop`) requires the token (`--token` or `PM_RUN_TOKEN` in the *controller's* environment — never a session's). Every state write is HMAC-signed with the token; every token-bearing read verifies. A `run.json` edited by anything not holding the token fails verification: an **integrity stop**, terminal by construction — the toolkit never re-signs unauthenticated bytes, so every later mutating command keeps failing closed and the tampered file survives as evidence. A *wrong* token is a plain error, not an integrity stop. One deliberate exception: `stop --scavenge` still sweeps `pm-*` tmux sessions when state is missing or unverifiable — it is cleanup of local processes, not a state write. Read-only commands (`status`, `observe`, `check-plan`) load without verification when tokenless — treat that output as unverified. When a token IS available (`--token` or `PM_RUN_TOKEN` — the PM agent's normal situation), `status` and `observe` verify the MAC and fail with `INTEGRITY:` on tampered state, and `status --report` never regenerates the report from unverified state. The token is never inherited by subordinate sessions: Developer tmux sessions launch with `PM_RUN_TOKEN` explicitly unset and reviewer subprocesses receive a sanitized environment.

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
    "id": "Slice 3", "artifact_dir": "…", "tmux_session": "pm-<run-id>-s03a0",
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
- **Attempts:** 0 on the initial launch; +1 per relaunch (`start-slice` again) and per steer (`finalize --steer`); pure observation and `send` nudges are free. `attempts > policy.max_attempts` forces a genuine stop: the live session is killed, and `send`, `finalize --steer`, and `finalize --accept` are refused for the slice — only `finalize --stop` (record the story) and `stop` remain. Persisted in the slice entry, so budgets survive process restarts. Known semantics to be aware of: re-running a slice that was explicitly stopped (`finalize --stop`, then `start-slice` after human review) starts a fresh budget — the reset is the recorded stop/re-run pair, visible in events and the assessment.
- **Review freshness:** each review records the HEAD it reviewed and the report's sha256. Any tree change after a mandatory review invalidates it for acceptance; re-commission against the new HEAD.
- **`wake_at`:** a reserved slot for a persisted resume time for whoever continues the run (PM agent or human). The toolkit initializes it and carries it in state; it has no setter command and no scheduler — multi-hour autonomous recovery depends on the PM harness's own scheduling, a declared dependency.
- **Recovery:** `run.json` + the artifact dir + git are sufficient. `status` reconstructs the situation and checks session liveness. With state deleted or unreadable, `stop --scavenge` still sweeps `pm-<run-id>-*` (or all `pm-*`) tmux sessions.
- **Superseded attempts** live in `attempt-<n>/` subdirectories of the slice's `.pm/` artifact dir and in the event log — never as state rows.
