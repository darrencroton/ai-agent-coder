# Repo Review & Simplification Assessment — 2026-08-09

A holistic, high-to-medium level joint code review and simplification pass over every skill in this repository, evaluated against `docs/VISION.md`. Methodology: the repo's own `code-review` and `code-simplifier` skills set the review bar; five parallel deep-review sessions covered the four large skills (with live field tests of `code-health` and `lint` against `../mimic` and `../SAGE26`), the eight small skills, and the repo docs; the supervising session independently mapped the cross-skill workflows and re-verified every load-bearing claim. Test suites were run for all four large skills. The finished draft was then **independently reviewed by a read-only Codex delegate (gpt-5.6-sol, high effort) through the orchestrator's managed launcher**; every one of its findings was assessed against source and this version folds in the corrections (see "Independent verification" at the end).

## Executive summary

**The repo is in strong shape.** The honest headline is the one the vision asks for: the four large skills are fit for purpose, their measurements and mechanical guarantees survived independent field verification, and none of them needs a simplification campaign. Three of the five reviews explicitly concluded "already near minimum for what it honestly claims." The feared over-testing pathology is mostly absent — the big suites pin real incidents and real process outcomes, not padding.

What the review did find is targeted, not systemic:

- **1 P1 (field-verified):** `lint`'s differential mode breaks on repos whose markdownlint config declares `"globs"` — on mimic it reported 132 out-of-scope findings as "introduced by the change." One-token fix (`--no-globs`).
- **2 new P2 robustness gaps surfaced by the independent review of this report:** both PM's `start-slice` and orchestrator's `launch` have a window where the session/process is live before state persists — a persistence failure there strands live work that normal tracking can't see. Real and worth closing; rated P2 rather than the reviewer's P1 because the failures are loud, narrow (I/O failure at one moment), and PM ships a state-independent recovery command (`stop --scavenge`) for exactly this.
- **1 high-value P2 with a live side effect:** `skills/orchestrator/references/claude.md` collides with `CLAUDE.md` on macOS's case-insensitive filesystem (same inode, confirmed) and was observed being auto-injected as overriding instructions into a Claude Code session during this review. One-line rename.
- **~300 LOC of orchestrator mechanism with no downstream consumer** — the single largest simplification opportunity, squarely the vision's "no mechanism for a hypothetical future need" category. One item (`--depends-on`) is a public CLI flag, so its removal is an intentional interface deletion, not a silent cleanup.
- **Two correctness P2s in project-manager:** stale `validation.md` survives attempt rotation, and the poll guard false-denies paginated reads.
- **Prose duplication in the small skills** (~70–110 lines), concentrated in `implementation-plan`, `code-review`, and `handoff`.

Projected net LOC reduction if all recommendations are taken: **≈ 700–930 lines (~3% of the tracked repo)**, dominated by orchestrator dead mechanism and test consolidation — with ~25 lines *added* for the fixes. That is the right size of answer for a repo that has already been through deliberate simplification passes; there is no large low-hanging fruit left, and this report says so rather than inventing any.

## Metrics — before and projected after

All counts exclude `archive/`, untracked runtime residue, and this report itself. "After" is projected from the itemised recommendations; no changes have been applied.

| Measure | Before | Projected after | Δ |
|---|---|---|---|
| Total tracked lines (py+md+sh+yml+json) | 25,330 | ≈ 24,400–24,630 | −700 to −930 |
| Production Python | 11,362 | ≈ 10,930–10,990 | −370 to −430 |
| Test Python | 10,733 | ≈ 10,340–10,480 | −250 to −390 |
| Markdown (docs + SKILL.md prose) | 3,089 | ≈ 2,980–3,010 | −80 to −110 |
| Test-to-code ratio | 0.94 : 1 | ≈ 0.95 : 1 (unchanged in character) | — |

Per-skill projected deltas (sizes are total Python LOC incl. tests, except the last row):

