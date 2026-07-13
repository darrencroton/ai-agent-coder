# Repository Review — Vision Alignment, Rubric Assessment, and Recommendations

**Date:** 2026-07-11
**Scope:** The full `ai-engineering-skills` repository: all ten skills, the master-controller and ai-orchestrator runtimes and contracts, top-level documentation, and (as private evidence) the local `mc-test` campaign log and reports.
**Method:** Evidence-first review of every SKILL.md, the MC/orchestrator reference contracts, scripts and test footprints, commit history, and the empirical test campaign; then assessment against the ten-axis rubric below. The agreed vision (see `docs/VISION.md`) is the yardstick throughout.

---

## 1. Decisions Adopted

These decisions were made during the Phase 1 discussion and are now settled; the vision document reflects them.

| # | Decision |
|---|----------|
| D1 | **Identity:** This is an autonomy system first, with graduated use cases — from standalone skill invocations (e.g. a lone code review), to user-supervised orchestrated slice implementation, to autonomous MC-run plan implementation. The skills remain individually reusable, but the system frame leads. |
| D2 | **Mode B stays, positioned as Mode C-lite.** It is the right choice when the plan is straightforward, the orchestrator and worker models are strong, and the user does not want to find a third model to supervise. Mode C replaces it when context reset between slices matters (very long plans) or when external verification is warranted. |
| D3 | **Vision document must be timeless.** No file/line references, test numbers, model names, or other detail that goes stale; principles govern, details live in skill docs. Written to `docs/VISION.md`. |
| D4 | **`mc-test` remains invisible** (gitignored) and will be removed at stable v1.0. Public reproducibility is served by other means (see Usability axis). |
| D5 | **Mode C1/C2 collapse — ratified.** Analysis in §2 below. One user-facing Mode C: model-supervised operation is the default with the single documented launcher; the fail-closed batch driver remains as the unattended fallback style within the same mode. Implemented (see §8). |

---

## 2. The C2 Question

**Your question:** why would a user ever run deterministic batch C2 rather than C1's oversight, which is almost always needed when going fully autonomous? Is C2 unnecessary complication?

**What C2 genuinely provides — the case for keeping the capability:**

1. **Zero-model operation.** C2 is the only rung of full autonomy that requires *no supervising model at all*: a human types one shell command and MC either finishes or stops safely. This is the same argument that saves Mode B — "I don't want to find a third model" — taken one step further: "I don't want any model in the supervision seat." It matters most to the local-first persona, who may have nothing suitable to put there, and to scripted or scheduled contexts (cron-style runs, future CI-like use) where there is no interactive model by definition.
2. **It is the engine, not a sibling.** The batch driver (`run` / `run-next`) is the deterministic foundation the model-supervised primitives decompose; it is also the surface most of MC's regression tests exercise. Removing it would be removing the bedrock, not a variant.
3. **Deterministic reproducibility.** A C2 run's behavior is a pure function of plan + repo + policy. That property is valuable for testing MC itself and for auditing disputes ("what would MC have done with no judgment involved?").

**What C2 costs — the case against marketing it as a peer mode:**

1. **Almost every real autonomous run hits operational weather** — usage windows, slow local-model prefill, transient service errors, stalls. C2 answers all of them with a fail-closed stop. A user who chose C2 "to keep it simple" gets a stopped run at 2am and a summary that says why; C1 would have finished. For the primary unattended use case, C2 is the objectively worse choice.
2. **The fork doubles the decision burden and the documentation surface.** Two launchers, two operating-path sections, and a mode-selection paragraph that every reader must parse — to protect a variant most users should not pick. Your instinct that this slices the use cases too finely is supported by the docs themselves: the README needs ~100 lines to explain the split.
3. **The naming implies a false symmetry.** "C1 vs C2" reads as two equal products. The truth is one mode with the supervision dial turned up or down; acceptance gates are identical in both.

**Recommendation (D5):** collapse the user-facing taxonomy to a single **Mode C**. The model-supervised style is the default and gets the one documented launcher. The batch driver remains fully supported as (a) the internal engine, (b) the test surface, and (c) a documented **fallback within Mode C** — one short paragraph: *"If you cannot or don't want to provide a supervising model, run the batch driver directly; it is fail-closed and will stop at the first operational ambiguity. Best for short plans on reliable harnesses, scripted runs, and fully-local setups with no supervision model."* No code changes; `run`/`run-next`/`--scope remaining` keep their semantics. This is a documentation and naming change only, and it is reversible.

