# Run State Schema

PM writes an auditable JSON state mirror under `.ai-pm/runs/<run-id>/run.json` in the target repository. Immediately before the first harness launch it creates an undisclosed controller-owned copy outside the worktree; linked worktrees resolve their external Git directory correctly. Harness prompts and environments receive neither control-state path. While the protected copy exists, normal reads require the mirror and controller copy to match exactly. This detects and recovers from accidental or model-driven worktree corruption, but it is not an OS security boundary against a same-user unsandboxed process that deliberately searches and rewrites arbitrary filesystem locations. The schema is intentionally explicit so a stopped run can be audited or resumed without reading chat history. PM supports only the complete current schema and performs no migration or compatibility backfill; an older or incomplete run must be archived and reinitialized.

## `run.json`

```json
{
  "schema_version": 5,
  "run_id": "20260704T013000Z",
  "created_at": "2026-07-04T01:30:00Z",
  "updated_at": "2026-07-04T01:30:00Z",
  "status": "initialized",
  "repo_path": "/absolute/path/to/repo",
  "plan_path": "/absolute/path/to/plan.md",
  "worktree_root": null,
  "branch": "feature/example",
  "harness": {
    "name": "codex",
    "adapter": null,
    "launch_config": {
      "harness_command": null,
      "harness_model": "gpt-5.4",
      "harness_effort": "high",
      "reviewer_tools": ["claude"],
      "reviewer_model": "sonnet",
      "reviewer_effort": "high",
      "allow_profile_command": true,
      "allow_unattended_default": false
    },
    "preflight": {
      "platform": "macOS-15.5-arm64-arm-64bit",
      "git": "/usr/bin/git",
      "tmux": "/usr/bin/tmux",
      "python": "/usr/bin/python3",
      "python_version": "3.13.5"
    }
  },
  "policy": {
    "dirty_state": "clean-required",
    "approval_gated_slices": "stop",
    "max_repair_attempts": 3,
    "commit_required": true
  },
  "plan": {
    "slice_count": 4,
    "parser": "implementation-plan-markdown-v2",
    "sha256": "<hex digest of the plan file at init>"
  },
  "current_slice": {
    "slice_id": "Slice 1",
    "title": "Define Skill Contract and Reference Docs",
    "artifact_dir": ".ai-pm/runs/20260704T013000Z/slices/slice-001",
    "tmux_session": "pm_20260704T013000Z_slice-001_a1",
    "attempt": 1,
    "started_at": "2026-07-04T01:35:00Z",
    "before_head": "<commit HEAD immediately before this slice attempt started>",
    "developer_session_id": "<optional Claude session id for transcript capture>",
    "reviewer_tools": ["<tool names required for this slice attempt, empty if none>"],
    "reviewer_policy": {
      "sha256": "<digest of the exact PM-generated reviewer-policy.json>",
      "policy": {"<normalized policy object>": "<stored before developer launch>"}
    },
    "prior_slice_context": {
      "path": ".ai-pm/runs/20260704T013000Z/slices/slice-001/prior-slice-context.md",
      "sha256": "<digest PM generated before developer launch>"
    },
    "launch_config": {
      "harness_command": null,
      "harness_model": "gpt-5.4",
      "harness_effort": "high",
      "reviewer_tools": ["claude"],
      "reviewer_model": "sonnet",
      "reviewer_effort": "high",
      "allow_profile_command": true,
      "allow_unattended_default": false
    },
    "repair": {
      "round": 0,
      "last_signature": "",
      "signature_streak": 0,
      "session_generation": 1,
      "operational_round": 0
    },
    "pause": null
  },
  "supervision": {
    "mode": "deterministic-batch",
    "pause_policy": {
      "rolling_usage_limit": "wait-until-reset-plus-buffer",
      "weekly_usage_limit": "stop-for-user",
      "transient_service_unavailable": "bounded-retry",
      "unknown_operational_event": "stop-for-user"
    },
    "default_resume_prompt": "You were interrupted. Review what you were doing then continue.",
    "default_reset_buffer_seconds": 180,
    "max_single_pause_seconds": 21600,
    "max_consecutive_pauses_per_slice": 2,
    "max_cumulative_pause_seconds_per_run": 43200,
    "max_transient_retries_per_slice": 3,
    "max_observe_staleness_seconds": 600,
    "min_idle_observation_windows": 3,
    "pause_counters": {
      "consecutive_pauses_current_slice": 0,
      "cumulative_pause_seconds_run": 0
    }
  },
  "operational_events_path": ".ai-pm/runs/20260704T013000Z/operational-events.jsonl",
  "approvals": {},
  "slices": [],
  "stop_reason": null
}
```

