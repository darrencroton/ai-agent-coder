# Project Manager

Project Manager (PM) supervises execution of an already-approved implementation plan. It is not a planner and it is not an implementer. It runs one frozen slice at a time through an AI coding harness, records durable artifacts, and verifies gates from outside the harness session. When verification finds a fixable gap, PM runs a bounded self-correcting repair loop: it surfaces the specific violation back into the live developer session (preserving the context the developer already built), lets it fix the gap, and re-verifies with the complete, unrelaxed gate. Only integrity breaches, an exhausted repair budget, a tripped same-signature circuit breaker, or policy-required approvals stop the run for a human.

The three roles: **PM** is the deterministic supervisor — it owns run state, Reviewer policy, and gates, steers repairs, never writes slice code, and never delegates to a Reviewer itself. The **slice Developer** is the harness in tmux executing one slice (scoped-implementation → validation → drift-audit → code-review → commit); it owns implementation, tests, fixes, and commits, but reports to PM for acceptance. A **Reviewer** is a read-only evidence provider requested semantically by the Developer and launched through `orchestrator`'s deterministic policy/request interface; it may investigate, gather evidence, perform drift audit, and perform code review, but never edits, runs mutation-prone tests, performs Git/GitHub mutations, owns gates, commits, or re-delegates. In model-supervised operation a fourth seat, the **supervising model**, drives PM's commands and owns operational judgment only — never acceptance (see "Roles and Topology" in `SKILL.md`).

PM is one mode with a supervision dial, not two modes. Model-supervised operation is the default: the PM model stays in the loop for live operational judgment while deterministic commands own state transitions and gates. The fail-closed `run-next` and `run --scope remaining` paths are the unattended batch style — the fallback when no supervising model is available or a short conservative run is enough; they stop at the first operational ambiguity by design. Acceptance gates are identical in both styles. The current implementation provides contract docs, whole-plan sanity checking, durable run state, conservative plan discovery, tmux-backed slice execution, model-supervised observe/send/start/wait/pause/finalize/stop primitives, structured result capture, fail-closed gate verification, looping over remaining slices, cancellation, and summaries.

Runtime requirement: Python 3.13 or newer. PM uses Python 3.13's segment-aware `pathlib` glob matching for its authorization gate.

## What PM Owns

- Creating an auditable `.ai-pm/runs/<timestamp>/run.json` mirror and, before the first harness launch, an undisclosed controller-owned state copy outside the worktree.
- Updating `.ai-pm/current` to the active run.
- Recording repo, branch, harness, plan, environment preflight, and policy.
- Parsing implementation-plan markdown conservatively enough to identify frozen slice contracts.
- Sanity-checking the whole plan before a run begins (`check-plan`, also run automatically at `init`): every slice's required sections, authorized surface, and approval flag — including rejecting fenced or malformed slice-like headings and unusable path entries — plus lint warnings for dependency/license-shaped authorized files, whole-repo surfaces, plain entries that name existing directories, and Mode A-only batch groupings.
- Refusing incomplete, ambiguous, approval-gated, or unauthorized slices.
- Reporting the next eligible slice in `run-next --dry-run`.
- Running one eligible slice with `run-next`.
- Running eligible slices sequentially with `run --scope remaining`.
- Generating a bounded, read-only `prior-slice-context.md` before every slice so a fresh Developer receives authoritative accepted outcomes, repository effects, validation and audit history, structured decisions and lessons, failed approaches, interface/tooling discoveries, risks, and residual findings from earlier slices.
- Capturing prompt, pane output, git status, git diff, validation, drift audit, code review, structured continuation notes and residual findings, per-slice summaries, and the aggregate run report.
- Writing `reviewer-policy.json`, embedding the orchestrator contract into the slice prompt, and verifying validated reviewer-launch evidence without composing reviewer harness commands itself.
- Recording supervision state and append-only operational event logs for model-supervised runs.
- Verifying developer claims against git evidence, classifying every non-pass gate outcome with a stable failure signature as repairable or terminal.
- Driving the bounded repair loop: archiving the stale result, steering the live session with a targeted correction (or relaunching a fresh session per the circuit breaker), re-verifying with unrelaxed gates, and enforcing the repair budget from persisted state.
- Stopping for a human on integrity breaches (HEAD not advanced or not descended from the slice start, wrong slice reported), exhausted repair budget, a tripped circuit breaker, or approval-gated slices.

## What PM Does Not Own

