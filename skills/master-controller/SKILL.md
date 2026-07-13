---
name: master-controller
description: Supervise execution of an existing implementation-plan by running eligible slices one at a time, preserving durable state, enforcing authorization and quality gates, and stopping for human approval when policy requires it.
---

# Master Controller

Use this skill when the user wants a controller to execute an already-approved implementation plan one slice at a time — the top rung of this repository's autonomy ladder (Mode B). The master controller (MC) is a supervisor backed by deterministic tools: it sanity-checks the whole plan before starting, creates durable run state, launches one orchestrator harness per slice, observes or records harness evidence, verifies every claim against local evidence — recomputing the highest-risk gates (file authorization, commit ancestry, clean worktree) itself rather than trusting any report — and, when its verification finds a fixable gap, steers the live orchestrator session back onto the correct path through a bounded self-correcting repair loop. Only when the orchestrator repeatedly fails to satisfy the same gate, exhausts the repair budget, or breaches integrity does MC hard-stop and wait for a human.

Supervision is a dial within this one mode, not a fork. By default, the MC model stays in the loop for operational judgment (usage resets, stalls, transient interruptions) while deterministic commands own exact state transitions, tmux control, artifact capture, and gate verification. When no supervising model is available or wanted, MC's fail-closed unattended batch style (`run-next`, `run --scope remaining`; recorded in run state as `supervision.mode: deterministic-batch`) runs without live model judgment and stops at the first operational ambiguity — the right trade for short plans on reliable harnesses, scripted runs, and fully-local setups with nothing suitable in the supervision seat. Acceptance gates are identical in both styles; the supervising model only ever handles operational judgment, never acceptance.

Do not use this skill to create, repair, broaden, or materially amend an implementation plan. Planning belongs to `implementation-plan`. If the plan is missing, ambiguous, incomplete, or needs material edits, MC stops and reports that a separate planning step is required.

## Roles and Topology

A supervised MC run involves up to three model seats plus MC's deterministic tools. Who runs, who judges operations, and who accepts:

| Seat | What it is | Decides | Never |
|---|---|---|---|
| **Supervising model** | The assistant driving MC's commands (this skill's reader, in model-supervised operation) | Operational judgment: wait, nudge, pause, resume, stop-with-evidence | Acceptance — gates are deterministic |
| **MC deterministic tools** | The `mc.py` CLI: state, tmux control, artifact capture, gates, repair loop | Acceptance: every gate verdict, every state transition | Writing code, planning, delegating |
| **Slice orchestrator** | The harness session in tmux (codex / claude / opencode / copilot), one per slice | How to implement inside the frozen contract; when to delegate | Final authority, scope expansion, external side effects |
| **Worker** | A bounded helper the slice orchestrator launches through `ai-orchestrator`'s validated policy/request contract | Nothing — it executes one bounded task | Gates, commits, re-delegation |

- **Master Controller (MC)** — the deterministic supervisor. Owns run state, launches exactly one orchestrator harness per slice, and verifies every claim against local evidence: it recomputes the highest-risk gates itself (changed files against the frozen authorized surface, commit ancestry, clean worktree) and evidence-checks the rest (see "Safety Rules" for the exact trust boundary). Drives the bounded self-correcting repair loop by surfacing gate violations back into the live orchestrator session. Holds sole authority to hard-stop for a human when a threshold is crossed (repair budget exhausted, integrity breach, approval-gated slice). MC never writes slice code and never delegates to a worker itself.
- **Slice orchestrator** — the harness in tmux. Executes one slice under MC's supervision by applying `scoped-implementation` → validation → `drift-audit` → `code-review` → `commit`, self-correcting within its own run first, and delegating bounded sub-tasks to workers. Reports a structured result but holds no final authority; MC verifies it. On an MC-surfaced correction it fixes the specific gap using the context it already built. It may not expand scope, push, deploy, or perform external side effects. (Standalone `ai-orchestrator` calls the human-facing equivalent an *orchestrating assistant*; under MC the slice orchestrator has the same discipline and a different boss.)
- **Worker** — a bounded, single-purpose helper the orchestrator requests through `ai-orchestrator`'s semantic policy/request launcher. Owns no gates, never commits, never re-delegates. MC writes worker policy; `ai-orchestrator` validates the request, embeds worker/skill instructions, composes harness flags, and launches; MC verifies the validated contract and outcome.

