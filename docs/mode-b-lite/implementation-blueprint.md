# Mode B Lite — Implementation Blueprint

**Status:** Stage-1 design report. No implementation in this stage. This blueprint assumes a later, deliberate decision to adopt, and then a **greenfield reimplementation from the approved reports** — never an incremental simplification of the current Project Manager.

Inputs the implementation must follow, in order of authority: [proposed-vision.md](proposed-vision.md) → [target-design.md](target-design.md) → [replacement-ledger.md](replacement-ledger.md) → this sequencing document. The current implementation may be consulted only as behavioural evidence (readiness markers, glob semantics, failure scenarios) — the ledger §9 lists the only four sanctioned carry-overs.

---

## 1. Preconditions (before any code)

1. **Owner approval of the report set**, explicitly including the assurance-loss register (ledger §10) and the vision judgement.
2. ~~Owner decision on the one open capability question~~ **Resolved (2026-07-18): the unattended no-model batch mode is dropped entirely; no minimal scripted substitute will be built** (design §16.1). The owner has also accepted the ~85–90% practical-value retention estimate as the adoption bar, subject to §7's validation runs. This blueprint is therefore decision-complete: role ownership, PM authority, workflow, risk levels, gates, state, artifacts, session lifecycle, failure handling, commands, interfaces, escalation conditions, mechanical invariants, and delegation boundaries are all resolved in the target design. An implementation agent inventing architecture beyond these reports is a defect (see §8).
3. **Freeze the acceptance tests in prose first** (§5.1) so "done" is defined before code exists.

## 2. Branch & isolation strategy (how greenfield is enforced)

Implement on a dedicated branch (e.g. `feature/mode-b-lite-impl`, from `main`). **The branch's first commit deletes the entire current Mode B surface**: all of `skills/project-manager/`, `skills/orchestrator/references/pm-slice-contract.md`, and the PM-facing orchestrator machinery (ledger §5). The new system is then built under the final names into an *empty* `skills/project-manager/`.

Why delete-first: it makes whittling-down structurally impossible (there is nothing to whittle), makes every hidden dependency on old paths/state/terminology fail loudly during development, and leaves `main` intact as the reference implementation and baseline for §7's comparison runs. The old code remains permanently available in history.

## 3. Build sequence (dependency order)

Each stage lists: files, its acceptance criteria (AC), and what may not exist yet. Stages are small enough to implement and review independently; later stages never require reworking earlier ones (interfaces come from the design, not discovery).

**Stage 1 — State & plan foundations.** `pm_lib/state.py`, `pm_lib/plan.py`, `pm_lib/git_ops.py`, `pm.py`, `pm_lib/cli.py` (parse-only), tests.
AC: `lite-1` state round-trips atomically with events; plan parser passes the retained behaviour suite (headings, sections, surface path rules, segment-aware matching incl. the full edge-case list from ledger §9.2, approval-flag exactness, digest, duplicate ids, lint warnings); `check-plan` runs standalone; state lives under `<git-common-dir>/pm/`; no worktree mirror exists.

**Stage 2 — Sessions & floor.** `pm_lib/sessions.py`, `pm_lib/profiles.py`, `pm_lib/floor.py`, tests (tmux + fake harness; recorded marker fixtures per ledger §9.1).
AC: fresh-session launch/readiness/inject/capture/liveness/stop for all four profiles; hard-prompt refusal on send; OpenCode model-identity check fails closed on mismatch; `floor.py` exposes one function returning the six floor facts (digest, slice identity, surface, commit/ancestry, cleanliness, result presence) with evidence paths — and *no* accept/reject decision.

**Stage 3 — Slice lifecycle.** `pm_lib/slice_ops.py`, `pm_lib/prompts.py`, wire `init/status/approve/start-slice/observe/send/finalize/stop`.
AC: full fake-harness end-to-end — init → start-slice → observe → finalize(`--accept`) → status shows an accepted slice with commit; `finalize` refuses `--accept` when any floor fact fails; attempt budget persists and blocks a fourth intervention; `stop` captures pane + writes terminal state; a dead session is detected and reported, not driven; a plan edited mid-run stops before the next slice.