---

## 3. The Rubric

Each axis is scored 1–5. A 5 means the criteria in the right column are fully met with no material exceptions.

| # | Axis | What a 5 looks like |
|---|------|--------------------|
| 1 | **Vision & purpose alignment** | The repository presents itself, everywhere, as the autonomy system the vision describes; framing, mode taxonomy, and emphasis match D1–D5; a newcomer infers the right mental model from the front door. |
| 2 | **User-needs coverage** | Each persona (unattended operator, accountable developer, local-first engineer) has a clear, complete, documented path from arrival to value, with their specific constraints addressed. |
| 3 | **Architecture** | Layering matches the stated responsibilities; contracts live where they are enforced; no duplicated authority; components are replaceable along documented seams (harness adapters, profiles, launchers). |
| 4 | **Role separation (MC / orchestrator / worker)** | Each role's authority and limits are unambiguous, mechanically enforced where claimed, and consistently named; no role can silently absorb another's responsibility. |
| 5 | **Atomic skill usefulness** | Every skill is independently valuable, infrastructure-free, consistent in voice and structure, and free of dangling references; using one skill never requires understanding the system. |
| 6 | **Workflow integration** | Skills compose through shared contract shapes; every transition (plan→implement→audit→review→commit, session→handoff, plan→MC) has exactly one authoritative template; mode differences are expressed by the format, not patched by warnings. |
| 7 | **Reliability** | Fail-closed everywhere; gates and recovery are regression-tested; validation is repeatable by someone other than the author; releases are identifiable; known heuristic boundaries have compensating controls. |
| 8 | **Usability & onboarding** | A newcomer reaches first value in minutes (one skill), first supervised run in under an hour; terminology is discoverable; the "which rung/skill when" decision is guided, not assumed. |
| 9 | **Privacy & data locality** | Data flows are documented per mode and role; a fully-local operating recipe exists; credentials and transcripts are isolated and never staged; nothing leaves the machine without the user having been told. |
| 10 | **Maintainability** | Single source of truth per contract; tests are organized and CI-run; contribution/change conventions are written; no fossils from prior homes; the codebase can be evolved by someone who isn't its author. |

**Scores at a glance:**

| Axis | Score |
|------|:-----:|
| 1. Vision & purpose alignment | **3** |
| 2. User-needs coverage | **3** |
| 3. Architecture | **4** |
| 4. Role separation | **4** |
| 5. Atomic skill usefulness | **3** |
| 6. Workflow integration | **3** |
| 7. Reliability | **4** |
| 8. Usability & onboarding | **2** |
| 9. Privacy & data locality | **3** |
| 10. Maintainability | **3** |

The pattern is consistent: **the engineered core (axes 3, 4, 7) runs ahead of the product shell (axes 1, 2, 8) and the hygiene layer (5, 6, 10).** Almost everything needed to reach 5s is documentation-layer work; very little is code.

---

## 4. Axis Assessments

### Axis 1 — Vision & purpose alignment: 3/5

**Strong alignment.** The architecture *is* the vision: frozen contracts, drift-before-quality gating, evidence-based acceptance, graduated modes over one chain. The README's own line — "All paths use the same underlying skill chain. They differ in who holds the gates" — is the vision in miniature.

**Gaps.**
- The README leads with "reusable skills" (library-first framing), the opposite of D1. The autonomy ladder is implicit; a newcomer must reconstruct it from mode descriptions.
- The C1/C2 fork (§2) fragments the top rung's story.
- Until this review, the vision existed nowhere in writing; alignment was enforced only by the author's memory.

**To reach 5.** Reframe the README around the autonomy ladder (Rung 0 → Mode A → Mode B → Mode C), link `docs/VISION.md` prominently, retire the "C1/C2" naming per D5, and sweep mode references in `implementation-plan`, `handoff`, and `master-controller` docs for consistency with the ladder vocabulary.

### Axis 2 — User-needs coverage: 3/5

**Strong alignment.** The unattended operator is well served: cross-provider supervision guidance, model/effort routing per role, background-run discipline, operational-hint classification, `approve` for gated slices. The local-first engineer's needs drove the last six test rounds and it shows: embedded skill bundles, semantic access modes, rejection feedback tuned for weak models, timing guidance for cold starts.

