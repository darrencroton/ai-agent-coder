# Mode B Lite — Target Design

**Status:** Stage-1 design report. Describes the proposed final state; no code in this stage.
**Governing documents:** the [proposed vision](proposed-vision.md) (assurance model) and the practical spine identified in §1 below (derived from the [current-state map](current-state-map.md)). The current Mode B is used here only as evidence of required behaviour and known failure modes — never as the structural starting point. Dispositions for every current component are in the [replacement ledger](replacement-ledger.md); build order is in the [implementation blueprint](implementation-blueprint.md).

Mode B Lite is a clean replacement for the current Mode B. When adopted, the repository contains exactly one Mode B — this one.

---

## 1. The practical spine (Phase 3)

The smallest structural core responsible for most of the current system's practical value, assessed outcome-by-outcome. Format: **outcome — what protects it in Lite — why, in terms of failure frequency/impact — mechanical or judgement — disposition relative to today.**

| # | Outcome | Protection in Lite | Failure it prevents (freq/impact — judgement, informed by the test record) | Mechanical? | Disposition |
|---|---|---|---|---|---|
| S1 | Work begins from a sufficiently clear objective | Frozen plan contract; parse + `check-plan` at start; fail closed on missing sections, unusable surfaces, non-exact approval flags | Running on a defective contract (medium/high) | Mechanical | **Retain** (C1) |
| S2 | Important constraints are visible | Contract + risk flags rendered into every Developer prompt; non-goals restated | Constraint-blind implementation (high/medium) | Prompt | **Retain, simplified prompt** |
| S3 | Implementing agent understands its scope | Authorized surface in prompt; segment-aware matching semantics documented once | Innocent scope confusion (high/low-med) | Prompt + mechanical backstop S6 | **Retain** |
| S4 | Long work survives context limits | Fresh session per slice + PM-curated run notes fed to each new session | Knowledge loss across sessions (high/medium) | Judgement (curation) over mechanical transport | **Redesign** (replaces C8/C9 machinery) |
| S5 | Progress remains observable | tmux capture, `observe`, event log, live-pane preservation | Silent unattended failure (medium/medium) | Mechanical capture, judgement reading | **Retain, simplified** (C12/C13) |
| S6 | Repository changes remain inspectable & authorized | **Floor:** changed-files-vs-frozen-surface recomputed from git by PM code; diff captured | Unauthorized surface change (low freq/**highest impact**) | **Mechanical, non-waivable** | **Retain verbatim in spirit** (C5 floor) |
| S7 | Material drift is detected | S6 for files; PM reads the diff against intent/non-goals for in-surface drift; independent drift-audit on elevated slices | Semantic drift inside authorized files (medium/medium-high) | Judgement (was already model judgement — the old gate checked the verdict string, not the drift) | **Redesign: judge the artifact, not the verdict string** |
| S8 | Failed work is not accepted on say-so | PM assesses from evidence: diff, validation output, review artifacts; narration is never evidence | Generous self-grading (high/medium — Tests 10/11/13/18) | Judgement over mechanical evidence, plus floor S6/S9 | **Redesign** (replaces verdict relay) |
| S9 | Accepted work is committed coherently | **Floor:** commit exists, HEAD advanced, descends from `before_head`, worktree clean; PM reads the commit from git, not from the result | Broken/absent history (low/high) | **Mechanical, non-waivable** | **Retain** (C5 floor); commit-hash relay deleted (PM proves HEAD itself) |
| S10 | Interruptions are recoverable | Durable single-copy state outside the worktree; artifacts + report in-repo; idempotent resume | Lost runs (medium/medium) | Mechanical persistence | **Retain, single copy** (replaces C3) |
| S11 | Consequential decisions reach a human | **Floor:** approval-gated slices stop without recorded approval; hard-stop condition floor (credential/billing/trust/side-effect markers) refuses unattended continuation; PM escalates by judgement above the floor | Self-approved consequential work (low/high) | Mechanical floor + judgement above it | **Retain** (C1/C11/C12 floors) |
| S12 | Accepted work is reviewable afterwards | Per-slice `assessment.md`: what PM checked, what it read, what it decided and why; run report | Un-auditable acceptance (—/medium) | Judgement, durably recorded | **Redesign** (replaces provenance derivation + shape verdicts) |
| S13 | Unresolved risk stays visible | Findings/residual-risk section in every assessment + aggregated in run report; PM curates across slices | Findings vanishing (medium/medium — Tests 1/11/18) | Judgement (the mechanical version demonstrably both over- and under-fired) | **Redesign** (replaces ledgers/retention) |
| S14 | Responsibility for the final decision is clear | Every acceptance signed by PM assessment; floor failures cannot be accepted by anyone; humans own plan-flagged approvals | Diffuse accountability (—/medium) | Structural | **New, explicit** |
| S15 | The wrong/dead session is never driven blind | Session liveness/readiness checks; hard-prompt refusal on send; one live slice at a time | Typing into prompts/dead panes (medium/medium) | Mechanical | **Retain** (C11) |

Distinctions the brief asks for: S6, S9, S10, S11, S15 protect against **meaningful engineering failures** and stay mechanical because they are cheap and their failure is catastrophic or unrecoverable. S7, S8, S12, S13 were always **model judgement wearing mechanical clothes** (verdict strings, shape checks) — Lite makes the judgement explicit and accountable instead. The current system's **confidence-enhancing controls** (provenance labels, changed-files bookkeeping match, commit-hash relay) and **weak-model compensations** (closed vocabularies, verbatim retention, schema rigour on developer output) are dropped: their enforcement cost exceeded their risk reduction in live runs (vision assessment §1.1), and their review-easing value is replaced by the assessment record.

---

## 2. Roles

Three seats. Fewer was considered and rejected; more is unjustified.

| Role | Responsibility | Authority | May not | Outputs |
|---|---|---|---|---|
| **PM** (one agent + its deterministic toolkit) | Supervise the run: launch/observe/steer/stop sessions; enforce the floor; assess every slice; curate run notes; commission review; manage risk; escalate | Accept/reject slices (above a passing floor); choose validation & review depth; steer/relaunch/stop under budget; resolve minor plan ambiguity on the record; raise (never lower) risk | Write slice code; author/expand plans; waive the floor; approve plan-flagged human gates; push/deploy/external side effects | run state, events, per-slice `assessment.md`, `notes.md`, `run-report.md` |
| **Developer** (fresh harness session per slice) | Implement one slice inside the frozen contract: code, tests, validation, commit | Engineering decisions inside the surface | Expand scope, touch other files, push/deploy, change deps/licenses without plan authorization, self-approve | the diff + commit, `validation.md`, `result.json` (minimal) |
| **Reviewer** (read-only session, commissioned by PM, risk-dependent) | Answer the drift-audit and/or code-review question against the final diff | None — evidence only | Edit, mutate git, decide acceptance | a review report artifact |

**Why the merges and non-merges:**

- **Supervising model + deterministic controller → one PM.** The current split (model judges operations, code judges acceptance) forced every judgement into either a schema or a stanza. Merged: the toolkit is PM's hands, not a co-equal authority. This is the design's central merge.
- **PM ↔ Developer: not merged.** Execution-vs-assessment separation is the load-bearing role boundary — the implementer must not grade its own work (the repository's founding observation, re-confirmed in every test that caught a false self-report). Also practical: fresh Developer sessions per slice are the context-limit strategy (S4).
- **Reviewer: kept as a role, demoted as an apparatus.** Independent eyes on elevated slices are worth a real role; a 2,540-LOC launch-contract subsystem to prove the Developer really hired those eyes is not, because in Lite the Developer never hires them — PM does. The judge commissions the audit; the observed verdict-shaping channel (Tests 13/14) closes structurally.
- **Human approver: a decision point, not a seat.** Humans enter at plan-flagged gates, floor violations, and PM escalations; they hold no in-run operational role.

## 3. Workflow

Four stages. One drive path (the PM agent), not two.

### 3.1 Prepare
- **Purpose:** establish that the run can proceed safely. **Owner:** PM.
- **Inputs:** plan file, target repo, branch intent, harness/model choices, reviewer availability.
- **Mechanical:** parse + `check-plan` (fail closed on errors), plan sha256 freeze, git/tmux/harness preflight, clean-worktree check, branch check/create (only when explicitly authorized), state creation.
- **Judgement:** surface lint warnings to the user or proceed; confirm inferred parameters (repo/plan/branch/scope) when not stated.
- **Proceed when:** state initialized, first eligible slice identified. **Stop for:** plan errors, dirty tree, missing tools, ambiguous target.

### 3.2 Execute (per slice)
- **Purpose:** get one slice implemented. **Owner:** Developer, supervised by PM.
- **Mechanical:** eligibility (in-order next uncompleted slice; approval flag cleared or recorded), fresh tmux session, prompt rendered from contract + curated notes, `before_head` recorded, hard-prompt refusal on any send, liveness checks, artifact capture.
- **Judgement (PM, during):** observation cadence; nudge stalls (confirmed idle across observations — the byte-identical-pane discipline is kept as *guidance*, not statute); pause/resume on clear rolling usage resets; stop on hard-stop evidence; recover or stop on process exit.
- **Slice-complete signal:** `result.json` appears (or the session dies/times out → PM decides relaunch vs stop).

### 3.3 Assess (per slice)
- **Purpose:** decide the slice. **Owner:** PM.
- **Mechanical floor — the authoritative enumeration (checked at slice launch and again, in full, at `finalize`; non-waivable):**
  1. Plan digest unchanged since `init`.
  2. Repo/worktree identity and **current branch** match the run state (re-validated at every launch and finalize — a commit on a different branch descending from `before_head` fails here).
  3. Approval eligibility: an approval-flagged slice has a recorded human approval.
  4. `result.json` present and names the expected slice.
  5. Changed files (computed `before_head`→HEAD + status) ⊆ frozen authorized surface.
  6. Commit exists (when required), HEAD advanced, descends from `before_head`, and is the head of the recorded branch.
  7. Worktree clean outside the artifact dir.
  8. A fresh hard-stop scan of the live pane at finalize: no visible credential/trust/permission/billing/side-effect prompt (a hard-stop condition blocks acceptance even when a result and commit exist).

  The floor's guarantee is scoped honestly: it covers the **final Git-visible worktree state**. Ignored files, Git metadata/hooks, transient writes reverted before finalize, and external side effects are outside it — controlled, as today, by prompt prohibitions, hard-stop markers, and plan-level surface exclusion, and registered as such in the assurance-loss register. Any floor failure → steer a restore/fix or stop; never accept.
- **Judgement, recorded in `assessment.md`:** read the diff against intent and non-goals (the drift question); read `validation.md` and judge whether the contract's validation plan was actually satisfied (rerun spot commands at PM's discretion); on elevated slices, commission independent drift-audit and/or code-review against the final diff and weigh the reports; judge materiality of deviations; extract findings and lessons into `notes.md`; decide **accept / steer (with a written correction) / stop**.
- **Proceed when:** accepted → next slice. **Stop for:** floor integrity breaches, exhausted attempt budget, plan-flagged approvals, anything outside PM's brief.

### 3.4 Finish
- **Purpose:** end the run honestly. **Owner:** PM.
- **Mechanical:** final state write, run report regeneration.
- **Judgement:** the closing report — slices accepted (with commits), slices stopped and why, residual findings and risk, next actions for the human.

## 4. Risk model

Two levels. No taxonomy, no decision engine.

- **Standard** (default): everything in §3 minus independent review. PM's own assessment of the diff *is* the review.
- **Elevated.** The plan-declared part is **mechanically derived at parse time** and recorded immutably in the slice's state: `plan_risk = elevated` iff `Approval needed before implementation:` is `yes` (also requires a human), or `Independent audit required:` is `yes`, or the `Risky surfaces touched:` value is anything other than an exact `none` (with or without a trailing period). PM cannot initialize such a slice as standard — the parser decides. Above that floor, **PM escalates on evidence** (diff unexpectedly broad; touches auth/billing/persistence/schema/deps/CI even inside an authorized surface; validation evidence surprising; anything PM finds suspicious).
- **Elevated effects (automatic):** independent review commissioned by PM — **both** drift-audit and code-review, always, each fresh at the exact final HEAD *(amended post-implementation: the first revision allowed skipping code-review "unless the flag names only one concern"; the shipped rule requires both unconditionally — strictly safer, simpler to enforce mechanically, and what every operator doc states)*; deeper validation (PM reruns the contract's commands rather than reading output); assessment must name what was independently checked. Plan-flagged approval additionally requires a recorded human `approve`.
- **Ratchet:** PM may raise a slice to elevated and must record why. PM may never lower a plan-declared elevation. The plan is the floor of the risk level; PM judgement is only ever additive. This is how "every task gets classified high" is prevented (the plan sets the baseline, and elevation costs PM real work, so there is no incentive to inflate) and how "casual downgrade" is prevented (downgrade is structurally impossible).
- **What does not raise the risk level:** slice length, file count within the surface, wording. Model choice also does not change the *level* — but SKILL.md directs PM to commission independent review as a matter of course when the Developer seat holds a weak or unproven model, recorded as a discretionary escalation of review depth, not of risk level.
- **Recording:** one line in the slice's assessment: level, trigger, effects applied.

## 5. Gates

Three gates total, each protecting a distinct outcome:

| Gate | Protects | Decider | When | Mechanical part | Judgement part | On failure |
|---|---|---|---|---|---|---|
| **Floor** | S6/S9/S10/S11/S15: surface, history, cleanliness, frozen plan, approvals, hard-stop conditions | PM's code | continuously + at assess | all of it | none | steer restore/fix within budget, else stop; integrity breaches stop immediately |
| **Assessment** | S7/S8/S12/S13: authorized-in-substance, actually validated, actually good enough, findings preserved | PM agent | per slice at assess | evidence collection (diff, files, outputs) | all of the decision, recorded with reasoning | steer with written correction, or stop |
| **Human** | plan-flagged consequential work | Human | before flagged slices; on escalations/stops | approval recording; the stop itself | the human's decision | run waits |

**Review freshness is a floor-adjacent mechanical fact:** every commissioned review is recorded in run state with the HEAD (and `before_head`) range it reviewed. Any tree change after a mandatory (elevated-slice) review mechanically invalidates it for acceptance — the review must be re-commissioned against the final HEAD. This is a single freshness comparison, not a revival of launch-contract forensics, and it closes the stale-audit gap (Test 13) for real rather than rhetorically.

Consolidations from the current nine-plus verdict points (validation, drift verdict, drift artifact, review verdict, review artifact, ledger shape, ledger retention, reviewer evidence, changed-files bookkeeping, commit relay…): all semantic checks fold into **Assessment**; all invariants fold into **Floor**. The drift-audit-before-code-review *ordering* survives inside Assessment as the required order of questions ("authorized before good"), and in the commissioned-review sequence on elevated slices.

## 6. PM authority (Phase 4 core)

Per area: **judgement / evidence normally inspected / hard boundaries / human-escalation / assurance lost vs current / simplification gained / why acceptable / accountability / residual mechanical safeguard.**

1. **Evidence sufficiency & acceptance.** Judge whether diff + validation + (risk-dependent) review support acceptance. Evidence: diff, `validation.md`, review artifacts, git state. Hard boundary: floor must pass first; narration alone never suffices. Escalate: evidence contradictory or beyond brief. Lost: deterministic reproducibility of the accept decision. Gained: deletion of the verdict-relay economy (~1,500 LOC + its failure modes). Acceptable because the old determinism was shape-checking (map §3.3) and its cost is documented. Accountable via `assessment.md` citing inspected evidence. Safeguard: floor + per-slice commit (one revert undoes a bad call).
2. **Materiality of deviations.** Accept trivial in-surface deviations (naming, an extra test, comment placement) without ceremony; steer or stop material ones. Boundary: file-surface deviations are never "minor" — floor. Escalate: deviations touching behaviourally sensitive areas. Lost: nothing mechanical existed here (the old system delegated this to reviewer models). Gained: no repair round burned on trivia. Accountable: deviations noted in assessment.
3. **Minor plan-ambiguity resolution.** Resolve typos/inconsistencies (a misspelled path *in prose* where the file list is clear; an obviously wrong validation command flag) by recording an interpretation. Boundaries: never widen the surface, never reinterpret an approval or risk flag, never invent scope; the *file list* itself is what the floor enforces, uninterpreted. Escalate: any ambiguity affecting authorization or acceptance criteria. Lost: strict fail-closed-on-any-ambiguity. Gained: runs stop only for real planning defects. Accountable: interpretation notes in assessment + events.
4. **Validation depth.** Choose between reading validation output and rerunning commands; rerun on elevated slices. Boundary: the contract's validation plan is the minimum bar to be satisfied. Lost: nothing (the old gate read a status field). Gained: honest depth where it matters. Safeguard: elevated slices force the deeper mode.
5. **Independent-review necessity & tooling.** Decide review on standard slices; choose reviewer tool/model; always review elevated slices. Boundary: plan-flagged independence is mandatory; reviewer sessions are read-only-by-instruction with the floor as backstop (same honesty as today). Lost: mechanical launch-contract proof. Gained: deletion of the forensics apparatus (C6) and *structurally stronger* independence (PM commissions, against the final diff — closing the Test 13 stale-audit gap and the verdict-shaping channel). Accountable: review artifacts + assessment.
6. **Steer / relaunch / stop; retries; failure classification.** Judge whether a failure is fixable-in-session, needs a fresh session, or ends the run — under a hard attempt budget (default 3 PM interventions per slice). Boundaries: budget is mechanical; integrity breaches (broken ancestry, tampered state, wrong-slice work) always stop; hard-stop conditions always stop. Lost: the 19-signature taxonomy and streak breaker. Gained: deletion of the machinery whose internal interactions caused Tests 17/18; corrections written from the actual gap. Accountable: every intervention is an event with reason; attempts persist in state.
7. **Operational recovery** (pauses, resets, transients, stalls). Judge from pane/log evidence; wait/nudge/resume as warranted. Boundaries: hard-stop marker floor refuses sends into credential/billing/trust/side-effect prompts, deterministically, as today. Lost: pause budgets/counters, deterministic idle-stall statutes. Gained: C14 deleted; the supervising judgement that already existed stops being duplicated in code. Safeguard: marker floor + event log.
8. **Session freshness & model selection.** Fresh session per slice is the default; PM may steer the live session for corrections (default) or relaunch when context is poisoned; PM may run cheap models for bounded work (docs slices, standard reviews) and must keep strong models where the plan or risk demands. Boundary: launch configuration is recorded per slice; silent model substitution is checked where a harness exposes an inventory (kept — it caught real bugs). Lost/gained: as C7/C11 dispositions.
9. **Completion shape tolerance.** Accept a `result.json` that is imperfect in form when the *evidence* is complete (e.g., missing summary but diff/commit/validation all present) — recording the tolerance. Boundary: result presence itself, slice identity, and the floor are not tolerable-away. Lost: schema-v5 strictness. Gained: no run dies on paperwork (Tests 14/16 class). Accountable: tolerance noted in assessment.

**Not transferred to PM:** plan authorship/expansion; human approvals; the floor; push/deploy/external side effects; dependency/license changes (still plan-level controls, still heuristically guarded by the marker floor + prompt prohibitions — honestly documented as such).

**Rulebook discipline:** PM's prompt-side instructions (SKILL.md) must fit ~120 lines. The empowerment is judgement within a small charter, not a longer statute book — this is a hard acceptance criterion in the blueprint.

## 7. Requirements model

- **Mandatory core (always):** frozen plan; floor; per-slice commit; `result.json` (minimal); PM assessment per slice; durable state + events; run report.
- **Risk-triggered:** independent review; validation rerun; human approval; deeper capture (full transcript retention on elevated slices).
- **Optional guidance:** observation cadence, nudge phrasing, notes-curation style, reviewer tool choice — SKILL.md guidance, not requirements.
- **Sufficient compliance, by example:** a Developer that implements, validates, commits, and writes six honest lines of `result.json` has fully complied — there is no other Developer-side paperwork. A PM assessment of a docs slice may be five lines. An elevated slice's assessment must show the independent review and what it concluded.
- **Non-goals restated:** no Developer-side ledgers, no verdict relays, no evidence formats beyond the artifacts themselves, no proof-of-process files.

## 8. State

Authoritative state: **one copy, outside the worktree**, at `<worktree-git-dir>/pm/<run-id>/run.json` — the worktree-*specific* Git directory (`git rev-parse --git-dir`, resolved as the current controller path does), so linked worktrees get distinct state. A `<worktree-git-dir>/pm/current` pointer names the active run; every command defaults to it and accepts `--run` to select another. State and event writes take an advisory `fcntl` lock; a stale lock is reported, never silently stolen.

**Authority and tamper model — stated honestly.** The Developer session runs as the same OS user, needs Git-metadata access to commit, and can therefore *find* both the state directory and the `pm.py` CLI. There is no OS boundary here, exactly as the current system admits of itself, and building one is a declared non-goal. Within the declared threat model (corner-cutting and sloppy models, not determined adversaries), three cheap mechanisms make forgery a deliberate act rather than a shortcut, and make it detectable:

1. **Run capability token.** `init` mints a random token, prints it once to the operator/PM agent, and stores only its SHA-256 in state. Every mutating command (`finalize`, `approve`, `send`, `stop`, `start-slice`, `review`) requires the token; the Developer prompt and environment never contain it, so a Developer cannot invoke PM authority by simply running the CLI it can see. (Test 7's Developer edited state under gate pressure; this closes the CLI-shaped version of that move.)
2. **Authenticated state writes.** The toolkit HMACs every state write with the token; every read verifies. A Developer editing `run.json` directly (minting an approval, marking a slice accepted) produces a verification failure, which is an integrity stop — restoring the tamper *detection* the dual-copy design provided, without a second copy.
3. **Controller-owned originals.** Every PM-authored artifact — `assessment.md`, `notes.md`, `review-*.md`, the run report — is written under the state directory as the authoritative original and *mirrored* into `.pm/` for human reading. Developer-authored evidence (`result.json`, `validation.md`) stays in `.pm/` — it is inherently a Developer claim, judged against floor-computed facts, never trusted as control input.

So the honest sentence is: `.pm/` vandalism damages the human-facing mirror and the Developer's own evidence, never PM's records or decisions; direct state tampering is detected and terminal; and the residual gap — a same-user process that also captures the token in flight or subverts the PM agent itself — is outside the threat model and documented as such.

`run.json` (schema `lite-1`, validated for the fields PM reads, tolerant of extras):

```json
{
  "schema": "lite-1",
  "run_id": "20260718T090000Z",
  "created_at": "…", "updated_at": "…",
  "status": "active | needs-human | complete | stopped",
  "repo": "/abs/path", "branch": "feature/x",
  "plan": {"path": "/abs/plan.md", "sha256": "…", "slice_count": 5},
  "harness": {"name": "codex", "model": "…", "effort": "…"},
  "reviewer": {"tools": ["copilot"], "model": "…", "effort": "…"},
  "policy": {"max_attempts": 3, "commit_required": true},
  "auth": {"token_sha256": "…"},
  "current_slice": {
    "id": "Slice 3", "artifact_dir": "…", "tmux_session": "…",
    "before_head": "…", "started_at": "…", "attempts": 0,
    "risk": "standard | elevated", "plan_risk": "standard | elevated",
    "wake_at": null, "reviewer_pids": []
  },
  "slices": [
    {"id": "Slice 1", "title": "…", "status": "accepted | attested | stopped",
     "risk": "standard", "plan_risk": "standard", "commit": "…", "attempts": 1,
     "decision": "one-line acceptance reason",
     "reviews": [{"skill": "code-review", "head": "…", "artifact": "…", "sha256": "…"}],
     "assessment": "<state-dir>/slices/slice-001/assessment.md", "summary": "one line"}
  ],
  "approvals": {"Slice 4": {"at": "…", "reason": "…"}},
  "stop_reason": null
}
```

- **Run statuses: 4** (`active` covers running/paused/resuming/between-slices — the event log carries the texture). **Slice statuses: 3** (`accepted`, `attested` — operator-attested prior completion at init, `stopped` — any non-accepted end, with the reason in the entry and assessment). **Risk levels: 2.**
- **Interruption recovery needs exactly:** run.json + the artifact dir + git. `pm status` reconstructs the situation; a live tmux session is re-attached or declared dead by liveness check. `current_slice.wake_at` is a **reserved** slot for a persisted resume time *(amended post-implementation: the shipped toolkit carries the field but provides no setter command — recording a resume time is done in the event log via `send`/`stop` reasons and in the PM's own notes; a dedicated setter is deliberately deferred until a live run demonstrates the need)*; multi-hour *autonomous* recovery depends on the PM harness's own scheduling ability, a declared dependency, not a toolkit feature. If state is deleted or unreadable, `stop --scavenge` still works: session names carry the run-id prefix and reviewer process groups are recorded per launch alongside their artifacts, so a state-independent sweep can find and stop everything (the minimal survivor of the current emergency-stop path).
- **Not persisted:** pause budgets/counters, signature streaks, session generations, launch-config freeze objects (the harness block *is* the run's configuration; per-slice overrides are recorded in the slice entry when used), reviewer policy snapshots, provenance objects, ledger arrays. Superseded attempts live in the event log and artifacts, not as state rows.
- **Human decisions:** `approvals` (as today, simplified) + stop/approve events.
- **Events:** `events.jsonl` beside run.json (append-only): observations on change, sends (with reason), pauses, nudges, interventions, escalations, approvals, stops. This is the reviewability substrate for PM behaviour — same instrument as today, fewer mandatory fields (`ts`, `kind`, `slice`, `note`, optional `evidence` path).

## 9. Artifacts

Per run: `run.json` + `events.jsonl` + the controller-owned originals of `notes.md`, `run-report.md`, and every assessment and review (all in the state dir, per §8); `.pm/runs/<id>/` carries the human-facing mirror of those plus the Developer-authored evidence. `notes.md` (PM-curated: decisions, lessons, interfaces, failed approaches, open findings) replaces prior-slice-context generation, continuation-note ledgers, and residual-finding ledgers as *transport*; the report remains the human-facing sink and regenerates from controller-owned data alone — never from the mirror.

Per slice (`.pm/runs/<id>/slices/slice-NNN/`), each with producer → consumer / lifetime / why irreplaceable:

| Artifact | Producer → Consumer | Why it can't be simpler |
|---|---|---|
| `prompt.md` | PM → Developer, audit trail | the rendered contract is the authorization record for the session |
| `pane.txt` (final; plus `pane-live.txt` rolling) | toolkit → PM, humans | debugging dead/stalled sessions (Test 4 lesson); two files, not six families |
| `transcript.jsonl` (when the harness exposes one) | toolkit → PM, humans | richer than pane text where available |
| `diff.patch`, `status-before.txt`, `status-after.txt` | toolkit → PM assessment, reviewers, humans | the change itself; review input |
| `validation.md` | Developer → PM | validation evidence in the Developer's own words + captured output |
| `result.json` | Developer → PM | completion signal + minimal facts (below) |
| `review-*.md` (elevated/discretionary) | Reviewer session → PM, humans | the independent opinion, as written; controller-owned original, sha256 + reviewed-HEAD recorded in state |
| `assessment.md` | PM → humans | **the accountability record**: floor results, what was read, risk level + trigger, decision + reasoning, findings, interventions; controller-owned original |

Each Developer launch gets its own `attempt-<n>/` subdirectory for its `result.json` and pane capture; the toolkit rotates any prior attempt's completion signal there before launching, so a restarted or steered session can never be mistaken for complete on stale evidence. These are launch-scoped folders, not the old per-round archive families — nothing gates on their contents.

Gone, with their reason recorded in the ledger: reviewer-policy/request/manifest/status/launch families, per-round archived results and repair prompts, per-round pane/status families, reconciliation files, provenance objects, `prior-slice-context.md` (superseded by `notes.md`), observation-latest snapshots, activity JSONL families, tool-home/credential seeding trees (PM-commissioned reviewers run under ambient auth like any other session PM starts).

Lifetimes: everything persists for the run's life; `archive/`-style cleanup is the operator's business. There is no `archive-sensitive` command because credential seeding is gone — but pane captures, transcripts, validation output, and review reports still contain code and can contain echoed secrets or environment values, so the README's privacy section retains the artifact-sensitivity table and cleanup guidance.

## 10. Session model

- **One fresh tmux Developer session per slice** — retained deliberately: it is the context-limit strategy and the clean-authorization boundary (`before_head`). Not dogma: PM steers the *live* session for corrections (cheaper than relaunch, preserves context — today's in-session repair, minus the statutes) and relaunches fresh when the session is dead or its context is poisoned (today's fresh-session escalation, minus the streak algebra).
- **Reviewer sessions:** short-lived, PM-commissioned, read-only-by-instruction, run to completion, captured, torn down. One-shot (`-p`/exec) harness modes preferred where the tool supports them; tmux otherwise — using the same profile table as Developer launches. **Review input is pinned, not live:** the toolkit generates the review input from `git diff <before_head>..<HEAD>` plus files at the pinned HEAD, so a still-running or restarted Developer session cannot race the reviewer; PM quiesces (or has already stopped) the Developer session before commissioning a mandatory review. Reviewer child pids/process-groups are recorded in state so `stop` reaps them (the Test 8 orphaned-reviewer lesson).
- **Lifecycle costs addressed:** stale sessions reaped at slice start (kept); liveness re-checked before any send (kept); local-model cold-start/prefill patience is SKILL.md guidance (kept as words, not windows-and-ceilings).
- **Context transfer between sessions:** `notes.md` (bounded by PM curation; the 512 KiB hard cap is kept as a tripwire with a warning, since a runaway notes file would silently degrade every later prompt).

## 11. Failure handling

- **PM resolves directly:** stalls (nudge), transients (wait/retry), imperfect result shapes (tolerate + note), trivial deviations (accept + note), fixable gaps (steer with a written correction), dead sessions (relaunch with the frozen prompt + notes).
- **Bounded by:** `max_attempts` PM *interventions* per slice — precisely: `attempts` starts at 0 on the initial launch and increments on every steer (a corrective `send` after a floor/assessment failure) and every relaunch; pure observation, waits, and stall nudges do not count. Default budget 3: initial launch, then at most three interventions before a mandatory stop. Mechanical counter, persisted, non-negotiable. On exhaustion: stop with the full story in the assessment.
- **Always stops, no discretion:** integrity breaches (HEAD not descended / tree rewritten / state tampered / wrong-slice work); plan digest changed; approval-gated slice without approval; hard-stop marker conditions; floor-failing work PM cannot get restored within budget.
- **New plan needed (stop + report):** contract defective or infeasible in a way interpretation can't honestly bridge; repeated failures that PM judges to be plan-caused (the report says so — PM still never edits the plan).
- **Distinguishing model-misbehaviour from bad plans:** PM's job, in the assessment: repeated same-shape failures across a relaunch with a clean prompt point at the plan or the task; failures that shift shape point at the model; PM writes which it believes and why. No mechanism pretends to make this call.
- **No repair-budget taxonomy, no circuit breaker, no signature streaks, no operational-vs-substantive round split.** One budget, one judge, full narrative in events + assessment. The current machinery's own record (Tests 14–18) is the argument that the taxonomy's interactions cost more than its classifications earned.

## 12. Command surface

Ten commands (from 19). All read/write the single authoritative state; all are safe to re-run. Every mutating command (`init` excepted — it mints the token) requires `--token` (or `PM_RUN_TOKEN` in the *controller's* environment, never the Developer's): `approve`, `start-slice`, `send`, `finalize`, `review`, `stop`. Read-only commands (`check-plan`, `status`, `observe`) do not.

| Command | Purpose / user intention | Reads → Writes | Essential? |
|---|---|---|---|
| `check-plan --plan [--repo]` | "Is this plan runnable?" | plan, worktree → stdout | Yes (also auto at `init`) |
| `init --repo --plan --harness [--branch/--create-branch] [--attest "Slice 1,…"] [--max-attempts]` | "Set up this run" (incl. preflight) | plan, git → run.json, `.pm/` skeleton | Yes |
| `status [--report]` | "Where are we?" (+ regenerate report) | state, tmux, git → stdout, report | Yes |
| `approve --slice --reason` | "I approve this gated slice" | → approvals, event | Yes |
| `start-slice [--model/--effort/--reviewer-tools…]` | "Run the next eligible slice" | state, plan → session, prompt, current_slice | Yes |
| `observe [--wait N]` | "Show me evidence" (bounded wait folded in) | tmux, files, git → stdout, events, pane-live | Yes |
| `send --text --reason` | "Steer the live session" (hard-prompt floor enforced) | → tmux, event | Yes |
| `finalize` | "Run the floor and collect assessment evidence" — outputs floor results + evidence paths; **accepts only on a passing floor plus PM's explicit `--accept "reasoning"` / records `--stop`/`--steer` otherwise** | git, artifacts → slice entry, state, report | Yes |
| `review --slice --skill drift-audit\|code-review [--tool/--model]` | "Commission an independent review of the final diff" | diff, contract → review artifact | Yes (elevated path) |
| `stop --reason [--slice-status stopped] [--scavenge]` | "End it, preserving evidence" (evidence capture folded in — one stop, not three); `--scavenge` sweeps run-prefixed tmux sessions and recorded reviewer process groups even when state is unreadable | tmux, files → terminal state, captures | Yes |

Dropped, with their need re-housed: `run-next`/`run --scope remaining` (the PM agent *is* the loop; the unattended no-model mode is dropped by owner decision — §16.1), `wait` (→ `observe --wait`), `pause-until` (PM schedules its own re-observation), `profiles`/`preflight` (→ `init`/`start-slice` checks + `--help`), `reconcile` (its cause — gate-stopped recoverable runs — is designed out; a stopped slice is re-run with `start-slice` after human review), `stop-with-evidence` (→ `stop`), `summarize` (→ `status --report`), `archive-sensitive` (nothing sensitive written).

**`finalize` is deliberately not auto-accepting:** the mechanical part reports; the acceptance is PM's recorded act. This is the accountability seam made visible in the CLI.

*(Amended post-implementation, from the Stage 7 Test 19 findings — two observation-honesty fixes, no new command, no state-shape change: **`observe --wait N`** waits the full `N` and returns early only on a meaningful signal — session death, `result.json` appearing, or a hard-stop marker becoming visible — never on a mere pane byte-change; TUI spinner/stream churn made the original any-change early-return defeat the wait entirely and misled the PM about elapsed time. The outcome now also reports the actual elapsed wait so requested and elapsed duration can never be conflated. **`review`** prints the report path, stderr path, and reviewer process-group id at launch, before its synchronous wait — a slow-but-alive local reviewer and a hung one were otherwise indistinguishable from the PM seat — and accepts an optional `--timeout N` that kills the reviewer process group and fails closed with a recorded event; there is deliberately no default timeout, because picking a ceiling for a legitimately slow cold local model would be a statute where patience is the PM's judgement.)*

## 13. Interfaces and data shapes

1. **Plan format** — unchanged, owned by `implementation-plan` (producer: planning session; consumer: PM parser + humans). Already minimal for its job; `Independent audit required: yes` maps to elevated risk. Versioning: none needed (heading-shape contract, checked by `check-plan`).
2. **`result.json`** (producer: Developer; consumer: PM):

   ```json
   {"slice": "Slice 3", "status": "done | blocked",
    "summary": "one paragraph", "notes": "optional free text for the run notes"}
   ```

   Why structured at all: file-appearance is the completion signal and slice identity must be machine-checkable (wrong-slice work is an integrity stop). Why nothing more: every removed field (changed_files, commit hash, verdicts, ledgers, validation array) was either recomputable from git, a relay of another artifact, or a schema trap. Mandatory: `slice`, `status`. Tolerated: extra fields ignored; missing `summary` is a noted imperfection, not a failure. Versioning: additive-only by policy; no version field.
3. **`run.json` (`lite-1`)** — §8. Producer/consumer: PM toolkit only. Versioning: the `schema` string; the toolkit refuses future-versioned state with a clear message (no migration machinery — a run is days long, not years).
4. **Prompt contracts** (developer, reviewer, steer-messages) — reference-doc templates rendered by the toolkit, single-sourced, each ≤ ~60 lines. The reviewer prompt embeds the named skill's **complete transitive bundle** — SKILL.md plus every locally-linked Markdown resource, path-escape-guarded — because `code-review` mandates `review-matrix.md` and conditionally `scientific-and-language-priorities.md`; SKILL.md-only embedding would silently truncate the review contract. (~40 LOC, behaviour re-specified from the current launcher's proven approach.)
5. **No reviewer policy/request JSON, no manifests, no verdict sentinels, no provenance records.** Where the old system needed them, the need dissolved with the topology change (map C6).

## 14. Repository structure (proposed final state)

```text
ai-agent-coder/
├── README.md                      # updated: Mode B described per Lite
├── CHANGELOG.md                   # + Lite replacement entry
├── CONTRIBUTING.md                # doc map updated
├── docs/
│   └── VISION.md                  # ← replaced by proposed-vision.md at adoption
├── .github/workflows/ci.yml      # compiles + tests the new pm + orchestrator
├── skills/
│   ├── implementation-plan/       # unchanged except: PM-parsing section updated to Lite fields
│   ├── scoped-implementation/     # unchanged
│   ├── drift-audit/               # unchanged (vocabulary + standalone skill)
│   ├── code-review/               # unchanged
│   ├── code-simplifier/           # unchanged
│   ├── commit/                    # unchanged
│   ├── handoff/                   # PM-exclusion note updated
│   ├── report/                    # PM-artifact pointer updated
│   ├── orchestrator/              # standalone skill retained; PM-specific surface removed:
│   │   ├── SKILL.md               #   (pm-slice-contract.md deleted; PM-binding policy
│   │   ├── references/…           #    fields + reserved_skill_sets + PM_AUDIT_VERDICT
│   │   └── scripts/…              #    machinery deleted; see replacement ledger)
│   └── project-manager/           # ★ Mode B Lite — all files new
│       ├── SKILL.md               # ~120 lines: charter, workflow, launcher, floor, escalation
│       ├── README.md              # ~150 lines: CLI, state layout, verify-your-setup trial
│       ├── references/
│       │   ├── developer-prompt.md    # ~60 lines
│       │   ├── reviewer-prompt.md     # ~40 lines
│       │   └── run-state.md           # ~80 lines
│       ├── scripts/
│       │   ├── pm.py              # thin entrypoint
│       │   └── pm_lib/
│       │       ├── cli.py         # ~120  argument parsing, 10 commands
│       │       ├── plan.py        # ~250  parse/check/freeze/eligibility (behaviour respecified, code new)
│       │       ├── state.py       # ~200  lite-1 state, events, report rendering
│       │       ├── git_ops.py     # ~150  changed-files, ancestry, cleanliness, surface matching
│       │       ├── floor.py       # ~150  the mechanical floor, one module, one function surface
│       │       ├── sessions.py    # ~280  tmux control, readiness, hard-prompt markers, capture
│       │       ├── profiles.py    # ~160  4 harness profiles + model-identity check
│       │       ├── slice_ops.py   # ~300  init/start/observe/send/finalize/stop orchestration
│       │       ├── review.py      # ~120  PM-commissioned reviewer sessions
│       │       └── prompts.py     # ~100  template rendering
│       └── tests/                 # ~2,800–3,400 LOC, ~140–170 tests, fake-harness pattern retained
└── (deleted: every current skills/project-manager file; .ai-pm/ vocabulary; pm-slice-contract)
```

Estimated new implementation: **~2,200–2,600 LOC** toolkit (vs 8,406 + the 2,540-LOC orchestrator dependency) — a non-binding range, re-estimated after the Stage 1–2 spike, that includes the run token/HMAC, locking and run discovery, attempt directories, review freshness recording, transitive skill embedding, and the state-independent scavenge path; ~450–550 lines of PM docs (vs 1,647). Per-module figures in the tree above are indicative, not caps — behaviour coverage beats the reduction number wherever they conflict. State lives in `<worktree-git-dir>/pm/`; artifacts in `.pm/`; prompts in `references/`; approval rules in the plan + SKILL.md charter; the adopted vision at `docs/VISION.md`.

## 15. Conceptual walkthroughs

1. **Small low-risk change** (docs slice). `start-slice` on a cheap model; Developer edits two files, commits, writes result.json. `finalize`: floor passes in milliseconds; PM reads the 20-line diff, checks the rendered docs claim against the contract, accepts with a five-line assessment. No reviewer, no human. Total ceremony: one prompt, one result file, one assessment.
2. **Normal multi-step feature** (5 slices). Prepare once; per slice: fresh session, implement, floor, assessment; notes.md accumulates the API decisions slice 2 made so slice 4's session knows them. Human sees nothing until the final report unless a flag or floor trips.
3. **Long task crossing context limits.** Slice 3's Developer session grows slow and confused; PM relaunches fresh (attempt 2) with the frozen prompt + current notes.md; the new session reads the accepted state from git and continues. The old system's per-round archives and recombined prompts aren't needed — git + notes are the state.
4. **Developer drifts materially.** Diff shows a refactor of an unauthorized module. Floor fails (`changed files ⊄ surface`) before any judgement: PM steers a restore ("revert files X,Y; keep authorized work"), attempt 2 restores, floor passes, assessment proceeds. Had it not restored within budget: stop, human sees exactly what the floor saw.
5. **Minor deviation accepted by judgement.** Contract says "add validation to `load_config`"; Developer also added a two-line type hint to its caller *inside the authorized file list*. Old system: reviewer-model lottery, possible repair round. Lite: PM notes "in-surface, behaviour-neutral, aids the acceptance criterion — accepted" in the assessment. One line instead of one round.
6. **Failed validation.** `validation.md` shows a failing test the result glosses over. Assessment gate: PM steers with the actual failure text (attempt 2). Fixed → re-assess from scratch (floor re-run, diff re-read). Not fixed within budget → stop with the failure in the assessment.
7. **Repeated failed attempts.** Same test fails after a steer (intervention 1), a fresh relaunch (2), and one more steer (3) — the budget is exhausted. PM stops; assessment records the interventions, PM's judgement that the contract's acceptance criterion conflicts with an existing behaviour, and the recommendation to re-plan. No signature streak needed to reach "three strikes" — the budget is the breaker.
8. **Stalled/interrupted harness.** Pane byte-identical across two observations, process near-idle. PM nudges (event logged; costs an attempt only if it escalates to steer/relaunch — a pure nudge is an observation-level action). Harness later killed by a usage limit with a clear 5-hour reset: PM waits (its own scheduling; no `pause-until` statute), re-observes, sends the continuation. Weekly-cap wording instead → hard-stop floor refuses continuation; PM stops for the human.
9. **Consequential change requiring human approval.** Slice 4 is `Approval needed: yes`. `start-slice` refuses; PM reports why and waits. Human runs `approve --slice "Slice 4" --reason …`; slice proceeds as elevated (independent review mandatory).
10. **Independent review unnecessary.** Standard slice, clean 40-line diff, validation rerun cheap and green, PM finds nothing suspicious: assessment says "review: PM assessment only (standard risk)". That sentence is the honest replacement for today's `developer-self-audit` provenance machinery.
11. **Independent review required.** Slice flagged `Independent audit required: yes`. After the Developer commits, PM runs `review --skill drift-audit --tool copilot`, then `review --skill code-review`, both pinned to the committed HEAD. The code-review report lists a P1; PM steers the fix — which changes the tree, so **both** recorded reviews are mechanically stale for acceptance. PM re-runs the floor and re-commissions both reviews against the new final HEAD, then accepts. The freshness rule (§5) makes the Test 13 stale-audit shape impossible by construction, and the reviewer never talked to the Developer, so there is no verdict string to shape.
12. **PM discretion where the old system was mechanical.** Developer's result.json is missing `summary` (Test 16's class of paperwork failure). Old: `result-malformed`, repair round, possible ledger-retention cascade. Lite: PM notes the imperfection, takes the summary from the pane, accepts on the evidence. Zero rounds.
13. **Deliberately less protection.** A subtly wrong-but-plausible in-surface change passes validation, PM review, and (standard slice) no independent reviewer sees it. The old system would not have caught it either (its review gate checked a verdict string produced by a model that also missed it) — but the old system *claimed* more. Lite's report says what was and wasn't checked; the residual risk is one revertable commit. This is the honest cost of the trade, stated.
14. **Low-cost subagent for bounded work.** Docs-only slices run on the cheapest configured harness model; standard-slice reviews use a mid-tier reviewer model. PM records model choices per slice. Nothing in the floor or state cares which model ran — only the assessment's confidence language does.
15. **Strong-model judgement deliberately retained.** Slice touches the risk-flagged persistence layer (elevated). PM does not delegate the acceptance reasoning: it reruns validation itself, commissions both reviews, reads the full diff, and writes the acceptance analysis itself. If the operator has placed a weak model in the PM seat, SKILL.md's charter says plainly: elevated slices deserve a strong PM model or a human checkpoint — the seat, not the machinery, is the assurance.

## 16. Where this design consciously touches its own limits

Surfaced per the brief, not hidden. (Items 5–8 were added after the independent Codex design review of the first committed revision; see the review record in the implementation blueprint.)

1. **The unattended-batch capability is not reproduced.** The current fail-closed no-model path (`run --scope remaining`) has no Lite equivalent; Lite always has a PM agent in the loop. The one persona use it served (fully-local, no supervising model, accept first-ambiguity stops) loses a capability. Judgement: its deterministic-acceptance machinery is exactly the mass being removed; rebuilding it would rebuild the old system. Mitigation: a local model can hold the PM seat. **Owner decision (2026-07-18): the loss is accepted — the unattended batch mode is dropped entirely, with no minimal scripted substitute.** Lite always has a PM agent in the loop.
2. **PM-seat quality is now load-bearing** — stated in the vision, the SKILL.md charter, and walkthrough 15, rather than mitigated by machinery.
3. **Dependency/license/side-effect stops remain heuristic** (markers + prompts + plan-level exclusion), as today — Lite inherits the current honesty rather than solving the unsolved.
4. **`attested` slices remain narration** (operator-attested prior work at `init --attest`), as today's `assumed-complete` — kept because re-verifying another run's work is out of scope, and the entry says what it is.
5. **Role authority is capability-raised, not OS-enforced.** The run token and HMAC (§8) make Developer self-acceptance and state forgery a deliberate, detectable act rather than a shortcut; a same-user process that hunts for the token in the controller's environment or subverts the PM agent itself defeats them. That residual is squarely outside the declared threat model and is stated in the vision's guarantees section.
6. **The floor covers Git-visible final state only.** Ignored files, Git metadata/hooks, and write-then-revert effects inside the slice's lifetime escape it (§3.3); they are controlled heuristically (markers, prompts, plan-level exclusion), exactly as today, and appear in the assurance-loss register rather than in the mechanical-guarantee list.
7. **Multi-hour autonomous recovery depends on the PM harness.** The toolkit persists `wake_at` and refuses hard-stop continuation, but it has no scheduler; a PM agent that cannot wait five hours hands the resume to a human. Declared, not solved.
8. **PM-curated notes are a trusted input.** `notes.md` is controller-owned (§8), which removes Developer tampering, but a PM that curates badly poisons its own later prompts — another face of the PM-seat dependency (item 2).
