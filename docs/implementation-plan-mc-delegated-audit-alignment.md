# Implementation Plan: Realign Master Controller to Delegated-Audit-by-Default with an Opt-In Independence Gate

**Difficulty:** medium–hard — cross-cutting change to a safety-critical supervisor (prompt template, plan parser, acceptance gate, run-state policy, docs, and two test suites), but each concern isolates cleanly into its own slice with a tight surface.

## Context

Master Controller (MC, Mode B) currently diverges from the Mode A launcher in a way that was never intended: the Mode A launcher tells the orchestrator to *delegate the hostile drift-audit and an independent code-review to another model for independence, and fall back to doing them locally when no worker is available*, whereas MC's rendered per-slice prompt (`skills/master-controller/references/orchestrator-prompt.md`) drops that instruction entirely and addresses both audits straight at the orchestrator. Separately, MC's mechanical worker-launch verification (`worker_evidence_failure` in `gates.py`) fires as a hard acceptance gate whenever `--worker-tools` is set, which turned the worker into a mandatory-but-trivial validation-rerun helper in every test round rather than the independent auditor the design intends.

The operator's settled intent — the correct design — is: **Mode B is Mode A with MC standing in for the human.** The per-slice prompt should be structurally the same as the Mode A launcher; delegating the two audits for independence is a smart, gracefully-degradable prompt choice (self-audit is a valid accepted outcome when only one model is available), not a mechanically-mandated gate. MC keeps its stricter machinery — the three always-recomputed gates (file authorization, commit ancestry/HEAD advancement, clean worktree), the recorded drift/review verdict checks, the fresh-session-per-slice model, and the bounded repair loop — because those are what make MC safe to run unattended. The worker-launch verification is not deleted: it is **demoted to reporting-only by default** (already surfaced in `summarize`) and **re-armed as a hard gate only for a slice that opts in** via a new plan field, `Independent audit required: yes`, for high-stakes slices where the operator wants mechanical proof that an independent model actually audited the change.

This plan is executed **directly by the assistant in this session**, not run through MC — MC cannot rewrite its own supervisor code mid-run. It therefore ends with the Mode A checkpointed launcher.

### Settled design decisions (frozen — do not relitigate during implementation)

- Default posture, every slice: the rendered prompt instructs the orchestrator to delegate drift-audit (hostile) and code-review (independent) to the configured/available worker, read the returned reports, **hold the gate itself**, and self-audit locally when no worker is configured. Self-audit alone never fails a gate.
- MC's acceptance basis is unchanged except for the worker gate: recomputed file authorization, commit ancestry/HEAD advancement, clean worktree, recorded validation/drift/review verdicts + non-empty artifacts.
- `worker_evidence_failure`'s anti-forgery checks (policy-digest match, real positive pid, returncode 0, out/err files inside `worker_artifact_root`, model/role/access/repo match) are **preserved verbatim**. Only the *condition under which a non-pass becomes a blocking gate* changes: it blocks only when the slice's `independent_audit_required` is true; otherwise its outcome is recorded/reported, never blocking.
- New plan field `Independent audit required:` lives in the `Risk Flags` section, sibling to `Approval needed before implementation:`, parsed as exact `yes`/`no`; absent or anything unclear ⇒ `False` (default off).
- `--worker-tools` / `--worker-model` semantics shift from "tools that MUST run" to "the worker MC makes available for delegation." Keep the flag and run-state field **names** to limit churn; change only meaning, comments, help text, and prompt wording.
- Prompt convergence is **content alignment**, not mechanical single-sourcing. One file both Mode A and MC read is an explicit non-goal here.

## Operating Rules for This Plan

- Execute slices in order; each depends on the prior committed state conceptually, and Slices 2–3 depend on the field added in Slice 1.
- Use only the Python standard library and the repo's existing `pytest` setup. Add no dependency, config, or manifest file.
- Do not broaden any slice beyond its Intended Change. In particular, do not refactor unrelated MC code, rename the `worker_tools` field/flag, or attempt mechanical single-sourcing of the Mode A and MC prompts.
- After the final slice, run the full `master-controller` and `ai-orchestrator` suites green and perform the operator-requested fresh-eyes review (see `## Final Validation`).