## Preconditions

Before running MC, confirm:

- Python 3.13 or newer is available (the authorization gate uses its segment-aware `pathlib` glob matcher).
- The plan file exists and contains frozen slice contracts.
- The target repo path is a git worktree.
- The current branch is the intended feature branch.
- Required local tools for the selected operation are available.
- The selected harness is configured for this environment.
- The starting worktree is clean, unless the user has explicitly authorized a dirty-state policy.

Docker and container setup are out of scope. MC may run inside a container or on a host machine, but it does not create, configure, or rely on container isolation.

## Workflow

1. **Sanity-check the plan** - `check-plan` validates every slice contract up front (canonical slice headings — rejecting slice-like headings hidden in fenced code blocks and unclosed fences — required sections, usable repository-relative authorized surfaces, exact yes/no approval flags, unique slice numbers) and lints for conditions MC cannot mechanically guard mid-run (dependency/license-shaped authorized files, whole-repo surfaces, plain entries that name existing directories, Mode A-only batch groupings). `init` runs the same check automatically and fails closed on errors, so a defective plan stops for the user before any harness launches.
2. **Initialize** - create `.ai-mc/runs/<timestamp>/run.json` in the target repo and update `.ai-mc/current`.
3. **Check eligibility** - identify the next uncompleted slice and fail closed on approval-needed risk flags without a recorded approval.
4. **Run or supervise one slice** - launch a fresh tmux-backed harness session for one eligible slice. In model-supervised operation, keep the MC model in the loop to observe live pane/log/json/git evidence and choose safe operational actions.
5. **Capture artifacts** - preserve prompt, transcript or pane capture, git status, diff, validation summary, drift audit, code review, commit data, operational events when present, and structured orchestrator result.
6. **Verify gates** - independently compare the orchestrator result to git state, plan authorization, validation, drift audit verdict, exact-`PASS` review verdict, residual-finding ledger, and commit state. Delegating the drift-audit and code-review to a separate worker model for independence is the preferred posture (the same the Mode A launcher expresses), but by default it is a degradable preference, not a gate: a slice audited locally by a single model is a valid accepted outcome, and worker delegation is reported (see `summarize`), never blocking. Only when a slice opts in with `Independent audit required: yes` in its Risk Flags does MC additionally require mechanical evidence that every available worker tool launched through a validated semantic contract matching MC's stored and on-disk `worker-policy.json`, used the required model/effort and permitted role/access, ran in the target repository, and completed successfully, with separate contracts whose required skills are exactly `drift-audit` and `code-review`; on such a slice, narration, one generic worker launch, a matching executable launched raw, a missing tool or audit purpose, a mismatched/mutated policy, or no worker made available at all does not pass. Every non-pass outcome is classified with a stable failure signature as either `repairable` (a fixable gap — validation, drift, review, residual-ledger/result shape, opt-in worker evidence, unauthorized files, changed-files bookkeeping, malformed result, missing commit, dirty worktree) or terminal `needs-human` (an integrity/trust breach — HEAD did not advance or is not descended from the slice start, or the orchestrator worked the wrong slice).
7. **Repair, advance, or stop** - on a repairable gate with budget remaining, MC archives the stale result, writes a targeted repair prompt naming the exact violation, surfaces it into the **live** orchestrator session (preserving the context the orchestrator already built), and re-verifies the complete, unrelaxed gate when a fresh result lands — a repair can never lower the bar, only grant another chance to clear the identical one. A signature-keyed circuit breaker bounds the loop: first failure earns an in-session correction, the same signature failing again earns exactly one fresh-session retry with the frozen prompt plus targeted repair context and the cumulative residual-findings ledger, and a third consecutive failure is terminal regardless of remaining budget (`policy.max_repair_attempts`, default 3). MC advances to the next slice only when every gate passes, and stops with a precise reason for human approval, integrity breach, exhausted budget, harness failure, or incomplete evidence.

The CLI supports state creation, dry-run eligibility checks, one-slice tmux execution, model-supervised observing/sending/waiting/pausing/finalizing, structured artifact capture, MC-side gate verification, sequential remaining-slice execution, cancellation, and summaries. Keep deterministic acceptance gates unchanged: model-supervised primitives provide operational control and evidence, not acceptance.