- Creating or repairing implementation plans.
- Docker or container setup.
- Semantic code review inside PM.
- Dependency, license, remote push, PR, release, or deployment actions.
- Bypassing human approval for approval-gated work.
- Inferring authorization when plan sections are missing.
- Accepting a slice from screen text, transcript claims, or operational hints without deterministic gate evidence.

## CLI

Sanity-check a plan before starting (also runs automatically at `init`; errors fail init, warnings print). `--repo` (default: the current directory) lets the check lint authorized entries against the worktree — for example a plain entry that names an existing directory; outside a git repository the plan-intrinsic checks still run:

```bash
python3 skills/project-manager/scripts/pm.py check-plan --plan /path/to/plan.md --repo /path/to/repo
```

Initialize a run:

```bash
python3 skills/project-manager/scripts/pm.py init \
  --repo /path/to/repo \
  --plan /path/to/plan.md \
  --harness codex
```

Initialize on an explicitly authorized branch, creating it when missing:

```bash
python3 skills/project-manager/scripts/pm.py init \
  --repo /path/to/repo \
  --plan /path/to/plan.md \
  --harness codex \
  --branch pm-trial/pi-calculator \
  --create-branch
```

Initialize while adopting slices the operator attests were already completed and committed under a previous run (for example after a plan revision forced a fresh init), and/or with an explicit repair budget:

```bash
python3 skills/project-manager/scripts/pm.py init \
  --repo /path/to/repo \
  --plan /path/to/plan.md \
  --harness codex \
  --assume-complete "Slice 1,Slice 2" \
  --max-repair-attempts 3
```

Record operator approval for an approval-gated slice so the run can proceed without editing the frozen plan:

```bash
python3 skills/project-manager/scripts/pm.py approve \
  --repo /path/to/repo \
  --slice "Slice 3" \
  --reason "risk reviewed with operator"
```