`policy.max_repair_attempts` and `policy.commit_required` are set at `init` (`--max-repair-attempts`, `--no-commit-required`) and default to 3 and true. Supervision pause budgets keep their defaults; editing them by hand in `run.json` before the first slice starts is the supported way to change them for a run.

`approvals` maps an approval-gated slice id to a recorded operator approval:

```json
{
  "Slice 3": {
    "approved_at": "2026-07-04T02:00:00Z",
    "reason": "risk reviewed with operator",
    "approved_by": "operator"
  }
}
```

Written only by the `approve` command (which also appends a `"kind": "approval"` operational event). An entry clears exactly one condition: a plan slice whose `Approval needed before implementation` flag is an explicit `yes`. Missing or unclear flags stay blocking regardless of approvals.

Allowed run `status` values:

- `initialized`
- `running`
- `paused`
- `resuming`
- `partial`
- `needs-human`
- `blocked`
- `failed`
- `complete`
- `cancelled`

## Run Integrity

- The first live slice freezes `harness.launch_config` for the whole run. Omitted flags on later `start-slice`, `wait`, or `finalize-slice` invocations inherit that exact harness command/model/effort, reviewer tools/model/effort, and launch-policy configuration. Supplying a conflicting value fails closed and requires a new run. Boolean launch-policy options are affirmative `store_true` flags: omission inherits a frozen `true`, while an affirmative flag conflicts with a frozen `false`. `current_slice.launch_config` snapshots the same complete contract for repair relaunches.
- Every slice launch replaces `harness.model_identity` with a fresh, slice-tagged check. Profiles with a queryable inventory (currently OpenCode) fail closed on inventory mismatch and verify the live display before prompt injection. Ambient-default or non-queryable profiles record `catalog_verified: false`; they never retain a prior slice's verified identity.
- Once controller state is activated, `load_run` requires the Git-metadata copy to remain present and compares it with the worktree mirror; deletion, corruption, or mismatch fails closed. `stop-with-evidence` is deliberately independent of a valid mirror: it recovers from the controller copy, archives the tampered mirror, stops the harness and reviewers, and rewrites a consistent terminal state. If neither copy parses, it still scans the run namespace, stops matching processes, and writes `emergency-stop/emergency-stop.json` with `state_updated: false`.

- `plan.sha256` freezes the plan file at init. Before each slice, PM re-hashes
  the plan and stops with an error if it changed, so a mid-run plan edit cannot
  silently alter authorization, ordering, or approval flags. The digest is
  mandatory in schema v5; a revised plan or incomplete state requires a fresh
  `init`.
- Slice numbers must be unique; `init` fails closed on a duplicate `## Slice N:`
  because completion tracking keys on the slice id.
- A revised plan requires a fresh `init`. When earlier slices were already
  completed and committed under the previous run, `init --assume-complete
  "Slice 1,Slice 2"` records operator-attested `assumed-complete` entries so
  the new run resumes at the next real slice instead of re-running finished
  work. These entries are attestations, not gate verdicts: PM never assigns
  the status itself, and the entry's `gate_reason` says so.
- PM assumes one logical controller for a run. Concurrency control is
  deliberately partial: the operational-events JSONL and `approve` use
  advisory locks (`update_run_locked` / the events lock), and `pause-until`
  locks its counter updates because it overlaps long waits — but
  `start-slice`, `finalize-slice`, `stop`, and the batch loop rewrite
  `run.json` unlocked under the one-controller assumption (they also refuse
  to run while another slice is live). Do not drive one run from two
  controllers concurrently. High-frequency observations must be appended to
  JSONL artifacts instead of repeatedly rewriting `run.json`.

## Supervision State

`supervision.mode` is set explicitly by both paths: the batch driver (`run-next` / `run --scope remaining`) records `deterministic-batch`, and the model-supervised `start-slice` primitive records `model-supervised`. The policy fields describe defaults and budgets; they do not by themselves authorize accepting a slice.

`pause_policy` names the intended operational policy:

- rolling usage limits: wait until reset plus buffer when evidence is clear and the harness process is still resumable
- weekly, monthly, account, billing, and unknown limits: stop for the user
- transient service unavailable: bounded retry
- unknown operational event: stop for the user

Pause budget fields:

- `default_reset_buffer_seconds`: buffer added after a clear reset time
- `max_single_pause_seconds`: maximum one pause may wait
- `max_consecutive_pauses_per_slice`: maximum repeated pauses in the same slice
- `max_cumulative_pause_seconds_per_run`: maximum total paused time in the run
- `pause_counters.consecutive_pauses_current_slice`: count for the active slice
- `pause_counters.cumulative_pause_seconds_run`: total paused seconds for the run