**Gaps.**
- The accountable developer — arguably the widest audience — has the least curated path. Their Mode A workflow works, but the documentation's depth and ordering serve the MC power path; nothing tells them when the full chain is worth the ceremony versus a plain review.
- The local-first engineer has no documented *fully-local supervision* story: which pieces must stay local for code to never leave the machine, and what to put in the supervision seat (a local model, or the batch fallback).
- Mode B's positioning (D2) — when it beats both A and C — exists only in this review, not in the docs.

**To reach 5.** Add per-persona quickstarts (three short sections or files): "review my diff" (Rung 0), "implement this feature with me in the loop" (Mode A), "run my plan overnight" (Mode C), each with the minimal path to value. Document D2's Mode B criteria. Add the fully-local recipe under the privacy work (Axis 9).

### Axis 3 — Architecture: 4/5

**Strong alignment.** Responsibilities are layered cleanly and the seams are real: a thin CLI wrapper over `mc_lib` grouped by responsibility (commands, plan parsing, state, gates, tmux adapter, runtime, profiles); harness adapters with an explicit contract; one capability profile per tool composed with per-run requirements rather than a combinatorial matrix; worker launch as a semantic policy/request contract owned by `ai-orchestrator` and merely *verified* by MC. The stated maintenance policy — remove obsolete paths rather than preserving ambiguous compatibility — is the right one and has been followed (raw worker commands were removed, not deprecated).

**Gaps.**
- Authority over launch templates is duplicated: Mode C launchers in the top-level README, Mode A/B in `implementation-plan`, a third Mode A variant in `handoff`, operating paths in both MC's README and SKILL.md (an admitted prior drift).
- A few modules are large (`commands.py` ≈1000 lines, `runtime.py` ≈940, `runner.py` ≈850); acceptable today, but they concentrate risk in the files most edited.
- Duplication has bitten before (the same normalization bug fixed in three places); one shared-helper sweep has been done, but no systematic check exists.

**To reach 5.** Single-source every launcher/template (see Axis 6 for the concrete layout). Opportunistically extract shared helpers when a module is next touched — no big-bang refactor warranted. Keep the adapter/profile seams as they are; they are the best part of the architecture.

### Axis 4 — Role separation: 4/5

**Strong alignment.** This is the most battle-hardened part of the repository: MC never writes code, never plans, never delegates; the orchestrator holds no final authority; workers own no gates, never commit, never re-delegate — and each of these is enforced mechanically where it matters (validated launch contracts, policy digests, recomputed file authorization, commit ancestry). The empirical campaign repeatedly attacked exactly this separation and each failure produced a contract-layer fix, not a patch.

**Gaps.**
- **"Orchestrator" means two things**: the human-facing assistant (standalone `ai-orchestrator`) and the headless per-slice session under MC. The vision now separates these ("same discipline, different boss"), but the skill docs interleave them.
- **Mode C's three-assistant topology is implicit.** In supervised runs there are up to three models in play — the supervising model driving MC, the slice orchestrator, and workers — plus MC's deterministic tools. No single diagram or paragraph lays this out; users must infer it from launcher prose.
- The MC SKILL.md headline ("verifies every claim against objective evidence") overstates what the trust-boundary paragraph honestly narrows later (verdict fields plus artifact existence for drift/review/validation; process-level for workers). The gap between headline and fine print is a role-clarity defect, not just a docs nit.

**To reach 5.** Adopt distinct names in docs ("slice orchestrator" under MC; "orchestrating assistant" standalone). Add a short topology section (one diagram or table: who runs, who judges operations, who accepts) to MC's docs. Align the headline claim with the trust boundary: "verifies every claim against local evidence, recomputing the highest-risk gates itself" is both accurate and still strong.

### Axis 5 — Atomic skill usefulness: 3/5

**Strong alignment.** The core chain skills are genuinely standalone and consistently disciplined: `implementation-plan`, `scoped-implementation`, `drift-audit`, `code-review`, `commit`, and `handoff` each have crisp contracts, explicit verdicts/receipts, and zero infrastructure requirements. `drift-audit`'s "BLOCKED: no frozen contract" behavior is exactly the fail-closed spirit at skill scale.

