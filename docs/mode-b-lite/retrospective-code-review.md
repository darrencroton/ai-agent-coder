# Mode B Lite — Retrospective Holistic Code Review (Stages 1–6)

**Status:** Retrospective review, written after Stage 6 cutover and before Stage 7 (live validation). Reviewed at `87ee375` on `feature/mode-b-lite-impl`.
**Scope:** the complete shipped system — `skills/project-manager/` (11 modules, 4,297 implementation LOC; 10 test modules, 252 tests), the three references, SKILL.md/README.md, the cutover surfaces (root README, CONTRIBUTING, CI, `.gitignore`, `docs/VISION.md`), and the orchestrator's reduced surface — judged against the six binding reports in `docs/mode-b-lite/` (authority order: proposed-vision → target-design → replacement-ledger → implementation-blueprint).
**Method:** every implementation and documentation file read in full; both test suites executed (PM 252/252 OK in 158 s with tmux; orchestrator 26/26 OK); specific behaviours spot-verified by hand (harness CLI flags checked against the installed `claude`/`codex`/`opencode` binaries; state/floor/review-freshness paths traced end-to-end).

---

## 1. Verdict up front

The implementation is a faithful, high-quality realisation of the approved design. The three structural moves that carry the design — PM-commissioned review with mechanical freshness invalidation, the eight-fact non-waivable floor with judgement above it, and capability-token-raised authority with HMAC-authenticated single-copy state — are all implemented exactly as specified, tested at their boundaries, and documented honestly. Module responsibility boundaries (blueprint §4) are respected: `floor.py`/`git_ops.py` compute facts only, `sessions.py` owns all tmux contact, `prompts.py` owns all prompt text, nothing imports from `skills/orchestrator/`, and a test pins the stdlib-only import graph. The terminal-integrity semantics (never re-sign tampered state) are correct and pinned by tests.

The findings are: **two functional gaps** (F1, F2), **one dead CLI flag** (F3), and a set of hardening and documentation-consistency items. Several are directly relevant to Stage 7's Run A/C and are worth fixing first; recommendations for extending Stage 7's scenario list are in §5.

> **Verdict revised after the independent assessment (§7).** This review's original conclusion — "no finding below blocks Stage 7" — was wrong. The commissioned Codex assessment (verdict: DISSENT) found two genuine high-severity defects this review missed: the run capability token leaked into Reviewer subprocesses (and could reach Developer tmux sessions) through environment inheritance, and attempt-budget exhaustion was recorded but not enforced — the session kept running and acceptance stayed open. Both defeated guarantees this review had marked "Yes" in the conformance table (token withheld from sessions; budget mechanical and non-negotiable). Both, along with most other findings, have since been fixed and regression-pinned; the full disposition record is §7. The lesson stands on the record: the lead reviewer verified what the code does, but under-tested what the *documented operating workflow* does (exporting `PM_RUN_TOKEN`, exhausting a budget mid-session) — exactly the class of gap independent review exists to catch.

## 2. Conformance to the binding reports