## Default Operating Path

When the user gives MC a complete implementation plan and asks to implement it, do not require them to restate the whole launch recipe. Model-supervised operation is the default: live recovery from quota/session/service interruptions almost always matters on real runs. Use the unattended batch style instead when the user asks for it, when no supervising model can stay in the loop, or when a short fail-closed run on a reliable harness is explicitly acceptable.

Model-supervised path (default):

1. Run `check-plan` on the plan file. Stop and report if it finds errors; surface warnings to the user before continuing. Initialize or reuse the run, run preflight, and dry-run the next slice. When the user has authorized a specific branch and it is not already current, initialize with `--branch <name>` and, if explicitly authorized, `--create-branch`.
2. Start the next eligible slice with `start-slice`; do not use `run --scope remaining` for model-supervised operation.
3. Observe live pane/log/json/git evidence with `observe` or bounded `wait` on a calm cadence.
4. If the harness reports a clear rolling 5-hour usage reset and the process is still alive, pause until reset plus buffer with `pause-until`, re-observe for hard-stop prompts, then send a short continuation prompt with `send --text <text> --reason <reason>` (`--reason` is required and fails closed without it).
5. If a rolling-limit message appears after the harness process exits before a structured result, restart only from a clean authorized state or stop with evidence; do not send into the old session.
6. If a structured result appears, finalize through deterministic MC gates with `finalize-slice` before advancing.
   When an audit or review artifact has a narrow, syntactically non-empty findings/observations section but `residual_findings` is empty, finalization treats the artifact/ledger mismatch as repairable. This is a deliberately narrow structural consistency check for tagged or explicitly non-blocking list entries, not a semantic completeness proof; prose findings may remain outside its coverage. MC does not reinterpret severity or redefine the audit skills' output contracts.
7. When `finalize-slice` returns `"status": "repairable"` with `"mode": "in-session"`, the session is still alive and `current_slice` remains populated: deliver the returned `send_text` (a one-line pointer to the rendered `repair-prompt-repair-<round>.md`) into the live session with `send --text <send_text> --reason <reason>`, `wait` for a fresh result, then `finalize-slice` again. With `"mode": "fresh-session"` or `"mode": "relaunch"`, finalize has already relaunched a new session for the same slice itself — just `wait` and re-finalize. Budget and the same-signature circuit breaker are enforced deterministically from persisted `current_slice.repair`; finalize goes terminal on its own when they trip. MC persists the original launch configuration in `current_slice`, so later `wait` and `finalize-slice` invocations reuse the same custom/profile command, model, effort, and unattended-default choices during automatic relaunches; repeating the flags remains harmless but is no longer required for new runs.
8. Do not use `run-next` / `run --scope remaining` while a model-supervised slice is live (they refuse when `current_slice` is populated); finish it through wait/send/finalize or stop it explicitly first.
9. Stop with evidence for weekly, monthly, account, billing, credential, trust, permission, dependency/license, remote-side-effect, destructive-action, approval-gated, ambiguous, or policy-sensitive conditions.

Unattended batch style (fallback, fail-closed):

1. Use `codex` as the default orchestrator harness when no harness is specified. `claude`, `copilot`, and `opencode` are also validated orchestrator harnesses (see `references/harness-adapter-contract.md`) — use one of them when the user names it explicitly, or when the task and configured model make it the better functional fit per `ai-orchestrator`'s role definitions.
2. Run `check-plan`; stop on errors and surface warnings. Initialize an MC run if `.ai-mc/current` is missing or is for a different plan; otherwise reuse the current run after checking status. When the user has authorized a specific branch and it is not already current, pass `--branch <name>`; add `--create-branch` only when branch creation is explicitly authorized.
3. Run `preflight` before the first slice. Include `--worker-tools <tool[,tool]>` to make a worker available to the orchestrator for delegating the drift-audit and code-review (recommended for independence, and required for any slice marked `Independent audit required: yes`), and include `--allow-profile-command` for normal local execution.
4. Run `run-next --dry-run` and confirm the selected slice is eligible.
5. If the user requested one slice, run `run-next`. If the user requested all remaining work, run `run --scope remaining` in the background and poll `status`.
6. After the run stops or completes, run `summarize`, inspect the generated `run-report.md`, `run.json`, and selected slice artifact directories, and check git status before reporting. Confirm that residual post-plan considerations from every slice appear in the report. Expect fail-closed stops at the first operational ambiguity (usage limits, unclear pane text); that stop is the design, not a failure — re-run under model supervision or resolve manually and resume.