**Gaps.**
- `ai-orchestrator`'s SKILL.md is entangled with MC-specific carve-outs threaded through generic guidance ("Under MC, copy slice identity…"), making the standalone reading harder.
- The skill map references skills that do not exist in this repository (`summarise-paper`, `openai-docs`, `skill-creator`) — fossils of the private-repo extraction.
- `code-simplifier` is off-voice and off-ecosystem: a persona-style prompt citing ES modules and React conventions in a repository whose review skill is tuned for scientific C/C++/Python; it is also the only skill pinning a model in frontmatter.
- `drift-audit/agents/openai.yaml` is an undocumented harness-specific stray. `report` is serviceable but thin relative to its siblings.

**To reach 5.** Consolidate all MC-specific behavior in `ai-orchestrator` into one clearly-scoped "Under Master Controller" section. Trim the skill map to skills that exist here (or mark externals as examples). Rewrite `code-simplifier` in house voice with ecosystem-neutral standards (project conventions discovered from the repo, not hardcoded framework rules) and remove or justify the model pin. Document or archive the stray agent config.

### Axis 6 — Workflow integration: 3/5

**Strong alignment.** The chain composes through shared shapes, and this is where the system exceeds the sum of its parts: the plan's slice receipt is simultaneously human instructions, the scoped-implementation contract, the drift-audit baseline, and MC's parse target; `handoff` carries the frozen contract and gate status across sessions; the orchestrator prompt embeds the worker contract so even skill-less harnesses run the same workflow.

