# HANDOFF

## Objective
- Complete the approved Mode B Lite greenfield replacement of the Project Manager system, per the binding six-report spec in `docs/mode-b-lite/` (authority order: proposed-vision → target-design → replacement-ledger → implementation-blueprint). Do not re-litigate the design. Stages 1–6 are done; Stage 7 (live validation & reassessment) remains.

## Task List
- [x] Deletion, Stages 1–4 (`f9d36a3`, `20e6ecc`, `c767127`, `b41acaa`, `db0f0f2`): full toolkit (`pm_lib/`, 11 modules), 252 tests green, every stage AC verified end-to-end.
- [x] Stage 5 (`61074d9`): docs validated flag-by-flag against the shipped CLI; README "Verify your setup" trial run verbatim by a fresh session (floor 8/8, acceptance, report, scavenge).
- [x] Stage 6 (this change-set): root README Mode B sections rewritten per Lite; CONTRIBUTING source-of-truth map updated; `implementation-plan`/`handoff`/`report` SKILL.md texts re-bound (`Independent audit required: yes` ⇒ elevated risk); orchestrator docs rewritten standalone-only; `.gitignore` state-dir entry now `.pm/`; `docs/VISION.md` replaced with the adopted vision; blueprint §6 no-baggage greps pass and run in CI (`ci.yml`, two steps with historical-file + orchestrator carve-outs).
- [x] Retrospective project review (pre-Stage 7): holistic code review written (`docs/mode-b-lite/retrospective-code-review.md`), independently assessed by Codex `gpt-5.6-sol` at xhigh effort (verdict DISSENT — two high-severity defects the review missed: run-token environment inheritance into Reviewer/Developer sessions, and unenforced attempt-budget exhaustion); outcomes scorecard written (`docs/mode-b-lite/outcomes-review.md`). All straightforward findings **fixed in the same change-set** (token sanitization, budget exhaustion made terminal, launch-time hard-stop screening, `complete` transitions, reviewer-tools wiring, verified read-only loads, review dirty-tree guard, JSON/MAC read locking, pgid cleanup, readiness deadline 60 s, doc corrections F9–F11, design amendments for the both-reviews rule and `wake_at`). Deferred owner items in review §7.3.
- [ ] Stage 7 — Run C (adversarial spot-checks, blueprint §7 + review §7.4): scripted fake-harness scenarios — false success report, unauthorized file change, `.pm/` vandalism (must not corrupt PM state or decisions; damaged Developer evidence fails the slice closed via floor fact 4), **authenticated `<git-dir>/pm/run.json` vandalism incl. `auth.token_sha256` (must be a terminal `INTEGRITY:` stop)**, wrong-slice result, approval-gate bypass, **launch-time hard prompt (must refuse to inject), stale-review acceptance attempt (must refuse), exported-token isolation (Developer and Reviewer must both see `PM_RUN_TOKEN` absent), budget-exhaustion end-to-end (session killed; send/steer/accept refused; `finalize --stop` records), completion lifecycle (`status=complete` on final acceptance and all-attested), reviewer-failure pgid cleanup**. Bar: every one caught or rendered harmless. Mechanisable without owner input.
- [ ] Stage 7 — Run A (killer case, **owner gates**): local pairing qwen3.6-27b Developer / qwen3.6-35b review seat on the `pm-test` fixture's hard 5-slice plan. Bar: ≥ 4/5 slices sound where the four documented baseline runs (Tests 14/16/17/18) got 0/5.
- [ ] Stage 7 — Run B (strong pairing, **owner gates**): Test 6/12-class pairing. Bar: 5/5 parity with fewer PM interventions and materially fewer model interactions (count from `events.jsonl` vs baseline operational events).
- [ ] Stage 7 — bookkeeping: record per run slices completed, wall-clock, interventions, human touches, artifact volume, assessment usefulness; update blueprint §9 metrics as *measured*; write the CHANGELOG adoption entry. If Run A misses the bar, apply blueprint §8: diagnose implementation vs design vs vision — do not patch forward.
- [ ] Blueprint §6.6 anti-resurrection review — a one-time **human** check (no enum failure classifiers, no second state copy, no per-round artifact families, no verdict-string parsing); flagged to the owner, not an agent task.