Ask the user only when required information cannot be inferred safely, such as the target repo, plan path, intended branch, whether to run the next slice or all remaining slices, or whether an approval-gated slice should proceed. Do not ask the user to hand-compose harness sandbox flags; use MC profiles and preflight instead.

Approval-gated slices: when MC stops on an approval-needed slice and the user explicitly approves it, record that approval with `approve --slice "<Slice N>" --reason "<why>"` and re-run. The approval is stored in run state and logged as an operational event; it clears only an exact `yes` approval flag. Never edit the plan's approval flag to get past the gate — that changes the frozen digest and forces a fresh `init`. If a plan genuinely must be revised mid-run, re-`init` with `--assume-complete "<Slice 1,Slice 2>"` naming the slices already completed and committed, so the new run resumes at the right slice instead of re-running finished work.

## Launcher

This is the single authoritative Mode B launcher (the top-level README and `implementation-plan` point here rather than restating it). Paste it into a fresh session with a supervising model to start a run:

```md
Plan file: <path>
Target repo: <path>
Scope: <next slice, or all remaining slices>
Harness: codex unless I specify otherwise. (claude, copilot, and opencode are also validated MC orchestrator harnesses — name one explicitly to use it.)
Worker tools: <a worker to make available for delegating the drift-audit and code-review for independence, e.g. copilot or opencode; omit to have the orchestrator self-audit locally — a valid default. Required for any slice marked "Independent audit required: yes".>

Use master-controller as the supervising skill for this run.

Read the full plan file first. If the plan is incomplete, ambiguous, not an implementation-plan output, or needs material editing, stop and report instead of improvising.

Use the current feature branch unless I explicitly name another branch. Confirm the target repo, plan file, branch, scope, harness, and worker tools before starting runtime execution.

Run check-plan on the plan file first; stop and report if it finds errors, and surface any warnings to me before continuing. Then initialize or reuse the MC run for this repo and plan, run preflight, and dry-run the next slice. Use MC profiles for normal local execution; do not ask me to hand-compose harness sandbox flags.

For each eligible slice, keep the MC model in the loop. Use the model-supervised primitive loop rather than `run --scope remaining`: `start-slice`, then repeated `observe` or bounded `wait`, then exactly one of `pause-until`, `send` (requires `--reason`; it fails closed without one), `finalize-slice`, or `stop-with-evidence` based on the evidence. After `finalize-slice` passes, dry-run or start the next eligible slice. Keep all acceptance decisions inside MC's deterministic gates; pane text can justify operational wait/resume/stop decisions only.

Recover only bounded operational interruptions that are clearly transient and do not expand the slice contract. A rolling 5-hour usage window with a parseable reset can be paused until reset plus buffer when the harness process is alive; after re-observing for hard-stop prompts, send a continuation prompt such as `You were interrupted. Review what you were doing then continue.` with `send --text <that text> --reason <why you are resuming>`. If the process exited before writing a result, restart only from a clean authorized state; otherwise stop with evidence.

Stop and report for any approval-gated slice, missing evidence, validation failure, drift, review failure, unauthorized file change, dirty post-commit state, branch or plan mismatch, weekly/monthly/account/billing cap, credential/trust/permission prompt, requested external side effect, destructive action, dependency/license change, harness failure, ambiguous operational state, or blocker outside the frozen contract. Do not self-approve human-gated work.

When the requested scope stops or completes, summarize the MC run from its generated `run-report.md`: slices attempted, slices committed, gate result for each slice, residual findings and post-plan considerations, operational stop or recovery evidence if any, artifact location, current git status, and the next action needed from me.
```

No supervising model available, or a short fail-closed run is explicitly acceptable? Skip the launcher and run the unattended batch style directly from a shell: `check-plan` → `init` → `preflight` → `run --scope remaining` in the background, then poll `status`/`summarize`. It stops at the first operational ambiguity by design.