Check state (`status` also warns when an active run's tmux session has vanished — the signature of a controlling command killed mid-run):

```bash
python3 skills/project-manager/scripts/pm.py status --repo /path/to/repo
python3 skills/project-manager/scripts/pm.py summarize --repo /path/to/repo
```

Every run-state write refreshes `.ai-pm/current/run-report.md`. Each terminal slice also gets `slice-summary.md`. These deterministic reports carry gate results, commits, blockers, next actions, continuation notes, residual findings, and PM-derived provenance for each audit: `reviewer` with the successful tool/label, `developer-self-audit` only when the Developer result and non-empty in-slice audit artifact prove it occurred, or `not-observed` when neither evidence path exists (including timeouts, missing results, pre-audit stops, and operator-attested pre-run completion). Before a later slice starts, PM selects only the latest authoritative outcome for each earlier slice, orders it numerically, and renders its accepted history into that new slice's `prior-slice-context.md`; superseded or terminal outcomes are retained for audit but are not presented as accepted implementation truth. Each slice gets a newly rendered, launch-time cumulative snapshot rather than a shared mutable file. PM protects its path and digest in `current_slice`, reverifies it before finalization, and limits the artifact to 512 KiB; continuation reporting is limited to 100 notes with 4,000 characters per field. Before accepting a non-final slice, PM projects the exact next cumulative artifact and makes an oversized result repairable, preventing immutable accepted history from stranding later work. While a harness is live, the outside-worktree copy is Mode B's controller authority and `.ai-pm/current/run.json` is its auditable mirror; a mismatch fails normal commands closed, while `stop-with-evidence` can recover from the isolated copy. This protects against normal workspace edits and weak-model corruption, not a same-user unsandboxed process deliberately searching outside the worktree.

Repeated terminal outcomes for the same slice are grouped together in the run report. Earlier outcomes remain visible as superseded evidence and the final recorded outcome is marked authoritative; aggregate residual findings, blockers, and next actions come from the authoritative outcome.

List harness launch mechanics:

```bash
python3 skills/project-manager/scripts/pm.py profiles
```

Preflight the next slice before starting a tmux harness:

```bash
python3 skills/project-manager/scripts/pm.py preflight \
  --repo /path/to/repo \
  --reviewer-tools copilot \
  --allow-profile-command
```

Preview the next runnable slice:

```bash
python3 skills/project-manager/scripts/pm.py run-next --repo /path/to/repo --dry-run
```

Run the next eligible slice:

```bash
python3 skills/project-manager/scripts/pm.py run-next \
  --repo /path/to/repo \
  --reviewer-tools copilot \
  --allow-profile-command
```

Run with explicit developer and reviewer model/effort requests:

```bash
python3 skills/project-manager/scripts/pm.py run-next \
  --repo /path/to/repo \
  --harness-model <model> \
  --harness-effort <effort> \
  --reviewer-tools codex \
  --reviewer-model <model> \
  --reviewer-effort <effort> \
  --allow-profile-command
```

Run eligible slices until all are complete or a stop condition is reached:

```bash
python3 skills/project-manager/scripts/pm.py run \
  --repo /path/to/repo \
  --scope remaining \
  --reviewer-tools copilot \
  --allow-profile-command
```

Start the next eligible slice in model-supervised mode and return immediately:

```bash
python3 skills/project-manager/scripts/pm.py start-slice \
  --repo /path/to/repo \
  --reviewer-tools copilot \
  --allow-profile-command
```

Observe or wait on the active model-supervised slice without finalizing gates:

```bash
python3 skills/project-manager/scripts/pm.py observe --repo /path/to/repo
python3 skills/project-manager/scripts/pm.py wait --repo /path/to/repo --seconds 120 --poll-seconds 2
```

Send a short literal instruction to the active slice session:

```bash
python3 skills/project-manager/scripts/pm.py send \
  --repo /path/to/repo \
  --text "You were interrupted. Review what you were doing then continue." \
  --reason "resume after rolling usage reset"
```

Pause until an explicit timestamp with timezone, then return control for re-observation:

```bash
python3 skills/project-manager/scripts/pm.py pause-until \
  --repo /path/to/repo \
  --until 2026-07-05T18:30:00+10:00 \
  --buffer-seconds 180 \
  --reason "rolling usage reset"
```

Finalize or stop the active model-supervised slice:

```bash
python3 skills/project-manager/scripts/pm.py finalize-slice --repo /path/to/repo
python3 skills/project-manager/scripts/pm.py stop-with-evidence \
  --repo /path/to/repo \
  --reason "weekly usage cap shown on screen"
```

Cancel a run and record the reason:

```bash
python3 skills/project-manager/scripts/pm.py stop --repo /path/to/repo --reason "manual stop"
```

Archive sensitive reviewer state from a completed or stopped run:

```bash
python3 skills/project-manager/scripts/pm.py archive-sensitive --repo /path/to/repo --dry-run
python3 skills/project-manager/scripts/pm.py archive-sensitive --repo /path/to/repo
```

`run-next --dry-run` exits successfully only when the next uncompleted slice has the required sections, has a non-empty authorized file surface, and is not approval-gated. Runtime execution also requires a clean target worktree outside `.ai-pm/`.

## Internal Layout

`scripts/pm.py` is intentionally a thin executable wrapper. The implementation lives in `scripts/pm_lib/`, grouped by responsibility: CLI construction, command handlers, plan parsing, run state, git/process helpers, harness profiles, tmux control, runtime artifacts, and gate verification. When a stronger deterministic contract replaces an older path, remove the obsolete path and update tests/docs together rather than preserving ambiguous compatibility.

## Default PM Execution Flow

`SKILL.md` is the source of truth for the default operating path and holds the single authoritative Mode B launcher (this README previously duplicated the operating path and the two copies drifted). In short: **model-supervised operation is the default**; use the **unattended batch style** when no supervising model is available or a conservative fail-closed run is enough; follow the step sequences in `SKILL.md` → "Default Operating Path" and the launcher in `SKILL.md` → "Launcher". Do not ask users to hand-compose harness sandbox, model, or effort flags — use `profiles`, `preflight`, `--reviewer-tools`, `--harness-model`, `--harness-effort`, `--reviewer-model`, `--reviewer-effort`, and `--allow-profile-command`.

Two operational rules worth repeating here because they bite in practice (details in `SKILL.md` → "Long-Running Command Discipline"):

- PM's blocking commands outlive assistant tool-call limits. Run `run`/`run-next` in the background and poll `status`; in model-supervised mode use repeated bounded `wait` calls, not one long wait.
- For model-supervised runs on subscription harnesses, put the PM model on a different provider than the developer harness so one usage window cannot stall both.
- Keep live PM observation/control calls separate from supplementary commands that may need approval; an approval delay does not pause the harness.

`observe` and `wait` expose `operational_hints` in their JSON output. These hints summarize common pane/transcript evidence such as rolling usage limits, weekly/monthly/account limits, service unavailable messages, network transients, auth/trust/permission prompts, external side-effect requests, idle/no-progress, result-ready, and process-exited-without-result. Ordinary hints are evidence for the PM model, not commands. Hard-stop hints are deterministic guards: `send`, `pause-until`, and wait/retry/resume paths refuse unattended continuation when weekly, monthly, account, billing, unknown-limit, auth, trust, permission, or external-side-effect evidence is present.

PM automatically routes only an explicit high-confidence transient service-unavailable terminal report into the existing repair loop. Generic server/network text is not enough. Repeated idle observations spanning the configured ten-minute ceiling likewise use the existing repair signature streak: automatic in-session nudge, fresh-session retry, then terminal circuit breaker. This is operational recovery only; acceptance still requires every normal gate.

Every idle escalation requires a fresh ten-minute no-progress window after the previous repair/send boundary. The first slice freezes the complete run launch configuration, including developer and reviewer models/effort. Later slices and wait-triggered fresh sessions inherit it when flags are omitted; conflicting reconfiguration fails closed. Model identity evidence is refreshed and tagged to every slice launch instead of carrying a stale prior-slice check.

Reset parsing is intentionally narrow. Relative durations such as `try again in 3 hours` are preferred. Absolute local reset times are accepted only when they are unambiguously near-future in the controller timezone or include an explicit timezone; otherwise PM reports an unknown-limit hard stop for human judgment.

## Profiles and Launch Requirements

PM stores one mechanical profile per tool. Codex, Claude, Copilot, and OpenCode are equally eligible for Developer or Reviewer selection; the profile does not rank models, encode role suitability, or reject an operator's choice. It records command composition, authentication handling, readiness evidence, monitoring behavior, and the factual strength or limits of read-only enforcement. The user chooses the tool/model through the launcher or plan.

At runtime, PM composes the launch command from the selected harness plus explicit requirements:

- `--reviewer-tools copilot` tells PM the slice will use a Copilot reviewer and the harness needs reviewer-compatible setup.
- `--allow-profile-command` tells PM to use the tested profile command instead of requiring a hand-written `--harness-command`.
- `--allow-unattended-default` opts in to the harness profile's known unattended-safe base command without full profile composition (no model/effort/reviewer flags). Like `--allow-profile-command`, it disables per-action approval, so PM's post-hoc gates are the safety boundary; without any of the three launch flags, a bare harness name fails closed rather than deadlocking on an approval prompt.
- `--harness-model` and `--harness-effort` are composed by PM only when the selected developer profile supports them.
- When a profile exposes a model inventory, `preflight` and slice start require the exact requested id to exist. OpenCode also compares the inventory-provided display name with its ready-state TUI before prompt injection, preventing its known silent fallback behavior.
- `--reviewer-model` and `--reviewer-effort` are rendered into the slice prompt with per-tool command guidance. If the selected Reviewer cannot launch or honor its authentication/model/effort contract, the Developer preserves the exact failure in `reviewer-evidence.md`. On a default slice the Developer then self-audits and documents that fallback; only a slice marked `Independent audit required: yes` must stop because validated Reviewer evidence is mandatory.
- `commit_required=true` in run policy tells the Codex profile to add scoped git-directory access for local commits.

This avoids a large matrix of incomplete names such as `codex-copilot-commit`. Role selection remains an explicit run choice, and the slice requirements stay visible on the command line.

## Run State

State is stored under the target repository:

```text
.ai-pm/
  current -> runs/<timestamp>
  runs/
    <timestamp>/
      run.json
      operational-events.jsonl
      slices/
        slice-001/
          prompt.md
          activity-attempt-1.jsonl
          pane-capture.txt
          pane-capture-live-latest.txt
          reviewer-runs/
          tmp/
          tool-homes/
          copilot-home/
          prior-slice-context.md
          git-status-before.txt
          git-status-after.txt
          git-diff.patch
          validation-summary.md
          drift-audit.md
          code-review.md
          reviewer-evidence.md
          developer-result.json
```

PM does not edit the project's own `.gitignore`. Instead, `init` writes a self-ignoring `.ai-pm/.gitignore` containing `*`, so the audit directory — including seeded reviewer credentials and full transcripts — is never staged by a stray `git add -A`. PM's own dirty-tree and changed-file checks already exclude `.ai-pm/`.

Each `activity-attempt-<n>.jsonl` line records `checked_at`, `running`, and `active` fields from the tmux pane activity check. `pane-capture-live-latest.txt` preserves the last live pane text seen during polling, which is useful when the final pane capture is unavailable after a fast harness exit. Batch polling also records `observation` operational events to `operational-events.jsonl` (on state change, with a 60-second floor while nothing changes) and refreshes `observation-latest.json` — the same evidence the model-supervised `observe`/`wait` primitives produce.

`run.json` includes a required `supervision` object with pause/retry policy, pause budgets, and the default continuation prompt. PM supports only the complete current schema-v4 shape and fails closed with a fresh-init instruction when durable state is missing or uses another version. High-frequency model-supervised observations and actions belong in `operational-events.jsonl`, an append-only log, rather than repeated `run.json` rewrites.

While a slice is running, `current_slice` records the slice id, title, artifact directory, tmux session, attempt, start time, `before_head`, a `repair` object ({round, last_signature, signature_streak, session_generation} — the persisted repair-loop and circuit-breaker state), an optional `developer_session_id` for transcript lookup, and an optional `pause` object. Persisting `before_head` is required for model-supervised finalization because changed-file verification must compare against the real slice start, not guess `HEAD^`; it stays fixed across repair rounds and relaunches so verification remains cumulative. Repair rounds add per-round artifacts (`developer-result-repair-<n>.json`, `repair-prompt-repair-<n>.md`, `pane-capture-repair-<n>.txt`, `git-status-repair-<n>.txt`) beside the standard slice artifacts. See `references/run-state-schema.md` for the full semantics.

Reviewer state and temporary files should stay under the slice artifact directory. PM exports fixed paths for reviewer runs, prior-slice context, temporary files, and tool-specific home directories so developers do not have to invent locations. Historical context is explicitly data rather than instruction: the current frozen contract always wins, the artifact cannot expand scope, and Developer-authored lessons are labelled separately from PM-verified repository and gate facts.

## Plan Eligibility

PM expects implementation-plan slice sections with these headings:

- `### Intended Change`
- `### Acceptance Criteria`
- `### Authorized Surface`
- `### Explicit Non-Goals`
- `### Risk Flags`
- `### Validation Plan`
- `### Rollback Path`

The parser fails closed when a slice-like heading is malformed or sits inside a fenced code block (including an unclosed fence), when a required section is missing, when no usable repository-relative paths are listed under `Files allowed to change` (entries with unwrapped whitespace — usually a trailing annotation — are unusable; backtick-wrap the path to annotate it), or when `Approval needed before implementation` is anything other than an exact `no` (a prefix like "not yet decided" or "none" is treated as unresolved, not as "no", and stops the run). Heading text beginning with the standalone word `Slice` is reserved for machine-consumed slice headings, except `Slice Batches`. `check-plan` applies these checks to every slice at once — plus surface lint for dependency/license-shaped files, whole-repo globs, top-level-only `*`, plain entries that name existing directories, and batch groupings — so a defect in slice 5 stops the operator at init, not twenty minutes into the run.

An explicit `yes` approval flag can be cleared at runtime with `approve --slice "<Slice N>" --reason "<why>"`, which records the operator's approval in run state and the operational event log — the plan file itself stays frozen. A missing or unclear flag cannot be approved away; that is a planning defect to fix in the plan (which then requires a fresh `init`, using `--assume-complete` to adopt slices already completed under the previous run).

Authorized file entries are matched with segment-aware globbing: a plain path matches exactly (never beneath a directory — write `docs/` rather than `docs` for a subtree), a trailing `/` matches everything under a directory, and a `*`/`?` glob matches within a single path segment (so `*.md` authorizes only top-level markdown). Use `**` explicitly for a recursive match such as `docs/**/*.md`.

The plan is frozen at `init` by content digest. If the plan file changes mid-run, PM stops before the next slice; a revised plan requires a fresh `init`. Duplicate `## Slice N:` numbers are rejected at `init`, and each runtime slice re-checks that the current branch still matches the branch captured at `init`. `init --branch <name>` records and switches to an intended branch before the run; `--create-branch` creates it only when explicitly requested and only from a clean worktree.

## Model-Supervised State Contract

The model-supervised transition adds these durable concepts without changing deterministic gate acceptance:

- `supervision.mode`: defaults to `deterministic-batch`; `start-slice` sets it to `model-supervised`, and the batch driver re-asserts `deterministic-batch` at each slice start.
- `supervision.pause_policy`: names recoverable rolling-window and transient-service handling while preserving hard stops for weekly/account/unknown events.
- `supervision.pause_counters`: tracks consecutive pauses for the current slice and cumulative paused seconds for the run.
- `operational_events_path`: points at the append-only JSONL event log for observations, sends, waits, pauses, resumes, and stops.
- `current_slice.before_head`: records the commit at slice start for out-of-process finalization.
- `current_slice.developer_session_id`: records the launched Claude session id when PM composed one for transcript capture.
- `current_slice.pause`: records `paused_until`, `reason`, and an evidence event id when a bounded pause is active.
- `operational_hints`: appears in observation JSON with `kind`, optional `subtype`, confidence, reset/retry fields when parseable, `hard_stop`, evidence excerpt, source, detection time, and recovery guidance.

The model-supervised primitives are `observe`, `send`, `wait`, `pause-until`, `start-slice`, `finalize-slice`, and `stop-with-evidence`. They must not accept work by interpreting natural-language output; they only provide operational control and evidence capture before deterministic gates run.

## Privacy and Data Flows

Everything PM produces is local: run state, artifacts, transcripts, and reviewer evidence live under `.ai-pm/` in the target repository, PM's deterministic tools make no network calls, and there is no telemetry. What leaves the machine is determined entirely by which models sit in which seats:

| Seat | Sees | Leaves the machine when |
|---|---|---|
| Slice Developer | The full repository, the plan, embedded skill instructions | Its model is a hosted API or subscription service |
| Reviewer | The files named in its request, plus embedded reviewer/skill instructions | Its model is hosted |
| Supervising model | `observe`/`wait`/`summarize` output: pane excerpts, operational hints, flagged reviewer-output tails — **which can include fragments of the code under work** | It is a hosted assistant |
| PM deterministic tools | Local git, tmux, and files | Never |

The supervising seat is the one people forget: running local developer and reviewer models but driving PM from a hosted assistant still sends code fragments to that assistant's provider through observation evidence. Two configurations keep everything on-machine:

1. **Fully local supervision** — local models in all three seats (for example a local-model harness as developer and reviewer, with a local model driving PM's commands).
2. **No supervising model** — run the unattended batch style (`run --scope remaining`) from a shell; only the developer and reviewer seats then matter, and you are the fallback when it fail-closed stops.

Artifact sensitivity, for cleanup and sharing decisions (per-slice under `.ai-pm/runs/<run-id>/slices/<slice>/`):

| Artifact | Contains |
|---|---|
| `pane-capture*.txt`, `transcript.txt` | Full session output: code, diffs, command output |
| `git-diff.patch` | The complete code change |
| `reviewer-runs/` | Reviewer prompts and outputs, including code and embedded instructions |
| `validation-summary.md`, `drift-audit.md`, `code-review.md` | Code excerpts and findings |
| `slice-summary.md`, `../run-report.md` | Gate outcomes, per-audit Reviewer/Developer provenance, commit hashes, blockers, next actions, and residual post-plan considerations |
| `prompt.md`, `developer-result.json`, `git-status-*.txt` | Plan text, file paths, verdicts, commit hashes |
| `tool-homes/`, `codex-home/`, `copilot-home/` | **Seeded reviewer credentials** — marked sensitive; move them out of a finished run with `archive-sensitive` |
| `../operational-events.jsonl` (per run) | Operational events with pane-excerpt evidence fields |

`init` writes a self-ignoring `.ai-pm/.gitignore` so none of this — credentials and transcripts included — can be staged by a stray `git add -A`.

## Verify Your Setup (Safe Local Trial)

This is the "verify your setup" step the top-level README's Quickstart points at. Use a temporary git repo and a small plan before supervising real work. The local harness below writes the same structured artifacts expected from an AI developer, commits only the authorized file, and then waits long enough for PM to capture the tmux pane:

```bash
tmp="$(mktemp -d)"
git -C "$tmp" init
git -C "$tmp" config user.email pm-test@example.invalid
git -C "$tmp" config user.name "PM Test"
cat > "$tmp/plan.md" <<'PLAN'
# Toy Plan

## Slice 1: Add docs

### Intended Change
- Add README content.

### Acceptance Criteria
- Dry run identifies this slice.

### Authorized Surface
- Files allowed to change:
  - README.md
- Functions/classes/components allowed to change: none.
- Tests allowed or expected to change: none.

### Explicit Non-Goals
- Do not change runtime code.

### Risk Flags
- Risky surfaces touched: none.
- Approval needed before implementation: no.

### Validation Plan
- Commands to run:
  - git diff --check

### Rollback Path
- Revert README.md.
PLAN
touch "$tmp/seed.txt"
git -C "$tmp" add plan.md seed.txt
git -C "$tmp" commit -m "Seed toy repo"
cat > "$tmp/fake_harness.py" <<'PY'
import json
import os
import subprocess
import time
from pathlib import Path

artifact = Path(os.environ["PM_SLICE_ARTIFACT_DIR"])
Path("README.md").write_text("toy slice complete\n", encoding="utf-8")
subprocess.run(["git", "add", "README.md"], check=True)
subprocess.run(["git", "commit", "-m", "Complete toy slice"], check=True)
commit_hash = subprocess.run(["git", "rev-parse", "HEAD"], check=True, text=True, stdout=subprocess.PIPE).stdout.strip()
(artifact / "validation-summary.md").write_text("PASS\n", encoding="utf-8")
(artifact / "drift-audit.md").write_text("PASS\n", encoding="utf-8")
(artifact / "code-review.md").write_text("PASS\n", encoding="utf-8")
(artifact / "developer-result.json").write_text(json.dumps({
    "schema_version": 4,
    "slice_id": "Slice 1",
    "status": "pass",
    "summary": "toy slice complete",
    "changed_files": ["README.md"],
    "validation": [{"command": "toy validation", "result": "pass", "notes": ""}],
    "drift_audit": {"verdict": "PASS", "path": "drift-audit.md"},
    "code_review": {"verdict": "PASS", "path": "code-review.md"},
    "commit": {"requested": True, "created": True, "hash": commit_hash},
    "next_action": "",
    "blockers": [],
    "residual_findings": [],
    "continuation_notes": []
}), encoding="utf-8")
time.sleep(5)
PY
python3 skills/project-manager/scripts/pm.py init --repo "$tmp" --plan "$tmp/plan.md" --harness codex
python3 skills/project-manager/scripts/pm.py run-next --repo "$tmp" --dry-run
python3 skills/project-manager/scripts/pm.py run-next --repo "$tmp" --harness-command "python3 $tmp/fake_harness.py" --timeout-seconds 10 --poll-seconds 0.1
python3 skills/project-manager/scripts/pm.py summarize --repo "$tmp"
```

This trial creates only `.ai-pm/` state and the authorized toy commit inside the temporary repo. The `--harness-command` override is for controlled local validation; normal runs use the command named by `--harness`.

To trial model-supervised usage-limit handling, reuse the same temporary repo setup and plan, then replace the harness with one that waits for the continuation prompt before writing a result:

```bash
cat > "$tmp/usage_limit_resume_harness.py" <<'PY'
import json
import os
import subprocess
import sys
import termios
import time
from pathlib import Path

artifact = Path(os.environ["PM_SLICE_ARTIFACT_DIR"])
attrs = termios.tcgetattr(sys.stdin)
attrs[3] = attrs[3] & ~(termios.ECHO | termios.ICANON)
attrs[6][termios.VMIN] = 0
attrs[6][termios.VTIME] = 1
termios.tcsetattr(sys.stdin, termios.TCSANOW, attrs)
time.sleep(2.5)
print("\033[2J\033[HUsage limit reached. Try again in 1 minute.", flush=True)
seen = ""
deadline = time.monotonic() + 12
while time.monotonic() < deadline:
    chunk = os.read(sys.stdin.fileno(), 4096).decode(errors="ignore")
    if chunk:
        seen += chunk
    if "You were interrupted. Review what you were doing then continue." in seen:
        break
else:
    raise SystemExit(3)

Path("README.md").write_text("resumed after rolling limit\n", encoding="utf-8")
subprocess.run(["git", "add", "README.md"], check=True)
subprocess.run(["git", "commit", "-m", "Complete resumed slice"], check=True)
commit_hash = subprocess.run(["git", "rev-parse", "HEAD"], check=True, text=True, stdout=subprocess.PIPE).stdout.strip()
(artifact / "validation-summary.md").write_text("PASS\n", encoding="utf-8")
(artifact / "drift-audit.md").write_text("PASS\n", encoding="utf-8")
(artifact / "code-review.md").write_text("PASS\n", encoding="utf-8")
(artifact / "developer-result.json").write_text(json.dumps({
    "schema_version": 4,
    "slice_id": "Slice 1",
    "status": "pass",
    "summary": "resumed after rolling limit",
    "changed_files": ["README.md"],
    "validation": [{"command": "toy validation", "result": "pass", "notes": ""}],
    "drift_audit": {"verdict": "PASS", "path": "drift-audit.md"},
    "code_review": {"verdict": "PASS", "path": "code-review.md"},
    "commit": {"requested": True, "created": True, "hash": commit_hash},
    "next_action": "",
    "blockers": [],
    "residual_findings": [],
    "continuation_notes": []
}), encoding="utf-8")
time.sleep(2)
PY
python3 skills/project-manager/scripts/pm.py init --repo "$tmp" --plan "$tmp/plan.md" --harness codex
python3 skills/project-manager/scripts/pm.py start-slice --repo "$tmp" --harness-command "python3 $tmp/usage_limit_resume_harness.py"
python3 skills/project-manager/scripts/pm.py wait --repo "$tmp" --seconds 3 --poll-seconds 0.1
python3 skills/project-manager/scripts/pm.py pause-until --repo "$tmp" --until "$(python3 - <<'PY'
from datetime import datetime, timezone
print(datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"))
PY
)" --buffer-seconds 0 --reason "rolling usage reset"
python3 skills/project-manager/scripts/pm.py send --repo "$tmp" --text "You were interrupted. Review what you were doing then continue." --reason "resume after rolling usage reset"
python3 skills/project-manager/scripts/pm.py wait --repo "$tmp" --seconds 10 --poll-seconds 0.1
python3 skills/project-manager/scripts/pm.py finalize-slice --repo "$tmp"
python3 skills/project-manager/scripts/pm.py summarize --repo "$tmp"
```

The first `wait` should show a non-hard-stop rolling-window usage hint with recovery guidance to pause and send a continuation prompt. `pause-until` records the bounded pause, `send` delivers only the continuation text to the current slice session, and `finalize-slice` still requires the normal validation, drift-audit, code-review, commit, and clean-worktree evidence before accepting the slice.

To trial the exited-process usage-limit path, use a fresh temporary repo from the setup above and a harness that exits before creating `developer-result.json`:

```bash
cat > "$tmp/usage_limit_exit_harness.py" <<'PY'
import time

time.sleep(2.5)
print("\033[2J\033[HUsage limit reached. Try again in 1 minute.", flush=True)
time.sleep(1)
PY
python3 skills/project-manager/scripts/pm.py init --repo "$tmp" --plan "$tmp/plan.md" --harness codex
python3 skills/project-manager/scripts/pm.py start-slice --repo "$tmp" --harness-command "python3 $tmp/usage_limit_exit_harness.py"
python3 skills/project-manager/scripts/pm.py wait --repo "$tmp" --seconds 3 --poll-seconds 0.1
python3 skills/project-manager/scripts/pm.py wait --repo "$tmp" --seconds 5 --poll-seconds 0.1
python3 skills/project-manager/scripts/pm.py finalize-slice --repo "$tmp"
python3 skills/project-manager/scripts/pm.py summarize --repo "$tmp"
```

The first `wait` should preserve the rolling-limit pane evidence while the harness is still alive. The second `wait` should return after the process exits without a structured result, and PM must not send a continuation prompt into the old session. `finalize-slice` should block because `developer-result.json` is missing, so the PM model should restart only from a clean authorized state or stop for the user.

For a minimal fast-exit variant that may close before tmux can preserve the final pane text, use:

```bash
cat > "$tmp/usage_limit_exit_fast_harness.py" <<'PY'
import time

print("Usage limit reached. Try again in 1 minute.", flush=True)
time.sleep(0.2)
PY
python3 skills/project-manager/scripts/pm.py init --repo "$tmp" --plan "$tmp/plan.md" --harness codex
python3 skills/project-manager/scripts/pm.py start-slice --repo "$tmp" --harness-command "python3 $tmp/usage_limit_exit_fast_harness.py"
python3 skills/project-manager/scripts/pm.py wait --repo "$tmp" --seconds 10 --poll-seconds 0.1
python3 skills/project-manager/scripts/pm.py finalize-slice --repo "$tmp"
python3 skills/project-manager/scripts/pm.py summarize --repo "$tmp"
```

This variant still verifies the fail-closed gate behavior, but the final observation may contain only process-exited evidence if the tmux session closed before PM captured the usage-limit pane text.