`supervision` and `operational_events_path` are required schema-v5 fields. PM does not synthesize them when state is incomplete. Schema-v5 objects reject unknown fields at every defined level; retired Worker fields cannot coexist with their Reviewer replacements and are never ignored as extensions. This extends to a slice entry's Developer-reported evidence fields (`summary`, `changed_files`, `validation`, `drift_audit`, `code_review`, `commit`, `next_action`, `blockers`, `residual_findings`): their shape is validated on every entry regardless of status, but PM validates shape only, never Developer-narration semantics, and `slice_entry_from_gate` normalizes a malformed reported value to its documented default before persisting so that recording a Developer's own malformed failure can never itself wedge the run.

## Operational Events

`operational_events_path` points at an append-only JSONL file. Model-supervised primitives append observations, waits, sends, pauses, resumes, retries, hard-stop detections, approvals, finalization attempts, and stop-with-evidence records there. Event ids come from a sidecar `.counter` file maintained under the same lock; if that sidecar is lost, PM reconstructs it once from the JSONL log as current-state recovery. During `wait`/`pause-until` polling, observation events are recorded on decision-relevant change or on a 60-second floor — not on every poll — plus always for the final snapshot, so a multi-hour pause does not flood the log with identical entries.

Example line:

```json
{
  "event_id": "op-0001",
  "slice_id": "Slice 1",
  "attempt": 1,
  "kind": "usage_limit",
  "subtype": "rolling_window",
  "status": "handled",
  "detected_at": "2026-07-04T01:40:00Z",
  "evidence_path": ".ai-pm/runs/20260704T013000Z/slices/slice-001/pane-capture-live-latest.txt",
  "evidence_excerpt": "session limit reached and will reset at 6:30pm",
  "decision": "pause-until",
  "decided_by": "pm-model",
  "resume_at": "2026-07-04T08:33:00Z",
  "action_taken": "sent continuation prompt",
  "notes": ""
}
```

Append-only event writes must not rewrite unrelated `run.json` state.

## Operational Hints

`observe` and `wait` include an `operational_hints` array in their JSON output. Hints are extracted from live pane text and the transcript tail when present. They are not acceptance evidence and they do not finalize gates.

Observation events also record compact hint kinds. Three or more separate `idle_no_progress` observation windows spanning `supervision.max_observe_staleness_seconds` (default 600 seconds) produce an operational `idle-stall`. The stall uses the same persisted `current_slice.repair` signature streak, repair budget, fresh-session escalation, and terminal circuit breaker as gate repairs; it does not create a parallel retry policy. A progress observation resets the consecutive event sequence. Hard-stop evidence continues to prohibit an automatic nudge.

An automatic repair or send event also resets the idle observation window, so every escalation requires a fresh sustained period without progress. Both idle-supervision fields are required in schema v5; incomplete or older state is rejected without backfill.

Example hint:

```json
{
  "kind": "usage_limit",
  "confidence": "high",
  "subtype": "rolling_window",
  "reset_at": "2026-07-04T08:30:00+10:00",
  "retry_after_seconds": null,
  "hard_stop": false,
  "evidence_excerpt": "session limit reached and will reset at 8:30am",
  "source": "tmux-pane",
  "detected_at": "2026-07-04T01:40:00+10:00",
  "recovery_guidance": "pause-until-reset-plus-buffer-then-send-continuation"
}
```

Current hint kinds are:

- `usage_limit`
- `service_unavailable`
- `network_transient`
- `auth_required`
- `trust_prompt`
- `permission_prompt`
- `external_side_effect_request`
- `idle_no_progress`
- `process_exited_without_result`
- `result_ready`

Usage-limit subtypes are:

- `rolling_window`
- `weekly_window`
- `monthly_window`
- `account_or_billing`
- `unknown_limit`

Hard-stop hints are deterministic guards, not just advice. `send`, `pause-until`, and unattended retry/resume paths must refuse when the strongest visible hint is weekly, monthly, account, billing, unknown-limit, auth, trust, permission, or external-side-effect related. Relative reset durations are preferred over absolute times. Absolute local reset times are accepted only when they are unambiguously near-future for the controller timezone or include an explicit timezone; otherwise they become `unknown_limit` hard stops.

## Current Slice

`current_slice.before_head` records the commit at the beginning of the active slice attempt. This is mandatory for `finalize-slice` because out-of-process finalization must compare changed files against the real slice start. Guessing `HEAD^` can miss earlier commits made by the same slice.

`current_slice.developer_session_id` is optional and records the launched Claude session id when PM composed one. `finalize-slice` and `stop-with-evidence` use it to capture `developer-transcript.jsonl` without relying only on pane text.

`current_slice.repair` tracks the self-correcting repair loop for the active slice:

- `round`: repairable gate failures handled so far for signatures that can plausibly change the tree (in-session nudge, fresh-session escalation, or dead-session relaunch). The repair budget (`policy.max_repair_attempts`, default 3) is enforced from this persisted counter — never from counting appended slice entries, because in-session repairs deliberately append none. The `idle-no-progress` and `transient-service-unavailable` signatures never advance `round` and so never count toward this budget; see `operational_round` below.
- `last_signature` / `signature_streak`: the signature-keyed circuit breaker. The first failure of a signature earns an in-session nudge into the live developer session; the same signature failing again earns exactly one fresh-session retry; a third consecutive failure is terminal regardless of remaining budget. A dead-session relaunch consumes a round but leaves the breaker untouched. This applies identically to `idle-no-progress`/`transient-service-unavailable`, bounding a run stuck repeating the identical operational signature even though those signatures don't consume `round`.
- `session_generation`: increments only when a fresh tmux session is launched; the session name keys on it, so in-session repair rounds keep one live session.
- `operational_round`: a separate counter for `idle-no-progress` and `transient-service-unavailable` repair rounds, checked against the same `max_repair_attempts` budget independently of `round`. Neither an idle stall nor a transient-service retry implies the developer changed the tree by itself, so they should not compete with a real defect-fixing repair for the same budget — but both repair prompts explicitly tell the developer to continue the interrupted work, which can still go on to touch files, so this counter advances unconditionally on every accepted operational-signature call (including dead-session relaunches, and regardless of whether the signature matches the previous call) so it cannot be evaded by alternating between the two operational signatures or by a run of dead-session relaunches. It is also embedded in `reviewer-policy.json`'s digest (see below) precisely because `round` and, for an in-session repair, `session_generation` stay frozen across operational rounds — without a digest input that actually changes, an operational repair's policy refresh would write byte-identical JSON and leave stale Reviewer evidence valid.

Every repair is re-verified by the complete, unrelaxed gate against the slice starting commit, so a `repairable` classification can only grant another chance to satisfy the identical gate — it can never accept a bad slice.

The complete failure-signature taxonomy — which signatures are repairable versus terminal — is owned by `REPAIRABLE_SIGNATURES` and `TERMINAL_SIGNATURES` in `skills/project-manager/scripts/pm_lib/gates.py`. That module is the single source of truth; the enumeration below is for readability only, and if it and the code ever disagree, the code wins.

Repairable signatures (steer the live developer session, then re-verify the identical gate):

- `validation`, `drift`, `review` — failed or missing validation, a non-`PASS` drift-audit verdict, a non-`PASS` code-review verdict
- `reviewer-evidence` — opt-in mechanical Reviewer evidence missing, mismatched, or malformed
- `unauthorized-files` (restore-only) and `changed-files-mismatch` — authorized-surface and changed-file bookkeeping violations
- `result-malformed` — `developer-result.json` fails shape/contract validation
- `residual-ledger-mismatch` — a malformed `residual_findings` ledger
- `commit-missing`, `dirty-worktree` — a missing commit or an unclean worktree
- `developer-repairable` — the Developer self-reported a repairable status
- `context-budget` — the projected next-slice `prior-slice-context.md` would exceed the byte budget
- `transient-service-unavailable` — a high-confidence, current-attempt transient-outage signal alongside a terminal self-report; retrying the same slice in the same session is the fix, so this is steerable rather than a trust breach
- `idle-no-progress` — no visible pane progress across the observation ceiling is a stall, not evidence the developer session did anything wrong
- `ledger-retention` — a fresh repair result silently dropped a previously archived residual-finding or continuation-note item; exempt for the round immediately following a `context-budget` repair, which explicitly requires condensing and rewording ledger items

Terminal `needs-human` signatures (integrity/trust breaches — never steered, because continuing to reason from a context that already holds a false belief about reality is itself the risk):

- `integrity-head` — HEAD advance and descent from the slice starting commit is validated on git evidence alone, before any comparison with the self-reported hash, so a truthful report of a reset-to-unrelated HEAD still fails
- `slice-id-mismatch` — a result reported for the wrong slice
- `reviewer-unavailable` — an opt-in slice (`Independent audit required: yes`) with no reviewer made available; an operator/plan configuration mismatch that steering the developer cannot fix, so it stops for a human immediately rather than burning repair budget
- `prior-context-integrity` — the protected prior-slice-context digest no longer matches what was embedded at launch

A missing `developer-result.json` stays terminal `blocked` (a dead or unresponsive session is a runner condition, not a steerable content defect).

`repair` and all four of its fields are required on every active and terminal slice in schema v5. A first attempt records the explicit round-zero value rather than relying on a missing-field default.

Both execution paths drive the identical loop from this state — by construction.

**Batch path** (`run-next` / `run --scope remaining`): an in-process driver over the same start/wait/finalize primitives with a fixed no-judgment policy.