## Current Status
- Branch `feature/mode-b-lite-impl`; committed through Stage 6, with the retrospective-review fix change-set on top (uncommitted pending owner approval). Both test suites green; no-baggage greps clean and enforced in CI.
- Stage 6 work was independently reviewed pre-commit; the full retrospective was independently assessed post-Stage 6 (see Developer / Reviewer State).

## Stage 7 Runbook — exact prompts and pre-steps

**Before anything else (order matters):**
1. Owner approves and commits the retrospective change-set (review + outcomes docs, fixes, tests, design amendments).
2. Owner performs the blueprint §6.6 **human anti-resurrection review** and records the outcome (review §2's pre-check is advisory input, not a substitute). Gate: required before Runs A/B.
3. Script and run **Run C** from a scratch clone of `pm-test` using the `--harness-command` fake-harness pattern (reuse `skills/project-manager/tests/` builders; remember: fake scripts must drain stdin with `cat -`, never bare `sleep`). No owner gate, no model cost.
4. For Runs A/B: confirm `~/.claude/skills/project-manager` (and any other harness's skill dir) resolves to **this branch's** skill; reset the `pm-test` fixture repo to its clean baseline commit; archive any stale `.pm/` in the fixture and any old run dirs under the fixture's `<git-dir>/pm/`; confirm the local models are loaded and use **fully-qualified OpenCode model ids** (`macstudio/...` — unqualified ids silently fall back, the Test 1 lesson).

**Run A/B launch prompt** — Mode B uses the launcher in `skills/project-manager/SKILL.md` ("Launcher" section — the single source of truth). Filled in for the Stage 7 fixture, paste this verbatim into a fresh PM-capable session:

```md
Plan file: /Users/dcroton/Documents/AI/repos/ai-agent-coder/pm-test/docs/implementation-plan-hard-pi-convergence.md
Repo: /Users/dcroton/Documents/AI/repos/ai-agent-coder/pm-test

Use the project-manager skill. You are the PM. Run this plan under Mode B:
init with --harness opencode --model macstudio/qwen/qwen3.6-27b-bf16 --reviewer-tools opencode --reviewer-model macstudio/qwen/qwen3.6-35b-a3b-bf16 (keep the printed PM_RUN_TOKEN in your environment only), then loop start-slice / observe / assess / finalize per the skill's workflow until every slice is decided, stopping where the plan or floor requires a human. Then report from run-report.md: what was accepted on what evidence, what stopped and why, and residual risk.
```

That is the Run A (killer-case) pairing, matching baseline Tests 14/16/17/18. For **Run B**, swap the two model ids for a Test 6/12-class strong pairing (e.g. `--model github-copilot/gpt-5.6-terra --reviewer-model opencode-go/qwen3.7-plus`) and keep everything else identical. Baselines are already recorded in `pm-test/docs/pm-lessons-learnt.md` — do not rerun them; Lite runs execute from this branch.

**Per run, record** (blueprint §7): slices completed, wall-clock, interventions (count `steer`/`relaunch` events in `events.jsonl`), human touches, artifact volume, and a qualitative read of the assessments — then update blueprint §9 with values marked *measured*, and write the CHANGELOG adoption entry. If Run A misses the ≥4/5 bar, apply §8: diagnose implementation vs design vs vision — do not patch forward.

## Decisions Made
- Owner authorized per-stage commits once ACs pass; owner gates Stage 7 Runs A/B (they cost real model usage on the owner's local pairings).
- Stage 7 validation runs use the existing local `pm-test/` fixture (gitignored, own repo) and `pm-test/docs/implementation-plan-hard-pi-convergence.md`; baseline runs execute from a `main` checkout, Lite runs from this branch (blueprint §7).
- Delegation policy: non-trivial coding via Sonnet subagents from frozen written briefs, tests first; the lead reviews, verifies ACs end-to-end, and owns commits.
- IntegrityError is terminal: never rewrite/re-sign state after a MAC failure; tampered `run.json` survives as evidence.
- Elevated acceptance requires BOTH drift-audit and code-review fresh at the exact final HEAD; `finalize --accept` needs ≥40-char reasoning and a passing floor.

## Failed or Rejected Approaches
- Fake-harness scripts must drain stdin (`cat -`), not bare `sleep` — the pasted multi-KB prompt can saturate the pty queue and drop a later steer line (documented in `test_slice_ops.py`). Directly relevant to scripting Run C.
- A subagent's "heal" of tampered state (re-sign + continue) was rejected; the tamper test pins terminal behaviour.

## Active Blockers
- Runs A/B await explicit owner go-ahead and model pairing confirmation. Run C needs no input.

## Files That Matter
- `skills/project-manager/scripts/pm_lib/*.py` + `tests/` — the shipped toolkit and 252-test suite (fake-harness pattern to reuse for Run C).
- `pm-test/` — local validation fixture (own git repo; not tracked here); `docs/implementation-plan-hard-pi-convergence.md` is the Stage 7 plan; `docs/pm-lessons-learnt.md` holds the baseline test record (Tests 14/16/17/18 = the 0/5 baselines).
- `docs/mode-b-lite/implementation-blueprint.md` — §7 validation plan and bars, §8 stop rule, §9 metrics table to update.
- `.github/workflows/ci.yml` — the two no-baggage grep steps; keep them green in any follow-up.

## Developer / Reviewer State
- Orchestrator run dir: `.orchestrator/runs/` (see latest `reviewers-*` entry). Stage 5+6 change-set reviewed pre-commit by codex `gpt-5.6-sol` (high effort, read-only sandbox): drift-audit against the ledger/blueprint dispositions, then code-review. Findings and dispositions are recorded in the Stage 6 commit message.
- Retrospective review independently assessed by codex `gpt-5.6-sol` (**xhigh** effort, read-only sandbox), run `reviewers-20260719-105911-94898`: verdict DISSENT, 11/11 findings confirmed or upgraded, 7 new findings (2 high-severity). Dispositions in `docs/mode-b-lite/retrospective-code-review.md` §7.
- Audit provenance: all reviews Reviewer-performed (codex); the lead session verified and dispositioned every finding.

## PM-Seat Model for Stage 7
- **Sonnet 5 is the right PM/operator seat for Stage 7; Fable-level is not required.** The strongest-model work (design conformance, greenfield implementation, retrospective + fix hardening) is done and independently reviewed. Run C is bounded scripting against existing test patterns. Runs A/B deliberately measure the *system* with a realistic PM seat — the baselines' supervising sessions were Sonnet-class, so a Sonnet 5 PM keeps the comparison clean, and PM-seat quality is itself one of the things Stage 7 should exercise (the vision declares it load-bearing).
- Escalate back to a Fable-level model (or the owner) for exactly two things: diagnosing a missed Run A bar under blueprint §8 (implementation vs design vs vision attribution), and any amendment to the `docs/mode-b-lite/` reports.

## Validation
- Done: per-stage suites; Stage 5 fresh-reader trial; Stage 6 §6 checks 1–5 (terminology grep, path grep, `pm_lib` import graph stdlib-only, README/CONTRIBUTING link reachability, fixture sweep) all pass locally and the greps run in CI.
- Still needed: Stage 7 Runs A/B/C and the §6.6 human anti-resurrection pass.

## Authorization Gate
- Governed by blueprint §8: any deviation touching roles/gates/floor/state/commands/artifacts/risk/authority requires amending `docs/mode-b-lite/` first in a dedicated commit. None has been needed through Stage 6.

## Next Action
- Owner: approve/commit the retrospective change-set, then perform and record the blueprint §6.6 human anti-resurrection review.
- Then script and run Stage 7 Run C: build the adversarial fake-harness scenarios listed in the Task List (blueprint §7 + review §7.4) against a scratch clone of `pm-test`, record each outcome (caught / rendered harmless) with evidence paths, then report to the owner and request the Run A/B go-ahead using the Stage 7 Runbook prompt above.
