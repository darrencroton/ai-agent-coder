# Repository Review — Design-Principle Rubric, Joint Code Review + Simplifier Pass

**Date:** 2026-07-12
**Scope:** The full `ai-engineering-skills` repository at commit `6f2b534` (post "MC delegated-audit-by-default with opt-in independence gate" merge): all ten skills, the master-controller runtime (`mc_lib`, 16 modules), the ai-orchestrator launcher (`worker_contract.py`, `worker_jobs.py`), all reference contracts, top-level docs, CI, and both test suites.
**Method:** A joint `code-review` + `code-simplifier` pass anchored on `docs/VISION.md`. Every SKILL.md, reference contract, and Python module was read in full; both test suites were run (results below); each of the vision's eight design principles was turned into a rubric axis and scored 1–5 against concrete evidence. This review deliberately uses a different rubric from the 2026-07-11 report (`docs/report-repo-review-20260711.md`), which scored ten product/engineering axes; today's yardstick is the vision's own design principles.

---

## 1. Validation Evidence

- Compile checks: `py_compile` clean over `mc.py`, all of `mc_lib/`, `worker_contract.py`, `worker_jobs.py`.
- Master Controller suite: **214 tests, OK** (~120 s, includes tmux-backed runtime tests with fake harnesses).
- AI Orchestrator suite: **13 tests, OK**.
- CI workflow (`.github/workflows/ci.yml`) matches CONTRIBUTING's claims: Python 3.13, tmux installed, read-only token, no persisted credentials.
- Working tree clean; strays (`.DS_Store`, `__pycache__`, `.pytest_cache`, `mc-test/`, `.claude/`) all correctly gitignored, none tracked.

---

## 2. The Rubric

Each axis is one of VISION.md's eight design principles. A **5** means the principle is fully realized in both code and documentation, with no material exception found in this review.

| # | Principle | Score |
|---|-----------|:-----:|
| 1 | Trust the architecture, not the model | **5** |
| 2 | One responsibility per layer, fixed at the owning layer | **5** |
| 3 | Graduated autonomy, constant chain | **5** |
| 4 | Design for the weakest model in the loop | **5** |
| 5 | Atomic usefulness is non-negotiable | **5** |
| 6 | One source of truth per contract | **4** |
| 7 | Fail closed; repair bounded; never relax a gate | **5** |
| 8 | An honest threat model, stated where it matters | **5** |

**Total: 39/40.** The one non-5 axis is carried by three small, mechanical single-source defects (§4), all fixable in under an hour of work combined.

---

## 3. Per-Principle Assessment

### P1 — Trust the architecture, not the model: 5/5

The highest-risk checks are genuinely recomputed, never read from the report. `gates.py:verify_gate` derives changed files from git (`changed_files_between` over the persisted `before_head`), matches them against the frozen surface with segment-aware `PurePath.full_match` (so `*.md` cannot cross directories, `git_ops.py:131-147`), and validates HEAD advance and descent from the slice start *on git evidence alone, before any comparison with the self-reported hash* (`gates.py:481-487`) — so a truthful report of a reset-to-unrelated HEAD still fails. Verdict-field trust is hardened at the edges: `artifact_exists` (`gates.py:122-146`) requires a non-empty file that resolves *inside* the slice artifact directory, closing both the "point `path` at `/etc/hosts`" and the empty-placeholder shapes. The opt-in worker gate requires a positive subprocess pid plus real out/err files inside `worker_artifact_root` (`gates.py:249-273`), defeating the hand-authored-JSON forgery. Everywhere a claim is *not* recomputed, the docs say so (see P8).

### P2 — One responsibility per layer: 5/5

MC never writes code — its only mutation of an orchestrator artifact is the commit-hash reconciliation, which runs strictly after every other gate has passed and is bounded to one proven evidence field (`gates.py:329-365`). Repair prompts instruct the orchestrator to fix its own gap; the restore-only stanza for unauthorized files even hands over the exact `git checkout` command rather than running it (`runtime.py:722-737`). Semantic verification of worker output is explicitly the orchestrator's job; MC's `worker_delegation_overview` is commented "Observability only — never part of gate acceptance" and behaves that way (`runtime.py:846-856`, `summarize`). The two execution drivers share one decision core (`resolve_repair_action`, `runner.py:110-166`) precisely so budget/breaker judgments cannot fork by path — a structural enforcement of the layer boundary, not a convention. MC's import of `worker_jobs.py` as a library is the right direction: session-path knowledge stays at its owning layer and is reused, not reimplemented (`runtime.py:398-414`).