| Skill | Size (LOC) | Projected cut | Character of cut |
|---|---|---|---|
| orchestrator | 5,515 | **−450 to −585 (~8–11%)** | Dead/unconsumed mechanism (~300–335) + test consolidation (~150–250) |
| project-manager | 12,214 | −120 to −140 (~1%) | Hygiene + two test folds |
| code-health | 2,441 | −40 to −60 (~2%) | Test merges only |
| lint | 1,925 (+480 md/config) | ≈ −10 to −25 net | ~19 test LOC cut, +~10 for the P1 fix, doc fixes |
| 8 small skills + repo docs (Markdown) | ~1,290 | −70 to −110 | Prose de-duplication |

## Part 1 — Per-skill assessment

### project-manager (12,214 Python LOC incl. tests) — fit for purpose, unusually tight

Implements Mode B exactly as the vision commits: deterministic toolkit owning state, sessions, artifacts, and the eight-fact floor; everything semantic explicitly assigned to the PM agent. Every CLI command was traced against its documentation and does what it says. The floor is correctly implemented and fails closed: frozen digest, repo/branch identity, approval records, result identity, segment-aware surface matching, ancestry + branch-tip + clean worktree, hard-stop scanning. The trust model in the README honestly matches what the code enforces (HMAC-authenticated state, token withheld from Developer/Reviewer environments, tampered state terminal and never re-signed). Specific hunts for floor bypasses, token leakage, review-staleness holes, and budget evasion found none.

Findings:

- **P2 — stale `validation.md` survives attempt rotation.** `pm_lib/slice_ops.py:503-511` rotates only `result.json`, `pane.txt`, `pane-live.txt`; both relaunch and steer call it. A crashed second attempt leaves the prior attempt's `validation.md` in place — plausible stale evidence the PM is told to read (SKILL.md step 3), indistinguishable from fresh. Fix: add `"validation.md"` to the rotation tuple. *(Verified by two independent readers.)*
- **P2 — poll guard false-denies paginated reads.** `hooks/pm-poll-guard.py:159-182` extracts only `file_path`, hashes the whole file, and keys the stamp by session/task — `offset`/`limit` never participate, so page 2 of a long unchanged task output is denied with a message claiming "no new output". Contrary to the guard's own fail-open philosophy. *(Verified by two independent readers.)*
- **P2 — launch-before-persistence window in `start_slice`** *(new; surfaced by the independent report review, verified in source, severity assessed here)*. The tmux session goes live at `slice_ops.py:691-693`; the cleanup guard covers readiness and prompt injection (`:701-713`) but ends before `save_state` (`:739-745`). If persistence fails there (disk full, permissions, lock), the CLI errors while a live Developer session exists that no authenticated state names — invisible to `observe`/`finalize` and to `stop`'s recorded-session path. The code's own comment shows the window is known and deliberately guarded for the launch steps; the residual gap is the persistence step. Rated P2, not P1: the failure is loud, the trigger is a narrow I/O fault, and `stop --scavenge` (`slice_ops.py:1462`, a state-independent tmux sweep) exists precisely to recover unrecorded sessions. Fix: extend the cleanup guard through successful `save_state`, with a regression test injecting a `save_state` failure after launch.
- **P3 (selection):** `write_git_diff` writes an empty diff for unborn-branch runs while `changed_files_between` handles the same case correctly via `git show` (`git_ops.py:104-133`); `finalize --accept ""`, `--steer ""`, and `--stop ""` all silently degrade to bare evidence-only finalize (`cli.py:454-482` tests truthiness; use `is not None`); dead `except PmError` branches in `floor.py:203-218` (`git_result` never raises `PmError`); `wake_at` is write-only mechanism with no setter or scheduler (vision §9); `policy.commit_required` is read by the floor (`floor.py:193-201`) but no public CLI path can ever set it false (`slice_ops.py:358` hardcodes true) — unreachable configuration, vision §9; a stale docstring in `review.py:280-283` contradicts the documented 3600 s timeout default; floor facts enumerated in four places (`floor.py:16-27`, PM SKILL.md, PM README, repo README — vision §8; make two of them pointers).
- **Test suite:** 374 tests, ~5 min, **374/374 on an independent rerun** (one flaky tmux-timing failure on an earlier run, classified not-real). The suite is heavyweight but not padded — tests assert real process death, persisted state, and elapsed timing rather than mocks. `pm_test_helpers.py` (534 lines) earns its size (tmux-server isolation encodes a real incident that once destroyed an operator's live sessions). Cuts worth taking: fold two per-command token tests into the existing every-mutating-command case list; drop one subsumed timeout test (~60 test LOC total).

### orchestrator (5,515 Python LOC incl. tests) — fit for purpose; carries the repo's main dead weight

The contract machinery is real and sound: schema-v3 validation is coherent and fail-closed, five-CLI support is genuine (every composed flag verified against the installed harnesses: claude 2.1.226, codex 0.146.0, copilot 1.0.78, opencode 1.18.12, qwen 0.21.3), continuation lineage is properly enforced, process lifecycle handling is careful (identity-checked signalling, atomic writes, SIGTERM→SIGKILL escalation). The read-write "authorized surface" question has a clean answer: **sound, not theater, because it never claims to be mechanism** — every layer correctly attributes prompt-enforced boundaries as prompt-enforced (vision §10 done right), while what is mechanical (codex `workspace-write` sandbox, pinned `approval_policy="never"`) is correctly claimed and tested.

Findings:

- **P2 — `references/claude.md` collides with `CLAUDE.md`.** On macOS's case-insensitive filesystem the two names resolve to the same inode (confirmed), and Claude Code loads the file as directory-level overriding instructions — observed live in one Claude Code session during this review (the injection itself is a harness behavior a read-only re-check could not reproduce without launching a session; the filesystem precondition is beyond doubt). Fix: rename to `claude-code.md`, update the single repository link (`orchestrator/SKILL.md:52`). Zero risk.
- **P2 — launch-before-persistence window in `start_tracked_delegate`** *(new; surfaced by the independent report review, verified in source)*. The detached wrapper is started at `delegate_jobs.py:660-667`; the first durable manifest entry is written at `:677-705` with no exception cleanup between. A `save_manifest` failure leaves a running wrapper (and child) that `status`/`cancel` cannot address, with the pid lost from the error path. Rated P2: narrow I/O-failure trigger, loud CLI error, process still visible to the OS. Fix: wrap post-`Popen` persistence in identity-aware cleanup, plus an injected-failure test.
- **P2 — ~300 LOC of mechanism with no downstream consumer** *(each item independently verified by two readers; the Codex pass additionally grepped every skill, test, doc, and CI file for consumers)*:
  - Run index: `index.json` is written under lock on every init/launch/exit and read only by its own sync path — no command, no other skill, nothing downstream consumes it (~75 LOC).
  - `--depends-on`: validates and persists dependency metadata, but never gates a launch — status/activity merely print warnings; undocumented in SKILL.md/references/templates, unused by tests (~55 LOC). **Note: this is a public CLI flag — removing it is an intentional interface deletion needing a changelog entry, not a silent cleanup.**
  - `codex_prompt_from_command` (`delegate_sessions.py:76-171`): ~95 lines parsing command shapes `compose_delegate_command` can never emit (the prompt is always argv index 2; continuations short-circuit on the captured parent id); reducible to ~8 lines (~85 LOC).
  - Unreachable Claude session-correlation heuristic (`delegate_sessions.py:417-454` + private helpers): first launches always set `--session-id`, so the exact-id branch always returns first (~70 LOC).
  - The `-rN` suffix check is validated twice (`delegate_contract.py:381-389`, re-checked at `delegate_jobs.py:849-860`); the self-parent rule is *not* duplicated — the duplication is the suffix check only (~10 LOC).
  - `_LIBRARY_WRAPPERS` dict written and never read, with a comment claiming consumers that do not exist (~7 LOC).
- **P3 (selection):** qwen is launched with `--yolo`, which no longer appears in `qwen --help` (neither does `--approval-mode`, but upstream's bundled docs name `--approval-mode=yolo` as the preferred unified form and the equivalence is documented) — switch before a release drops the alias silently, since qwen's non-strict argv parsing would make the breakage invisible; double-issue emission on missing fields (`delegate_contract.py:162-169` + `:399-438`); six poll-until-deadline loops share a skeleton — consolidation is plausible but each loop has distinct stores, match conditions, and ambiguity rules, so only take it if the abstraction doesn't obscure those fail-closed differences.
- **Over-testing: the headline ratio is misleading.** `test_delegate_contract.py` (1,503 lines) is misnamed — roughly half tests `delegate_jobs.py`, so the real ratio is 2,141 test lines against 3,374 script lines, proportionate for a fail-closed contract. The composition tests protect genuinely distinct per-harness behaviors (repo flags, access modes, generated ids, ordering, resume-only flag subsets, approval pinning, qwen headless) — **consolidate them into a table-driven form rather than dropping any**; the safe cut is the duplicated golden-argv assertions plus the jobs-side duplicate of contract-side lineage tests and the mocks freed by the dead-mechanism removals. Total ~150–250 test LOC, contingent on preserving each distinct behavior. Tests: 90/90 pass.

### code-health (2,441 Python LOC incl. tests) — fit for purpose; field test exact on every spot-check

The skill delivers precisely its claim: evidence, never verdicts. Field test on mimic (~1,300 files): full `analyze --all --history --json` in 7.9 s; churn count for `src/core/main.c` (23 revisions) matched independent `git log` exactly; the top duplication candidate was verified line-for-line (two byte-identical 815-line test files); the include graph's 651-of-794 basename-guessed edges are disclosed exactly as the methodology promises. On SAGE26: reported cyclomatic complexity of 116 for `model_starformation_and_feedback.c:79` matched an independent lizard run exactly, as did a differential delta of −17 against the base blob. Empty differential correctly exits 3. The complexity that exists — raw-string masking, run-merged duplication, coverage voiding, bitmask reachability — each maps to a tested failure, a methodology paragraph, or a measured cost. **Nothing qualifies as dead code or unjustified over-engineering.**

Findings (no P1):

- **P2 — text-mode `analyze` omits the evidence the skill exists to produce.** `output_text` (`health.py:1254-1275`) prints only coverage rows plus a candidate *count*, while SKILL.md's workflow steps 4–6 require distributions, deltas, and candidate details — impossible from text output, and the documented command examples omit `--json`. Minimal fix: document `--json` as the operative form (or print candidate lines in text, ~8 LOC).
- **P3:** binary data files (fonts, `.doctree`) pollute `unreadable_files` (57 rows on SAGE26, burying real problems). The sound fix is **not** to skip data files from composition (that would change `totals_by_category` semantics) but to stop recording their expected decode failures in `unreadable_files` — or tag them with a `binary_data` reason. Also: whole-file duplicates — the dominant clone shape in both field repos — surface as truncated block rows (a disclosed limit; ~10 LOC would close it); a cosmetic double parser construction in `main()` (`health.py:1415-1418`).
- **Over-testing: largely absent.** 92 tests, 11 s, nearly all pin a disclosed contract or named regression. Concrete merges: ~30–50 LOC of verbatim-duplicated assertions across four test pairs. Keep the 200-iteration reachability sweep — it is the only practical check on the SCC bookkeeping and costs under a second.

### lint (1,925 Python LOC + 480 docs/config) — fit for purpose, with one field-verified P1

The differential engine is done right: changed files linted at head and at the base ref in a throwaway worktree, findings compared by line-insensitive signature with count comparison, renames remapped so pre-existing debt cannot masquerade as new. The core honesty promise **holds under test**: with all linters stripped from PATH, every language reported `UNAVAILABLE` with named binaries and install commands, exit 3 — never a pass. All five spot-checked findings on the field repos were genuine defects (including a real `fscanf` crash vector and a `%zd`-for-`size_t` UB case in mimic). 71/71 tests pass.

- **P1 — project markdownlint config globs blow the differential scope open.** `scripts/lint.py:443` passes only the selected files to `markdownlint-cli2`, but cli2 *appends* a project config's `"globs"` to CLI arguments (confirmed in the installed cli2 source, `markdownlint-cli2.mjs:427-430`). On mimic (`.markdownlint-cli2.jsonc:48`: `"globs": ["**/*.md"]`) the head-side run linted the entire tree including `.git/pm/**` artifacts; the base worktree lacks those paths, so out-of-scope findings were reported as introduced — the field run measured 132 of them against a real delta of one typo, exit 1 on a clean change. This breaks the skill's core contract on exactly the configured-project case the design serves. Fix: add `--no-globs` to the build argv (verified: suppresses config globs, still honors rule config and ignores) + one regression test. *(Mechanism verified by three independent readers; the specific field numbers come from the live run.)*
- **P2:** README self-contradiction on the finding signature (`README.md:159` describes an abandoned digit-normalization design; `:170` and the code say verbatim message); stale hardcoded test count ("38 tests" vs 71).
- **P3:** the "misspellings in identifiers" claim overstates — codespell tokenizes on word characters including underscores, so snake_case identifier typos are missed (behavioral inference from codespell's tokenizer; soften the wording); a no-op conditional in `package_managers()` (`lint.py:815-820`); `_needs_clang_format_config` checks the repo root plus five ancestors *above* it (`lint.py:304-316`) — mirrors clang-format's own lookup, but a stray `~/.clang-format` silently enabling checks for every repo beneath it deserves at least a comment.
- **Over-testing: not present.** ~19 test LOC of justified cuts (a source-grepping compliance test, a four-constants-are-distinct test, three one-assert folds). The FakeTool orchestration tests cover exactly the P1-class paths and must stay. Net LOC for this skill is roughly flat: ~19 cut, ~10 added for the fix.

### The eight small skills + repo docs — no P1s; duplication is the only defect class

Every cross-reference resolves; every contract shape matches its consumer; the launcher single-sourcing rule genuinely holds (both Mode A launchers only in `implementation-plan`, Mode B launcher only in `project-manager`, handoff derives rather than restates). Per-skill: **drift-audit, code-simplifier, report, commit, scoped-implementation** are right-sized — the tightest files in the repo. **code-review** is sound but states its don't-skim rule three times (~15 lines). **handoff** works but carries a 56-line worked example and a section that restates its own template (~35–50 lines trimmable). **implementation-plan** mostly earns its 217 lines, but "Machine-Consumed Fields" and "Execution Modes" state the same three facts twice each — exactly the revision-drift trap the skill itself warns plans about (~12–15 lines).

One-source-of-truth findings: contract-defect handling is a near-verbatim duplicate across **two** skills (`drift-audit:48-52` and `code-review:53-57`; implementation-plan's related text is preventive planner guidance serving a different purpose, not a third copy). Any consolidation must respect atomic usefulness — each skill needs enough local instruction to classify a malformed contract on its own — so the realistic move is shortening one copy and adding a source-of-truth map entry, not extraction to a shared home. "Reviewers never commit" appears in five places; the Vision itself defines the Reviewer role, so the copies in atomic skills (e.g. `commit:13`) are defensible standalone safety lines rather than leakage — leave them unless a rewording lands anyway. The README glossary enumerates all eight floor facts that the map homes in PM's SKILL.md.

Doc accuracy: README is accurate end to end (skill table matches the directory 12/12). CHANGELOG is accurate but essayistic (~700-word entries — where the repo's prose budget actually goes; observation, not a defect). **CONTRIBUTING's test section is two generations stale** — lists three suites, CI runs four ("both suites" predates lint and code-health). `ci.yml` mechanics are careful and test what matters; its comments cite an archived "Blueprint §6" and a dead `docs/mode-b-lite` grep exclusion.

## Part 2 — Workflow map and interlock evaluation

The skills compose into one chain used at three rungs, differing only in who holds the gates:

```text
implementation-plan ──(frozen slices + launcher)──▶ scoped-implementation
        │                                                  │ (step 4: lint --base <start>)
        │                                                  ▼
        │                                        drift-audit (authorization gate)
        │                                                  │
        │                              [optional: code-health, structural changes]
        │                                                  ▼
        │                                          code-review (quality)
        │                                                  ▼
        │              [optional: code-simplifier]      commit (lint again pre-staging)
        │                                                  ▼
        └───────────── handoff (Mode A boundaries) ◀── report (explicit request only)

Mode A: the Developer session walks this chain itself (checkpointed or autonomous);
        orchestrator supplies read-only Reviewer delegation for the audit/review steps.
Mode B: project-manager holds the gates from outside — Developer commits FIRST, then
        PM runs the floor, reruns lint itself, optionally runs differential code-health,
        and commissions drift-audit + code-review against the committed diff.
```

**What interlocks well** (verified by tracing both sides of each handshake):

- The seven-section slice receipt is consumed consistently by `scoped-implementation` (preconditions), `drift-audit` (required inputs), `handoff` (Frozen Contract section), and PM's parser (`check-plan` + floor fact 5). No shape mismatch anywhere.
- The drift-audit → code-review handshake is clean, including code-review's handling of the PM-checked-authorization case — the two skills were clearly written against each other.
- The lint integration is the model for how the one-source principle should look: commit, scoped-implementation, implementation-plan's validation template, code-review's deferral, and PM's independent rerun each describe the same contract from their own seat without restating lint's rules.
- The Mode B commit-first reversal is documented identically in README, PM SKILL.md, and implementation-plan's launcher guidance.
- Machine-consumed plan fields (`Approval needed…: no` exact-match, `Independent audit required:`, batch semantics) bind consistently between implementation-plan's declaration and PM's parser.

**Interlock gaps** (the workflow-level findings):

1. **P2 — "Reviewer" vs "delegate" terminology drift.** Orchestrator retired the Reviewer role for two-mode delegates, but `scoped-implementation`, both Mode A launchers, `commit`, `report`, and `handoff` still instruct in "Reviewer" terms. The bridging synonym lives only in the README glossary — which atomic, standalone skills cannot assume was read. Not a contradiction (Reviewer ⊆ read-only delegate; the Vision itself defines the Reviewer role), but a fresh session given only two skills must infer the mapping. Cheapest coherent fix: add the one-line synonym to `orchestrator/SKILL.md`.
2. **P3 — one verdict label, two meanings.** drift-audit's `PASS WITH RISKS` ("contract or evidence incomplete; human should review") and code-review's ("only P2/P3 issues or validation gaps remain") differ semantically. Each skill defines its vocabulary in situ so nothing breaks, but a PM consuming both reports sees the same label meaning different things. Worth a conscious decision.
3. **Deployment note (not a repo defect):** every skill is symlinked into `~/.claude/skills` **except `code-health`** — the newest large skill is not currently installed in the harness that would trigger it. Worth adding the symlink.

Overall workflow verdict: **the interlock design is the repo's strongest asset.** The contract shapes genuinely act as the shared vocabulary the vision claims, and the three-rung degradation (standalone → Mode A → Mode B) is real — each skill was verified to stand alone without the infrastructure above it.

## Part 3 — Consolidated recommendations, ordered

**Fix now (correctness):**

1. `lint.py:443` — add `--no-globs` to the markdownlint argv + regression test. *(P1)*
2. Rename `skills/orchestrator/references/claude.md` → `claude-code.md`; update the one link. *(P2, live side effect)*
3. `pm_lib/slice_ops.py:503-511` — add `validation.md` to attempt rotation. *(P2)*
4. `hooks/pm-poll-guard.py` — respect `offset`/`limit` in the read stamp. *(P2)*
5. Close both launch-before-persistence windows: extend `start_slice`'s cleanup guard through `save_state`; wrap orchestrator's post-`Popen` manifest write in identity-aware cleanup. Injected-failure tests for both. *(P2 ×2)*
6. `delegate_contract.py:766-778` — qwen `--yolo` → `--approval-mode yolo`. *(P3, silent-breakage risk)*

**Simplify (the net-negative-LOC work, in value order):**

7. Orchestrator no-consumer sweep (~300 LOC + freed mocks): run index, codex command parser, correlation heuristic, duplicated suffix check, `_LIBRARY_WRAPPERS`. Treat `--depends-on` separately as a deliberate interface deletion with a changelog entry (~55 LOC of the total).
8. Test consolidation across orchestrator + PM + code-health + lint (~250–380 test LOC, itemised above; orchestrator's composition tests become table-driven, preserving every distinct per-harness behavior).
9. PM hygiene: `commit_required`, `wake_at`, dead floor branches, empty-decision truthiness (`--accept/--steer/--stop ""`), scavenge double-load, stale docstring (~60–80 LOC).
10. Small-skill prose de-dup: implementation-plan mode rules, code-review self-restatement, handoff example/capture-list; shorten one of the two contract-defect copies + add a source-map entry (~70–110 lines).

**Docs (accuracy):**

11. CONTRIBUTING test section (add lint suite, fix "both suites"); lint README signature line + test count; code-health SKILL.md `--json` note; ci.yml stale Blueprint pointers; README glossary floor-facts entry → count + pointer.

**Decide once (no code change required):**

12. Reviewer/delegate synonym line; `PASS WITH RISKS` semantics; whether CHANGELOG entries stay essay-length; symlink `code-health` into `~/.claude/skills`.

## Part 4 — What was checked and found fine

Beyond the per-skill lists above, the review specifically probed and cleared: PM floor bypass routes, token leakage into Developer/Reviewer sessions, review staleness across tree changes, attempt-budget evasion; orchestrator's five-harness command composition against installed CLIs, schema-v3 fail-closed validation, continuation lineage, skill-bundle escape; code-health's differential attribution against independent base-blob measurements, churn against raw `git log`, exit-code contract; lint's coverage honesty under a stripped PATH, rename remapping, exit-code precedence, worktree hygiene on the field repos (both restored to their pre-test state); all cross-skill references and contract shapes; README accuracy; CI mechanics (parallel failure collection, grep exit-code discipline).

**Test status at review time:** orchestrator 90/90; lint 71/71; code-health 92/92; project-manager 374/374 on an independent rerun (one flaky tmux-timing failure on an earlier run, classified not-real).

## Independent verification of this report

Per instruction, the draft was reviewed by a **read-only Codex delegate (gpt-5.6-sol, high effort)** launched through `delegate_jobs.py` (label `01-codex-review-repo-review-report`, run dir under `.orchestrator/runs/`, codex `workspace-write`-free read-only sandbox as the enforcement boundary). Its report verified every material claim against source, grepped the whole active tree before confirming any dead-code claim, and rendered **"UNRELIABLE without corrections"** on the draft. Disposition of its findings:

- **Accepted and folded in:** two omitted launch-before-persistence defects (added above — the review's highest-value contribution); PM's size corrected (12,214 Python LOC, not ~9,900); the orchestrator dead-mechanism subtotal corrected (~300 LOC — the draft's 340–350 double-counted the optional poll consolidation); category-table arithmetic reconciled; `lint.py:443` (not 444); one `claude.md` link (not two); index.json reworded to "no downstream consumer" (it is read by its own sync path); continuation duplication narrowed to the `-rN` suffix check; qwen `--help` wording corrected; clang-format ancestor count corrected; empty-string fallthrough extended to all three finalize flags; contract-defect duplication narrowed from three skills to two; the `commit:13` cut withdrawn (the Vision defines Reviewer as a repo-wide role; the line is standalone safety, not leakage); the code-health binary-data fix restated soundly (keep data files in composition; stop logging their expected decode failures); the golden-argv test cut restated as table-driven consolidation preserving all behaviors; `--depends-on` removal reclassified as an intentional interface deletion.
- **Accepted with a severity disagreement, recorded:** the delegate rated both new lifecycle defects P1. This report rates them P2 after checking the mitigations: both failures are loud at the CLI, both triggers are narrow I/O faults in a single window, and PM ships `stop --scavenge` — a state-independent tmux sweep built for exactly the stranded-session case. The disagreement is about recoverability, not about whether the defects are real.
- **Noted, no change:** items the delegate marked UNVERIFIABLE are the field-run results (mimic/SAGE26 numbers, suite pass rates, the live injection observation) — those were executed and verified by the review sessions that produced them, and the mechanisms behind each were confirmed in source by the delegate itself.

Delegate effectiveness: high — it confirmed ~85% of the report's claims independently, refuted six wording/precision errors, corrected the metrics, and contributed the two most valuable new findings of the whole exercise. Its evidence was sufficient and cited `path:line` throughout.

## Outcome — measured after implementation

Every recommendation above was implemented the same day (five parallel implementation sessions, one per area, each diff reviewed by the supervising session; plus the unborn-branch `write_git_diff` fix from the PM findings, applied directly). Measured result, same counting rules as the "before" table:

| Measure | Before | Projected | **Actual after** |
|---|---|---|---|
| Total tracked lines | 25,330 | 24,400–24,630 | **25,108 (−222)** |
| Production Python | 11,362 | −370 to −430 | **11,165 (−197)** |
| Test Python | 10,733 | −250 to −390 | **10,750 (+17)** |
| Markdown | 3,089 | −80 to −110 | **3,047 (−42)** |

The actual net (−222) landed well short of the projection (−700 to −930), and the reasons are worth recording honestly rather than hidden in the ranges:

- **The projection counted only removals.** The seven correctness fixes — six from this review plus two contributed by the Codex verification pass — added ~270 lines of regression tests and rationale comments the projection never budgeted. Test LOC ended net *positive*: ~250 lines of redundant tests were cut, and slightly more genuine coverage was added for real defects (injected persistence failures, the markdownlint globs regression, attempt-rotation of `validation.md`, blank-decision refusals, the unborn-branch diff).
- **Implementers were rightly conservative where a projected cut would have cost coverage or clarity.** The orchestrator's four composition tests became one table-driven test asserting the *full* argv for all five harnesses — stronger, not just shorter — costing part of the projected test savings. The prose merges preserved every fact, so sections whose duplication carried surrounding context saved less than estimated (implementation-plan −2 vs. the projected −12).
- The largest single item held: the orchestrator's no-consumer sweep landed at **−274 net LOC** for that skill, and the dead PM mechanism (`commit_required`, `wake_at`, dead floor branches) went with it.

Verification at completion: lint 68/68, code-health 90/90, orchestrator 89/89, project-manager 374/374, CI-style compile checks pass, and the repo's own `lint` skill run differentially over the entire change reported zero new findings in the changed code (its one catch was an unlanguaged fence in this report — fixed). The deliberate interface deletion (`--depends-on`) and every fix are recorded in `CHANGELOG.md`.

---

The one-sentence verdict the vision asks this review to be honest about: **this repo does not need saving — it needs seven small correctness fixes, one dead-code sweep in orchestrator, and a modest de-duplication pass in the prose.**
