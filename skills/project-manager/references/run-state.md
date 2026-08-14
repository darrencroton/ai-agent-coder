# Run State Reference (`lite-1`)

Authoritative run state is a **single copy outside the worktree**: `<worktree-git-dir>/pm/<run-id>/` (found via `git rev-parse --absolute-git-dir`, so each linked worktree gets its own state). `<worktree-git-dir>/pm/current` names the active run; every run-scoped command (all except `check-plan` and `init`) defaults to it and accepts `--run <id>`.

## Files in a run directory

| File | Written by | Purpose |
|---|---|---|
| `run.json` | toolkit only | the run's authoritative state (schema below) |
| `run.json.mac` | toolkit only | HMAC-SHA256 of `run.json`, keyed by the run capability token |
| `events.jsonl` | toolkit only | append-only log: `{ts, kind, slice, note, evidence?}` |
| `notes.md` | the PM agent (via `pm notes`) | curated run knowledge fed to each new Developer session (mirrored into `.pm/`) |
| `model-performance.md` | the PM agent (via `pm rate`) | once-per-run harness/model performance rating (see [model-performance-rubric.md](model-performance-rubric.md)); embedded verbatim into `run-report.md` |
| `run-report.md` | toolkit | human-facing report, regenerated from controller-owned data only |
| `slices/slice-NNN/assessment.md` | toolkit (PM reasoning embedded) | the accountability record per decided slice |
| `slices/slice-NNN/review-*.md` | reviewer sessions via toolkit | independent review reports, sha256-recorded in state |

## Authority model