- Never interrupts a wait for hard-prompt or hard-stop-hint heuristics: their markers are broad substring matches that routinely occur in harness output, so the unconditional safety boundary is the send-time refusal to type into a session showing a hard prompt; the signals are still observed and recorded.
- Delivers in-session repair prompts itself, immediately.
- Converts timeout, interrupt, and unexpected exception into forced fail-closed terminal entries.
- Records `observation` operational events during polling, refreshes `observation-latest.json`, may briefly show run status `resuming` while an in-session repair round is live, and reaps stale run sessions at slice start.

**Model-supervised path:** spreads the same loop across separate invocations. On a repairable gate with budget remaining, `finalize-slice` does **not** force-stop the session, appends **no** slice entry, keeps `current_slice` populated (so `start-slice` still refuses a concurrent second attempt), records the new repair state, and returns `"finalized": false, "status": "repairable"` with a `mode` field:

- `"mode": "in-session"` — also returns `send_text`, a single-line pointer to the rendered `repair-prompt-repair-<round>.md`, and sets run status to `resuming` (send-eligible); the PM model delivers `send_text` with `send`, `wait`s for a fresh result, and finalizes again.
- `"mode": "fresh-session"` (circuit-breaker escalation) or `"mode": "relaunch"` (dead session) — `finalize-slice` has already force-stopped the old session and launched a new one itself with the frozen prompt, targeted repair instructions, archived-result paths, and the cumulative recovered residual-findings and continuation-notes ledgers. `start-slice` cannot be used because it refuses while `current_slice` is populated, and clearing `current_slice` would drop the breaker state, so the run is left at status `running`; the PM model just `wait`s and re-finalizes.

Invariants that hold across both paths, and across every round and relaunch:

- `current_slice.before_head` never changes, so verification stays cumulative.
- `current_slice.reviewer_tools` keeps the all-configured-tools reviewer gate enforced across invocations.
- `current_slice.reviewer_policy` preserves the exact PM-generated digest and normalized policy used to detect later mutation.
- A relaunch composes its harness launch from the current `finalize-slice` invocation's flags, so invoke `finalize-slice` with the same `--harness-command`/`--allow-profile-command`/model/effort flags used at `start-slice`.

On budget exhaustion, a tripped breaker, or an integrity gate, `finalize-slice` force-stops, appends the terminal entry, clears `current_slice`, and stops for a human as before. `run-next` and `run --scope remaining` refuse to start while any `current_slice` is populated, so a batch command cannot orphan a live model-supervised repair session.

`current_slice.pause` is either `null` or:

```json
{
  "paused_until": "2026-07-04T08:33:00Z",
  "reason": "rolling usage limit reset",
  "evidence_event_id": "op-0001"
}
```

## Slice Entry

Runtime slices append entries to `slices`:

```json
{
  "slice_id": "Slice 1",
  "title": "Define Skill Contract and Reference Docs",
  "status": "pass",
  "started_at": "2026-07-04T01:35:00Z",
  "completed_at": "2026-07-04T01:42:00Z",
  "artifact_dir": ".ai-pm/runs/20260704T013000Z/slices/slice-001",
  "before_head": "<full commit HEAD immediately before this slice ran>",
  "changed_files": ["<repo-relative path string, not an object>"],
  "summary": "Concise slice outcome",
  "validation": [],
  "drift_audit": {
    "verdict": "PASS",
    "path": ".ai-pm/runs/20260704T013000Z/slices/slice-001/drift-audit.md"
  },
  "code_review": {
    "verdict": "PASS",
    "path": ".ai-pm/runs/20260704T013000Z/slices/slice-001/code-review.md"
  },
  "audit_provenance": {
    "drift-audit": {
      "performed_by": "reviewer",
      "reviewer_tool": "claude",
      "reviewer_label": "01-claude-drift-audit",
      "fallback_context": null
    },
    "code-review": {
      "performed_by": "developer-self-audit",
      "reviewer_tool": null,
      "reviewer_label": null,
      "fallback_context": "no successful validated Reviewer PASS evidence exists for this audit; Developer performed the audit"
    }
  },
  "commit": {
    "requested": true,
    "created": true,
    "hash": "abc123"
  },
  "next_action": "",
  "blockers": [],
  "residual_findings": [],
  "continuation_notes": [],
  "slice_summary": ".ai-pm/runs/20260704T013000Z/slices/slice-001/slice-summary.md",
  "gate_reason": "all gates passed",
  "reviewer_tools": ["<tool names required for this slice attempt, empty if none>"],
  "reviewer_policy": {
    "sha256": "<digest of the exact PM-generated reviewer-policy.json>",
    "policy": {"<normalized policy object>": "<stored before developer launch>"}
  },
  "prior_slice_context": {
    "path": ".ai-pm/runs/20260704T013000Z/slices/slice-001/prior-slice-context.md",
    "sha256": "<digest PM generated before developer launch>"
  },
  "repair": {
    "round": 1,
    "last_signature": "validation",
    "signature_streak": 1,
    "session_generation": 1,
    "operational_round": 0
  }
}
```