## Long-Running Command Discipline

MC's blocking commands outlive the tool-call limits of the assistants that drive them (for example a 10-minute shell-tool cap). A foreground `run --scope remaining` (≈30 minutes per slice) or a multi-hour `pause-until` that is killed mid-call leaves run.json stuck at `running` with the tmux harness still editing the repo unsupervised; `status` warns when it detects this (active status, recorded tmux session gone).

- Run `run --scope remaining` and `run-next` in the background (or under `nohup`/a detached shell), then poll `status`/`summarize`.
- In model-supervised operation, prefer repeated bounded `wait --seconds <n>` calls that fit inside the tool limit (for example 240–540 seconds) over one long wait.
- Local/open-weight model harnesses can have long silent periods (roughly a minute of cold start, several minutes of silent prefill on a large embedded prompt) with no visible token output before progress resumes; this is a timing characteristic, not a stall. Confirm genuine idleness across at least two separate observations before treating it as one.
- While a live harness is running, keep MC observation/control commands separate from supplementary diagnostics that may require a new approval. An approval wait does not pause the harness and can leave it editing or committing without supervision.
- For long pauses, prefer scheduling a later re-observe over a single blocking `pause-until` when the controller cannot safely block that long; `pause-until` remains correct when the controlling process genuinely can wait.
- For model-supervised runs on subscription harnesses, prefer an MC model on a different provider than the orchestrator harness: if both share one subscription, a usage window stalls the supervisor and the supervised session at the same time.

## Safety Rules

The safety invariant of the repair loop: MC re-runs the complete gate with unrelaxed rigor after every repair attempt, so a `repairable` classification can never let a bad slice through — the worst case of a generous repair is a wasted attempt followed by a hard stop at the cap. Fixable violations (failed/missing validation, non-PASS drift or review verdicts, missing worker evidence, unauthorized changed files — repaired restore-only, changed-files bookkeeping, malformed results, missing commits, dirty worktrees) are therefore steered in-session within the repair budget rather than stopping the run on first occurrence.

MC must stop, without attempting a repair, on:

- Missing or ambiguous plan/slice contract.
- Approval-needed slice without a recorded operator approval (`approve` command).
- Dirty starting git state outside configured policy.
- Integrity/trust breaches: a required commit that did not advance HEAD, a HEAD not descended from the slice starting commit, or a result reported for the wrong slice. These are never steered — continuing to reason from a context that already holds a false belief about reality is itself the risk.
- Repair budget exhaustion or a signature that keeps failing through the circuit breaker (in-session correction, then one fresh session, then terminal).
- A hard prompt on screen when a repair would be delivered (the delivery refuses; MC stops with evidence).
- Harness or tmux failure, terminal timeout, or missing required result evidence. (Pane/transcript capture failures degrade to recorded placeholder/note files rather than stopping the run; the structured result and git evidence remain the acceptance basis.)
- Any proposed destructive filesystem action outside the target repo/worktree.
- Secret exposure, credential prompt, dependency/license change, remote push, release, deploy, or external side effect not explicitly authorized.
- Weekly, monthly, account, or billing usage caps.
- Unknown or ambiguous usage-limit messages without a clear bounded reset.
- Ambiguous operational interruptions after reasonable observation.

MC may recover from a rolling 5-hour usage window, temporary service interruption, or similar transient only when pane/log evidence is clear, the recovery is bounded, the same slice contract remains in force, no hard-stop prompt is visible, and incomplete work is not accepted as passing. Operational screen text can guide wait, retry, resume, or stop decisions; it can never accept a slice.

An idle tool-call stall — pane output unchanged across two or more consecutive `wait`/`observe` calls, with no hard-stop prompt visible — is also a recoverable transient: send one short, specific pointer nudge with `send --text <text> --reason <reason>` into the live session before considering a restart or a stop. Confirm the stall is real (byte-identical pane across separate observation windows) rather than slow generation before nudging.

`observe` and `wait` include lightweight `operational_hints` extracted from pane and transcript tails. Treat ordinary hints as evidence for MC-model judgment, not as automatic decisions. Treat hard-stop hints as a deterministic floor: `send`, `pause-until`, and any unattended retry/resume must refuse when the visible evidence indicates weekly, monthly, account, billing, unknown-limit, auth, trust, permission, or external-side-effect conditions. Prefer relative reset durations over absolute local times; absolute local reset times are usable only when they are unambiguously near-future for the controller timezone or include an explicit timezone.

