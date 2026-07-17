# Mode B Lite — Replacement Ledger

**Status:** Stage-1 design report. The deletion-and-replacement plan that enforces the clean-replacement requirement: after implementation, no stale current-Mode B implementation, terminology, schema, or hidden dependency remains.
**Treatments used (only these):** Retain · Retain but simplify · Merge · Replace with PM judgement · Make risk-dependent · Replace completely · Delete.

Reading rules for the tables:

- **"Replace completely" means behaviour re-specified, code written from scratch** in the new `pm_lib` — never copied/edited. "Retain" for a *file* means the file survives as-is; "Retain" for a *behaviour* inside a rewritten module means the behaviour is re-specified in the design and newly implemented.
- **Default: no code reuse.** The **only** proposed reuse candidates in this entire ledger are listed in §9, each with its independent justification. Everything else is greenfield.
- **Complexity reduction** is stated per group (est. = judgement anchored on the measured baseline in the [current-state map](current-state-map.md) §1 and the blueprint's metrics).
- **Loss / retained value** columns state assurance lost and practical value kept, per the brief. Cross-references (C-numbers) point into the current-state map; (S-numbers) into the target design's spine.

---

## 1. `skills/project-manager/` — every file

### 1.1 Documentation

| File (lines) | Purpose | Treatment | Lite replacement | Reason · loss · retained value |
|---|---|---|---|---|
| `SKILL.md` (214) | operating contract, roles, launcher, safety rules, trust boundary | **Replace completely** | new `SKILL.md` (~120 lines): charter, floor, workflow, launcher, escalation | The old text encodes the dual-path operating model and the 19-signature trust boundary. Lost: the deterministic-acceptance description (deliberate). Retained: launcher-in-one-place convention, honesty style, hard-stop list. |
| `README.md` (547) | CLI guide, run-state tour, plan eligibility, privacy, setup trial | **Replace completely** | new `README.md` (~150) | Lost: nothing practical — content tracks deleted mechanisms. Retained: the "Verify Your Setup" fake-harness trial pattern (rewritten for the new CLI), privacy/data-flow table (rewritten, simpler: no credential seeding). |
| `AGENTS.md` (45) | maintainer map | **Replace completely** | new short maintainer note (or fold into README) | Tracks the new module layout. |
| `references/run-state-schema.md` (543) | schema v5 semantics, repair state, reviewer binding, ledgers | **Delete** (superseded) | `references/run-state.md` (~80) | 543→~80 lines because the state it documents shrinks by that ratio. Lost: the documented v5 semantics (moot with the schema). |
| `references/developer-prompt.md` (183) | prompt + repair templates + 12 stanzas | **Replace completely** | `references/developer-prompt.md` (~60) + `reviewer-prompt.md` (~40) | Lost: per-signature stanzas (PM writes corrections from the actual gap). Retained: single-sourced template discipline, format-string editing note. |
| `references/harness-adapter-contract.md` (115) | adapter methods, tmux rules, profile composition, identity verification | **Retain but simplify** (as content), file itself **Replace completely** | folded into `README.md`/`run-state.md` + `profiles.py` docstrings | The observed per-harness facts (readiness markers, paste discipline, coverage gaps) are hard-won evidence — carried forward as content, not as a standalone contract doc for a pluggable-adapter abstraction Lite doesn't build. |

### 1.2 Implementation (`scripts/`)

| File (LOC) | Purpose | Treatment | Lite replacement | Reason · loss · retained value |
|---|---|---|---|---|
| `pm.py` (21) | thin entrypoint | **Replace completely** | new `pm.py` | Trivial; new file for the new CLI. |
| `pm_lib/cli.py` (234) | 19-command parser | **Replace completely** | new `cli.py` (~120), 10 commands | Commands dropped per design §12; loss/retention recorded there. |
| `pm_lib/commands.py` (1,281) | command handlers incl. reconcile, archive-sensitive, emergency paths | **Replace completely** (most behaviour re-housed), with parts **Delete** | `slice_ops.py` (~300) + parts of `state.py` | Deleted outright: `reconcile` (cause designed out), `archive-sensitive` (nothing sensitive written), `_emergency_halt_without_state` + tampered-mirror archaeology (single-copy state), dual-path guards. Retained as respecified behaviour: init, status/report, approve, observe/send, stop-with-capture. |
| `pm_lib/plan.py` (348) | plan parse, check-plan, eligibility | **Replace completely** (behaviour **Retain**) | new `plan.py` (~250) | The one module whose *behaviour* is retained nearly 1:1 (spine S1). Newly written to the same spec: headings, seven sections, surface path rules, segment-aware match semantics, approval-flag exactness, digest freeze, duplicate-id rejection, lint warnings. Loss: none intended. |
| `pm_lib/state.py` (1,132) | schema-v5 validation, dual-copy control, reports, events | **Replace completely**, majority **Delete** | new `state.py` (~200) | Deleted: closed-field validation lattice (~450 LOC), controller-mirror machinery (C3), slice-entry evidence-shape validation, provenance rendering. Retained (respecified): atomic writes, event log + counter recovery, report rendering, approval records. Loss: tamper-*detection* on in-worktree state — replaced by tamper-*irrelevance* (authoritative state outside worktree). |
| `pm_lib/gates.py` (1,104) | verify_gate, 19 signatures, reviewer forensics, ledgers, reconciliation | **Split:** floor **Replace completely** (retained behaviour); everything else **Replace with PM judgement** or **Delete** | `floor.py` (~150) + `git_ops.py` share | Retained mechanically (S6/S9): changed-files⊆surface, ancestry/HEAD, clean worktree, digest check, slice-id match, result presence. Replaced with PM judgement: validation/drift/review verdict gates, artifact-shape checks, changed-files bookkeeping, ledger shape/retention/cross-check. Deleted: commit-hash reconciliation (+ its artifacts), reviewer-evidence forensics, provenance derivation. Loss: deterministic semantic verdicts (already shape-only — map §3.3); mechanical retention (net-negative in live runs). |
| `pm_lib/git_ops.py` (170) | changed-files, ancestry, status, surface matching | **Replace completely** (behaviour **Retain**) | new `git_ops.py` (~150) | Spine. Segment-aware `PurePath.full_match` semantics re-specified exactly (Python ≥3.13 stays a requirement). |
| `pm_lib/runner.py` (1,445) | dual-path drivers, repair resolver, breaker, relaunch, idle-stall | **Replace with PM judgement** (steer/relaunch/stop under budget) + **Delete** (taxonomy, breaker, dual budgets, batch driver) | judgement + ~80 LOC of `slice_ops.py` (attempt counting, relaunch) | Loss: deterministic repair classification & the unattended batch loop (capability loss accepted by owner decision — design §16.1). Retained: nudge/steer/relaunch/stop *capabilities*, attempt budget, frozen-prompt relaunch. |
| `pm_lib/runtime.py` (460) | slice env/paths, reviewer policy writing, credential seeding, transcript capture | **Delete** (reviewer policy, tool-home/credential seeding, `contracted_marker`) + **Merge** (env/paths/capture into `sessions.py`/`slice_ops.py`) | — | Env surface shrinks to `PM_SLICE_ARTIFACT_DIR`, `PM_PLAN_PATH`, `PM_SLICE_ID`, `PM_NOTES_PATH`, `PM_RESULT_PATH`, `TMPDIR`. Loss: reviewer-side env contract (moot — PM commissions reviews). |
| `pm_lib/context.py` (165) | prior-slice-context generation, digest, budget | **Replace with PM judgement** (curated `notes.md`) + **Retain** (size tripwire) | notes handling in `slice_ops.py` (~30 LOC) | Loss: mechanical provenance labelling and digest-terminal gate on context; retained: knowledge carry-forward (S4), hard size cap as warning. Judgement call recorded: the digest gate protected against a real-but-rare tamper; PM re-reads its own notes file each slice, and the file lives in the worktree — accepted residual risk, noted in assessment when suspected. |
| `pm_lib/prompts.py` (310) | developer/repair prompt rendering, stanzas, orchestrator embedding | **Replace completely** | new `prompts.py` (~100) | Lost: 12 repair stanzas, embedded 43-line delegation contract (both moot). |
| `pm_lib/hints.py` (362) | 10 hint kinds, 5 subtypes, confidence/reset parsing | **Retain but simplify** (hard-stop floor) + **Replace with PM judgement** (advisory taxonomy) | marker floor inside `sessions.py` (~60 LOC) | Retained mechanically: refuse-send/continue on credential/trust/permission/billing/side-effect/weekly-unknown-limit markers (S11). Replaced: structured kinds/subtypes/confidence/reset parsing — the PM agent reads the pane. Loss: machine-parsed reset times (PM parses them itself; the current parser was already deliberately narrow). |
| `pm_lib/observation.py` (363) | observe/wait/pause, event recording, idle-stall statute | **Merge** into `slice_ops.py`/`sessions.py`, statutes **Replace with PM judgement** | `observe --wait` (~100 LOC) | Loss: 3-window/600s idle statute, pause budgets. Retained: bounded wait, on-change event recording with floor, live-pane preservation. |
| `pm_lib/tmux_adapter.py` (356) | session control, readiness, hard-prompt markers, capture | **Replace completely** (behaviour **Retain**) | new `sessions.py` (~280) | Spine-adjacent operational infrastructure; per-harness readiness/marker facts carried forward verbatim as data. |
| `pm_lib/profiles.py` (264) + profile tables in `constants.py` | 4 profiles, command composition, model-identity query | **Retain but simplify** (behaviour), files **Replace completely** | new `profiles.py` (~160) | Retained: 4 profiles, compose-don't-hand-write commands, fail-closed bare-harness rule, OpenCode inventory + display check (caught real bugs). Simplified: launch-flag tri-state collapses (`--allow-unattended-default` deleted — one composed path + `--harness-command` for tests). |
| `pm_lib/constants.py` (226) | signatures, vocabularies, budgets, markers, profiles | **Delete** (vocabularies, signature sets, supervision defaults) / surviving constants move beside their single consumer | — | One source of truth per retained concept. |
| `pm_lib/models.py` (120), `process.py` (14), `utils.py` (18), `__init__.py` (13) | dataclasses, subprocess, helpers | **Replace completely** | minimal equivalents inline | No standalone value. |

### 1.3 Tests

| File (LOC / tests) | Treatment | Reason |
|---|---|---|
| `tests/pm_test_helpers.py` (865) | **Replace completely** | The *pattern* (fake harnesses via `--harness-command`, tmux-skip guards, `PmTestCase`) is explicitly carried into the new suite's design; the code is rewritten against the new CLI/state. |
| `test_plan_state.py` (1,063/63) | **Replace completely** | Plan-parser behaviours re-pinned nearly 1:1 (spine); state tests shrink with the schema. |
| `test_gates_verification.py` (1,780/65) | **Replace completely**, mostly **Delete** in effect | Floor tests re-pinned (~15 scenarios); verdict/ledger/reviewer-forensics tests are moot. |
| `test_supervision_repair.py` (1,961/46) | **Delete** in effect (taxonomy/breaker/dual-budget tests), **Replace** the steer/relaunch/stop/budget behaviours (~10 scenarios) | The largest single test mass pins deleted machinery. |
| `test_harness_adapters.py` (620/51) | **Replace completely** (behaviour retained) | Readiness/marker/profile/identity tests carry forward as scenarios with recorded fixtures. |
| `test_observation_hints.py` (524/22) | **Replace** floor-marker tests; **Delete** taxonomy tests | Per hints.py split. |
| `test_runtime_batch.py` (843/31) | **Delete** in effect | Pins the batch driver and reconcile; both gone. Timeout/dead-session scenarios re-pinned in slice_ops tests. |
| `test_prompts.py` (563/28) | **Replace completely** | New templates, ~8 tests. |

**Net test target:** ~120 tests / ~2,500 LOC (from 306 / 8,219). Reduction follows mechanism deletion, not lowered coverage standards: the retained behaviours keep boundary-focused tests.

---

## 2. `.ai-pm/` state & artifacts

| Item | Treatment | Lite replacement | Notes |
|---|---|---|---|
| `.ai-pm/` directory name & layout | **Delete** | `.pm/` (artifacts, self-ignoring) + `<git-common-dir>/pm/` (authoritative state) | No migration: existing `.ai-pm/` trees are historical data the new system never reads (prefer deletion over deprecation; the operator archives or deletes them). |
| `run.json` schema v5 (17 fields, closed) | **Replace completely** | `lite-1` (design §8) | 10→4 run statuses, 7→3 slice statuses, repair/supervision/launch-freeze/reviewer-policy/provenance objects deleted. |
| controller copy + mirror equality (C3) | **Delete** | single authoritative copy outside worktree | Same protection, no apparatus. |
| `operational-events.jsonl` + counter sidecar | **Retain but simplify** | `events.jsonl` (5-field lines) | Same instrument, fewer mandatory fields. |
| `prior-slice-context.md` (+digest, budget, projection) | **Replace with PM judgement** | `notes.md` curated by PM (+512 KiB tripwire) | See §1.2 context.py row. |
| `developer-result.json` schema v5 (13 fields, ledgers) | **Replace completely** | 4-field `result.json` | Loss: relayed verdicts/bookkeeping (recomputed or judged instead). |
| per-round repair artifacts (archived results, repair prompts, fresh-session prompts, per-round panes/statuses) | **Delete** | events + assessment narrative | Consumers (retention check, recombined prompts) are gone. |
| `slice-summary.md`, `run-report.md` | **Merge** | `assessment.md` per slice + `run-report.md` | Assessment is richer (reasoning, not just fields); report aggregates. |
| `audit_provenance`, `pm-reconciliation.*`, `observation-latest.json`, `activity-attempt-*.jsonl`, `model-identities.json` (as artifact), `reviewer-policy.json`, `reviewer-evidence.md`, `reviewer-runs-summary.json`, `reviewer-cancel-summary.json`, tool-home/credential trees, `emergency-stop/`, `run.json.tampered-*`, `stale-sessions/*.txt` | **Delete** | assessment text; model identity noted in slice entry; review artifacts as plain `review-*.md`; stale-session reaping logs to events | Each consumer is deleted or judgement-based. Credential seeding disappears entirely (PM-commissioned reviewers use ambient auth). |
| `pane-capture*` six-family | **Retain but simplify** | `pane.txt` + `pane-live.txt` (+ per-attempt suffix on relaunch) | Debugging value proven (Test 4); variants tracked deleted round structure. |
| `prompt.md`, `git-status-before/after`, `git-diff.patch`, `validation-summary.md` → `validation.md`, transcripts | **Retain** (names simplified) | same roles | Core evidence set (S5/S12). |

## 3. PM commands (19 → 10)

Full mapping in design §12. Ledger dispositions: **Retain (simplified):** `check-plan`, `init` (absorbs preflight/profiles; gains `--attest`), `status` (absorbs `summarize`), `approve`, `start-slice`, `observe` (absorbs `wait`), `send`, `stop` (absorbs `stop-with-evidence`). **Replace completely:** `finalize-slice` → `finalize` (floor + explicit PM accept/steer/stop — the accountability seam). **New:** `review`. **Delete:** `run-next`, `run --scope remaining` (batch driver; capability loss accepted by owner, design §16.1), `pause-until` (PM schedules), `reconcile`, `archive-sensitive`, `profiles`, `preflight`.

## 4. Repair machinery, classifications, budgets

| Item | Treatment | Replacement |
|---|---|---|
| 19-signature taxonomy (`REPAIRABLE_SIGNATURES`/`TERMINAL_SIGNATURES`) | **Replace with PM judgement** | PM classifies in prose; the only hard categories left are *floor-fail* (never acceptable) and *integrity/hard-stop* (always stop) |
| Signature-keyed circuit breaker (streaks) | **Delete** | attempt budget is the breaker |
| Dual budgets (`round` / `operational_round`) | **Delete** | one budget; pure nudges don't consume it (design §11) |
| `policy.max_repair_attempts` | **Retain** | `policy.max_attempts` (default 3), mechanical |
| Repair-prompt stanza table | **Replace with PM judgement** | PM writes the correction |
| Idle-stall statute (3×600 s) & transient reclassifier | **Replace with PM judgement** | guidance in SKILL.md; hard-stop floor unchanged |
| Reviewer-policy digest refresh per round (+`operational_round` binding) | **Delete** | moot — PM commissions reviews after implementation, against the final diff |
| Fresh-session prompt recombination (ledger re-injection) | **Delete** | relaunch = frozen prompt + current `notes.md` |

## 5. Reviewer-policy integration & orchestrator surface

| Item | Treatment | Notes |
|---|---|---|
| `orchestrator` skill as a standalone Rung-0/1 tool | **Retain** (out of Lite's scope) | Continues to serve Mode A / standalone Developers. |
| `references/pm-slice-contract.md` | **Delete** | Existed only for embedding in PM's developer prompt. |
| PM-binding policy fields (`before_head`, `session_generation`, `repair_round`, `operational_round`) in `reviewer_contract.py` + docs | **Delete** | Written only by PM; schema field list narrows accordingly (a compatible narrowing — standalone policies could always omit them). |
| `reserved_skill_sets` (policy + pre-launch check) | **Delete** | Existed to police Developer-commissioned audit requests. |
| `PM_AUDIT_VERDICT` sentinel capture (`skill_verdicts` in status files) | **Delete** | Existed to relay verdicts to PM's gate. Standalone orchestrator users read reviewer output directly, as they always could. |
| `reviewer-contract.md` / orchestrator SKILL.md text referencing PM enforcement, `Independent audit required` mechanics | **Retain but simplify** | Rewritten to describe the standalone contract only. |
| PM's dynamic import/subprocess use of `reviewer_jobs.py`, `PM_REVIEWER_*`/`ORCHESTRATOR_ARTIFACT_ROOT` env contract | **Delete** | Lite's `review.py` composes reviewer commands from its own profile table. |
| Orchestrator tests pinning PM-specific fields | **Delete** those cases; retain the standalone suite | |

## 6. Plans, planning requirements, shared skills, CI, root docs

| Item | Treatment | Notes |
|---|---|---|
| Plan format (7 sections, surface rules, approval flag) | **Retain** | Unchanged; the shared contract that makes plans portable across rungs. |
| `Independent audit required: yes` flag | **Retain, re-bound** | Now maps to elevated risk ⇒ PM-commissioned review (stronger, simpler). `implementation-plan` SKILL.md "Machine-Consumed Fields" + "Execution Modes" text updated. |
| Slice batches (Mode A-only) | **Retain** | Unchanged; Lite ignores batches as today. |
| `implementation-plan` SKILL.md PM references | **Retain but simplify** | Update parser-contract text and Mode B launcher pointer. |
| `handoff` SKILL.md Mode B exclusion note | **Retain but simplify** | Same exclusion, new state names. |
| `report` SKILL.md PM pointer | **Retain but simplify** | Points at `run-report.md`/assessments. |
| `scoped-implementation`, `drift-audit`, `code-review`, `commit`, `code-simplifier` | **Retain** | Untouched; drift-audit/code-review remain the question-vocabulary for PM-commissioned reviews (their `PASS WITH RISKS` etc. verdicts become reviewer *output PM reads*, not gate inputs). |
| `.github/workflows/ci.yml` | **Retain but simplify** | Same shape (py_compile + unittest + tmux); paths update; orchestrator suite stays. |
| Root `README.md` Mode B sections | **Replace completely** (those sections) | Rung-2 description per Lite; decision table survives with the "no supervising model" row removed (the unattended mode is dropped by owner decision; a local model can hold the PM seat). |
| `docs/VISION.md` | **Replace completely** at adoption | With `proposed-vision.md`; timing in blueprint §7. |
| `CHANGELOG.md`, `CONTRIBUTING.md` | **Retain** + new entries / updated doc-map | History is history; no retro-editing. |
| `pm-test/` fixture + lessons log | **Retain** | Historical evidence (heavily cited by these reports); new test runs get new entries. Its archived terminology (`MC`, `.ai-mc`) is already quarantined as historical. |
| `.gitignore` `.ai-pm/` entry | **Replace completely** | `.pm/` entry (plus the self-ignoring `.pm/.gitignore`). |

## 7. Duplicated state & representations (map §3.1) — dispositions

| Duplication | Disposition |
|---|---|
| run.json ×2 (mirror/controller) | **Delete** one (single authoritative copy) |
| Evidence shapes validated ×2 (gates + state) | **Delete** both validators; floor checks facts, not shapes |
| Knowledge ×4 (result ledgers → entries → summaries → context) | **Merge** to ×2: `notes.md` (working) + `run-report.md`/assessments (reporting) |
| Launch config ×3 (flags/frozen/snapshot) | **Merge** to ×1 (`harness` block; per-slice override recorded in the slice entry) |
| Reviewer contract ×4 statements | **Delete** the two PM-facing statements; orchestrator keeps its own two (SKILL + reference) |
| Authorized surface ×2 (lint + gate) | **Retain both** — good duplication (authoring-time vs acceptance-time) |

## 8. Terminology sweep (enforced at implementation)

To disappear from all *active* docs/code (historical files exempt: CHANGELOG, pm-test logs, archives): `deterministic-batch`, `model-supervised` (as a mode name), `repairable`/`needs-human` (as signature vocabulary), all 19 signature strings, `repair round`, `circuit breaker`, `operational_round`, `session_generation`, `reviewer-policy`, `reserved_skill_sets`, `PM_AUDIT_VERDICT`, `audit_provenance`, `developer-self-audit` (as a mechanical label), `prior-slice-context`, `residual_findings`/`continuation_notes` (as schema fields), `assumed-complete` (→ `attested`), `.ai-pm`, `schema_version: 5`, `pm-reconciliation`, `stop-with-evidence`, `run-next`, `--allow-unattended-default`, `--assume-complete` (→ `--attest`). The blueprint's acceptance criteria include a mechanical grep for this list. "Mode B" itself is **retained** — Lite *is* Mode B after replacement.

## 9. Proposed reuse (the complete list — everything else is written fresh)

Per the brief, reuse is the exception and each candidate is justified against a fresh rewrite:

1. **Recorded pane fixtures and marker/readiness strings** (test data + constants: readiness banners, hard-prompt marker sets, usage-limit phrasings). *Justification:* these are **observations of external tools**, not architecture — re-deriving them means re-running live CLIs to rediscover identical strings. Carrying data forward inherits no abstraction.
2. **Segment-aware surface-matching semantics** (the *specification* of plain-path/`/`-suffix/single-segment-glob/`**` behaviour, and its test scenario list). Code rewritten; the semantics and edge-case suite are proven and must not drift, since plans in the wild depend on them.
3. **The fake-harness test pattern** (`--harness-command` injection, tmux-skip guards). A testing *idea*, reused as a pattern; helper code rewritten against the new CLI.
4. **`orchestrator` scripts as a standalone skill** — retained unchanged *for their own users*, minus the PM-facing surface (§5). This is retention of an out-of-scope sibling, not reuse inside Lite: Lite's `review.py` shares none of its code, because Lite's reviewer launch is ~120 LOC (compose command from profile, run, capture) and importing a 1,282-LOC job manager to do that would re-import the abstraction being removed.

Explicitly considered and rejected for reuse: `plan.py` (rewrite to the retained spec — its code is clean, but importing it wholesale drags `models.py`/`constants.py` coupling and the temptation to keep its non-retained lint plumbing), `tmux_adapter.py` (same reasoning; the new `sessions.py` is specified from its observed behaviours), `git_ops.py` (small enough that rewriting to spec is cheaper than auditing for hidden coupling).

## 10. Assurance-loss register (consolidated)

The honest single list of everything Lite stops guaranteeing, gathered from the rows above: deterministic semantic acceptance; mechanical audit-independence proof; mechanical ledger retention; mechanical provenance labels; closed-schema state/result validation; deterministic repair classification; pause/idle statutes; in-worktree state tamper *detection* (superseded by placement); the unattended no-model batch mode (loss accepted by owner decision); machine-parsed reset times. Each is replaced by recorded PM judgement over the same or better evidence, bounded by the unchanged mechanical floor — and each replacement is named in the run's own artifacts (`assessment.md`), not just in this ledger.