## Implementation Profiles

- Recommended for frontier/senior implementer (this session): run slices individually, in order, committing each after its targeted tests pass; run the full suite after Slice 3 and again after Slice 4.
- Slices 1–3 are code+test; Slice 4 is docs-only. Do not batch across the Slice 3 → Slice 4 boundary (code vs. docs review differ).

## Slice Batches

- No batches. Each slice is gated alone.

---

## Slice 1: Parse the `Independent audit required:` opt-in field

### Intended Change
- Add an `independent_audit_required` property to the `PlanSlice` dataclass in `skills/master-controller/scripts/mc_lib/models.py`, mirroring the existing `approval_needed` parser: read the `Risk Flags` section, match `Independent audit required:\s*(value)` case-insensitively, return `True` only for an exact `yes` (after `strip().lower().rstrip(".")`), `False` for an exact `no`, and `False` for absent/unclear/missing (fail-closed to off, since independence is a degradable preference by default).
- Add unit tests covering: field present `yes` ⇒ True; present `no` ⇒ False; absent ⇒ False; ambiguous (`maybe`, blank, `not yet`) ⇒ False.

### Acceptance Criteria
- Inputs: `PlanSlice` instances built from Risk Flags text with the new line present/absent/ambiguous.
- Outputs: `slice.independent_audit_required` returns the booleans above.
- User-visible behaviour: none yet — the property is added but not yet consumed by any gate or prompt (that is Slices 2–3).
- Behaviour that must not change: `approval_needed`, `authorized_files`, `missing_sections`, and all existing `PlanSlice` parsing behave exactly as before; `REQUIRED_SECTIONS` is unchanged (the new field is optional, not required).

### Authorized Surface
- Files allowed to change:
  - `skills/master-controller/scripts/mc_lib/models.py`
  - `skills/master-controller/tests/test_plan_state.py`
- Functions/classes/components allowed to change: the `PlanSlice` dataclass (new `independent_audit_required` property only); new test functions in `test_plan_state.py`.
- Tests allowed or expected to change: `test_plan_state.py` (add tests for the new property).

### Explicit Non-Goals
- Do not consume the new field anywhere yet (no gate, prompt, policy, or `check-plan` wiring in this slice).
- Do not add the field to `REQUIRED_SECTIONS` or make it mandatory.
- Do not change `approval_needed` or any other existing property.

### Risk Flags
- Risky surfaces touched: plan parser (shared type consumed by MC), but additive and inert until later slices wire it.
- Approval needed before implementation: no
- Independent audit required: no

### Validation Plan
- Tests to add/update: new cases in `test_plan_state.py` for the four parse outcomes.
- Commands to run:
  - `python3 -m pytest skills/master-controller/tests/test_plan_state.py -q`
- Manual checks: confirm no existing `test_plan_state.py` assertion changed behaviour; confirm the property is pure (no side effects).

### Rollback Path
- Remove the `independent_audit_required` property and its tests.

---

## Slice 2: Realign the orchestrator prompt template and worker policy framing to Mode A

### Intended Change
- Rewrite the required-workflow and worker sections of `skills/master-controller/references/orchestrator-prompt.md` so the orchestrator is instructed to **delegate the drift-audit (hostile) and the code-review (independent) to the available worker for independence, read the returned reports, and hold the gate itself**, with an explicit **fall back to performing the audits locally when no worker is configured/available** — mirroring the Mode A checkpointed launcher's "delegate when it helps, else keep local" language. Add a one-line cross-reference noting this mirrors the Mode A launcher contract in `implementation-plan`.
- Reframe the worker as *available for delegation* rather than *required to run*: change prompt wording from `Required worker tool(s) for this run:` / `Required worker model` / `Required worker effort` to an "available"/"offered" framing, and state that using the worker for the audits is the default-preferred path but self-audit is acceptable when none is available. Keep the authoritative-policy language ("do not construct or invoke a worker harness command yourself"; launch through `worker_jobs.py`) intact.
- Update `render_orchestrator_prompt` (and only the helper functions it calls that produce the reframed strings: `worker_auth_policy_text` phrasing if it references "required") in `skills/master-controller/scripts/mc_lib/runtime.py` to match the new template wording. Do not change `write_worker_policy`'s schema in this slice (policy field names stay; see Slice 3/4 for semantics/reporting).
- Update `skills/master-controller/tests/test_prompts.py` assertions that pin the old "Required worker tool(s) for this run" wording, and add a test asserting the rendered prompt contains both the delegate-the-audits-for-independence instruction and the self-audit-when-no-worker degradation fork.