| Design commitment | Where implemented | Conforms? |
|---|---|---|
| Eight-fact floor, exact enumeration (design §3.3) | `floor.py` — facts 1–8 match one-for-one, incl. branch-head check inside fact 6 and finalize-time hard-stop pane scan (fact 8); no accept/reject verdict rendered | Yes |
| Floor computed, never judged, by code (blueprint §4) | `floor.py`/`git_ops.py` are pure fact computation; `test_evaluate_floor_does_not_write_state_or_files` pins side-effect freedom | Yes |
| Capability token + HMAC single-copy state (design §8) | `state.py`: token minted once, SHA-256 stored, every write MAC'd, `IntegrityError` terminal; `load_writable_state` never re-signs; tamper test pins every mutating command failing closed with `INTEGRITY:` prefix | Yes |
| Token withheld from Developer/Reviewer sessions | `sessions.start_session` asserts `PM_RUN_TOKEN` absent from the explicit env map; env carries only the six declared `PM_*`/`TMPDIR` vars | **No at Stage 6 → fixed** — the assertion missed *inherited* environment (tmux server / reviewer `Popen`); now `unset PM_RUN_TOKEN` prefixes every session command and reviewers get a sanitized env (§7.2.1) |
| Review commissioned by PM, pinned to `before_head..HEAD`, freshness invalidation (design §5) | `review.py` pins diff + changed files at commission time, records reviewed-HEAD + artifact sha256; `finalize_accept` refuses elevated acceptance without both reviews fresh at the exact current HEAD; `test_missing_then_stale_then_fresh_reviews` walks the full lifecycle | Yes (see F5 for one nuance) |
| Mechanical `plan_risk` derivation, immutable; ratchet raise-only (design §4) | `plan.py::plan_risk` (silence ≠ "none"); `apply_risk_ratchet` rejects anything but "elevated"; `plan_risk` never touched after parse | Yes |
| Attempt budget: 0 at launch, +1 per steer/relaunch, nudges free (design §11) | `start_slice`/`finalize_steer` increment-then-check; persisted; `send` free; budget survives restarts (tested) | Yes |
| Attempt isolation (`attempt-<n>/` rotation) (design §9) | `_rotate_prior_attempt` rotates result/pane before every relaunch; nothing gates on rotated contents | Yes |
| Controller-owned originals + `.pm/` mirror, report from controller data alone (design §8/§9) | `write_controller_artifact`/`mirror_artifact`; `render_run_report` reads only state dir; regeneration with `.pm/` deleted is tested | Yes |
| 10 commands, `finalize` non-auto-accepting, ≥40-char reasoning (design §12) | `cli.py` — exactly the ten; bare `finalize` decides nothing; accept/steer/stop are explicit recorded acts | Yes |
| Scavenge without state (design §8) | `stop --scavenge` sweeps `pm-<run-id>-*` (or all `pm-*`) sessions; reviewer process groups recorded in state and reaped; both tested | Yes |
| Plan parser behaviour retained 1:1 incl. segment-aware matching (ledger §9.2) | `plan.py`/`git_ops.py` — `PurePosixPath.full_match`, backtick extraction, exact approval-flag matching, fence masking, batch warnings; 48 + 31 tests | Yes |
| Transitive skill-bundle embedding, path-escape-guarded (ledger §9.4) | `prompts.compile_skill_bundle`; `review-matrix.md` embedding tested with the real `code-review` skill | Yes |
| SKILL.md ≤ ~130 lines; single-sourced launcher; honest trust model | SKILL.md is 62 lines; launcher in one place; README "Trust model, honestly" carries the inherited-gaps register | Yes |
| No-baggage checks in CI (blueprint §6) | `ci.yml` — terminology/signature/path greps with fail-on-grep-error discipline, doc-link reachability; import-graph check in the test suite | Yes |
| Anti-resurrection §6.6 (advisory pre-check; the human check still stands) | No failure-classification enums, no second state copy read for control, no schema validation of unread fields, no per-round gating artifact families, no verdict-string parsing anywhere in `pm_lib` | Pre-check clean |

## 3. Findings

Severity: **M** = should fix before Stage 7; **L** = fix opportunistically or record; **D** = documentation-only.

### F1 (M) — Run status `complete` is unreachable

`state.RUN_STATUSES` includes `complete` (design §8: four run statuses) but no code path ever assigns it. When the last slice is accepted, the run stays `active` forever; `start-slice` prints "all slices complete" without a state transition, and `stop` on a finished run records it as `stopped`. Design §3.4 (Finish: "final state write, run report regeneration") is therefore only half-implemented, and one of the four statuses is dead vocabulary — the kind of unused enum the no-baggage discipline exists to prevent. A run-report reader cannot distinguish a completed run from one mid-flight between slices.