`init` mints a random capability token, prints it once, and stores only its SHA-256 in `auth.token_sha256`. Every mutating command (`approve`, `grant`, `start-slice`, `send`, `finalize`, `review`, `notes`, `rate`, `stop`) requires the token (`--token` or `PM_RUN_TOKEN` in the *controller's* environment — never a session's). Every state write is HMAC-signed with the token; every token-bearing read verifies. A `run.json` edited by anything not holding the token fails verification: an **integrity stop**, terminal by construction — the toolkit never re-signs unauthenticated bytes, so every later mutating command keeps failing closed and the tampered file survives as evidence. A *wrong* token is a plain error, not an integrity stop. One deliberate exception: `stop --scavenge` still sweeps `pm-*` tmux sessions when state is missing or unverifiable — it is cleanup of local processes, not a state write. Read-only commands (`status`, `observe`, `check-plan`) load without verification when tokenless — treat that output as unverified. When a token IS available (`--token` or `PM_RUN_TOKEN` — the PM agent's normal situation), `status` and `observe` verify the MAC and fail with `INTEGRITY:` on tampered state, and `status --report` never regenerates the report from unverified state. The token is never inherited by subordinate sessions: Developer tmux sessions launch with `PM_RUN_TOKEN` explicitly unset and reviewer subprocesses receive a sanitized environment.

Writes are atomic (temp file + rename) under an advisory `fcntl` lock (`.lock`); a held lock is reported after ~5 s and never stolen. Individual *writes* are serialized, but a command is not a transaction: most mutating commands read a snapshot, decide, and write it back, so two mutating commands run concurrently can lose the earlier one's write. PM is a single controller issuing commands one at a time, which is what makes that safe — do not run two mutating commands at once. `grant` and `review` are the exceptions that hold the lock across their whole read-decide-write, because each can legitimately overlap a long-running reviewer subprocess.

A run id is `<UTC timestamp>-<random nonce>`. The nonce is load-bearing, not decoration: the timestamp has one-second resolution and the collision check can only see run directories under a single state root, while every linked worktree deliberately gets its own. Two runs started in the same second in two worktrees could otherwise mint the same id — and therefore the same `pm-<run-id>-s<NN>a<N>` tmux session name, on a tmux server that is global to the machine. Sessions are recovered by matching that full shape, never by `pm-<run-id>` as a string prefix, which could not tell run `X` from run `X-2`.

`PM_TMUX_SOCKET`, when set, confines every PM tmux call to that named server (`tmux -L`). Unset — the default — PM uses the caller's default server, so an operator can `tmux attach` to a Developer session as usual.

## `run.json` shape

```json
{
  "schema": "lite-1",
  "run_id": "20260718T090000Z-3f9a1c",
  "created_at": "…", "updated_at": "…",
  "status": "active | needs-human | complete | stopped",
  "repo": "/abs/path", "branch": "feature/x",
  "plan": {"path": "/abs/plan.md", "sha256": "…", "slice_count": 5},
  "harness": {"name": "codex", "model": null, "effort": null, "command_override": null},
  "reviewer": {"tools": ["copilot"], "model": null, "effort": null},
  "policy": {"max_attempts": 10},
  "auth": {"token_sha256": "…"},
  "current_slice": {
    "id": "Slice 3", "artifact_dir": "…", "tmux_session": "pm-<run-id>-s03a0",
    "before_head": "…", "started_at": "…", "attempts": 0,
    "risk": "standard", "plan_risk": "standard",
    "reviewer_pids": []
  },
  "slices": [
    {"id": "Slice 1", "title": "…", "status": null,
     "risk": "standard", "plan_risk": "standard", "commit": null, "attempts": 0,
     "decision": "…", "reviews": [{"skill": "code-review", "tool": "…", "model": null, "head": "…", "grants_seen": 0,
       "before_head": "…", "artifact": "…", "sha256": "…", "at": "…"}],
     "grants": [{"path": "…", "evidence": "…", "at": "…"}],
     "assessment": "<state-dir>/slices/slice-001/assessment.md", "summary": "…"}
  ],
  "approvals": {"Slice 4": {"at": "…", "reason": "…"}},
  "stop_reason": null
}
```

Validation is tolerant: only the fields PM reads are checked; unknown extras pass through. A different `schema` value is refused with no migration — runs are days long, not years.

## Semantics worth knowing

- **Slice statuses:** `null` = pending; `accepted` (PM's recorded decision), `attested` (operator-attested prior completion at `init --attest` — narration, not verification), `stopped` (any non-accepted end; reason in the entry and assessment).
- **Risk:** `plan_risk` is derived mechanically at parse time (approval `yes`, independent-audit `yes`, or risky-surfaces ≠ exact `none` ⇒ `elevated`) and never changes. `risk` starts equal and may only be **raised** (`--risk elevated` on `start-slice`/`finalize`, or automatically by `grant` — see below); elevated slices cannot be accepted without both a fresh `drift-audit` and `code-review` review pinned to the exact final HEAD.
- **Attempts:** 0 on the initial launch; +1 per relaunch (`start-slice` again) and per steer (`finalize --steer`); pure observation and `send` nudges are free. `attempts > policy.max_attempts` forces a genuine stop: the live session is killed, and `send`, `finalize --steer`, and `finalize --accept` are refused for the slice — only `finalize --stop` (record the story) and `stop` remain. Persisted in the slice entry, so budgets survive process restarts. Known semantics to be aware of: re-running a slice that was explicitly stopped (`finalize --stop`, then `start-slice` after human review) starts a fresh budget — the reset is the recorded stop/re-run pair, visible in events and the assessment.
- **Review freshness:** each review records the HEAD it reviewed and the report's sha256. Any tree change after a mandatory review invalidates it for acceptance; re-commission against the new HEAD. A recorded grant invalidates it too, via `grants_seen` — see *Grants* below.
- **Grants:** recorded per slice in `grants` (path, evidence, `at`); each authorizes exactly one file path — never a directory or glob — and they only ever widen a slice's effective authorized surface, never narrow it. The first grant on a slice ratchets `risk` to `elevated` the same way an explicit raise does, and every grant stales every review already recorded for the slice: a review records `grants_seen`, the number of grants its own prompt was rendered from, and counts as fresh only while that equals the slice's current grant count — so both mandatory reviews must be re-commissioned after the last grant, even on a slice that was already elevated. Because grants are append-only, that count is a monotonic authorization revision and no clock is involved: a review still running when a grant lands is staled by what its prompt showed, not by when it happened to finish. The plan digest is untouched — a grant never touches the plan file. Compatibility is one-directional: absent `grants` stays valid state, and an older toolkit that does not know the field ignores it and enforces the narrower original surface, which fails closed rather than open. One honest limit: a grant persists on the slice entry through `finalize --stop` → human review → a later `start-slice` re-run, so the re-run inherits the widening along with a fresh attempt budget — visible in the report and assessment, but never re-confirmed by the human who cleared the stop.
- **Recovery:** `run.json` + the artifact dir + git are sufficient. `status` reconstructs the situation and checks session liveness. With state deleted or unreadable, `stop --scavenge` still sweeps that run's sessions (or, with no run id, all `pm-*`).
- **Superseded attempts** live in `attempt-<n>/` subdirectories of the slice's `.pm/` artifact dir and in the event log — never as state rows.