**Stage 4 — Review & reporting.** `pm_lib/review.py`, `notes.md` handling, `run-report.md`/`assessment.md` scaffolding.
AC: `review --skill drift-audit` composes from the profile table, embeds the skill's SKILL.md, runs read-only against the final diff, captures `review-*.md`; notes file is injected into the next slice's prompt and tripwires at the cap; report regenerates from state alone.

**Stage 5 — Documentation & operator trial.** New `SKILL.md`, `README.md`, three references; the rewritten "Verify Your Setup" trial (fake-harness scripts updated to the 4-field `result.json`).
AC: SKILL.md ≤ ~130 lines; the trial runs green from a clean checkout; a reader who has never seen old Mode B can run a toy plan from these docs alone (test this literally with a fresh session).

**Stage 6 — Cutover.** Update cross-references (root `README.md`, `CONTRIBUTING.md`, `implementation-plan`/`handoff`/`report` SKILL.md texts, `.gitignore`, `ci.yml`); orchestrator PM-surface removal lands here too if not in the first commit; **replace `docs/VISION.md` with the (approved, possibly amended) proposed vision in this same stage** — the vision swap and the implementation swap must ship together, never separately, so the repository never claims an assurance model it doesn't run.
AC: §6's no-baggage checks all pass.

**Stage 7 — Live validation & reassessment.** §7's comparison runs; update the metrics table below with measured values; write the adoption CHANGELOG entry.
AC: §7's bar met, or the stop-and-revise rule (§8) triggers.

## 4. Responsibility boundaries (implementation-time)

- `floor.py` and `git_ops.py` may never call a model or read prose; they compute facts.
- `slice_ops.py` orchestrates but decides nothing semantic; every accept/steer/stop enters through an explicit CLI act by the PM agent.
- `sessions.py` owns all tmux/process contact; nothing else shells out to tmux.
- `prompts.py` renders only from templates in `references/` — no inline prompt fragments in logic modules.
- `review.py` shares the profile table, not the Developer launch path (reviews are one-shot/exec where supported).
- No module imports from `skills/orchestrator/`.

## 5. Test strategy

### 5.1 Behavioural acceptance tests (written first, in prose, then encoded)
The minimum protected behaviours, each one test scenario or a small family: the fifteen walkthroughs in design §15 (each becomes at least one scripted scenario with a fake harness where feasible — the drift/restore, budget-exhaustion, approval-gate, mid-run-plan-edit, hard-prompt-refusal, dead-session, stall-nudge, and elevated-review flows are all mechanisable); the plan-parser suite; the floor suite (~15 boundary cases: surface globs, ancestry, dirty tree, digest, wrong slice, missing result); state round-trip/recovery; marker fixtures per harness.

### 5.2 Structure
Retain the proven patterns (ledger §9.3): pure-Python by default, `@skipUnless(tmux)` for session tests, fake harnesses via `--harness-command`, recorded pane fixtures, no real coding CLIs in CI. Target ~120 tests / ~2,500 LOC. Boundary-focused; no permutation mills.

### 5.3 CI
Same workflow shape as today: py_compile + unittest with tmux installed; orchestrator's (reduced) suite unchanged. Add the §6 no-baggage grep as a CI step.

## 6. Proving the replacement is clean

Mechanical checks, run at Stage 6 and kept in CI:

1. **Terminology grep:** the ledger §8 list returns hits only in historical files (`CHANGELOG.md`, `pm-test/**`, `docs/mode-b-lite/**`, `archive/**`).
2. **Path grep:** no references to `.ai-pm`, `pm-slice-contract`, `reviewer-policy.json`, `PM_REVIEWER_*`, `ORCHESTRATOR_ARTIFACT_ROOT` outside historical files and the standalone orchestrator's own docs/scripts.
3. **Import graph:** `pm_lib` imports nothing outside itself + stdlib.
4. **Doc reachability:** every doc link from root README/CONTRIBUTING resolves; no link reaches a deleted file.
5. **Fixture sweep:** no test fixture embeds schema-v5 shapes, signature strings, or old command names.
6. **Anti-resurrection review** (human, once): confirm no module reintroduces old structure under new names — specifically: any enum/frozenset classifying failures, any second copy of run state, any schema validation of fields the toolkit never reads, any per-round artifact family, any verdict-string parsing, any Developer-side ledger field. Each of these appearing is, by definition from the design, reproduced old complexity — reject the change or invoke §8.