### Acceptance Criteria
- Inputs: `render_orchestrator_prompt(...)` with and without configured worker tools.
- Outputs: rendered prompt contains (a) an instruction to delegate drift-audit and code-review to the available worker for independence, (b) an explicit local-self-audit fallback when no worker is configured, (c) the "available"/"offered" worker framing, and (d) the retained "do not construct or invoke a worker harness command yourself" + `worker_jobs.py` launch contract language.
- User-visible behaviour: MC's rendered per-slice prompt now reads structurally like the Mode A launcher.
- Behaviour that must not change: the frozen-contract block (intended change / acceptance / authorized surface / non-goals / risk flags / validation / rollback), the commit-hash verification instruction, the result-schema pointer, the repair template, and the `str.format` placeholder discipline all remain intact and render without raising.

### Authorized Surface
- Files allowed to change:
  - `skills/master-controller/references/orchestrator-prompt.md`
  - `skills/master-controller/scripts/mc_lib/runtime.py`
  - `skills/master-controller/tests/test_prompts.py`
- Functions/classes/components allowed to change: the main prompt template's required-workflow and worker-helper sections and their reframed placeholder labels; `render_orchestrator_prompt` and the string-producing helpers it calls (`worker_auth_policy_text` phrasing only); `test_prompts.py` assertions and new tests.
- Tests allowed or expected to change: `test_prompts.py`.

### Explicit Non-Goals
- Do not change acceptance-gate logic (`gates.py`) or `write_worker_policy`'s schema/fields in this slice.
- Do not change the repair template's fix stanzas beyond wording needed for consistency.
- Do not attempt mechanical single-sourcing of the Mode A and MC prompts — content alignment plus the cross-reference line only.
- Do not rename `worker_tools`/`worker_model`/`worker_effort` parameters or run-state fields.

### Risk Flags
- Risky surfaces touched: the exact prompt every MC orchestrator session reads (behavioural surface); mitigated by keeping the frozen-contract block and launch-contract language unchanged and by test assertions on both the new instruction and the retained guardrails.
- Approval needed before implementation: no
- Independent audit required: no

### Validation Plan
- Tests to add/update: reword old "Required worker tool(s)" assertions to the new framing; add a test for the delegate-for-independence + self-audit-fallback instructions.
- Commands to run:
  - `python3 -m pytest skills/master-controller/tests/test_prompts.py -q`
  - `python3 -c "from mc_lib.runtime import render_orchestrator_prompt"` style import smoke via the repo's normal test path (ensure the template still renders with no `str.format` KeyError).
- Manual checks: eyeball a rendered prompt for a sample slice with and without a configured worker; confirm it reads like the Mode A launcher and still forbids raw worker harness commands.

### Rollback Path
- Revert `orchestrator-prompt.md`, `runtime.py`, and `test_prompts.py` to their pre-slice content.

---

## Slice 3: Demote the worker gate to reporting-by-default; re-arm it on opt-in

### Intended Change
- In `skills/master-controller/scripts/mc_lib/gates.py`, change `verify_gate` so the worker-evidence check blocks acceptance **only when `plan_slice.independent_audit_required` is true**. `verify_gate` already receives `plan_slice`, so read the flag directly — no run-state threading needed. When the flag is false (default), still call `worker_evidence_failure` for its computed result but do **not** convert a non-pass into a `gate_failure`; instead let acceptance proceed (the reporting path already exists in `summarize`).
- Preserve `worker_evidence_failure`'s body verbatim (all anti-forgery checks). Only its *use as a blocking gate* is now conditional.
- Ensure `summarize` in `skills/master-controller/scripts/mc_lib/commands.py` continues to list worker launches and flag outputs missing their contracted `RESULT:`/`SECTION:` marker, and clearly labels whether a slice was under the opt-in independence gate or default (reporting-only) mode.
- Update `skills/master-controller/tests/test_gates_verification.py`: rescope the existing worker-gate-blocks tests to opt-in slices (`independent_audit_required: yes`), and add a test that a **default** slice with `--worker-tools` set but no genuine worker launch still **passes** (reporting-only, non-blocking). Keep the forged-manifest and narration-without-launch rejections, but under the opt-in condition.