`repair` is always present and records the final repair-loop state for the attempt that produced the entry. A slice accepted on its first attempt records the explicit round-zero state.

`audit_provenance` is required on every terminal schema-v5 slice entry and is derived by PM, never copied from Developer narration. Each audit is attributed to `reviewer` only when the latest successful exact launch contract for that audit matches the immutable policy snapshot, normalized `role: reviewer` / `access: read-only`, repository, tool/model/effort, process footprint, completion state, and helper-recorded `PASS`; its tool and label are retained. Without Reviewer proof, an audit is attributed to `developer-self-audit` only when the Developer result contains the corresponding audit entry and its non-empty artifact is inside the slice directory. Mixed execution is valid. Missing results, pre-audit stops, timeouts, and operator-attested `assumed-complete` entries use `not-observed` for any audit PM cannot prove occurred. `slice-summary.md`, `run-report.md`, and `summarize` always render the same provenance.

Completed statuses for slice selection are `pass` and `assumed-complete` (the latter written only by `init --assume-complete` as an operator attestation). The latest recorded outcome for a slice is authoritative: an earlier pass does not keep a slice complete if a later terminal outcome supersedes it. Any other authoritative status is not completed. Only an `assumed-complete` entry has null `before_head` and `artifact_dir` fields and omits runtime-only `reviewer_policy`, `slice_summary`, and `prior_slice_context` evidence; every slice PM actually runs records those fields.

For an active slice, `current_slice.prior_slice_context` protects the generated context artifact's repository-relative path and SHA-256. PM embeds the digest in the Developer prompt and independently reverifies it before finalization; a missing or changed artifact is terminal and cannot enter the repair loop. The rendered context is limited to 512 KiB. Before accepting any non-final slice, PM renders the exact projected cumulative context for the next launch; an oversized candidate result is repairable and must be condensed before acceptance, so already accepted immutable history cannot strand later work.

Every non-`assumed-complete` slice entry carries the same `prior_slice_context` forward from the attempt that produced it, so a later, separate `reconcile` invocation can re-verify the protected artifact's digest instead of trusting the stopped record unchecked. `reconcile` refuses to accept a slice — even one otherwise passing `verify_gate` — when this re-verification fails or the entry carries no such metadata at all, and separately refuses when accepting the result would exceed the next slice's context budget; both refusals are terminal (`needs-human` for an integrity mismatch, `blocked` for a budget overrun) and are never steered by `reconcile`, matching the runner's own gate.

Each slice artifact directory contains:

- PM-generated: `prior-slice-context.md`, the rendered `prompt.md`, authoritative `reviewer-policy.json`, `slice-summary.md`
- `model-identities.json`, `activity-attempt-<n>.jsonl`
- `pane-capture.txt`, plus `pane-capture-live-latest.txt` when live pane text was observed
- `observation-latest.json` when `observe`, `wait`, or batch polling has run
- `git-status-before.txt`, `git-status-after.txt`, `git-diff.patch`
- `validation-summary.md`, `drift-audit.md`, `code-review.md`
- optional: `reviewer-evidence.md`, `reviewer-runs-summary.json`, `reviewer-cancel-summary.json`, `pm-reconciliation.json` / `pm-reconciliation.md`
- `developer-result.json`, when the Developer reaches the structured result stage

**`prior-slice-context.md`.** Generated before prompt rendering from only the latest authoritative accepted outcomes of lower-numbered slices.

- Includes: provenance-labelled repository effects, validation and audit outcomes, repair metadata, Developer-reported continuation notes and residual findings, and evidence paths.
- Excludes: raw transcripts, pane text, diffs, operational events, credentials, reviewer policy internals, controller state, and superseded or non-passing outcomes.
- Its SHA-256 is embedded in the Developer prompt. It is historical data, never authorization or acceptance evidence.

**Identity evidence.** `harness.model_identity` and the per-slice identity artifact record a fresh launch-scoped identity state, including `catalog_verified: false` when no positive inventory check exists.

**Repair-round artifacts.** Repair rounds add `repair-prompt-repair-<n>.md` and archived `developer-result-repair-<n>.json` evidence; a relaunched harness receives `fresh-session-prompt-repair-<n>.md`, which combines the frozen prompt, targeted repair, and the cumulative residual-finding and continuation-note ledgers recovered from archived results. Retention of every recovered archived ledger item in the next passing result is mechanically enforced at `verify_gate` (signature `ledger-retention`), except for the round that follows a `context-budget` repair, which is exempt because that repair explicitly requires condensing and rewording ledger items.

