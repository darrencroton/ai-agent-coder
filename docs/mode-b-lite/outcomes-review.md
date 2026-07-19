# Mode B Lite — Outcomes Review: Plan Goals vs. Delivered System

**Status:** Retrospective outcomes assessment, written after Stage 6 (cutover) and before Stage 7 (live validation). Measured at `87ee375` on `feature/mode-b-lite-impl`.
**Question answered:** did the implementation deliver the before→after outcomes the approved plan promised, judged against the *adopted vision* (`docs/VISION.md`)? Every goal is scored **1–5** (5 = fully achieved); every non-5 is explained, including where the ceiling was structural rather than a failure of execution.
**Measurement method:** raw line counts via `wc -l`; logical code lines via tokeniser (comments/docstrings/blanks excluded); test counts via `grep -c "def test_"`; baseline figures from the current-state map §1 (measured on `main` at `7a3ed6e`). Companion document: [`retrospective-code-review.md`](retrospective-code-review.md) (code-level findings F1–F11, independently assessed by Codex).

---

## 1. Summary scorecard

| Goal area | Score | One-line verdict |
|---|---|---|
| Structural moves (the design's substance) | **5** | All three shipped exactly as specified |
| Quantitative shrinkage (code, commands, state, schemas, docs) | **4.5** | Most axes met or beaten; implementation LOC and test count over projection |
| Responsibility & role simplification | **5** | One judge, one seat, clean module boundaries |
| Workflow simplification | **5** | 4-step loop, 10 commands, 44-line Developer prompt |
| Honesty & accountability goals | **4** | Excellent in the new system; two cutover framing misses (F9/F10) |
| Weak-model & completion-rate goals | **provisional** | Mechanisms delivered; the falsifiable claim is exactly what Stage 7 measures |
| **Overall (scorable goals)** | **≈4.6 / 5** | The plan's intent is delivered; residuals are small and named |

## 2. Quantitative goals (blueprint §9), measured

| # | Measure | Baseline (main) | Projected | **Measured (this branch)** | Score | Notes |
|---|---|---|---|---|---|---|
| 1 | PM implementation LOC | 8,406 | ~2,200–2,600 ◊ | **4,286 raw / 3,022 logical** | **3** | See §2.1 — the one materially missed number |
| 2 | Orchestrator LOC consumed by PM | 2,540 | 0 | **0** (no imports; pinned by test + CI grep) | **5** | |
| 3 | PM modules | 20 files | 11 | **12** (pm.py + 11 in `pm_lib`, incl. `__init__`) | **5** | Matches the design tree exactly |
| 4 | CLI commands | 19 | 10 | **10** | **5** | |
| 5 | Run / slice statuses | 10 / 7 | 4 / 3 | **4 / 3** | **4** | Vocabulary shrank as promised, but `complete` is currently unreachable (review F1) — one of four statuses is dead until the small fix lands |
| 6 | State transitions | ~35 | ~12 | **~11** (counted from code paths) | **5** | |
| 7 | Cross-file contracts | ~10 | 4 | **4** (plan fmt, run.json, result.json, prompt templates) | **5** | |
| 8 | Closed JSON schemas | 5 | 2 tolerant | **2, both tolerant** (`lite-1` validates only PM-read fields; result.json 2 mandatory keys) | **5** | |
| 9 | Mandatory plan fields | 7 sections + 2 flags | unchanged | **unchanged** | **5** | Deliberate 0% — the portable contract survived |
| 10 | Gates / verdict points | ~21 checks, 9 surfaces | 3 gates | **3** (floor / assessment / human) + review-freshness as a floor-adjacent fact | **5** | |
| 11 | Verdict/status vocabularies | ~45 values | ~12 | **13** (4 run + 3 slice + 2 risk + 2 result-status + 2 decision kinds) | **5** | |
| 12 | Failure classifications | 19 signatures + 10 adapter reasons | 0 + 2 hard categories | **0 signatures; 2 hard categories** (floor-fail, integrity/hard-stop) — retirement enforced by CI grep | **5** | |
| 13 | Retry/recovery mechanisms | 7 | 2 | **2** (attempt budget + PM judgement) | **5** | |
| 14 | Persistent artifact types per slice | ~35 | ~10 | **~12** (prompt, pane×2, status×2, diff, validation, result, attempt-dirs, review-input, review report+stderr, assessment, steer) | **5** | Within intent; nothing gates on artifact families |
| 15 | Role seats | 4 | 3 | **3** | **5** | The supervising-model/PM-tools split is genuinely gone |
| 16 | Tests | 336 / 8,889 LOC | ~140–170 / 2,800–3,400 ◊ | **252 / 4,309** | **4** | Over the projected count; see §2.2 |
| 17 | PM documentation lines | 1,647 | ~450–550 | **334** (SKILL 62 + README 112 + references 160) | **5** | −80%, beat the target with the honesty content intact |
| 18 | Developer prompt (before slice content) | ~160 lines | ~55 ◊ | **44 template lines** | **5** | The 43-line embedded delegation contract is gone entirely |
| 19 | Developer result format | 13 fields, 2 sub-schemas, 2 vocabularies | 4 fields | **4 fields, 2 mandatory** | **5** | |
| 20 | Model interactions per 5-slice run | ~60–100 supervising calls + relays | ~40–60 ◊ | **not yet measurable** | — | This is a Stage 7 Run B measurement by design (`events.jsonl` counting); scoring it now would be narration |
| 21 | Files to read to understand Mode B | ~10 docs + 20 modules | ~5 docs + 11 modules | **5 docs + 11 modules** | **5** | SKILL, README, run-state, 2 prompt refs |
| 22 | Sources of truth per concept | 4 duplicated clusters | 1 each | **1 each** (CONTRIBUTING map updated; every contract has one home) | **5** | |
| 23 | Harness-specific branches | 4 profiles | 4 | **4** (+ qwen reviewer-only) | **5** | Deliberately flat — external variance is real |
| 24 | Operational steps per normal slice | ~9-step loop | 4-step loop | **4** (start-slice / observe / finalize / decide) | **5** | |

### 2.1 The missed number: implementation LOC (score 3)

The one quantitative goal clearly missed. Raw reduction is −49% (8,406 → 4,286), against a projected ≈ −70%. Three honest observations:

1. **The like-for-like gap is smaller than it looks.** ~30% of the new files are comments and docstrings — deliberately dense, constraint-stating documentation (the review's §4 credits this style as load-bearing). Logical code is 3,022 lines, 16% over the projection's top end — within the ±30% band the blueprint itself marked on ◊ figures, and the projection was explicitly non-binding ("behaviour coverage beats the reduction number wherever they conflict").
2. **Where the growth went is known and defensible:** the post-Codex-review additions (run token + HMAC + locking + run discovery, attempt isolation, review freshness, scavenge, transitive skill embedding) were all listed as included in the estimate but were estimated optimistically; and evidence-rich floor facts (every fact returns a full evidence dict) cost lines the design's "~150" figure never contained.
3. **The system-level reduction still lands in the promised band.** Counting everything a maintainer owns — implementation + tests + docs + the orchestrator dependency (8,406 + 8,219 + 1,647 + 2,540 = 20,812 baseline vs 4,286 + 4,309 + 334 + 0 = 8,929) — the delivered reduction is **−57%**, inside the brief's 40–60% target, achieved without dropping any retained behaviour.

Why not a 2: no behaviour was added beyond spec (the drift-audit discipline held; deviations of record are the strictness items F5/F6). Why not a 4: the number was the headline of the metrics table, and it was missed on its own terms; the vision's "minimise the whole system" principle is satisfied at the system level, not at the flagship-metric level.

### 2.2 Tests over projection (score 4)

252 tests / 4,309 LOC vs. a projected ~140–170 / ~2,800–3,400. The overshoot is concentrated exactly where the ledger predicted density would survive: the retained plan-parser behaviour suite (48), profiles (31), git_ops surface matching (31), and sessions markers (30) — behaviours whose 1:1 retention was a design requirement. The blueprint's own rule ("coverage of the protected behaviours wins over the count") makes this the right side to err on, and the suite is boundary-focused, not permutational (verified in review §4). Not a 5 because the projection existed and a leaner suite covering the same boundaries was plausibly achievable; not lower because no test pins deleted machinery — the §6.5 fixture sweep is clean and CI-enforced.

## 3. Qualitative goals (blueprint §9 "qualitative measures" + design §14/§16)

| Goal | Evidence | Score |
|---|---|---|
| Concepts a new operator learns shrink to: floor, assessment, risk level, attempt budget, notes | SKILL.md teaches exactly these five on top of the unchanged plan vocabulary; signatures/rounds/streaks/generations/dual-budgets/policy-digests/provenance are gone from all active docs (CI grep) | **5** |
| "Is this deviation okay?" becomes one local PM judgement | One assessment file per slice; `finalize --accept` requires ≥40-char recorded reasoning; no reviewer-model × exact-string × repair-stanza composition remains | **5** |
| "What happens when a check fails?" becomes steer/relaunch/stop | Exactly three paths, all explicit CLI acts, one budget | **5** |
| Rules no longer duplicated (map §3.1's four clusters) | Single state copy; single evidence validation (floor facts only); knowledge ×4 → notes.md + report; launch config ×3 → one harness block + per-slice `launch` overrides | **5** |
| Failure paths readable as one narrative | A stopped slice = one assessment + events; verified in the Stage 5 operator trial | **5** |
| Complexity deliberately retained where variance is real | tmux/readiness/marker machinery, segment-aware matching, parser strictness all kept and tested | **5** |
| Complexity moved to prompts counted honestly (SKILL.md ≤ ~120–130 lines) | SKILL.md is **62 lines** — half the cap; the charter carries judgement guidance without becoming a statute book | **5** |
| SKILL.md/docs honesty style retained | "Trust model, honestly" + inherited-gaps register + per-layer enforcement attribution all present | **5** |
| No old structure under new names (§6.6 pre-check) | No failure enums, no second control-read state copy, no unread-field validation, no gating artifact families, no verdict-string parsing (review §2; the one-time human sign-off remains scheduled) | **5** (pending the human pass) |

## 4. Capability goals (blueprint §10), scored where scorable now

Rows whose truth depends on live runs are marked *provisional* — Stage 7 exists precisely to measure them, and pre-scoring them from the bench would repeat the old system's mistake of grading its own work.

| Capability (weight) | Promised | Delivered now | Score |
|---|---|---|---|
| Resistance to material scope drift (H) | 5 / 4 | Identical floor recomputation from git; PM reads every diff; reviews pinned to the final HEAD with mechanical freshness invalidation — the Test 13 stale-audit shape is impossible by construction | **5** |
| Consequential-change safety (H) | 5 (unchanged) | Approval flags exact-match + floor fact 3; hard-stop marker floor carried verbatim as data; fresh finalize-time pane scan added (fact 8) | **5** |
| Reviewability of accepted work (H) | 4–5 | Reasoned `assessment.md` per slice with floor results, risk provenance, reviews consulted, attempts, PM reasoning; report regenerates from controller data alone | **5** |
| Accountability (H) | 5 | One seat signs every acceptance; ≥40-char reasoning enforced; every intervention evented | **5** |
| Developer-side simplicity (M) | 5 | Implement, validate, commit, 4-field result; 44-line prompt; zero ledgers/schemas/sequences | **5** |
| PM-side simplicity (M) | 4 | 10 commands, 4 statuses, one loop; minor dents: F1 (unreachable `complete`), F3 (dead flag) | **4** |
| Maintenance burden (M) | 4 | 8.9k total lines vs 20.8k, no interacting statutes, stdlib-only, CI-enforced no-baggage | **5** |
| Interruption recovery (H) | 4 | Durable authenticated state + `status` reconstruction + scavenge; `wake_at` declared but writer-less (F6) — the declared harness-dependency honesty survives, the recorded-resume-time part doesn't yet | **4** |
| Failed-work detection (H) | 4 | Same evidence judged directly; elevated slices force both fresh reviews + PM validation rerun | **provisional 4** — bench-verified mechanics; live confirmation is Run A/B's job |
| Successful autonomous completion (H) | 4 (from 1–2 weak / 4 strong) | All wedge points removed on the bench (no schema traps, no retention gates, no digest churn) | **provisional** — this is *the* falsifiable claim; Run A's ≥4/5 bar decides it |
| Support for weaker Developer models (M) | 4 (from 1) | Minimal paperwork, PM tolerance, plan-level controls in place | **provisional** — Run A decides |
| Cost efficiency / operating overhead (M) | 4 | Shorter prompts, no relays, no forensics; interaction counts | **provisional** — Run B measures |
| Deterministic acceptance reproducibility (L) | 2 (deliberate regression) | Floor deterministic; acceptance recorded but not re-derivable — exactly as the vision trades | **5** (the trade landed as designed, and is documented as a trade) |
| Unattended no-model batch (L) | 0 (dropped by owner) | Gone; no vestige | **5** (as decided) |

## 5. Vision-commitment goals (scored against `docs/VISION.md` as adopted)

| Commitment | Delivered | Score |
|---|---|---|
| Contracts before code (frozen plan, digest, fail-closed parsing) | Fully — parser behaviour retained 1:1, digest checked at every launch and finalize | **5** |
| Mechanise the floor; judge the rest — "never blur which is which" | The eight facts are code with no verdict; acceptance is an explicit recorded act; the CLI itself makes the seam visible (`finalize` reports, `--accept` decides) | **5** |
| Evidence informs judgement; narration never decides | Floor consumes only git/filesystem facts; assessments must cite evidence; Developer summary is a pointer | **5** |
| One seat holds authority and accountability | Token-raised authority, HMAC state, terminal integrity semantics — all tested | **5** |
| Proportionality (risk-scaled process) | Two risk levels, mechanical `plan_risk`, raise-only ratchet, elevated effects automatic | **5** |
| Fail closed where it counts | Integrity stops terminal; approval gates mechanical; hard-stop floor at send, observe, and finalize — with one launch-path gap (F2: initial injection screens only trust prompts) | **4** |
| Bounded persistence | One budget, persisted, increment-first; nudges free — with one honesty caveat (F8: budget resets when a stopped slice is re-run; within the trusted-PM model but under-documented) | **4** |
| Atomic usefulness of sibling skills | Untouched; orchestrator retained standalone with PM surface removed | **5** |
| Minimise the whole system | −57% total maintained mass; no mechanism without a consumer — minus the two dead-surface items (F1, F3) | **4** |
| Honest threat model, stated where it matters | Outstanding inside `skills/project-manager/` (trust-model section, inherited-gaps register); dented at the repo's front door — README's opening still speaks the *abandoned* vision's identity ("moves trust out of the model", "the chain never changes"), and CONTRIBUTING cites a retired principle (F9/F10) | **3** — the vision-swap goal was "the repository never claims an assurance model it doesn't run", and two prominent framing passages currently do exactly that. Cheap to fix; scored honestly because this axis is the one the cutover stage existed to protect |

## 6. What cannot be scored yet, and what would change the scores

- **Run A (weak-pairing completion)** decides the provisional rows in §4 and, with them, whether the plan's central bet — judgement over machinery raises delivered outcome — is confirmed. A miss triggers blueprint §8's diagnose-don't-patch rule, not a quiet fix.
- **Run B (strong-pairing parity + interaction counts)** turns metric #20 into a measured value.
- **Run C (adversarial spot-checks)** converts several bench-verified "5"s (floor, tamper, approval bypass) into live-run evidence; the retrospective review recommends adding launch-time hard-prompt and stale-review-acceptance scenarios to it.
- **Fixing F1/F2/F3 + F9/F10** (all small) would raise §2 row 5 to 5, §5 "fail closed" to 5, "minimise" to 5, and "honest threat model" to 5 — i.e., the plan's non-provisional goals would then be met essentially in full.

## 7. Addendum: post-assessment fixes (same change-set)

The scores above describe the system **as delivered at Stage 6** (`87ee375`) and the metric snapshots in §2 are that build's. After the independent Codex assessment of the companion review (which found two additional high-severity defects — run-token environment inheritance and unenforced budget exhaustion — see review §7), the following landed in this retrospective change-set: both high-severity defects fixed; F1 (`complete` transitions), F2 (launch-time hard-stop screening), F3 (reviewer-tools wiring), F4 (verified read-only loads), review quiescence guard, JSON/MAC read locking, reviewer-pgid cleanup, readiness deadline raised, and all documentation findings (F9–F11 plus the two design-report amendments for F5/F6). The fixes moved the measured numbers modestly: PM implementation 4,286 → **4,411** raw lines, tests 252 → **263** (11 new regression tests, all green), PM docs unchanged at ~335 — leaving every §2 score intact and the whole-system reduction still ≈ −57%. With these landed and regression-pinned, the affected scores read: §2 row 5 → **5**; §5 "fail closed" → **5**; "minimise the whole system" → **5**; "honest threat model" → **5**; "one seat holds authority" — which the token-inheritance defect had silently undermined at Stage 6 — is restored to an earned **5**. The Stage-6-as-shipped scores stand above as the honest record of what the build alone achieved; the two high-severity items are also a caution against reading any bench scorecard, including this one, as a substitute for the independent review and live validation the plan requires.

## 8. Bottom line

Judged against the adopted vision, the implementation delivered the plan's substance completely: every structural move shipped as designed, every workflow/vocabulary/responsibility goal is met, the honesty apparatus survived the rewrite, and no goal was met by quietly reintroducing the machinery the plan deleted. The genuine misses are one headline metric (implementation LOC, missed on its own terms even after fair accounting), a handful of small dead or unfinished surfaces (F1, F3, F6), one launch-path floor gap (F2), and two front-door documentation passages still speaking the old vision (F9/F10). Nothing in the misses touches the design's load-bearing claims — those stand or fall with Stage 7, exactly as the plan intended.