### Acceptance Criteria
- Inputs: gate verification for (a) a default slice with no genuine worker evidence; (b) an opt-in slice with no genuine worker evidence; (c) an opt-in slice with real validated worker evidence; (d) an opt-in slice with a forged/narrated manifest.
- Outputs: (a) passes (worker verification non-blocking); (b) fails with signature `worker-evidence`; (c) passes; (d) fails with signature `worker-evidence`.
- User-visible behaviour: default MC runs no longer stop on missing/self-audited worker evidence; opt-in slices still get mechanical independence enforcement.
- Behaviour that must not change: the three always-recomputed gates (file authorization, commit ancestry/HEAD advancement, clean worktree), the validation/drift/review verdict + artifact checks, the changed-files bookkeeping check, the commit checks, repair-loop classification for all non-worker signatures, and `worker_evidence_failure`'s internal anti-forgery logic.

### Authorized Surface
- Files allowed to change:
  - `skills/master-controller/scripts/mc_lib/gates.py`
  - `skills/master-controller/scripts/mc_lib/commands.py`
  - `skills/master-controller/tests/test_gates_verification.py`
- Functions/classes/components allowed to change: `verify_gate` (worker-gate condition only) in `gates.py`; `summarize` (worker-delegation reporting/labelling only) in `commands.py`; tests in `test_gates_verification.py`.
- Tests allowed or expected to change: `test_gates_verification.py`.

### Explicit Non-Goals
- Do not alter `worker_evidence_failure`'s internal checks.
- Do not change any non-worker gate, the repair circuit breaker, or commit/ancestry/worktree logic.
- Do not modify `start_slice`/`run_next`/`finalize_slice` control flow beyond what `summarize` reporting requires (the opt-in flag is read in `verify_gate`, not threaded through these).
- Do not rename `worker_tools` state fields.

### Risk Flags
- Risky surfaces touched: MC's acceptance gate — the core safety mechanism. This is the highest-risk slice. Mitigated by preserving every other gate unchanged, keeping the anti-forgery body intact, and testing all four gate outcomes explicitly.
- Approval needed before implementation: no
- Independent audit required: no

### Validation Plan
- Tests to add/update: rescope worker-gate tests to opt-in; add the default-slice-passes-without-worker test; keep forged/narration rejections under opt-in.
- Commands to run:
  - `python3 -m pytest skills/master-controller/tests/test_gates_verification.py -q`
  - `python3 -m pytest skills/master-controller/tests -q` (full MC suite green after this slice)
- Manual checks: confirm no non-worker gate path changed; confirm a default slice with a self-authored (local) audit and no worker is accepted, and an opt-in slice without genuine worker evidence is rejected with signature `worker-evidence`.

### Rollback Path
- Revert `gates.py`, `commands.py`, and `test_gates_verification.py` to their pre-slice content (restoring the always-on worker gate).

---

## Slice 4: Align documentation, VISION, and the plan-field contract