**Run-level artifacts.** The run directory also contains a PM-generated `run-report.md`, refreshed with every run-state write, that groups repeated outcomes by slice, marks the final outcome authoritative, and aggregates gates, audit provenance, commits, blockers, next actions, continuation notes, and residual findings.

**Reviewer run directories.** Preserve the semantic request, copied policy, complete embedded skill prompt, normalized launch contract, resolved argv, enforced repository working directory, process status, stdout, stderr, helper-recorded audit verdicts, and actionable rejection feedback when validation fails. The latest chronologically completed verdict for each required audit is authoritative; a later non-PASS supersedes any earlier PASS. Terminal paths scan every slice's Reviewer-run directory so stale prior-slice processes are also cancelled. Timeout and failure paths preserve whatever capture and git evidence is available. Each activity log line is a JSON object with `checked_at`, `running`, and `active` fields.

Repair rounds add per-round artifacts keyed on the round number, so evidence from rounds sharing one session is never overwritten: `developer-result-repair-<round>.json` (the failing result, archived before deletion so the re-poll waits for a genuinely new result), `pane-capture-repair-<round>.txt`, `git-status-repair-<round>.txt`, `repair-prompt-repair-<round>.md` (plus `repair-prompt.md` as the latest), `pane-capture-repair-refused-<round>.txt` when repair delivery was refused by a hard prompt on screen, and one `"kind": "repair"` operational event per round recording the signature and delivery mode (`in-session`, `fresh-session`, or `relaunch`).

PM sets these environment variables for every slice harness:

- `PM_SLICE_ARTIFACT_DIR`
- `PM_PLAN_PATH`
- `PM_PRIOR_SLICE_CONTEXT_PATH`
- `PM_SLICE_ID`
- `PM_RESULT_SCHEMA_PATH`
- `PM_REVIEWER_JOBS_PATH`
- `PM_REVIEWER_ARTIFACT_ROOT`
- `ORCHESTRATOR_ARTIFACT_ROOT`
- `PM_SLICE_TMP_DIR`
- `TMPDIR`
- `PM_TOOL_HOME_ROOT`
- `COPILOT_HOME` when Copilot is a required reviewer and not the developer
- `CODEX_HOME` when Codex is a required reviewer and not the developer
- `PM_REVIEWER_POLICY_PATH` pointing at the current slice's authoritative `reviewer-policy.json`