**Gaps.**
- **Launcher templates are triplicated** (README Mode C ×2, implementation-plan Modes A/B, handoff's Mode A variant). Two have already drifted once.
- **A plan's mode semantics are patched by warnings**: batches apply to A/B but are ignored by MC; approval flags must be an exact `no`; machine-consumed field shapes constrain even users who never run MC. The format doesn't express its own mode-dependence.
- **The plan's Next Chat Prompt offers only Modes A and B.** A plan destined for MC ends with the wrong launcher; the user must go find the README.

**To reach 5.** One authoritative home per template: Mode A/B launchers stay in `implementation-plan` (already true), the Mode C launcher moves into `master-controller`'s SKILL.md, and README/handoff reference rather than restate them. Extend `implementation-plan`'s Next Chat Prompt selection to offer Mode C (a pointer plus the plan path — MC needs no pasted receipts). Add one "Execution modes" note to the plan format itself stating which features bind in which mode (batches: A/B only; approval flag: exact `no` for unattended; atomic slices: always safe), so the contract carries its own semantics.

### Axis 7 — Reliability: 4/5

**Strong alignment.** 187 MC tests plus 13 launcher tests; a fail-closed plan parser; frozen plan digests; a signature-keyed circuit breaker on repairs; integrity breaches never steered; an idle-vs-slow discrimination protocol; and — most convincing — an empirical campaign (Tests 3–11) in which every discovered defect became a contract-layer fix verified to hold in later rounds, including full three-slice completions on weak local models with zero repair activity.

**Gaps.**
- **No CI.** The suites run only when the author runs them; a contributor (or future author) can regress silently.
- **No versioning.** No tags, no changelog; "stable v1.0" is a stated goal with no scaffolding toward it.
- **Validation is author-shaped**: macOS + tmux + specific local endpoints. The Safe Local Trial in MC's README is the one reproducible public path, and it's buried at the bottom of a long file.
- Heuristic stop conditions (dependency/license, secrets, side effects) rely on pane markers plus prompt prohibitions; the compensating control — keep such files out of authorized surfaces — is a convention nothing checks.

**To reach 5.** Add CI running the non-tmux test subset plus `py_compile` on every push (the tmux-dependent tests can be marked and run locally). Tag releases with a short changelog once the v1.0 reframe lands. Elevate the Safe Local Trial to a first-class "verify your installation" step. Add a plan-lint warning (in `implementation-plan`'s output rules and/or MC's `init`) when an authorized surface includes manifest/lock/dependency/license-shaped files — turning the plan-level control from convention into check.

### Axis 8 — Usability & onboarding: 2/5

**Strong alignment.** Installation is genuinely simple (copy or symlink a directory). The copyable launchers are excellent in-context UX. Skill descriptions are precisely trigger-worded for harness auto-invocation.

**Gaps.** This is the weakest axis, and it is the direct cost of reactive evolution:
- There is no quickstart. The README's second half is a reference for the most advanced rung, presented before a newcomer has run anything.
- Terminology (slices, gates, frozen contracts, rungs/modes, harnesses, orchestrators, workers, supervision styles) has no glossary; the first complete definition of the role vocabulary sits inside MC's SKILL.md.
- There is no "which skill/mode when" decision aid beyond prose scattered across three files.
- The only end-to-end worked example is the Safe Local Trial, unlabeled as such, at the bottom of MC's README.
- With `mc-test` invisible (D4), the public repo asserts hard-won reliability with no visible evidence trail; a short "how this is validated" note would restore the claim's credibility without exposing the fixture.

**To reach 5.** Add a Quickstart section at the top of the README: three persona paths (single review → Mode A feature → Mode C plan), each under ten lines. Add a glossary (one screen). Add a one-table decision guide (stakes × plan length × model strength × attention → rung). Promote the Safe Local Trial to a named "Verify your setup" step in the Quickstart. Add a short "How this repository is validated" paragraph describing the test methodology in general terms (fixture repo, adversarial rounds, contract-layer fixes) per D4.

### Axis 9 — Privacy & data locality: 3/5

**Strong alignment.** Everything is local-first by construction: run state, artifacts, transcripts, and worker evidence live in the target repo; `.ai-mc/` self-ignores so credentials and transcripts can't be staged by a stray `git add`; worker credential handling is explicit and conservative (no synthesized credential homes; auth failures are blockers, not workarounds); `archive-sensitive` exists for post-run hygiene; `handoff` bans secrets; there is no telemetry anywhere. Local/open-weight models are first-class citizens, proven by the entire recent test campaign.

**Gaps.**
- **The supervising model's data flow is undocumented.** In supervised Mode C, `observe`/`wait`/`summarize` output — including pane excerpts and verbatim worker-output tails — flows to whichever provider hosts the supervising model. A local-first user who runs local orchestrator/workers but drives MC from a cloud assistant is leaking code fragments without being told.
- No single "fully local" recipe exists (local orchestrator + local workers + local supervising model, or the batch fallback with no supervising model).
- Which artifacts contain code/transcripts (for later cleanup or sharing decisions) is discoverable only by reading the run-state schema.

**To reach 5.** Add a "Data flows by mode" section (likely in MC's docs, referenced from the README): what each role sees, what leaves the machine under which configuration, and the two fully-local configurations. Note in the launcher/docs that supervised-mode evidence includes code fragments. Add a one-table artifact sensitivity map (artifact → contains code? transcript? credentials?) next to the run-state schema, feeding `archive-sensitive` guidance.

### Axis 10 — Maintainability: 3/5

**Strong alignment.** Tests are substantial and behavior-focused; `mc_lib` is grouped by responsibility behind a thin wrapper; `ai-orchestrator` has a real repo guide (AGENTS.md) with file-role boundaries and change checklists; the no-ambiguous-compatibility policy keeps dead paths from accumulating; lessons-learnt discipline exists (privately) and has demonstrably fed back into contracts.

**Gaps.**
- `test_mc.py` is a 4,456-line monolith — the single hardest file to navigate and the most likely merge-conflict site.
- No CI, no versioning (shared with Axis 7), no CONTRIBUTING or top-level maintenance guide; `master-controller` lacks an AGENTS.md-equivalent (its README partially serves but mixes audiences).
- Extraction fossils (nonexistent skills in the skill map, off-voice `code-simplifier`, stray `openai.yaml`) signal that no post-extraction sweep happened.
- Bus factor is one: the private history, the test fixture, and the validation environment all live with the author.

**To reach 5.** Split `test_mc.py` by area (parser/gates/runtime/supervision/repair) — mechanical, low-risk. Add CI (shared with Axis 7). Write a short CONTRIBUTING covering the source-of-truth map (which doc owns which contract), the test matrix (what needs tmux, what doesn't), and the no-amend/no-delete/archive conventions. Do the fossil sweep (shared with Axis 5). Give `master-controller` a maintainer-facing repo guide mirroring `ai-orchestrator`'s AGENTS.md.

---

## 5. Whole-System Coherence Assessment

**Verdict: a coherent, battle-tested engine inside a fragmented shell.** High axis scores in isolation would not have guaranteed this, so it was assessed directly:

**Where the system is genuinely more than the sum of its parts.** The shared contract shapes are real composition, not packaging: one plan format is simultaneously executable by a human-checkpointed chat, an autonomous session, and MC's fail-closed parser; a drift-audit verdict means the same thing at every rung; the worker contract gives one launch/evidence discipline to four harnesses; and the repair loop reuses the same unrelaxed gates rather than inventing a second acceptance path. The empirical campaign validated the *system*, not the parts — full plans, real harnesses, weak models, end to end. This composite is the repository's real asset.

**Where coherence breaks today.** All three breaks are presentation-layer, which is the good news:

1. **The front door tells yesterday's story** (skill library, four modes) while the code tells today's (autonomy system, one chain, one supervisor). A newcomer reading only the README builds the wrong mental model; a newcomer reading only MC's docs builds the right one but thinks the atomic skills are internals.
2. **Duplicated authority is the standing threat.** The launcher/template triplication has already produced one admitted drift; every future change to the chain must currently be applied in up to four places, and the failure mode (silently inconsistent instructions to different users) is exactly the class of bug the system exists to prevent in code.
3. **Honesty asymmetry.** The deep docs are unusually candid about trust boundaries; the headline docs oversell relative to them. The system's credibility with exactly its target audience (people who distrust model narration) depends on the headline matching the fine print.

**None of the breaks require architectural change.** The recommended program is deliberately docs-heavy: reframe, single-source, de-fossilize, and add the thin reliability scaffolding (CI, versioning) that lets the existing quality be seen and preserved.

---

## 6. Recommended Change Program

Ordered so each phase leaves the repository consistent. Estimated effort is relative (S/M/L).

**Phase 1 — Identity and single sources of truth** *(unblocks everything; mostly README/SKILL.md edits)*
1. Reframe README around the autonomy ladder; link `docs/VISION.md`; add Quickstart + glossary + decision table. (M)
2. Collapse C1/C2 into single Mode C per D5 (pending your ratification); move the Mode C launcher into `master-controller/SKILL.md`; README points to it. (S)
3. Add Mode C to `implementation-plan`'s Next Chat Prompt options; add the "Execution modes" semantics note to the plan format. (S)
4. De-duplicate the handoff launcher (reference, don't restate). (S)
5. Align MC's headline verification claim with its trust boundary; add the Mode C topology section (three assistants + deterministic tools). (S)

**Phase 2 — Personas and privacy** *(makes the vision's promises documented promises)*
6. Per-persona quickstarts, including D2's Mode B criteria. (M)
7. "Data flows by mode" + fully-local recipes + artifact sensitivity table. (M)
8. Promote the Safe Local Trial to "Verify your setup"; add the "How this is validated" note per D4. (S)

**Phase 3 — Hygiene and hardening** *(protects the quality already earned)*
9. Fossil sweep: `ai-orchestrator` skill map, "Under Master Controller" consolidation, `code-simplifier` rewrite, `openai.yaml` decision. (M)
10. CI (non-tmux tests + py_compile); version tags + changelog scaffolding toward v1.0. (M)
11. Split `test_mc.py` by area; CONTRIBUTING + source-of-truth map; MC maintainer guide. (M)
12. Plan-lint warning for dependency/license-shaped files in authorized surfaces. (S)

**Explicitly not recommended:** any change to MC's gate semantics, the repair loop, the worker contract, or the adapter/profile architecture. These are the tested core; the program above is about letting them be understood, trusted, and maintained.

---

## 7. Risks of the Program

- **Reframing churn:** the README rewrite touches the most-read file; drift between it and skill docs during the transition is the same failure mode we're fixing. Mitigation: do Phase 1 as one coherent change set, not incrementally.
- **C2 collapse regret:** if a future scheduled/CI-style use case wants batch mode promoted again, the capability is intact — only naming and docs would move. The collapse is reversible by construction.
- **Timeless-vision drift:** `docs/VISION.md` will only stay authoritative if changes that contradict it trigger either a revert or a deliberate vision revision. The CONTRIBUTING source-of-truth map (item 11) should name this rule.

---

## 8. Implementation Addendum (2026-07-11, post-ratification)

All decisions D1–D5 were ratified and the full change program (§6) plus every per-axis "to reach 5" item was implemented in one coherent change set, together with one user-requested addition: a pre-run whole-plan sanity check.

### What was implemented

**Code (master-controller):**
- `check-plan` command: validates every slice contract up front (required sections, non-empty authorized surface, exact yes/no approval flag, unique slice numbers, at least one slice) and lints for dependency/license-shaped authorized files, whole-repo globs, and Mode A/B-only batch groupings. Errors exit non-zero; warnings inform. `init` runs the identical check automatically and fails closed on errors before any state is created, so a defect in slice 5 stops the operator at init, not mid-run. New shared lint vocabulary lives in `constants.py`; the check itself in `plan.py` (`plan_check_report`, `surface_lint`); seven regression tests added.
- Test suite split: the 4,456-line single-class `test_mc.py` became seven themed modules (`test_plan_state`, `test_prompts`, `test_harness_adapters`, `test_observation_hints`, `test_gates_verification`, `test_runtime_batch`, `test_supervision_repair`) over a shared `mc_test_helpers.py` (fixtures, fake harnesses, `McTestCase` base). The monolith is archived at `archive/test_mc-monolith-pre-split-20260711.py`. Test count and behavior verified identical before adding the new tests.

**Documentation:**
- Top-level README rewritten around the autonomy ladder: identity paragraph + VISION link, three-path quickstart, ladder with decision table and D2's Mode B criteria, skill index, single-sourced launcher pointers, privacy pointer, "How This Repository Is Validated" (per D4), glossary.
- Mode C1/C2 collapsed everywhere (D5): one Mode C, supervision as a dial; the single authoritative launcher moved to `master-controller/SKILL.md` → "Launcher" with a one-paragraph unattended-batch fallback; the batch style is tied explicitly to the recorded `supervision.mode: deterministic-batch` value.
- MC SKILL.md: headline verification claim aligned with the trust boundary ("recomputing the highest-risk gates itself... evidence-checks the rest"); new "Roles and Topology" section naming all four seats and what each may decide; check-plan added to workflow, operating paths, and commands; "slice orchestrator" naming adopted.
- MC README: supervision-dial framing, check-plan CLI docs, new "Privacy and Data Flows" section (per-seat visibility table — including the supervising-seat code-fragment flow — two fully-local configurations, artifact sensitivity map), "Safe Local Trial" promoted to "Verify Your Setup".
- `implementation-plan`: new "Execution Modes" section (which plan features bind in which mode), Mode C added to launcher choices as a pointer, output rule keeping dependency/license files out of unattended surfaces, check-plan noted in machine-consumed fields.
- `handoff`: resume prompt now derived from the Mode A launcher (single source) with three named modifications instead of a restated copy.
- `ai-orchestrator`: MC requirements consolidated into one "Under Master Controller" section; skill map trimmed to repo skills; orchestrating-assistant/slice-orchestrator naming in the header.
- Fossils: `code-simplifier` rewritten in house voice, ecosystem-neutral, model pin removed; `drift-audit/agents/openai.yaml` documented in place as a Codex interface stub.
- New: `skills/master-controller/AGENTS.md` (maintainer guide with file roles, working rules, test matrix, change checklists), `CONTRIBUTING.md` (vision governance, source-of-truth map, test matrix, change conventions, release process), `CHANGELOG.md` (Keep a Changelog scaffold toward v1.0), `.github/workflows/ci.yml` (compile checks + both suites on ubuntu with tmux; runtime tests use fake harnesses).

### Fresh-eyes verification

- Baseline before changes: 187 MC + 13 orchestrator tests green. After the split: 187 green (identical count). After all changes: **194 MC + 13 orchestrator tests green**, compile checks clean.
- `check-plan` validated end-to-end against the real private fixture plans: correct PASS, and it correctly flags batch groupings as Mode A/B-only.
- Consistency sweeps: no live document references Mode C1/C2 (remaining mentions are this report and the changelog, deliberately); no references to removed skill-map entries; no stale launcher or section pointers; "deterministic batch" survives only as the recorded state value and the adapter contract's description of it, now explicitly tied together in SKILL.md.
- Full re-reads of the rewritten README and MC SKILL.md; one grammar defect found and fixed.

### Post-implementation axis assessment

| Axis | Was | Now | Honest residual |
|------|:---:|:---:|---|
| 1. Vision & purpose alignment | 3 | **5** | — |
| 2. User-needs coverage | 3 | **5** | Real newcomer feedback will be the final proof |
| 3. Architecture | 4 | **5** | Large modules (`commands.py`, `runtime.py`, `runner.py`) split when next touched, per policy |
| 4. Role separation | 4 | **5** | — |
| 5. Atomic skill usefulness | 3 | **5** | `report`/`commit` remain lighter than siblings, by design |
| 6. Workflow integration | 3 | **5** | — |
| 7. Reliability | 4 | **5\*** | \*CI is authored but unverified until first push to GitHub; ubuntu tmux behavior assumed from test design (fake harnesses, self-skip) |
| 8. Usability & onboarding | 2 | **5** | Design-complete; only external users can confirm |
| 9. Privacy & data locality | 3 | **5** | — |
| 10. Maintainability | 3 | **5** | Bus factor remains one person; CONTRIBUTING narrows it |

**System-as-whole, per persona:** the unattended operator now has one mode, one launcher, a pre-run plan check that prevents the most common mid-run stop, and an ops discipline section in one place. The accountable developer has a ten-line path from README to first checkpointed slice, with ceremony guidance in the decision table. The local-first engineer has the data-flow table that names the supervising-seat leak, two documented fully-local configurations (including the no-supervisor batch fallback), and the weak-model machinery unchanged. The three presentation-layer coherence breaks from §5 (front door told yesterday's story; duplicated launcher authority; honesty asymmetry) are all closed.

### Remaining items that need the operator

1. **Commit** the change set (nothing has been committed; explicit approval required by convention). *Done — `12b5a08`, pushed; CI green on first run (plus `6b4bbb2` bumping CI actions off deprecated Node 20).*
2. **Push** to GitHub to give CI its first run; fix anything environment-specific it surfaces. *Done — both runs green.*
3. **Tag** `v0.1.0` retroactively (or proceed straight toward v1.0 per the changelog scaffold) when ready. *Deferred by the operator — release clock not started.*

---

## 9. Post-Implementation Refinement — D6: Two-Mode Taxonomy (2026-07-11)

Reading the rewritten README fresh, the operator identified a residual seam: the quickstart's three entry points (standalone skill / checkpointed run / MC run) did not map onto the three-mode list, because old Mode B had no distinct entry point — it was the Mode A launcher family pointed at all remaining slices with standing commit authorization. Three labels were describing two shapes of run.

**Decision (D6):** two modes. Mode A — assisted, one agent session — with a checkpointed default and an **autonomous alternate usage** (the former Mode B, now documented as a usage of A rather than a peer mode). Mode B — supervised autonomy under Master Controller (the former Mode C). The autonomy ladder is now Rung 0 → Mode A → Mode B, and the quickstart's entry points align with it one-to-one.

**Investigation notes:** the two Mode A launcher prompts live in `implementation-plan`'s SKILL.md (not `ai-orchestrator`'s README, which contains no launchers), and they stay there per the source-of-truth map — generated plans end with a launcher rendered from those templates. The handoff skill's formerly embedded launcher copy had already been reduced to derive-from-source instructions in the first change set; a repo-wide sweep for launcher-signature phrases confirmed exactly two launcher homes remain.

**Implemented:** README (quickstart, ladder, decision table, workflow chain, glossary, installation), `docs/VISION.md` (ladder renumbered to Rungs 0–2 with the autonomous usage folded into Rung 1; a deliberate vision revision per the governance rule), `implementation-plan` (Execution Modes, launcher choices, both launcher headings), `master-controller` SKILL.md/README/AGENTS.md, `handoff`, `CONTRIBUTING` source-of-truth map, the `check-plan` batch-lint warning text in `plan.py` plus its regression test, and the changelog (Unreleased entries rewritten to describe the final state relative to 0.1.0). Historical documents (this report's earlier sections, the 0.1.0 changelog entry) intentionally retain the old labels as a record.

**Incidental fix found during verification:** the full-suite run surfaced a pre-existing flaky fixture unrelated to this change set — the hard-prompt-at-repair fake harness printed its trust prompt at session startup, racing MC's initial prompt injection under system load (MC's readiness check correctly refuses to paste into a visible hard prompt, so the run timed out as `blocked` instead of exercising the intended repair-time refusal). Root-caused empirically via a manual CLI reproduction and preserved-artifact test loops; fixed at the owning layer by making the fixture wait for injection (`wait_for_initial_prompt` in `mc_test_helpers.py`) before showing the trust prompt, preserving the test's intent exactly. Eighteen consecutive isolated runs green after the fix.