*Recommendation:* when `finalize_accept` (or `start-slice`'s all-complete path) finds no next eligible slice, set `status = "complete"` and regenerate the report. Small, contract-consistent, testable.

### F2 (M) — Initial prompt injection is not screened against the full hard-stop marker set

Design §3.2 requires "hard-prompt refusal on any send". `send_line` (steers/nudges) correctly refuses on any visible hard-stop marker. But the launch path — `wait_until_ready` → `send_prompt` — screens only `trust_prompt` markers during readiness polling (`_raise_on_trust_prompt`), and `send_prompt` itself screens nothing. A credential, approval, permission, or side-effect prompt visible at launch (e.g. a harness demanding login after a session expiry) would have the multi-KB slice prompt pasted into it, with double-Enter submission. Fact 8 catches this state later at finalize, but the floor's job is to prevent the blind keystrokes, not just to refuse acceptance afterwards.

*Recommendation:* scan the full `scan_hard_stop` set in the readiness poll (or immediately before `send_prompt`) and fail the launch closed, mirroring `send_line` semantics. One consideration: verify against recorded fixtures that no supported TUI's normal ready screen matches the approval/permission phrasings; the marker strings are carried from field-proven old evidence, so risk is low.

### F3 (M) — `start-slice --reviewer-tools` is accepted and silently ignored

`cli.py` defines the flag and passes it through; `slice_ops.start_slice(..., reviewer_tools=...)` accepts the parameter and never reads it. Design §12 lists it in the command signature and design §8 says per-slice overrides are "recorded in the slice entry when used". Today an operator or PM using it gets no effect and no warning — a silent no-op on a documented control.

*Recommendation:* either wire it (store in `current_slice.launch`, have `review._resolve_tool` prefer it) or remove the flag from `start-slice` and amend design §12 accordingly (per blueprint §8, either path needs a one-line doc change; removal is the simpler honest option since `review --tool` already provides per-review selection).

### F4 (L) — Read-only commands never verify the state MAC, even when the token is available

`status` and `observe` load state without MAC verification by design (documented in run-state.md: "treat their output as unverified when you have no token"). But the PM agent *does* hold the token (`PM_RUN_TOKEN` in its environment), and these are the commands it acts on between mutating calls — a tampered `run.json` could mislead observation/attempt/session-name reads for several decisions before the next mutating command fails closed. Authority is never at risk (every mutating path verifies), so this is hardening, not a hole.

*Recommendation:* when `PM_RUN_TOKEN` is present in the environment, verify opportunistically in `status`/`observe` and fail with the same `INTEGRITY:` message. Zero cost to the tokenless human-reader path.

### F5 (L) — Elevated review requirement is stricter than the design's stated nuance

Design §4: elevated slices get "drift-audit, and code-review *unless the flag names only one concern*". The implementation requires **both** reviews fresh at the final HEAD, always (`_REQUIRED_ELEVATED_REVIEW_SKILLS`). All shipped operator docs (SKILL.md, README, run-state.md) state the stricter rule consistently, so there is no doc/code contradiction — but the target design was never amended, and blueprint §8 makes gate-touching deviations a design change. Strictness in the safe direction; still, the reports are supposed to remain authoritative for later sessions.

*Recommendation:* amend target-design §4 (one sentence) to match the shipped both-reviews rule, in the dedicated-commit style §8 prescribes.

### F6 (L) — `wake_at` is a field with no writer

Design §8 describes `current_slice.wake_at` as "a PM-recorded resume time … so any later controller can see when continuation is due". The field exists, is initialised to null, and has no setter — no command can record a resume time, so the declared "whoever resumes can see when" capability doesn't exist. run-state.md documents this honestly ("no setter command"), so docs and code agree with each other but not with the design.

*Recommendation:* either add an optional `--wake-at` to `send`/`stop` (small CLI surface addition → design amendment per §8), or amend design §8 to declare the field reserved. Decide before Stage 7's Run A, where a usage-window pause is plausible.

### F7 (L) — Fixed 20-second readiness deadline may be too tight for the Run A local-model pairing

`wait_until_ready` hard-codes `deadline_seconds=20` (banner wait), with a 10-second stable-pane fallback, and deadline expiry is deliberately non-fatal — `send_prompt` then pastes regardless. For hosted CLIs this is field-proven. For Stage 7's Run A (local qwen3.6 via a local TUI, cold model load) a slow first paint can exceed 30 s, and the paste would land in a half-initialised TUI, likely producing a dead-looking session and burning a relaunch attempt on infrastructure rather than on the model. The old system carried per-profile readiness windows for exactly this reason; SKILL.md's "be patient with local models" guidance covers observation, not launch.

*Recommendation:* raise the default (e.g. 60 s — cheap, since banner match returns early) or thread a readiness deadline through `start_slice`. The former is code-level freedom under blueprint §8; no contract change.

### F8 (L) — A stopped slice's attempt budget resets silently on re-run

After `finalize --stop`, `current_slice` is cleared; a later `start-slice` treats the stopped slice as fresh (`attempts = 0`). Design §12 intends stopped slices to be re-runnable "after human review", but nothing mechanical marks that review — the same PM agent that stopped the slice can immediately relaunch it with a full budget, making the "mechanical, non-negotiable" budget (design §11) a per-stint rather than per-slice bound. This is within the threat model (the PM seat is trusted and every stop/relaunch is an evented, recorded act), but the budget's documentation reads stronger than its mechanics.

*Recommendation:* record the reset honestly — one sentence in run-state.md ("attempts reset when a stopped slice is re-run") — or carry `attempts` across the stop. The first is enough; the second changes recovery semantics the design didn't ask for.

### F9 (D) — Root README opening restates the *abandoned* vision identity

`README.md` line 5: "This repository moves trust out of the model and into contracts, evidence, and role separation" — that is the pre-Lite assurance claim the adopted vision explicitly replaced with "calibrated trust in an accountable PM" (vision-assessment §2.2.1). Line 7: "The safety chain … **never changes; only who holds the gates does**" — this is, almost verbatim, the "constant chain" identity sentence the vision revision deliberately abandoned in favour of "constant outcomes, risk-proportional process" (§2.2.3), and it sits four lines above a link to the new VISION.md that disclaims it. The Mode B section further down is correctly Lite-shaped; the front-page framing is not.

*Recommendation:* reword the two sentences to the adopted commitments (mechanical floor for the highest-harm failures + accountable, recorded judgement above it; constant protected outcomes with risk-proportional process). This is precisely the "repository claims an assurance model it doesn't run" condition Stage 6 existed to prevent — terminology greps can't catch it because it is phrased in ordinary words.

### F10 (D) — CONTRIBUTING names a retired vision principle as a review criterion

`CONTRIBUTING.md` line 3 tells contributors changes are judged against "*design for the weakest model in the loop*" — a principle the adopted vision explicitly rewrote (weak-model support now lives in plan-level structure and PM tolerance, not universal machinery; vision-assessment §2.2.2). A contributor following this line would optimise for exactly the machinery the replacement deleted.

*Recommendation:* replace with current principles, e.g. "*mechanise the floor; judge the rest*" and "*minimise the whole system*".

### F11 (D) — Small documentation inaccuracies

1. Root README glossary, "Floor": lists seven of the eight facts (repo/branch identity is missing).
2. `skills/project-manager/README.md` privacy table lists `transcript.jsonl` as a produced artifact, and design §9 lists it "when the harness exposes one" — but nothing in the toolkit captures a transcript into the artifact tree (the claude profile's `--session-id` supports operator-side lookup only). Either implement the copy for harnesses that expose one, or drop the row and amend §9.
3. Developer-prompt workflow step 2 says "Write what you ran … to `{artifact_dir}/validation.md`" — correct, but `validation.md` is nowhere machine-required; floor fact 4 checks only `result.json`. This matches the design (validation sufficiency is PM judgement), but SKILL.md's assessment step could say explicitly that a *missing* `validation.md` is a judgement call, not a floor failure, since new PM operators will look for the rule.

### Verified non-findings (checked and clean)

Suspicions raised and discharged during review: `claude --permission-mode auto` is a valid mode (verified against the installed CLI, as are `codex --no-alt-screen` and `opencode --auto`); `git status --short` leading-space parsing is deliberate and commented; rename/quoting edge cases in `status_path` fail closed; the `_INFORMATIONAL_USAGE_RE` sub-100% suppression correctly does not suppress "used 100%"; `create_run`'s partial-failure cleanup is best-effort by declared intent; reviewer pgid = pid via `start_new_session=True` avoids the getpgid race; the notes mirror is regenerated from the controller original at every launch, so Developer vandalism of the mirror cannot poison later prompts.

## 4. Quality observations (no action required)

- **Comment discipline is exemplary and load-bearing.** Nearly every non-obvious decision carries the *constraint* ("a prefix test fails open: 'not yet decided' begins with 'no'"), not narration. This is the main reason the 4.3k-LOC toolkit reads as small.
- **Test suite matches the blueprint's intent**, slightly over target (252 vs ~140–170) but boundary-focused rather than permutational; the overshoot is in the retained parser/profile behaviour suites, which the ledger predicted would stay dense.
- **Failure-message quality is uniformly high** — messages say what to do next, not just what failed, which matters for the weak-PM-seat scenario the vision flags.
- **The one deliberate asymmetry** — `approval_needed` fails closed to *blocking* on unclear values while `independent_audit_required` fails closed to *off* — is correct per the plan contract (approval is a human gate; audit is an opt-in elevation) and both directions are tested.

## 5. Stage 7 implications and scope recommendations

The review found nothing that invalidates the Stage 7 plan, but it argues for these adjustments (the review's licence to propose boundary changes):

1. **Fix F1–F3 (and ideally F7) before Run A/C.** F2 and F7 are exactly the operational shapes Run A's local pairing will probe; F1 affects the run-report bookkeeping Stage 7 records as evidence; F3 would corrupt any Run B methodology that tries per-slice reviewer selection.
2. **Add two Run C scenarios** to the blueprint §7 list: (a) *launch-time hard prompt* — fake harness that prints a credential prompt before readiness; must refuse to inject (verifies the F2 fix); (b) *stale-review acceptance attempt* — elevated slice, commission both reviews, add a commit, attempt `finalize --accept`; must refuse (the freshness rule is the design's answer to Test 13 and deserves an adversarial pin alongside the unit test).
3. **Run C's `.pm/` vandalism scenario should also vandalise `<git-dir>/pm/run.json`** to demonstrate the `INTEGRITY:` terminal path end-to-end from a live run, not only from the unit suite.
4. **No slice-boundary changes to Stages 1–6 are warranted retroactively** — the stage decomposition proved correct (each stage's AC was verifiable in isolation; the acceptance-after-assessment ordering rule prevented any interim acceptance path). For Stage 7, the boundary between Run C (mechanisable now) and Runs A/B (owner-gated) is right and should be kept.

## 6. Independent review record

This document was independently assessed by Codex CLI (`gpt-5.6-sol`, xhigh reasoning effort) via the orchestrator skill; findings and dispositions are recorded in §7 below.

## 7. Codex assessment findings and dispositions

**Reviewer:** Codex CLI, model `gpt-5.6-sol`, xhigh reasoning effort, read-only sandbox, commissioned via the orchestrator skill (run `reviewers-20260719-105911-94898`, label `01-codex-retro-review-assessment`; full report in the run's `-out.txt`). **Verdict: DISSENT** — the review's findings were individually accurate (all 11 CONFIRMED or ADJUSTED, none refuted) but its "nothing blocks Stage 7" conclusion was not, because of the new findings below. The lead session verified every Codex claim against the code before dispositioning; none was refuted.

### 7.1 Codex's assessment of F1–F11, and what was done

| Finding | Codex verdict | Disposition (same change-set) |
|---|---|---|
| F1 `complete` unreachable | Confirmed (M) | **Fixed:** `finalize --accept` on the last undecided slice and `start-slice`'s all-complete path now set `status=complete`, log a `complete` event, and regenerate the report |
| F2 launch injection unscreened | Confirmed, raised to **High** | **Fixed:** `send_prompt` now runs the full `scan_hard_stop` set against the pane immediately before pasting and refuses closed |
| F3 dead `--reviewer-tools` flag | Confirmed (M) | **Fixed (wired):** stored in `current_slice.launch.reviewer_tools`; `review` prefers it over the run-level configuration |
| F4 unverified read-only loads | Raised to M (report regenerated from unverified state) | **Fixed:** `status`/`observe` verify opportunistically when a token is available; `status --report` renders only from verified state; tokenless human reads stay unverified as documented |
| F5 both-reviews rule vs design nuance | Raised to M (report-set inconsistency) | **Fixed:** target-design §4 amended to the shipped stricter rule, with the amendment note in place |
| F6 `wake_at` writer-less | Raised to M | **Resolved by amendment:** target-design §8 now declares the field reserved (no setter; resume times live in events/notes); a setter is deferred until a live run demonstrates the need |
| F7 fixed readiness deadline | Confirmed (L) | **Partly fixed:** default deadline raised 20 s → 60 s (banner match still returns early). Per-profile deadlines and a fail-closed "not ready" outcome are deferred — deliberately, since deadline expiry being non-fatal is current designed behaviour; revisit if Run A shows launch losses |
| F8 budget resets on stopped-slice re-run | Raised to M; documentation alone insufficient | **Partly resolved:** run-state.md now states the reset explicitly (the recorded stop/re-run pair is the authorization trail). Codex's stronger options — carry attempts across stops, or a recorded human-authorized reset — are an **owner decision** (they change recovery semantics the design specified); logged in §7.3 |
| F9 README old-vision framing | Confirmed (D) | **Fixed:** intro rewritten to the adopted commitments (floor + accountable judgement; constant outcomes, risk-proportional process) |
| F10 CONTRIBUTING retired principle | Confirmed (D) | **Fixed:** principles updated to the adopted set |
| F11 doc inaccuracies | Confirmed (D) | **Fixed:** glossary floor list corrected; privacy table now points at harness-side transcripts instead of a phantom `transcript.jsonl` artifact; SKILL.md states the `validation.md` judgement boundary |

### 7.2 New findings from Codex, verified and dispositioned

1. **Token inheritance (High — Codex's #1; verified correct).** `review.py` launched reviewer subprocesses with the inherited environment — including an exported `PM_RUN_TOKEN` — and Developer tmux sessions could inherit it through the tmux server environment; the defensive assertion covered only the explicit env map. **Fixed:** reviewer `Popen` now receives a sanitized environment; every Developer session's shell command begins with `unset PM_RUN_TOKEN`; regression tests drive both through the documented exported-token workflow.
2. **Budget exhaustion not enforced (High — Codex's #2; verified correct).** Exhaustion set `needs-human` but left the session running, `send`/`finalize --steer` usable and `finalize --accept` open. **Fixed:** exhaustion now force-stops the live session, and `send`/`--steer`/`--accept` are refused while the exhaustion stands; `finalize --stop` (the recording path) and `stop` remain available. Regression tests pin the whole sequence.
3. **Review quiescence/pinning (M; verified correct).** **Partly fixed:** `review` now refuses on a dirty worktree, and SKILL.md instructs quiescing the Developer before commissioning. Residual (documented): a Developer that writes *and reverts* mid-review leaves HEAD-based freshness formally intact; the full remedy — reviewing in an isolated worktree checked out at the reviewed HEAD — is logged in §7.3 as future work.
4. **JSON/MAC read race (M; verified correct in principle).** A verified read could pair new `run.json` with the old MAC and report a false — terminal — integrity breach. **Fixed:** verified reads now take the same advisory lock as writes. Residual (documented): command-level read-modify-write races between two concurrent token-holding processes remain possible; the operating model is one sequential PM agent, and the lock now guarantees any single command sees a consistent pair.
5. **Blueprint §6.6 human anti-resurrection review still pending (process).** Correct; it was always flagged as a human check. Now recorded as an explicit **pre-Run-A/B gate** in HANDOFF.md rather than a floating item. This review's §2 pre-check is advisory input to it, not a substitute.
6. **Stale reviewer pgid on failed review (M; verified correct).** A nonzero-exit reviewer left its recorded process group in state; a later `stop` would SIGKILL that pgid, which PID reuse could point at an unrelated process. **Fixed:** the pgid is now cleared on every exit path before the failure is raised; regression test added.
7. **Remaining doc staleness (L; verified correct).** Glossary "Gate" definition and the over-broad "HMAC-authenticated" phrasing. **Fixed** alongside F9–F11.

### 7.3 Deferred items (owner decisions / future work)

- **F8 stronger form:** carry `attempts` across an explicit stop, or add a recorded human-authorized reset. Changes designed recovery semantics — owner call.
- **Isolated-worktree review input** (§7.2.3 residual): review from a snapshot at the reviewed HEAD instead of the live checkout. Real machinery; justified only if Run A/B shows the dirty-tree guard + quiescence guidance is insufficient.
- **Per-profile readiness deadlines with a fail-closed outcome** (F7 stronger form): revisit after Run A's local-pairing evidence.
- **Blueprint §6.6 human review:** owner performs and records it before Runs A/B.

### 7.4 Codex's Stage 7 additions (adopted)

Codex endorsed §5's scenario additions and contributed six more Run C scenarios, all adopted into the HANDOFF Stage 7 plan: exported-token isolation (Developer and Reviewer must both see `PM_RUN_TOKEN` absent); budget-exhaustion enforcement end-to-end; review race/quiescence; completion lifecycle (`status=complete` on final acceptance and on all-attested runs); reviewer-failure pgid cleanup; concurrent state read/write consistency. It also corrected §5's priority ordering (authority and budget-stop defects before the F-series) — reflected in the fix order actually executed — and endorsed keeping Runs A/B ungated until the fixes and the §6.6 human check land.