Two narrow operational exceptions are deterministic and bounded. First, a `blocked` or `fail` orchestrator result is routed into the existing repair loop only when current-attempt evidence exactly matches a high-confidence, non-hard-stop `service_unavailable` / `transient` / `bounded-retry` signature; medium-confidence server or network errors remain terminal. Second, repeated idle observations spanning the configured ten-minute ceiling trigger the same signature-keyed repair machinery: one automatic continuation nudge, one fresh-session escalation if the same stall recurs, then the existing terminal circuit breaker. Neither path accepts work or relaxes a gate.

MC decisions must not rely only on natural-language transcript interpretation. The orchestrator must produce `orchestrator-result.json`, and MC must verify claims against local evidence.

Trust boundary for audit verdicts: MC recomputes the highest-risk gate itself — the set of changed files against the authorized surface, using segment-aware matching so `*.md` does not cross directory boundaries — and it independently verifies commit ancestry, HEAD advancement, and a clean post-commit worktree. For the drift-audit, code-review, validation, and residual-finding gates, MC verifies the reported result shape plus the existence of required non-empty artifact files; it does not re-derive semantic verdicts or residual dispositions from the transcript and it does not re-run validation commands. A dishonest orchestrator that writes passing fields and fabricates coherent artifacts is therefore outside what MC detects, by design. The mitigation is that the file-level authorization gate — the change most likely to cause real harm — is always MC's own computation, never the orchestrator's claim. Worker delegation is a degradable preference by default, not a gate: delegating the drift-audit and code-review to a separate model is the preferred independent-second-opinion path, but a slice audited locally by a single model is a valid accepted outcome (the orchestrator still holds the verdict, exactly as in Mode A). By default MC therefore reports delegation rather than gating on it. A slice can opt in to mechanical enforcement with `Independent audit required: yes`; only then must validated launch evidence collectively cover every configured worker tool and two distinct purposes — one contract with exactly `required_skills: ["drift-audit"]` and another with exactly `required_skills: ["code-review"]` — with matching policy digest, slice/plan identity, model/effort, role/access, repository, positive subprocess pid, real out/err files under the worker artifact root, and successful completion. A raw command, one generic launch, narration alone, policy mismatch, crashed worker, missing audit purpose, or a hand-authored manifest/status pair with no real launch footprint does not pass. This raises the mechanical bar but is still process-level rather than semantic: the orchestrator remains responsible for reading worker output, retrying incomplete delegations, and deciding whether the returned analysis is sufficient.

Be equally candid about the heuristic stops: the dependency/license, secret/credential, remote-push/deploy, and external-side-effect stop conditions are enforced by pane-marker detection plus the orchestrator prompt's prohibitions, not by mechanical inspection of the diff. A silent dependency edit inside an authorized file surface would pass the file-authorization gate; keeping such files out of authorized surfaces (or approval-gating slices that touch them) is the plan-level control.

## Relationship To Other Skills

- `implementation-plan`: produces the plan MC consumes. MC does not plan.
- `ai-orchestrator`: may run inside the tmux session as the slice orchestrator when delegation improves quality or context economy.
- `scoped-implementation`: used by the orchestrator to implement one frozen slice.
- `drift-audit`: required before quality review; MC treats the verdict as an authorization gate and verifies the evidence exists.
- `code-review`: required after drift audit passes; MC treats unresolved material findings as blocking.
- `commit`: used by the orchestrator for passing slices. MC verifies the commit state.
- `handoff`: records stop state and the next slice in Mode A. Mode B keeps continuation state in `run.json`, writes an MC-generated `slice-summary.md` for each terminal slice, and continuously refreshes `run-report.md` with gates, commits, blockers, next actions, and residual post-plan considerations.

## Commands