PM does not set `CLAUDE_CONFIG_DIR` for Claude reviewers. Claude Code subscription OAuth is not portable by copying `.credentials.json` into an isolated config directory; use normal Claude Code auth, `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, or `CLAUDE_CODE_OAUTH_TOKEN` for unattended isolated auth.

## `developer-result.json`

Every developer session must write this file in the slice artifact directory:

```json
{
  "schema_version": 5,
  "slice_id": "Slice 1",
  "status": "pass",
  "summary": "",
  "changed_files": ["<repo-relative path string, not an object — a list of plain strings matching the actual diff, not {path, status, lines_added} or similar>"],
  "validation": [
    {
      "command": "",
      "result": "pass",
      "notes": ""
    }
  ],
  "drift_audit": {
    "verdict": "PASS",
    "path": ""
  },
  "code_review": {
    "verdict": "PASS",
    "path": ""
  },
  "commit": {
    "requested": true,
    "created": false,
    "hash": null
  },
  "next_action": "",
  "blockers": [],
  "residual_findings": [
    {
      "source": "code-review",
      "severity": "info",
      "location": "optional/path.py:12",
      "summary": "A concise post-plan consideration.",
      "disposition": "pre-existing",
      "rationale": "Why this does not block or belong to the current slice.",
      "suggested_follow_up": "What a later human or plan should consider."
    }
  ],
  "continuation_notes": [
    {
      "category": "interface-contract",
      "location": "optional/path.py:12",
      "summary": "The accepted implementation established a stable interface used by later slices.",
      "rationale": "Why later planned work needs this knowledge.",
      "applies_to": "Slice 2 and later integration work"
    }
  ]
}
```

`residual_findings` is required and must be `[]` when empty. Allowed sources are `implementation`, `validation`, `drift-audit`, `code-review`, `reviewer`, and `other`. Allowed dispositions are `deferred-inconsequential`, `pre-existing`, `unrelated-out-of-scope`, and `needs-follow-up`. It transports genuinely non-blocking post-plan considerations; it must not be used to convert a material defect introduced by the slice into a passing result merely because fixing it would require an out-of-contract change.

`continuation_notes` is required and must be `[]` when empty. It accepts at most 100 entries. Each entry has non-empty `category`, `summary`, `rationale`, and `applies_to` strings plus optional `location`, with every field limited to 4,000 characters. Allowed categories are `decision`, `implementation-lesson`, `failed-approach`, `interface-contract`, `validation-lesson`, `environment-tooling`, `reviewer-lesson`, `risk-warning`, and `future-slice-guidance`. It transports knowledge needed to execute later slices in the existing frozen plan: decisions and rationale, established contracts and invariants, discoveries, failed approaches, validation or tooling lessons, risks, and downstream guidance. It does not authorize new work. PM validates its shape and preserves it, but does not prove the semantics of Developer narration.

Allowed developer `status` values:

- `pass`
- `repairable`
- `needs-human`
- `fail`
- `blocked`

PM verifies this result against git state, artifacts, validation output, drift audit, code review, and commit state before accepting a slice.

Reviewer use is a degradable preference by default, not an acceptance gate: making a Reviewer available (`--reviewer-tools`) lets the Developer request the drift audit and code review as an independent second opinion, but local Developer self-audit is a valid `pass` and is reported explicitly. A slice opts in to mechanical enforcement with `Independent audit required: yes` in its Risk Flags. Only for such an opt-in slice does PM additionally require mechanical evidence that every configured tool launched under the current slice's deterministic policy and finished before it will accept a `pass`, and that separate successful launch contracts carry exactly `required_skills: ["drift-audit"]` and `required_skills: ["code-review"]`: a non-empty `reviewer-evidence.md`, plus `reviewer-runs-summary.json` containing one or more manifests whose passing `launch_contract` records collectively match every configured tool, both required audits, the exact stored and on-disk policy digest/content, slice/plan identity, required model/effort, intrinsic Reviewer/read-only role and access, repository, and enforced working directory, backed by a recorded positive-integer subprocess `pid` and `outfile`/`errfile` that actually exist inside `reviewer_artifact_root`, with each corresponding status `completed` and returncode 0. This is an existence-and-consistency check — it raises the cost of casual forgery, not proof that a matching process ran; PM does not verify process identity, the status file's recorded `child_identity`, or the manifest's command, and it does not claim to. That is consistent with the documented threat model (`docs/VISION.md`), which excludes a determined adversary fabricating coherent evidence. On an opt-in slice, a raw harness invocation, one generic Reviewer launch, matching executable without a validated contract, mutated policy, wrong repository/CWD, crashed/cancelled/running Reviewer, missing tool or audit purpose, no Reviewer made available at all, hand-authored manifest/status pair with no real launch footprint, or prose-only claim fails the repairable `reviewer-evidence` gate; on a default slice, these do not block acceptance but cannot receive Reviewer provenance. Available Reviewer tools and the policy snapshot remain persisted in `current_slice` and each terminal slice entry for out-of-process finalization and reconciliation. The policy digest is bound to the slice attempt's starting commit and repair round (`before_head`, `session_generation`, `repair_round`, `operational_round`) and is rewritten in both `reviewer-policy.json` and the persisted `current_slice.reviewer_policy` snapshot at the start of every repair round, so a launch contract's `policy_sha256` from before the latest repair round no longer matches and cannot satisfy this gate. `operational_round` carries this binding specifically for `idle-no-progress`/`transient-service-unavailable` rounds, which deliberately leave `repair_round` (and, for an in-session repair, `session_generation`) unchanged so they don't consume the substantive repair budget — without `operational_round` in the digest, those rounds would rewrite byte-identical policy JSON and leave a prior launch contract's `policy_sha256` still matching.

On an opt-in slice, `reviewer-policy.json` also carries `reserved_skill_sets: [["drift-audit"], ["code-review"]]` (empty on a default slice) as a pre-launch companion to the finalize-time check above: `reviewer_jobs.py launch` rejects, before any process starts, a request whose `required_skills` names `drift-audit` or `code-review` without matching one of those sets exactly — for example `["drift-audit", "code-review"]` in one request. This closes the mixed/misnamed case mechanically; it cannot catch an empty `required_skills` on a request that was meant to be one of the audits, since an empty list is also the valid shape for an unrelated ad hoc reviewer task, and the finalize-time check above remains the actual backstop for that gap.

When all authorization, validation, drift, review, changed-file, ancestry, and clean-worktree evidence passes but `commit.hash` is wrong or abbreviated, PM may reconcile that evidence field to the proven current `HEAD`, write `pm-reconciliation.json` / `pm-reconciliation.md`, update `developer-result.json`, and accept the slice. This reconciliation is limited to commit-hash evidence; it must not mask unauthorized files, missing validation, failed audits/reviews, dirty worktrees, or missing commits.