## 7. Baseline comparison (how we know it worked)

Baseline runs execute from a `main` checkout; Lite runs from the branch. Use the existing `pm-test` fixture and its hard 5-slice plan — the plan with the four documented 0/5 baseline runs (Tests 14/16/17/18) plus earlier 5/5 strong-pairing runs.

- **Run A (the killer case):** the Test 14/16/17/18 local pairing (qwen3.6-27b Developer / qwen3.6-35b Reviewer-or-review-seat). Bar: Lite completes ≥ 4/5 slices with sound code (independently spot-checked), where baseline completed 0/5 four times.
- **Run B (strong pairing):** a Test 6/12-class pairing. Bar: parity (5/5) with fewer PM interventions and materially fewer model interactions (count observe/send/finalize/review calls from `events.jsonl` vs the baseline's operational events).
- **Run C (adversarial spot-checks):** scripted fake-harness scenarios replaying the known attack/failure shapes from the test log — false success report (Test 10/11), unauthorized file change (floor), state-file vandalism now targeting `.pm/` (must not affect control flow), wrong-slice result, approval-gate bypass attempt. Bar: every one caught or rendered harmless.
- Record per run: slices completed, wall-clock, interventions, human touches, artifact volume, and a qualitative read of the assessments' usefulness. Update §9 with measured values and label them measured.

## 8. Design-before-code discipline & the stop rule

- The reports resolve the architecture; the implementer's freedom is code-level (naming, decomposition inside a module, test details). Any needed deviation touching roles, gates, state shape, commands, artifacts, risk model, or authority boundaries **stops implementation and amends the design reports first** (a short PR against `docs/mode-b-lite/` stating the gap and the fix), then resumes. Quietly adding machinery is the defined failure mode of this project's history — the reports are the guard.
- Convenience pressure to reuse old modules "because they're right there" is answered by §2: they aren't there.
- If Stage 7's Run A bar is missed, do not patch forward: diagnose whether the miss is implementation (fix in place), design (amend reports, re-run), or vision-level (return to the owner). The 0/5→≥4/5 expectation is the design's own falsifiable claim.

## 9. Baseline and projected metrics

Measured = from the current tree (subagent-verified). Projected = design targets (judgement; final values unknowable until implementation, marked ◊ where wide).

| Measure | Current (measured) | Lite (projected) | Δ |
|---|---|---|---|
| PM implementation LOC | 8,406 | ~1,850 ◊ | −78% |
| Orchestrator LOC consumed by PM | 2,540 | 0 (standalone skill remains for its own users) | −100% (as PM dependency) |
| PM modules | 20 files | 11 files | −45% |
| CLI commands | 19 | 10 | −47% |
| Run statuses / slice statuses | 10 / 7 | 4 / 3 | −60% / −57% |
| State transitions (est.) | ~35 legal run/slice transitions incl. repair/pause sub-states | ~12 | ≈ −65% |
| Cross-file contracts (plan fmt, run.json, result.json, reviewer policy/request, launch contract, manifest/status, prompt contracts, adapter contract, pm-slice-contract ≈ 10) | ~10 | 4 (plan fmt, run.json, result.json, prompt templates) | −60% |
| Closed JSON schemas | 5 | 2 (both small, tolerant) | −60% |
| Mandatory plan fields | 7 sections + 2 flags (unchanged) | same | 0% (deliberate) |
| Gates / verdict points | ~21 ordered checks, 9 distinct verdict surfaces | 3 gates (floor facts + assessment + human) | ≈ −70% |
| Verdict/status vocabularies (developer 5, gate 4+signatures, audit 4, run 10, slice 7…) | ~45 enumerated values | ~12 | −73% |
| Failure classifications | 19 signatures + 10 adapter failure reasons | 0 signatures; 2 hard categories (floor-fail, integrity/hard-stop) + prose | −~95% |
| Retry/recovery mechanisms | 7 (repair loop, breaker, dual budgets, idle statute, transient reclassifier, pause machinery, reconcile) | 2 (attempt budget, PM judgement) | −71% |
| Persistent artifact types per slice | ~35 | ~10 | −71% |
| Role seats | 4 (supervising model, PM tools, Developer, Reviewer) | 3 | −25% |
| Tests | 336 (8,889 LOC) | ~120 (~2,500 LOC) ◊ | −64% / −72% |
| PM documentation lines | 1,647 | ~450 | −73% |
| Developer prompt (rendered, before slice content) | ~160 lines incl. embedded contract | ~55 ◊ | −65% |
| Developer result format | 13 fields, 2 sub-schemas, 2 ledger vocabularies | 4 fields | −70% |
| Model interactions, representative 5-slice run (est.) | 5 dev sessions + 10 reviewer launches (opt-in) + ~60–100 supervising calls + repair rounds | 5 dev sessions + 0–10 review sessions + ~40–60 PM calls ◊ | ≈ −35% ◊ |
| Files a new developer must read to understand Mode B | ~10 docs + 20 modules | ~5 docs + 11 modules | ≈ −50% |
| Sources of truth per retained concept | mostly 1 (already good), 4 duplicated clusters (map §3.1) | 1 each | — |
| Harness-specific branches | 4 profiles (+1 reviewer-only) | 4 profiles | ≈ 0% (genuine external variance) |
| Operational steps, normal slice, operator-visible | launcher + ~9-step supervised loop | launcher + 4-step loop (start/observe/finalize/next) | ≈ −55% |

**Honest read of the forcing function:** the 40–60% target is exceeded on most axes (code −78%, requirements/docs −73%). That is not virtue by itself — the brief warns against optimising the number. The reduction is a *consequence* of two structural moves (PM-commissioned review; judgement over verdict-relay), not of trimming to a quota; the axes that stay flat (plan format, harness profiles, mandatory floor) stay flat because their value survived scrutiny. The ◊-marked numbers are the ones implementation could move ±30%.

### Qualitative measures

- **Concepts a new operator learns:** plan/slice/contract/surface (unchanged) + floor, assessment, risk level, attempt budget, notes — replacing signatures, rounds/streaks/generations, dual budgets, policy digests, provenance classes, ledger vocabularies, supervision modes, pause budgets.
- **Decisions that become local and obvious:** "is this deviation okay?" (one PM judgement in one assessment file, vs a distributed outcome of reviewer models × exact-string gates × repair stanzas); "what happens when a check fails?" (steer/relaunch/stop, vs 19-way classification).
- **Rules no longer duplicated:** map §3.1's four clusters, per ledger §7.
- **Failure paths easier to understand:** a stopped slice = one assessment narrative + events, vs archived per-round results + streak state + policy generations.
- **Complexity deliberately retained:** tmux/profile/readiness/marker machinery (external variance is real); segment-aware surface matching (authorization precision is the floor); plan parser strictness (authoring-side clarity).
- **Complexity moved into prompts/docs, counted honestly:** PM's SKILL.md charter (~120 lines) now carries judgement guidance the old system encoded in Python; this is the one place mass moves layers rather than disappearing — bounded by the hard SKILL.md length cap and the §6 anti-resurrection review.
- **Retained only for high-risk work:** independent review sessions, validation reruns, deep transcript retention, human approval.

## 10. Capability comparison

Weights: how much the capability matters to the proposed vision's users (H/M/L). Scores 1–5 (judgement). Confidence: how sure the projection is before live runs.

| Capability | W | Current | Lite | Mechanism (Lite) | Δ / acceptable? | Conf. | Nature in Lite |
|---|---|---|---|---|---|---|---|
| Successful autonomous completion | H | **1–2 with weak models** (4× 0/5), 4 with strong | 4 (projected, both) | fewer wedge points; PM buffers form | major improvement / yes | M (until Run A) | judgement over floor |
| Resistance to material scope drift | H | 5 (file surface) / 3 (in-surface semantic) | 5 / 4 | identical floor; PM reads every diff; reviews hit the *final* diff (closes Test 13 gap) | ≥ / yes | H | mechanical + judgement |
| Failed-work detection | H | 4 (envelope-strict, semantics via models) | 4 | same evidence, judged directly; validation rerun on elevated | ≈ / yes | M | judgement |
| Interruption recovery | H | 4 (statutes; brittle at edges) | 4 | durable state + PM judgement + hard-stop floor | ≈ / yes | M | judgement + floor |
| Reviewability of accepted work | H | 3 (rich artifacts, verdict-shaped summaries) | 4–5 | reasoned `assessment.md` per slice | improvement / yes | H | judgement, recorded |
| Consequential-change safety | H | 5 (approval flags, hard-stop floor) | 5 | identical mechanisms retained | = / yes | H | mechanical |
| Final engineering quality | H | 3–4 (gates check envelopes; quality = model quality) | 4 | quality = model quality + a judge who reads | ≈/↑ / yes | M | judgement |
| Support for weaker Developer/Reviewer models | M | **1 in practice** (machinery defeats them) | 4 | minimal paperwork; PM tolerance; plan-level controls | major improvement / yes | M | structural |
| Usability with strong models | M | 3 (ceremony tax) | 5 | proportional process | improvement / yes | H | structural |
| PM-side simplicity | M | 2 | 4 | 10 commands, 4 statuses, one loop | improvement / yes | H | structural |
| Developer-side simplicity | M | 2 (11-step workflow, ledgers, schemas) | 5 | implement, validate, commit, 4-field result | improvement / yes | H | structural |
| Human-review burden | M | 3 | 4 | fewer, richer artifacts | improvement / yes | H | judgement |
| Maintenance burden | M | 2 (8.4k LOC + interactions) | 4 | ~1.9k LOC, no interacting statutes | improvement / yes | H | structural |
| Operating overhead (tokens, wall-clock, interactions) | M | 2–3 | 4 | shorter prompts, fewer relays, no forensics | improvement / yes | M | structural |
| Adaptability to unusual tasks | L | 2 (statutes assume the taxonomy fits) | 4 | judgement generalises | improvement / yes | M | judgement |
| Transparency of decisions | H | 4 (deterministic but dense) / verdicts opaque | 4 (reasoned but non-reproducible) | assessments + events | different shape, ≈ / yes | H | judgement, recorded |
| Accountability | H | 3 (diffused across gates/models) | 5 | one seat signs every acceptance | improvement / yes | H | structural |
| Cost efficiency | M | 2 | 4 | −35% interactions, −65% prompt mass, model-tiering by risk | improvement / yes | M | structural |
| Deterministic acceptance reproducibility | L (per proposed vision) | 5 | 2 (floor only) | — | **regression / yes — the deliberate trade** | H | relinquished |
| Unattended no-model batch runs | L | 3 | 0 | — | regression / accepted by owner (§1.2) | H | relinquished |

**Practical-value retention estimate:** weighting the H-rows, Lite retains or improves every high-weight capability except deterministic reproducibility (deliberately relinquished, low weight under the proposed vision) — an honest estimate is **~85–90% of current practical value retained, with several high-weight capabilities improved**, exceeding the 80% objective *conditional on Run A confirming the completion-rate projection*. If PM-judgement quality in live runs disappoints, the true figure falls; that is exactly what §7 measures before adoption.

## 11. Vision replacement timing

`docs/VISION.md` is replaced in Stage 6, in the same change-set as the cutover, after (a) the owner approves the proposed vision (possibly amended by anything learned in Stages 1–5) and (b) Stage 7's plan is ready to run. Never earlier (the repo would describe a system it doesn't contain) and never later (the repo would run a system its vision disclaims). The CHANGELOG entry names the swap explicitly.