```bash
python3 skills/master-controller/scripts/mc.py check-plan --plan <path>
python3 skills/master-controller/scripts/mc.py init --repo <path> --plan <path> --harness <name>
python3 skills/master-controller/scripts/mc.py init --repo <path> --plan <path> --harness <name> --branch <branch> --create-branch
python3 skills/master-controller/scripts/mc.py init --repo <path> --plan <path> --harness <name> --assume-complete "Slice 1,Slice 2" --max-repair-attempts 3
python3 skills/master-controller/scripts/mc.py approve --repo <path> --slice "Slice 3" --reason <why>
python3 skills/master-controller/scripts/mc.py profiles
python3 skills/master-controller/scripts/mc.py preflight --repo <path> --worker-tools <tool[,tool]> --allow-profile-command
python3 skills/master-controller/scripts/mc.py status --repo <path>
python3 skills/master-controller/scripts/mc.py summarize --repo <path>
python3 skills/master-controller/scripts/mc.py run-next --repo <path> --dry-run
python3 skills/master-controller/scripts/mc.py run-next --repo <path> --worker-tools <tool[,tool]> --allow-profile-command
python3 skills/master-controller/scripts/mc.py run-next --repo <path> --harness-model <model> --harness-effort <effort> --worker-tools <tool[,tool]> --worker-model <model> --worker-effort <effort> --allow-profile-command
python3 skills/master-controller/scripts/mc.py run --repo <path> --scope remaining --worker-tools <tool[,tool]> --allow-profile-command
python3 skills/master-controller/scripts/mc.py start-slice --repo <path> --worker-tools <tool[,tool]> --allow-profile-command
python3 skills/master-controller/scripts/mc.py observe --repo <path>
python3 skills/master-controller/scripts/mc.py wait --repo <path> --seconds <n>
python3 skills/master-controller/scripts/mc.py send --repo <path> --text <text> --reason <reason>
python3 skills/master-controller/scripts/mc.py pause-until --repo <path> --until <iso-timestamp-with-timezone> --reason <reason>
python3 skills/master-controller/scripts/mc.py finalize-slice --repo <path>
python3 skills/master-controller/scripts/mc.py stop-with-evidence --repo <path> --reason <reason>
python3 skills/master-controller/scripts/mc.py reconcile --repo <path>
python3 skills/master-controller/scripts/mc.py stop --repo <path> --reason <reason>
python3 skills/master-controller/scripts/mc.py archive-sensitive --repo <path> --dry-run
```

Model-supervised primitives are `observe`, `send`, `wait`, `pause-until`, `start-slice`, `finalize-slice`, and `stop-with-evidence`. They preserve the same trust boundary: the MC model may reason over operational evidence, but acceptance still requires deterministic local gates.

Launch commands resolve fail-closed: a bare `--harness <name>` refuses to start because an interactive session would deadlock on the first approval prompt nothing unattended can answer. Provide exactly one of `--allow-profile-command` (compose the full tested command from the harness profile plus run requirements — the normal path), `--harness-command "<full command>"` (an explicit override, mainly for controlled local validation), or `--allow-unattended-default` (opt in to the profile's known unattended-safe base command without profile composition; per-action approval is disabled, so MC's post-hoc gates become the safety boundary for the run).

Runtime commands require `tmux`, the selected harness command, and a clean target worktree outside MC's `.ai-mc/` audit directory. MC starts one tmux session per slice and keeps it alive across in-session repair rounds; a fresh session is launched only for a circuit-breaker escalation or a dead-session relaunch. MC stops rather than advancing when evidence is missing or a gate fails and cannot be repaired within budget or safely reconciled from local evidence.

When all other slice gates pass and the only defect is an incorrect or abbreviated reported `commit.hash`, MC may correct `orchestrator-result.json` to the proven current `HEAD`, write `mc-reconciliation.json` / `mc-reconciliation.md`, and accept the slice. This recovery is allowed only when local git evidence proves the commit advanced from the slice starting point, changed files match the authorized surface and reported result, validation/drift/review artifacts pass, and the post-commit worktree is clean.

For a run that already stopped on a recoverable evidence problem, use `reconcile` to re-run MC's local gates against the stopped slice and update run state only when the same strict reconciliation criteria pass.

## References

- `references/run-state-schema.md`
- `references/orchestrator-prompt.md`
- `references/harness-adapter-contract.md`
- `README.md` → "Privacy and Data Flows" — what each seat sees and which configurations keep everything local. In model-supervised operation the supervising model reads pane excerpts and worker-output tails, which can include fragments of the code under work; place that seat accordingly.