### P3 — Graduated autonomy, constant chain: 5/5

This week's delegated-audit realignment is the principle applied end-to-end: the audit gate ("was this audited?") is constant at every rung; independence is a degradable preference expressed identically in the Mode A launcher, the MC orchestrator prompt (which now explicitly names itself "the Mode B counterpart of the Mode A assisted-run launcher"), and the worker contract; and only the opt-in `Independent audit required: yes` flag changes *who must prove* independence — enforced mechanically in Mode B, judged by the human/orchestrator in Mode A. `implementation-plan`'s "Execution Modes" section states exactly which plan features bind in which mode, and `check-plan` warns when a plan uses Mode-A-only batches. Launchers are single-sourced per mode with the handoff prompt derived, not restated. No gate differs in *what* it checks between rungs — only in who holds it.

### P4 — Design for the weakest model in the loop: 5/5

The structure-substitutes-for-capability commitment is visible at every seam: launch rejections produce field-specific corrections in `<label>-request-feedback.md` designed so "even a weak orchestrator can self-correct" (`worker_jobs.py:1271-1298`); required skills are embedded as complete transitive Markdown bundles with fail-closed rejection when a resource is missing (`worker_contract.py:401-444`); repair delivery is a one-line pointer because a newline typed into a TUI submits a partial message (`runner.py:601-612`, enforced twice — in `send` and `send_literal`); prompt injection uses a reproduced-and-documented settle-and-double-Enter discipline (`tmux_adapter.py:273-286`); readiness detection has banner-keyed paths with a stable-pane fallback because banner strings are version-fragile (`tmux_adapter.py:229-245`); and the monitoring rules in `ai-orchestrator`'s SKILL.md encode exactly the weak-model failure weather (silent prefill, refusals, no-output thinking periods) with conservative cancellation thresholds.

### P5 — Atomic usefulness: 5/5

Every chain skill reads standalone and infrastructure-free: `drift-audit` blocks itself without a frozen contract; `code-review` and `commit` assume nothing about MC; `handoff` derives rather than restates the launcher; the `openai.yaml` stray is now documented in place as a Codex UI stub. MC-specific behavior in `ai-orchestrator` is contained in one clearly-scoped "Under Master Controller" section. Residual nits only: `commit`'s worker-delegation line and `handoff`'s "Orchestrator State" section quietly assume the orchestrator ecosystem (harmless when absent), and `report` remains deliberately lighter than its siblings.

### P6 — One source of truth per contract: 4/5

The repository's best single-source device deserves naming: MC renders its orchestrator and repair prompts by *extracting the template from `references/orchestrator-prompt.md` at runtime* (`runtime.py:609-621`), so the documented contract and the executed contract are physically one artifact. The CONTRIBUTING source-of-truth map is accurate on every pointer spot-checked. Three mechanical defects keep this from a 5:

1. **`KNOWN_UNATTENDED_HARNESS_COMMANDS` duplicates each profile's `base_command`** (`constants.py:65-70` vs `72-145`). The same launch vocabulary lives twice in one file; editing a profile without the known-defaults dict silently makes `--allow-unattended-default` and `--allow-profile-command` launch different commands for the same harness.
2. **The authorized-surface matcher is duplicated across skills** — `git_ops.py:117-147` and `worker_contract.py:137-160` carry byte-identical logic and comments with no parity test. The duplication is a defensible price of atomic skills (ai-orchestrator cannot import from MC), but nothing currently pins the copies together; if they drift, the launcher's workspace-write file authorization diverges from the gate MC recomputes. MC's own gate remains the backstop, so the impact is bounded — but the vision explicitly calls duplicated guidance a defect *even when the copies currently agree*.
3. **`--allow-unattended-default` is documented in no Markdown file.** It is a safety-posture flag (it disables per-action approval, making MC's post-hoc gates the only boundary) whose sole documentation home is CLI help text and the error message that suggests it (`tmux_adapter.py:100-106`). Discoverable, but a contract without a home.

### P7 — Fail closed; repair bounded; never relax a gate: 5/5

Fail-closed is implemented at every entry: `check-plan` runs at `init` and errors abort before any state exists; approval flags must be an exact `yes`/`no` with a comment explaining precisely why a prefix test fails open (`models.py:69-80`); the plan digest freezes at init and any mid-run edit stops the run. The repair loop is bounded three ways (budget, signature streak, terminal third strike) from persisted state, and re-verification is the *identical* gate — the code comments state the invariant ("a repairable classification can never let a bad slice through") and the structure enforces it. The batch driver converts timeout, interrupt, refused repair delivery, and unexpected exceptions into forced fail-closed terminals rather than orphaning sessions (`runner.py:669-849`), `status` detects the killed-mid-command orphan signature, and the new `worker-unavailable` signature on opt-in slices correctly stops terminally instead of burning repair budget on a config mismatch the orchestrator cannot fix (`gates.py:43-53`). Slice 5's addition — preflight fails fast when an opt-in slice has no worker configured (`commands.py:988-996`) — moves the stop from after a full slice run to before launch, exactly the right direction.

### P8 — An honest threat model, stated where it matters: 5/5

The trust-boundary paragraph in MC's SKILL.md is the standard the rest of the industry doesn't meet: it names what is recomputed, what is evidence-checked, and states plainly that "a dishonest orchestrator that both writes passing fields and fabricates non-empty artifacts is therefore outside what MC detects, by design." The worker gate's candor survives the new opt-in strengthening — "it raises the mechanical bar rather than making forgery cryptographically impossible… a worker that refused its task but exited cleanly still satisfies it." Heuristic stops are labeled heuristic where users will read it, with the plan-level compensating control turned into a real check (`surface_lint`). Harness coverage gaps are enumerated per profile (which prompt classes were directly observed vs. keyword-inferred). `assumed-complete` entries are labeled operator attestations, never gate verdicts, in both code comment and recorded `gate_reason`.

---

## 4. Code-Review Findings

All findings are P3 (minor); no P0/P1/P2 defects were found. **Verdict: PASS.**

1. **[P3] `skills/master-controller/scripts/mc_lib/cli.py:55` — `--allow-unattended-default` undocumented.** Safety-relevant flag absent from SKILL.md "Commands" and README "CLI". Fix: one sentence in each, next to `--allow-profile-command`. (Also closes part of P6.)
2. **[P3] `skills/master-controller/scripts/mc_lib/constants.py:65` — duplicated launch vocabulary.** Derive `KNOWN_UNATTENDED_HARNESS_COMMANDS` from `HARNESS_PROFILES[...]["base_command"]` via `shlex.join`, or add a regression test asserting the two stay equal per harness.
3. **[P3] `skills/ai-orchestrator/scripts/worker_contract.py:137` — matcher duplication with `mc_lib/git_ops.py:117`, no parity test.** Keep the copy (atomicity justifies it) but add one MC-side test that runs a shared fixture table of (path, entry, expected) through both implementations. MC's tests may import `worker_contract.py` the same way `runtime.py` already imports `worker_jobs.py`.
4. **[P3] `skills/master-controller/scripts/mc_lib/commands.py:692` — `stop_with_evidence` re-parses the plan without `verify_plan_unchanged`,** unlike every other runtime path. Impact is low (terminal path; only affects the recorded slice entry), but the inconsistency is exactly the kind that later reads as intent. Add the check.
5. **[P3] `skills/master-controller/scripts/mc_lib/commands.py:596` — no-op:** `wait_args = copy.copy(args)` followed by `wait_args.poll_seconds = args.poll_seconds` reassigns an already-copied attribute; pass `args` directly.
6. **[P3] `skills/master-controller/scripts/mc_lib/tmux_adapter.py:304` — literal send text beginning with `-`** can be consumed by tmux option parsing. It fails loud (fail-closed), but inserting `--` before the text argument removes the edge entirely.

## 5. Simplifier Recommendations (behavior-preserving, not applied)

Per the code-simplifier contract these are reported, not implemented — the codebase is working, tested, and the repo's own policy is "split when next touched."

- **`worker_jobs.py` (1,757 lines)** mixes four concerns: contract launch, per-vendor session-transcript heuristics (Claude + Codex resolution/activity/extraction ≈ 600 lines), tracked-process running, and the CLI. When next touched, extract the session heuristics into a sibling module (the skill already ships two script files, so atomicity is preserved).
- **`claude_session_activity` / `codex_session_activity` (`worker_jobs.py:658-752`)** are structurally identical apart from the row summarizer; one function taking the summarizer would halve the code.
- **`stop_with_evidence` (`commands.py:672-676`)** re-implements `_capture_git_evidence` inline; reuse the runner helper.
- **`commands.py` (1,054) / `runtime.py` (938) / `runner.py` (849)** remain the concentration risk the 2026-07-11 review noted; the per-responsibility grouping inside them is clean, so this stays opportunistic, not urgent.

Explicitly *not* recommended: any change to gate semantics, the repair loop, the worker contract shapes, the prompt-template-extraction device, or the two-driver/one-engine structure. These are the tested core and the best parts of the design.

---

## 6. Whole-System Verdict

Yesterday's review closed the product-shell gaps; this review confirms the engine underneath deserves the shell it now has. Measured against its own constitution, the repository scores 39/40, and the single lost point is bookkeeping, not architecture. Two things stood out as genuinely better than the vision requires: the runtime extraction of prompt templates from the reference docs (making doc/code drift structurally impossible for the two most important prompts), and the consistency with which every hard-won behavioral fact — TUI paste races, banner fragility, credential portability, weak-model silences — is recorded as a code comment at the exact point of enforcement. The delegated-audit-by-default change landed clean across all five layers it touched (parser, gates, prompt, docs, vision) with the vision revised deliberately per the governance rule, which is the governance working as designed.

**Recommended next actions, in order:**

1. Fix findings 1–2 and 4–6 in one small hygiene slice (docs line, derived constant, digest check, no-op removal, `--` guard). (S)
2. Add the matcher parity test (finding 3). (S)
3. Schedule the `worker_jobs.py` session-heuristics extraction for whenever that file is next touched for behavior. (M, deferred)
4. Consider tagging `v0.2.0`: the two-mode taxonomy, check-plan gate, and delegated-audit default are a coherent contract change over `0.1.0`, and the changelog already describes them. (S)

**Residual risks (unchanged from 2026-07-11):** bus factor of one; validation environment is author-shaped beyond CI; external-user feedback remains the only unverified claim behind the onboarding scores.

---

## 7. Follow-Up Completion — 2026-07-13

All six review findings and all three behavior-preserving simplifications were completed before the `v0.2.0` release:

- Findings 1–2 and 4–6 were closed in `f2f3c23`; finding 3's cross-skill authorized-matcher parity table was added in `c50699f`.
- Vendor-specific Claude Code and Codex CLI session discovery, activity interpretation, and transcript extraction were moved from `worker_jobs.py` into the sibling `worker_sessions.py` module without changing the launcher or CLI contracts.
- The structurally identical Claude/Codex activity implementations now share one payload builder parameterized by the vendor row summarizer.
- `stop_with_evidence` now reuses the runner's canonical git-evidence helper; regression coverage pins both that reuse and the fail-closed mid-run plan-digest check.
- Maintainer maps and CI compile coverage include the new module.

An independent Claude Code Opus review inspected the complete diff since `6f2b534`, relevant callers and tests, and both full suites. Its initial verdict was **PASS WITH RISKS**: no P0/P1 findings, one P2 maintainer-documentation inconsistency, and two P3 changelog/test-coverage gaps. All three findings were resolved. A subsequent fresh-eyes review found and closed one additional gap in CI's explicit compile list.

Final validation: ai-orchestrator **17 tests, OK**; Master Controller **216 tests, OK** in 110.732 seconds with zero skips; compile checks and `git diff --check` clean. Final release-review verdict: **PASS**.