### Intended Change
- `skills/master-controller/SKILL.md`: update the roles table, the `Workflow` step-6 worker-gate description, the `Safety Rules` / trust-boundary text, and the `Default Operating Path` so they describe (a) delegated drift-audit + code-review as the default, gracefully-degradable posture with the orchestrator holding the gate, (b) worker-launch verification as reporting-only by default, and (c) the opt-in `Independent audit required: yes` gate. Reframe `--worker-tools` language from "required" to "available for delegation."
- `skills/implementation-plan/SKILL.md`: document the new `Independent audit required:` field in the `Machine-Consumed Fields` and `Execution Modes` sections (exact `yes`/`no`, sibling to `Approval needed`, default off, binds MC's worker-evidence gate when `yes`).
- `docs/VISION.md`: state explicitly (currently silent) the principle that cross-model audit independence is a **degradable preference chosen through the prompt**, not a mechanical requirement, and that a plan may **opt a high-stakes slice into** mechanical independence enforcement — consistent with "rungs vary who holds the gates, never what the gates are." Keep it at principle level; do not encode command syntax.

### Acceptance Criteria
- Inputs: a reader of each doc.
- Outputs: the three docs consistently describe the delegated-audit-by-default + opt-in-gate model, with no residual claim that MC mandates a worker on every slice.
- User-visible behaviour: documentation only; no code behaviour changes.
- Behaviour that must not change: no code or test file is touched in this slice; all machine-consumed field labels documented match the parser added in Slice 1 exactly.

### Authorized Surface
- Files allowed to change:
  - `skills/master-controller/SKILL.md`
  - `skills/implementation-plan/SKILL.md`
  - `docs/VISION.md`
- Functions/classes/components allowed to change: prose sections named above only.
- Tests allowed or expected to change: none.

### Explicit Non-Goals
- Do not change any code, script, or test.
- Do not revise `README.md` or other skills' docs beyond the three files listed (a follow-up may sweep README if needed; out of scope here to keep the surface narrow).
- Do not introduce command syntax into `VISION.md`.

### Risk Flags
- Risky surfaces touched: none (docs only), but these docs are the single source of truth for the contract, so accuracy against the shipped Slices 1–3 matters.
- Approval needed before implementation: no
- Independent audit required: no

### Validation Plan
- Tests to add/update: none.
- Commands to run:
  - `python3 -m pytest skills/master-controller/tests skills/ai-orchestrator/tests -q` (confirm docs slice broke nothing).
- Manual checks: cross-read the three docs against the shipped code from Slices 1–3; confirm the `Independent audit required:` field label matches the parser exactly and that no doc still says a worker is required on every slice.

### Rollback Path
- Revert the three documentation files to their pre-slice content.

---

## Slice 5: Fail fast and fail closed when an opt-in slice has no worker available

### Intended Change
- Two robustness gaps surfaced in the Slice 1–4 fresh-eyes review are closed here (added post-hoc, operator-authorized, after Slice 4 shipped):
  1. **Terminal, not repairable.** In `skills/master-controller/scripts/mc_lib/gates.py`, the opt-in branch's "no worker made available" case currently returns the `worker-evidence` signature, which is *repairable* — so the repair loop burns its whole budget trying to steer an orchestrator that cannot fix an operator/plan misconfiguration before finally stopping. Introduce a new terminal signature `worker-unavailable` (add to `TERMINAL_SIGNATURES`) and return it from that branch, so MC stops for a human immediately with a clear reason. The `worker_evidence_failure` path (a worker *was* available but did not produce genuine evidence) stays `worker-evidence`/repairable, since the orchestrator can fix that by actually running the audit.
  2. **Fail fast at preflight.** In `skills/master-controller/scripts/mc_lib/commands.py`, `preflight` should fail (not silently pass) when the next runnable slice is marked `Independent audit required: yes` but no `--worker-tools` was configured, so the operator learns at setup time rather than at the finalize gate. Add a `check("independent-audit worker available", ...)` that fails in exactly that condition.
- Update the existing `test_gate_opt_in_without_available_worker_blocks` test to expect `needs-human` / `worker-unavailable`, and add a preflight test in `test_harness_adapters.py` for the opt-in-without-worker failure.

### Acceptance Criteria
- Inputs: (a) `verify_gate` for an opt-in slice with no worker tools; (b) `preflight` for a run whose next slice is opt-in with `--worker-tools` empty; (c) `verify_gate` for an opt-in slice whose configured worker produced no genuine evidence.
- Outputs: (a) `GateDecision` status `needs-human`, signature `worker-unavailable`; (b) preflight returns non-zero with an `independent-audit worker available` failure line; (c) unchanged — status `repairable`, signature `worker-evidence`.
- User-visible behaviour: an opt-in slice with no configured worker stops the run cleanly for a human at preflight, and (as a backstop) terminally at the gate, instead of consuming repair attempts.
- Behaviour that must not change: default (non-opt-in) slices are unaffected; opt-in slices with a configured worker still run the full mechanical verification; the `worker_evidence_failure` body is untouched; all other gate signatures and their repairable/terminal classification are unchanged.

### Authorized Surface
- Files allowed to change:
  - `skills/master-controller/scripts/mc_lib/gates.py`
  - `skills/master-controller/scripts/mc_lib/commands.py`
  - `skills/master-controller/tests/test_gates_verification.py`
  - `skills/master-controller/tests/test_harness_adapters.py`
- Functions/classes/components allowed to change: `TERMINAL_SIGNATURES` and the opt-in no-worker branch of `verify_gate` in `gates.py`; `preflight` in `commands.py`; the opt-in-no-worker test in `test_gates_verification.py`; a new preflight test in `test_harness_adapters.py`.
- Tests allowed or expected to change: `test_gates_verification.py`, `test_harness_adapters.py`.

### Explicit Non-Goals
- Do not change the `worker_evidence_failure` body or any other gate signature.
- Do not alter the default-slice (reporting-only) path.
- Do not add a `start-slice`-level refusal in this slice (preflight fail + gate terminal backstop are sufficient); keep the surface tight.

### Risk Flags
- Risky surfaces touched: the acceptance-gate signature taxonomy and preflight. Mitigated by an additive terminal signature (no existing signature reclassified) and a preflight check that only fires on the precise opt-in-without-worker condition.
- Approval needed before implementation: no
- Independent audit required: no

### Validation Plan
- Tests to add/update: opt-in-no-worker gate test expects `worker-unavailable`/`needs-human`; new preflight opt-in-no-worker failure test.
- Commands to run:
  - `python3 -m unittest test_gates_verification test_harness_adapters` (from the tests dir)
  - Full MC + ai-orchestrator suites green.
- Manual checks: confirm a default slice with no worker still passes, an opt-in slice with a genuine worker still passes, and only the opt-in-without-worker case newly stops terminally/at preflight.

### Rollback Path
- Revert `gates.py`, `commands.py`, `test_gates_verification.py`, and `test_harness_adapters.py` to their pre-slice content (restoring the repairable `worker-evidence` classification and removing the preflight check).

---

## Final Validation

After Slice 4, before declaring the change complete:

- Run the full suites green: `python3 -m pytest skills/master-controller/tests skills/ai-orchestrator/tests -q`. If the run exceeds ~1 minute, delegate it to a subagent that captures output and returns a pass/fail summary (per standing instruction), then act on that report.
- Perform the operator-requested **fresh-eyes review**: re-read the four committed slices as a whole for correctness, robustness, gaps, and fit-for-purpose — specifically (1) that a default MC run now accepts a self-audited slice with no worker and no gate stop, (2) that an `Independent audit required: yes` slice still gets full mechanical worker-launch enforcement, (3) that no non-worker gate or the anti-forgery body was weakened, and (4) that the rendered prompt now instructs delegated-for-independence audits with a local fallback. Report findings; do not silently patch during the review — surface issues first.

---

## Next Chat Prompt

```md
Plan file: /Users/dcroton/Documents/AI/repos/ai-engineering-skills/docs/implementation-plan-mc-delegated-audit-alignment.md
Slices or batch this session: Slices 1–4 in order (executed directly by the assistant, not via MC)

Read the full plan file first. If a selected slice receipt is incomplete or the plan state is unclear, stop and tell me before coding.

Work on a dedicated feature branch for this plan; if none exists, create one and tell me the name.

Use ai-orchestrator as the controlling skill. Keep the implementation local; delegate per that skill's guidance when independence or context economy helps — primarily the hostile drift-audit skill, an independent code-review skill pass, and the long full-suite test run.

For each slice, in plan order:
1. Restate the frozen contract (authorized surface + non-goals) from the plan.
2. If any included slice's Risk Flags mark approval-needed, stop and get my approval before coding.
3. apply the scoped-implementation skill against the selected contract.
4. apply the drift-audit skill. Report the authorization gate result before any quality review.
5. If the gate passes, apply the code-review skill. If it fails, fix the drift and re-audit.
6. Surface drift and review findings to me, fix them, then re-run the relevant gate.
7. Ask me before committing. On my approval, commit the slice with the commit skill.

After Slice 4 is committed, run the Final Validation section (full suites green + fresh-eyes review) and report. Do not continue past the plan's scope.

Confirm before starting: plan file read, branch, and the first slice.
```
