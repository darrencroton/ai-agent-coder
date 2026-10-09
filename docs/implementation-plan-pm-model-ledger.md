# Implementation Plan — PM model ledger

2026-10-10 · Freezes the slices for [PM_MODEL_LEDGER_PLAN.md](PM_MODEL_LEDGER_PLAN.md) (the high-level plan, "HLP" below). The HLP fixes *what* is built and the rules it must obey (its §5); this plan fixes the code shapes, the order, and the gates. Where this plan and the HLP disagree, this plan wins and the disagreement is listed in *Decisions* below. Reviewed by an independent panel (Codex `gpt-6.1-sol` high, Claude `claude-opus-5-5` high); see *Review record*.

This repository is a development checkout. The live `ai-agent-coder` that the PM toolkit runs from is a separate checkout, pulled only after this plan has landed and been pushed. So the HLP §7 self-hosting concern does not apply here: the installed supervisor never changes under its own run. Each Developer session still edits only this checkout. The same fact is why every risky-surface slice below runs with `Approval needed before implementation: no`: there are no live users of this checkout, every such slice is elevated and gets both mandatory independent reviews, and the human reviews the whole branch before the live checkout pulls it.

## Purpose

Every PM-directed run leaves a deterministic, structured account of how each Developer and Reviewer performed on each slice, and `pm ledger render` turns those accounts into one leaderboard. The work adds: two plan covariates; `lite-2` run state with event `data`, launch identity, stop causes, failed-commission events and a code-metrics block; richer PM judgments with a strict panel order; a judgment gate on every exit from a Developer submission; a per-run ledger file regenerated after every PM command; and the leaderboard renderer. `pm rate` and `model-performance.md` are retired.

## Current-state facts this plan relies on

Verified against the code on 2026-10-10. Paths are relative to `skills/project-manager/`; `pm_lib` is `scripts/pm_lib/`.

- **Plan parsing.** `PlanSlice` is a frozen dataclass (`pm_lib/plan.py:76`) with `sections: dict[str, str]`; `_risk_flag(label)` (`plan.py:109`) regex-searches the `Risk Flags` section text, lowercases, and strips trailing periods. `plan_check_report` (`plan.py:320`) collects per-slice errors as `"{slice_id} ({title}): <msg>"` and `init` refuses on any error (`cli.py:248`). `init_run` builds each signed slice entry at `slice_ops.py:343` (`id, title, status, risk, plan_risk, commit, attempts`). The implementation-plan template (`skills/implementation-plan/SKILL.md:63-79`) renders Acceptance Criteria as `- Inputs:` / `- Outputs:` / `- User-visible behaviour:` / `- Behaviour that must not change:` bullets and has **no checkbox lines**; checkboxes appear only as advice in its "Writing Acceptance Criteria" prose. The Risk Flags template has no `Independent audit required:` line. `references/developer-prompt.md` and `references/reviewer-prompt.md` render the slice's `Acceptance Criteria` and `Risk Flags` sections **verbatim** into both prompts. Any `Risky surfaces touched:` value other than exactly `none` makes `plan_risk` elevated (`plan.py:138-150`).
- **Test plan builder.** `tests/pm_test_helpers.py:253` `render_slice()` is the single plan fixture; its default acceptance text is `It works.` with no checkbox, and its Risk Flags block has no `Difficulty:` line. `make_run` (`pm_test_helpers.py:395`) hand-builds slice entries mirroring `init_run`. Every `init`-driven test goes through `check-plan`. The README trial plan (`README.md:142-167`) has neither field and its walkthrough (`README.md:180-188`) accepts without any judgment.
- **`pm rate`.** Parser `cli.py:194-203`, handler `_run_rate` (`cli.py:784`), `slice_ops.write_model_performance` and `model_performance_path` (`slice_ops.py:99,196`), report section `state.render_run_report` (`state.py:604-613`). Tests: `test_finalize.py:246-249` (asserts `(not recorded)`), `TestRateCommand` (`test_finalize.py:1118-1160`), `test_status_report_recreates_mirror_after_pm_deleted` (`test_finalize.py:1163-1213`, calls `rate`), and one `rate` case in `test_slice_ops.py:203`. Docs: `README.md:42,112,184-187`, `SKILL.md:40,77` (step 5 ends with the only PM-seat sentence on `stop --reason` and `stop --scavenge`, which must survive), `references/run-state.md:13,20`, `references/review-judgments.md:3` and the harvest paragraph at `:112` (names `model-performance.md` as "final qualitative context"), and the whole of `references/model-performance-rubric.md`. No model aliases (`opus`, `sonnet`) appear in any PM doc today; the rubric's `gpt-5.6-sol` examples go with it.
- **Schema and legacy tolerance.** `SCHEMA = "lite-1"` (`state.py:30`); `_validate_shape` refuses any other value and tolerates unknown keys. Legacy code: `judgments.assessment_of` fallback (`judgments.py:218`, callers at `:245,:337,:346,:377` and `state.py:705`); `historical_review_count` (`judgments.py:400`, callers `cli.py:351` and `state._render_reviewer_judgments` at `state.py:734`); the missing-`grants_seen` default in `slice_ops.is_review_fresh` (`slice_ops.py:1252`, docstring `:1242`); the "remains historical/unjudged" messages at `judgments.py:282,521`. No dedicated assertion protects any of these (the report-header tests in `test_state.py:520` render reviews lacking `review_id` through `_render_reviewer_judgments`, so they exercise the counter without asserting on it). `lite-1` is also named in `state.py:196` (the `create_run` docstring), `README.md:195`, `references/run-state.md:1,32,89`, `plan.py:24`, `tests/test_state.py:1`.
- **Events.** `append_event` / `_append_event_unlocked` (`state.py:372-388`) write `{ts, kind, slice, note[, evidence]}` with `sort_keys=True`; `evidence` is omitted when None (`test_finalize.py:714` asserts absence). `_append_event_unlocked` is also called from `state.py:285` and `judgments.py:656`. `ts` is `%Y-%m-%dT%H:%M:%SZ` (one-second resolution). `read_events` (`state.py:391`) returns the list of dict entries and lets a `JSONDecodeError` escape on a corrupt line; an event's index is its position in that list, which is what `origin_event.index` already means. Budget exhaustion writes kind `stop` with `note="attempt budget exhausted"` from both `start_slice` (`slice_ops.py:777`) and `finalize_steer` (`:1536`); `finalize_stop` writes `slice-stop` (`:1686`); `pm stop` writes `stop` (`:1767`) and keeps `current_slice`; the `launch`/`relaunch` event is appended at `:933` after `new_current["developer"]` is built at `:892-901`. A review timeout writes a free-text `review` event (`review.py:515`) then raises; a non-zero reviewer exit writes nothing (`review.py:527`); the post-wait reload is `review.py:509-512`; a successful review is recorded on the slice entry by slice id regardless of `current_slice` (`review.py:533-570`); `_reap_reviewers` (`slice_ops.py:1698`) clears `reviewer_pids` on every reaping path, and accept/`finalize --stop` then set `current_slice = None`.
- **Judgments.** `_parse_input` (`judgments.py:55`) defaults an unavailable record's `assessment` by skill at `:89`; comparisons are `rank_groups` (`:132-157`); `_input_matches` (`:226`) whitelists `rank_groups` among the keys it compares for exact-retry detection; `_context_key` (`:277`) raises `PmError` on a review lacking `origin_event`/`review_context`; `_validate_comparable_panel` (`:295`) skips the context check for unavailable records, so an unavailable comparison may name reviews from different origins. `_scan_latest_developer_origin` (`:491`) returns the latest `launch`/`relaunch`/`steer` for the slice. `_origin_from_current` (`:510`) requires `current_slice.id == slice` and snapshots `head` at judgment time. `record_judgment` refuses overlapping active coverage of the same assessment kind unless superseding (`:342-355`). `_artifact_is_intact` (`:160`) is checked only at record time. `unranked_code_review_ids` (`:393`) lists code reviews not referenced by any active comparison; it is printed by `cli.py:354-360` and `state.py:722-727`. `judgments.py` imports `slice_ops`; `slice_ops` never imports `judgments` (`state.py:684,752` import it lazily).
- **Decision paths.** `finalize_accept` (`slice_ops.py:1373`): budget refusal `:1389` → risk ratchet `:1398` → evidence and `floor` event `:1401` → elevated review check `:1423` (saves state, returns `reviews_stale`) → assessment and entry mutation `:1447` → `current_slice = None` `:1456`. `finalize_steer` (`:1492`): budget refusal `:1502` → session liveness `:1509` → ratchet `:1520` → budget block `:1526-1540` (kills session, saves, writes the `stop` event, returns) → `current["attempts"] = attempts` `:1542`. `finalize_stop` (`:1628`): ratchet `:1648` → evidence `:1652` → stopped entry `:1671` → `current_slice = None` `:1683`; it has no refusal outcome and no budget refusal. `start_slice` (`:683`): `relaunch` decided `:709-716` (leftover `current_slice` whose entry is not accepted/attested, which covers resume after `pm stop`); `--risk` ratchet `:756`; relaunch budget block `:764-780`; `_rotate_prior_attempt` `:785`; stale session kill `:787-790`. `apply_risk_ratchet` (`:252`) returns True iff a valid `--risk` was supplied. All four load state unlocked and save at the end, so a concurrent `review`'s locked write can be lost by a later save (the single-controller rule in `run-state.md`). `finalize --stop` then `start-slice` is a fresh `launch` with attempts 0; `pm stop` then `start-slice` is a `relaunch`. CLI exit codes: `finalize --accept` refusal 1, other refusals and `PmError` 2.
- **CLI plumbing.** Every handler resolves `repo`, `run_dir` and the token itself; `main` (`cli.py:819`) sees only `args`, catches only `IntegrityError` and `PmError`, and any other exception is an uncaught traceback. Run-scoped commands all define `--run` and `--token`; `check-plan` and `init` define neither. `_resolve_token` (`cli.py:214`) returns `--token` or `PM_RUN_TOKEN`. `_load_state_unlocked(run_dir, token)` (`state.py:320`) verifies the token hash and the MAC when a token is given and raises `PmError` on a missing `run.json`. `_advisory_lock` (`state.py:106`) is non-reentrant; `judgments.publish_event` (`judgments.py:637`) is the precedent for lock-held read-then-append. `_atomic_write_bytes` is `state.py:91`. `run_elapsed` (`state.py:434`) parses `ts` inline (`fromisoformat` after `Z`→`+00:00`, skipping naive values); `TestRunElapsed` (`test_state.py:611`) protects it. `status --report` is `cli.py:296-310` and always returns 0.
- **Paths.** `prompts.py:27-30` resolves the skill dir as `Path(__file__).resolve().parents[2]` and the skills root as `parents[3]`; `~/.claude/skills/*` are symlinks into a checkout, and `resolve()` follows them. `PM_TMUX_SOCKET` is read at call time with `os.environ.get(...).strip()` (`sessions.py:165`) and pinned by the test helpers at import (`pm_test_helpers.py:55`). `state["repo"]` is the worktree's own top level, so for a linked worktree its basename is the worktree directory name; `git rev-parse --path-format=absolute --git-common-dir` gives the main checkout's `.git` from either (verified). `socket.gethostname()` is unused today. `.gitignore` does not cover `skills/project-manager/ledger/`. The claude harness profile has an `--effort` flag (`profiles.py:37-44`).
- **Code-health.** `skills/code-health/scripts/health.py` is stdlib-only with no import-time side effects. `language_for` (`:209`), `category_for` (`:214`, six categories: production, test, documentation, configuration, data, other), `line_counts` (`:252`, kinds code/comment/blank), `cyclomatic_for` (`:473`), `python_structure(list[SourceFile])` (`:498`, catches only `SyntaxError` into `parse_errors`), `SourceFile` (`:282`). **Loading it by path with `importlib` fails** (`AttributeError` from `@dataclass` under `from __future__ import annotations`) unless the module is registered in `sys.modules` before `exec_module`; verified. Its `analyze --base REF --json` output carries current-side totals only, not before/after per-file counts, so it cannot supply the code block directly. Lizard is reached only through `build_bundle`. `tests/test_plan.py::TestImportHygiene` scans `pm_lib` for `import`/`from` statements naming non-stdlib modules; a path-based `importlib` load contains none.
- **Tests and CI.** `python3 -m unittest <module>` per module from `tests/`, run in parallel by CI (`.github/workflows/ci.yml`); a new `test_*.py` module is picked up automatically, as is a new `pm_lib/*.py` in the compile step. Lint: `python3 skills/lint/scripts/lint.py check --base <ref>`. CI bans a list of retired tokens repo-wide (the no-baggage greps in `ci.yml`, exempting `CHANGELOG.md` and `archive/`); two of them are the hyphenated forms of "ledger retention" and "reviewer unavailable", so neither hyphenated string may appear in code or docs. No `STYLE-GUIDE.md`; the `style-guide` baseline applies. `init` requires a clean worktree including untracked files, so this plan must be committed before a Mode B `init`.

## Decisions

Each item either resolves something the HLP left to this plan or corrects a HLP assumption the code contradicts. The chosen option is first.

1. **Checkbox criteria are new to the template, not merely "made required".** The implementation-plan skill's Acceptance Criteria template becomes one or more `- [ ] <assertion>` lines; `Inputs:` and `Behaviour that must not change:` may remain as plain context bullets but are not counted. `check-plan` requires at least one checkbox line. *Rejected:* counting every bullet (the four template bullets are framing, not criteria).
2. **The `Difficulty:` and `Recommended Developer:` lines are visible to the Developer and Reviewer**, because both prompts render `Risk Flags` verbatim. Accepted as is: a difficulty label is not confidential, and stripping it would add a parser to two prompt paths for no gain. *Rejected:* a new plan section or prompt-side filtering.
3. **Repo name is recorded once at `init`** as `state["repo_name"]`, the basename of the directory that holds the git common dir. This names the main repository even when PM runs in a linked worktree, which is the normal case. It is a new fact, not a copy. *Rejected:* `Path(state["repo"]).name`, which names the worktree directory.
4. **The ledger directory is gitignored per host folder and per rendered output**, not as a whole: `.gitignore` gains `skills/project-manager/ledger/*/` and `skills/project-manager/ledger/leaderboard.md`. The frozen historical table (`ledger/historical-pre-ledger.md`, HLP W7) is then tracked beside the data it precedes. *Rejected:* ignoring the whole directory and keeping the table elsewhere.
5. **The judgment gate is one helper that raises `PmError`** (exit 2) naming every gap, called from four places after any budget kill and after the risk ratchet, and before any evidence collection or state mutation. On refusal the caller saves state **only if this command applied a `--risk elevated` ratchet** (so the ratchet and its event stay consistent); otherwise it raises without writing, so a refusal can never overwrite a review or judgment a concurrent `review` recorded. *Rejected:* a new outcome kind per command (four CLI branches for one behaviour); saving unconditionally. Consequence: `finalize --accept` refused by the gate exits 2, not 1; README's exit-code line is updated.
6. **`assessment` is required on every unavailable judgment**, drift-audit included, so the gate's predicates are the same for both skills. The compatibility defaults are removed.
7. **`--cause` lives on `finalize` and is validated in the handler**: argparse cannot require it inside the `--accept|--steer|--stop` group, so `_run_finalize` refuses `--stop` without `--cause` and refuses `--cause` with anything other than `--stop`.
8. **Code metrics live in a new `pm_lib/code_metrics.py`** and import `health.py` in-process by path (with the `sys.modules` registration the dataclass loader needs), inside the block's error handling. The `pm_lib/__init__.py` docstring records this as the one deliberate cross-skill import. *Rejected:* invoking `health.py analyze --json` as a subprocess (its output has no before/after per-file figures).
9. **The post-command ledger hook uses a structural predicate, not a command list**: it runs in `cli.main` after the handler returns or raises `PmError` (never after `IntegrityError`) when `args` has a `run` attribute (every run-scoped command; `check-plan`, `init` and `ledger render` have none) and `_resolve_token(args)` yields a token. The hook performs its own MAC-verified load under the run lock; it is silent when the repo or run directory cannot be resolved or there is no `run.json` (that is `stop --scavenge` with state gone, or a handler that already failed for the same reason). **Every other exception, of any type**, prints `pm: ledger: not written: <type>: <message>` to stderr and leaves the handler's exit code unchanged, with one exception: `status --report` returns 2 after a hook failure. `observe` with a token in the environment also triggers a write; it is cheap and uniform.
10. **Panel comparison `order` needs at least two members.** A singleton has nothing to compare and the gate never needs one. *Rejected:* keeping singleton comparisons for continuity with `rank_groups`.
11. **Eight slices, not the HLP's six.** W2 splits into events (Slice 3) and code metrics (Slice 4); W5 splits into rows plus the per-run write (Slice 7) and the renderer (Slice 8). Slice 3 stays whole but is rated hard. *Rejected:* a further split of Slice 3 (another Mode B cycle for two disjoint-looking halves that share `state.py`, `slice_ops.py` and the same docs).
12. **An exhausted window's `decided_at` is the first budget `stop` event in the window**, not the `finalize --stop` PM records afterwards, so wall time measures the Developer's window rather than PM's latency.
13. **A missing origin event is a gate gap.** Real runs always have one; tests that hand-build `current_slice` must append a launch event and a `developer` identity before deciding.
14. **One panel predicate, shared.** Slice 5 adds `judgments.panel_groups(entry, origin_index)`: the intact code-review reviews of that origin that lack an active unavailable rating, grouped by `_context_key`, keeping groups of two or more. `unranked_code_review_ids` is redefined on it (members of such a group not covered by an active `order` or unavailable comparison), and Slice 6's gate reuses it, so `status`, the report and the gate can never disagree about what a panel is. A covering `order` is one whose id set **includes** the group (HLP W4.3 says "includes every member"; equality would break the moment a member later gets an `unavailable` rating).
15. **Window-level identity is judged over scored submissions.** HLP W5 says a window counts at window level "when every submission in the window shares one identity"; this plan reads that as every *scored* submission, consistent with the HLP's own rule that unavailable submissions are left out of every Developer column. A window with no scored submission has no identity and is footnoted with the mixed count.
16. **CHANGELOG** gains its `Removed` entry in Slice 2 and its `Added` entry in Slice 8, so the retirement and the feature each land with their code.
17. **The "Independent audit required" line is not used.** Every slice whose `Risky surfaces touched:` is not `none` is already elevated, which makes both independent reviews mandatory in Mode B; a second flag saying the same thing would be a duplicate fact.

## Conventions binding every slice

- `pm_lib` stays stdlib-only apart from Decision 8. No new dependency anywhere.
- Timestamp parsing anywhere new uses the helper split out of `run_elapsed` in Slice 7; until then Slices 3–6 parse no timestamps.
- Missing data is `null` with a named reason, never 0. Model and effort strings are stored verbatim.
- Tests follow the existing helpers (`PlanTestCase`, `PmTestCase`, `TmuxRunTestCase`, `render_slice`, fake harness scripts). One focused test per behaviour; a pure function is tested on `unittest.TestCase` without a git fixture; no golden-file snapshot of a whole leaderboard; no test that restates an existing one or tests the standard library.
- Tokens banned by CI's no-baggage greps never appear (see *Current-state facts*, Tests and CI).
- Docs are not hard-wrapped. Every doc sentence that describes a removed behaviour is deleted, not left beside the new one.

## Developer model per slice (Mode B)

Each slice's `Recommended Developer:` line names the Claude model and effort PM passes with `start-slice --model <id> --effort <level>`. The rule behind the lines: easy → `claude-sonnet-5-5` at medium effort; moderate → `claude-sonnet-5-5` at high effort; hard → `claude-opus-5-5` at high effort; Slice 8 is the one exception (moderate, but `claude-opus-5-5` at medium effort, because its many statistics must come out exactly as specified).

Reviews: Slices 2–8 declare a risky surface and are therefore elevated, so both `drift-audit` and `code-review` are mandatory on each. Commission `code-review` on Slice 1 as well.

## Implementation Profiles

- Mode B (the intended mode): atomic slices in plan order; PM ignores batches.
- Mode A, frontier implementer: Batch A, then Batch B, then Slices 5–8 individually.
- Mode A, standard implementer: slices individually.

## Slice Batches

- Batch A: Slices 1–2 — both are mechanical and independent; they share `slice_ops.py`, the README and `test_slice_ops.py` but touch different functions and passages in each.
- Batch B: Slices 3–4 — both write new facts into `run.json`/events at existing decision points and share no code.

## Slice 1: Plan covariates — difficulty and checkbox criteria

### Intended Change

- `skills/implementation-plan/SKILL.md`: the Acceptance Criteria template block becomes `- [ ] <one verifiable assertion>` lines (at least one), keeping `- Inputs:` and `- Behaviour that must not change:` as optional context bullets; the Risk Flags template block gains `- Difficulty: easy | moderate | hard` (required) and `- Recommended Developer: <free text>` (optional); the "Writing Acceptance Criteria" prose says checkboxes are required because they are counted; "Machine-Consumed Fields" gains one bullet each for `Difficulty:` (exact one of `easy`, `moderate`, `hard`; rate the slice, never the model, before any attempt, using the three anchors from HLP §4 W1 quoted verbatim) and for checkbox criteria (the count of `- [ ]`/`- [x]` lines in `### Acceptance Criteria` is `criteria_total`; zero is an error); the `check-plan` bullet lists both. The `Recommended Developer:` line is documented as free text PM never parses.
- `pm_lib/plan.py`: `PlanSlice.difficulty -> str | None` reads `self._risk_flag("Difficulty")` and returns it only if in `{"easy", "moderate", "hard"}`; `PlanSlice.criteria_total -> int` counts lines matching `^\s*- \[[ xX]\]` in `sections["Acceptance Criteria"]`. `plan_check_report` appends an error for `difficulty is None` (`"'Difficulty:' must be exactly one of easy, moderate, hard"`) and for `criteria_total == 0` (`"'Acceptance Criteria' must contain at least one '- [ ]' checkbox criterion"`), following the approval-error pattern at `plan.py:392`.
- `pm_lib/slice_ops.py` `init_run`: each signed slice entry gains `"difficulty": plan_slice.difficulty` and `"criteria_total": plan_slice.criteria_total`, beside `plan_risk`.
- `tests/pm_test_helpers.py`: `render_slice`'s default `acceptance` becomes `- [ ] It works.` and its Risk Flags block gains `- Difficulty: {difficulty}.` with a new `difficulty: str = "moderate"` parameter; `make_run`'s hand-built slice entries gain `difficulty` and `criteria_total` so they keep mirroring `init_run`.
- `README.md` trial plan: add `- [ ] hello.txt contains the single word hello` and `- Difficulty: easy`.

### Acceptance Criteria

- [ ] `check-plan` reports an error naming the slice when `Difficulty:` is absent, blank, or not exactly one of `easy`, `moderate`, `hard` (after the existing lowercasing and trailing-period strip), and `init` refuses such a plan.
- [ ] `check-plan` reports an error naming the slice when `### Acceptance Criteria` contains no `- [ ]` or `- [x]` line, and `init` refuses such a plan.
- [ ] `init` stores `difficulty` (string) and `criteria_total` (int ≥ 1) on every signed slice entry, attested slices included.
- [ ] The implementation-plan skill's template, "Writing Acceptance Criteria" prose and "Machine-Consumed Fields" list document both fields exactly as the parser reads them, with the three difficulty anchors.
- [ ] `render_slice`, `make_run` and the README trial plan carry both fields, and every existing test passes.
- [ ] No prompt template changes: the lines reach Developer and Reviewer prompts verbatim (Decision 2).
- Behaviour that must not change: every other `check-plan` error and warning; `plan_risk` derivation; `_risk_flag` semantics.

### Authorized Surface

- Files allowed to change:
  - `skills/implementation-plan/SKILL.md`
  - `skills/project-manager/scripts/pm_lib/plan.py`
  - `skills/project-manager/scripts/pm_lib/slice_ops.py`
  - `skills/project-manager/README.md`
  - `skills/project-manager/tests/pm_test_helpers.py`
  - `skills/project-manager/tests/test_plan.py`
  - `skills/project-manager/tests/test_slice_ops.py`
- Functions/classes/components allowed to change: `PlanSlice` (two new properties), `plan_check_report` (two new errors), `init_run` (two new entry fields), `render_slice` (one new parameter and two default lines), `make_run` (two entry fields), the implementation-plan template and the two documentation sections named above, the README trial plan.
- Tests allowed or expected to change: `test_plan.py` (new error tests), `test_slice_ops.py` (init stores both fields), `pm_test_helpers.py`.

### Explicit Non-Goals

- No `_validate_shape` check for `difficulty` (validation stays tolerant).
- No change to prompts, `eligibility()`, or `plan_risk`.
- No per-slice recommended-model parsing.

### Risk Flags

- Risky surfaces touched: none
- Approval needed before implementation: no
- Difficulty: easy
- Recommended Developer: claude-sonnet-5-5 at medium effort (one parser property, one count, two error strings, template and fixture edits)

### Validation Plan

- Tests to add/update: in `test_plan.py`, one test per new error (missing difficulty; unrecognised difficulty such as the unfilled template text; zero checkbox lines) following `test_approval_blank_value_is_planning_defect_not_approvable`; in `test_slice_ops.py`, extend the existing init-entry assertion (near `:84`) with `difficulty` and `criteria_total`.
- Commands to run: `cd skills/project-manager/tests && python3 -m unittest test_plan test_slice_ops test_finalize`
- Lint (differential, via the `lint` skill): required.
- Manual checks: run `check-plan` against `docs/implementation-plan-pm-model-ledger.md` (this file) and confirm zero errors.

### Rollback Path

- Revert the slice commit; plans without the two fields parse again.

## Slice 2: Retire `pm rate` and `model-performance.md`

### Intended Change

- Remove the `rate` subparser, `_run_rate` and its `_HANDLERS` entry from `pm_lib/cli.py`; remove `write_model_performance` and `model_performance_path` from `pm_lib/slice_ops.py` and the `model-performance.md` mention in its artifact comment (`slice_ops.py:130`); remove the `## Harness/Model Performance` section from `state.render_run_report` and its docstring mention.
- Move `references/model-performance-rubric.md` to `archive/references/model-performance-rubric.md` (plain `mv`, then `git rm` the original): `archive/` is gitignored, so the copy is untracked and git history keeps the file.
- `SKILL.md`: delete "Keep the final `rate` narrative as context." from the judgments section; rewrite step 5 "Finish" to: run `status --report`, quote its total run time, read the regenerated report end to end, give the `Plan defects` list, and keep its closing `stop --reason` / `stop --scavenge` sentence. The launcher's `model <model name>` placeholders become `model <full model id, e.g. claude-opus-5-5>` and step 1 gains one sentence: launch Developers and Reviewers with full model ids, never aliases, because the ledger groups exact strings and an alias's meaning drifts.
- `README.md`: delete the `rate` row; delete "performance rating" from the Layout sentence; delete the `rate` call from the walkthrough; add the same full-model-id note to the `init --model` / `review --model` rows (one sentence, once).
- `references/run-state.md`: delete the `model-performance.md` row and `rate` from the token-requiring command list. `references/review-judgments.md`: delete the "supplement the final model-performance narrative" clause at `:3` and the `model-performance.md` sentence in the harvest paragraph at `:112`.
- `skills/implementation-plan/SKILL.md` Mode B launcher: the same `model <full model id, e.g. claude-opus-5-5>` placeholder (it mirrors SKILL.md's).
- `CHANGELOG.md` `[Unreleased] / Removed`: one entry for `pm rate`, `model-performance.md` and the rubric, naming the ledger as the replacement.
- Tests: delete `TestRateCommand`; drop the `rate` call and rating assertion from `test_status_report_recreates_mirror_after_pm_deleted`; replace the `(not recorded)` assertions in `test_finalize.py:246-249` with `assertNotIn("Harness/Model Performance", report_text)`; drop the `rate` case from `test_missing_token_exits_two_for_every_mutating_command`.

### Acceptance Criteria

- [ ] `pm rate` is an unknown command; no code path reads or writes `model-performance.md`.
- [ ] `run-report.md` has no Harness/Model Performance section.
- [ ] No tracked file under `skills/` mentions `pm rate`, `model-performance`, or the rubric (`git grep -n "pm rate\|model-performance\|performance-rubric" -- skills` is empty).
- [ ] Both launcher copies (PM `SKILL.md`, implementation-plan `SKILL.md`) carry the full-model-id placeholder, and PM `SKILL.md` step 1 and the README `--model` rows carry the one-sentence rule.
- [ ] `SKILL.md` step 5 describes `status --report`, the run-time quote, the end-to-end read, the Plan defects list and the `stop` / `stop --scavenge` sentence, and nothing about a rating.
- [ ] The rubric exists at `archive/references/model-performance-rubric.md`.
- Behaviour that must not change: every other report section; `notes`; the token-gating of every remaining mutating command.

### Authorized Surface

- Files allowed to change:
  - `skills/project-manager/scripts/pm_lib/cli.py`
  - `skills/project-manager/scripts/pm_lib/slice_ops.py`
  - `skills/project-manager/scripts/pm_lib/state.py`
  - `skills/project-manager/SKILL.md`
  - `skills/project-manager/README.md`
  - `skills/project-manager/references/run-state.md`
  - `skills/project-manager/references/review-judgments.md`
  - `skills/project-manager/references/model-performance-rubric.md`
  - `archive/references/model-performance-rubric.md`
  - `skills/implementation-plan/SKILL.md`
  - `CHANGELOG.md`
  - `skills/project-manager/tests/test_finalize.py`
  - `skills/project-manager/tests/test_slice_ops.py`
- Functions/classes/components allowed to change: the `rate` parser/handler/dispatch entry; `write_model_performance`, `model_performance_path`; `render_run_report` (section removal only); the doc passages named above.
- Tests allowed or expected to change: `test_finalize.py`, `test_slice_ops.py` as listed.

### Explicit Non-Goals

- No change to the judgments section's gate or tie wording (Slices 5 and 6).
- No `lite-2` text yet (Slice 3).
- No change to `notes`.

### Risk Flags

- Risky surfaces touched: public CLI command removal (`rate`)
- Approval needed before implementation: no
- Difficulty: easy
- Recommended Developer: claude-sonnet-5-5 at medium effort (deletion plus prose edits across five docs; needs care, not depth)

### Validation Plan

- Tests to add/update: as listed under Intended Change; no new tests (a removed command needs none).
- Commands to run: `cd skills/project-manager/tests && python3 -m unittest test_finalize test_slice_ops`; the grep in the criteria.
- Lint (differential, via the `lint` skill): required.
- Manual checks: read the regenerated `run-report.md` from `test_finalize`'s accept flow.

### Rollback Path

- Revert the slice commit (the rubric returns with it).

## Slice 3: `lite-2` — event `data`, launch identity, stop causes, failed commissions

### Intended Change

- `pm_lib/state.py`: `SCHEMA = "lite-2"`. `append_event` and `_append_event_unlocked` gain `data: dict | None = None`, serialised as `"data"` only when not None (same rule as `evidence`). The `data` object is written verbatim; values must be JSON-serialisable.
- `init` records `state["repo_name"]` per Decision 3 (`git_ops` gains `git_common_dir_name(repo) -> str`: one `git rev-parse --path-format=absolute --git-common-dir` call, resolved, then the parent directory's basename).
- Remove the legacy tolerance listed under *Current-state facts* (schema): `assessment_of`'s fallback becomes a strict read that raises `PmError` naming the record if `assessment` is missing; `historical_review_count` and both callers and their printed/rendered lines (`_run_status` in `cli.py`, `_render_reviewer_judgments` in `state.py`); the `grants_seen` default in `is_review_fresh` (a missing or non-int `grants_seen` is now stale) and its docstring paragraph; the two "remains historical/unjudged" messages become plain shape errors without the historical clause. `run-state.md` `:89` and `review-judgments.md` `:96,:102` lose their historical/legacy sentences; every `lite-1` mention in code and docs named in the facts becomes `lite-2`.
- `start_slice`: the `launch`/`relaunch` event carries `data={"developer": new_current["developer"]}` (the same dict, `{tool, model, effort}`).
- Budget exhaustion in `start_slice` and `finalize_steer`: the `stop` event carries `data={"cause": "developer"}`. The note and `_BUDGET_EXHAUSTED_REASON` are unchanged (`_refuse_if_budget_exhausted` still keys on `stop_reason`).
- `finalize --stop`: `cli.py` adds `--cause` with `choices=["plan", "developer", "environment"]` on the `finalize` parser; `_run_finalize` refuses `--stop` without it and `--cause` without `--stop` (`PmError`, exit 2). `finalize_stop` takes `cause: str` and writes it as `data={"cause": cause}` on the `slice-stop` event, and nowhere else.
- `review.py`: a new event kind `review-failed` with `data={"skill", "tool", "model", "effort", "command_override", "origin_event_index", "reason"}` where `reason` is `"timeout"` or `"exit <code>"`, written (a) on timeout, replacing the free-text `review` event, and (b) on non-zero exit (today nothing). Inside the existing post-wait `locked_update`, and **only when `timed_out or returncode != 0`**, compute `reaped = current is None or pgid not in (current.get("reviewer_pids") or [])` before the filter; when `reaped`, write no event and raise `PmError("reviewer was reaped by a PM decision (process group {pgid}); not recorded as a failure")`. A timeout concurrent with a reap counts as reaped. A reviewer that exited 0 is recorded exactly as today whatever `reviewer_pids` holds. Identity values are the resolved tool/model/effort already in scope (`resolved_tool`, `resolved_model`, `recorded_effort`, `bool(reviewer_command)`, `origin_event["index"]`).
- `README.md` CLI table: `finalize --stop "reason" --cause plan|developer|environment`; exit-code line unchanged here. `SKILL.md`: step 3's `finalize --stop` line gains `--cause`, with one sentence on choosing it (`plan`: the contract is defective or ambiguous; `developer`: the Developer could not deliver; `environment`: harness, sandbox, network or credentials). `run-state.md`: the events row documents `data`, the `review-failed` kind, `repo_name`, and the `lite-2` schema example; it also gains the two documented limits from HLP §6 and §8 in one sentence each: a commission whose controlling `pm review` process dies leaves no record, and the event log is unsigned, so a steer-event append that fails right after a delivered steer merges that submission into the previous one and is caught, if at all, by the gate's head check.

### Acceptance Criteria

- [ ] `init` writes `schema: "lite-2"` and `repo_name`; loading a run whose `schema` is any other value (one test, using a literal such as `"lite-1"`) fails with the existing "not supported by this toolkit version" error.
- [ ] Every `launch` and `relaunch` event carries `data.developer == current_slice.developer` at that launch.
- [ ] Both budget-exhaustion `stop` events carry `data.cause == "developer"`; a `pm stop` event, appended with `data=None`, has no `data` key.
- [ ] `finalize --stop` without `--cause` exits 2 without changing `run.json` or `events.jsonl`; with `--cause X` the `slice-stop` event carries `data.cause == X` and no other file or field stores it; `--cause` with `--accept`, `--steer` or bare `finalize` exits 2.
- [ ] A reviewer timeout writes exactly one event, kind `review-failed`, with the full `data` shape and `reason == "timeout"`, and no `review` event.
- [ ] A reviewer non-zero exit writes one `review-failed` event with `reason == "exit N"`.
- [ ] A reviewer reaped by `pm stop` or `finalize --accept` writes no `review-failed` event (extend `TestStopReapsHungReviewer` or `TestAcceptReapsHungReviewer` with that assertion over `read_events`); a reviewer that exits 0 is recorded as a review regardless of `reviewer_pids`.
- [ ] `assessment_of` raises on a record lacking `assessment`; `is_review_fresh` returns False for a review lacking `grants_seen`; `git grep -n "historical/unjudged\|lite-1" -- skills/project-manager ':!skills/project-manager/tests'` is empty.
- [ ] `run-state.md` states both documented limits.
- Behaviour that must not change: event ordering and indices; `_refuse_if_budget_exhausted`; `judgments.publish_event` idempotency; every existing `review` success event.

### Authorized Surface

- Files allowed to change:
  - `skills/project-manager/scripts/pm_lib/state.py`
  - `skills/project-manager/scripts/pm_lib/slice_ops.py`
  - `skills/project-manager/scripts/pm_lib/review.py`
  - `skills/project-manager/scripts/pm_lib/judgments.py`
  - `skills/project-manager/scripts/pm_lib/cli.py`
  - `skills/project-manager/scripts/pm_lib/git_ops.py`
  - `skills/project-manager/scripts/pm_lib/plan.py`
  - `skills/project-manager/README.md`
  - `skills/project-manager/SKILL.md`
  - `skills/project-manager/references/run-state.md`
  - `skills/project-manager/references/review-judgments.md`
  - `skills/project-manager/tests/test_state.py`
  - `skills/project-manager/tests/test_slice_ops.py`
  - `skills/project-manager/tests/test_finalize.py`
  - `skills/project-manager/tests/test_review.py`
  - `skills/project-manager/tests/test_git_ops.py`
  - `skills/project-manager/tests/pm_test_helpers.py`
- Functions/classes/components allowed to change: `SCHEMA`, `append_event`, `_append_event_unlocked`, `_render_reviewer_judgments` (historical line removal), `create_run`/`init_run` (`repo_name`), `start_slice` (event data), `finalize_steer` (event data), `finalize_stop` (`cause`), `is_review_fresh`, `run_review` (failure events, reaped detection), `assessment_of`, `historical_review_count` (removal), `_context_key` and `_origin_from_current` (message wording), `_run_finalize` and the `finalize` parser (`--cause`), `_run_status` (historical line removal), the `plan.py:24` comment, new `git_ops.git_common_dir_name`, and the doc passages named above.
- Tests allowed or expected to change: `test_review.py` (timeout event assertion at `:1024-1028`, new non-zero-exit event assertion in `TestReviewerPidsClearedOnFailure`), `test_finalize.py` (`--cause` on every `finalize --stop`; reaped-reviewer assertion; budget `stop` data), `test_slice_ops.py` (launch data; relaunch exhaustion data), `test_state.py` (`data` round trip and omission; unsupported-schema refusal; `lite-2` docstring), `test_git_ops.py` (`git_common_dir_name` in a linked worktree), `pm_test_helpers.py` (if a shared `--cause` default helps).

### Explicit Non-Goals

- No code-metrics block (Slice 4), no judgment changes (Slice 5), no gate (Slice 6).
- No migration from `lite-1`; no dual-schema path.
- No change to the `stop` command's own event beyond leaving it without `data`.
- No tracking of a commission whose controlling `pm review` process itself dies (documented as a limit instead).

### Risk Flags

- Risky surfaces touched: persisted run-state schema (`lite-2`), event log shape, public CLI flag (`--cause`)
- Approval needed before implementation: no
- Difficulty: hard
- Recommended Developer: claude-opus-5-5 at high effort (a schema bump, legacy removal, four event sites, an argparse subtlety and the reaped-vs-failed distinction across many files)

### Validation Plan

- Tests to add/update: as listed in the criteria; the budget-exhaustion `stop` event data is asserted in `TestBudgetExhaustionClosesAllPaths` and `test_relaunch_persists_attempts_rotates_prior_result_and_exhausts_budget`, which already reach those paths.
- Commands to run: `cd skills/project-manager/tests && python3 -m unittest test_state test_slice_ops test_finalize test_review test_judgments test_git_ops`; the grep in the criteria.
- Lint (differential, via the `lint` skill): required.
- Manual checks: run the README trial walkthrough end to end with `--cause` on a `finalize --stop` and read `events.jsonl`.

### Rollback Path

- Revert the slice commit; runs created in between are `lite-2` and must be stopped before reverting.

## Slice 4: Code metrics at accept

### Intended Change

- New `pm_lib/code_metrics.py` with a module constant `_HEALTH_PATH = Path(__file__).resolve().parents[3] / "code-health" / "scripts" / "health.py"`, a loader that registers the module in `sys.modules` under a fixed private name before `exec_module`, loads it once per resolved path and reuses it while the path is unchanged, and one public function `code_block(repo: Path, before_head: str | None, commit: str) -> dict`. It never raises: any exception, including a missing or unloadable `health.py`, becomes `{"error": "<type>: <message>"}`.
  - Files: `git diff --no-renames -z --name-only <before>..<commit>` via `subprocess` with `text=False`, names decoded UTF-8 with `errors="replace"` and sorted. When `before_head` is None, `<before>` is `git hash-object -t tree /dev/null` (hash-agnostic empty tree).
  - Lines: for each file, before and after contents from `git show <rev>:<path>` (absent side = empty text), decoded with `errors="replace"`; `language_for(path)`, `category_for(path, language)[0]`, and `line_counts(text, language)` from code-health; the block stores the net change per category and kind: `"lines": {category: {"code": int, "comment": int, "blank": int}}` with all six categories always present.
  - Complexity: over the touched files whose language is `Python`, build one `SourceFile` per side with each side's text and call `python_structure` once per side; `"complexity": {"before": {"sum": s, "max": m}, "after": {...}}` from `functions[*].cyclomatic` (0 and 0 with no functions). If either side's `parse_errors` is non-empty, `"complexity": null` and `"complexity_reason": "<path>:<line>: <reason>"` naming the first error; otherwise `"complexity_reason": null`.
- `finalize_accept`: after the elevated-review check passes and before the entry mutation, `entry["code"] = code_metrics.code_block(repo, current.get("before_head"), head)`. Git is read here once per accepted slice and never again by the ledger. `AcceptOutcome` gains `code_warning: str | None`, set when the block is `{"error"}` or its `complexity` is null; `_run_finalize` prints it to stderr as `pm: code metrics: <reason>` (loud, never blocking, HLP §5).
- `pm_lib/__init__.py` docstring: record the one deliberate cross-skill import and why it is path-based.
- `run-state.md`: document `code` on the slice entry with the shape above and state the two methods once (`line_counts` lexical heuristic; Python AST cyclomatic complexity).

### Acceptance Criteria

- [ ] A fixture repo whose slice commit adds a production `.py` with code, comment and blank lines, a `tests/test_x.py`, and a `.md` yields exactly the expected `lines` deltas by category and kind and `complexity.before/after` sums and maxima.
- [ ] A deleted file contributes negative deltas; a file touched but unchanged in count contributes zero.
- [ ] A Python file that fails to parse on either side gives `complexity: null` with a `complexity_reason` naming it, while `lines` is still filled.
- [ ] `before_head` None (unborn branch at launch) diffs against the empty tree.
- [ ] With `_HEALTH_PATH` monkeypatched to a missing file, `code_block` returns `{"error": ...}` (unit level); with `python_structure` on the loaded module monkeypatched to raise, `finalize --accept` still completes with `ACCEPTED`, stores `{"error": ...}` and prints `pm: code metrics:` to stderr (accept level).
- [ ] `TestImportHygiene` passes.
- Behaviour that must not change: every floor fact; the elevated-review refusal; the accept event and report.

### Authorized Surface

- Files allowed to change:
  - `skills/project-manager/scripts/pm_lib/code_metrics.py`
  - `skills/project-manager/scripts/pm_lib/slice_ops.py`
  - `skills/project-manager/scripts/pm_lib/cli.py`
  - `skills/project-manager/scripts/pm_lib/__init__.py`
  - `skills/project-manager/references/run-state.md`
  - `skills/project-manager/tests/test_code_metrics.py`
  - `skills/project-manager/tests/test_finalize.py`
- Functions/classes/components allowed to change: new module `code_metrics`; `finalize_accept` and `AcceptOutcome` (one assignment, one field); `_run_finalize` (one stderr line); the package docstring; the `run.json` shape in `run-state.md`.
- Tests allowed or expected to change: new `test_code_metrics.py` (`PmTestCase`, fixture commits via `_git`); `test_finalize.py` accept-flow assertion that `entry["code"]` is a dict with the `lines` key, plus the injected-failure accept test.

### Explicit Non-Goals

- No change to code-health itself; no lizard; no churn figures; no per-file breakdown stored; no method constant on every entry.
- No ledger rows yet (Slice 7).
- No metrics for stopped or exhausted windows.

### Risk Flags

- Risky surfaces touched: persisted run-state schema (`code` on the slice entry); first cross-skill import
- Approval needed before implementation: no
- Difficulty: moderate
- Recommended Developer: claude-sonnet-5-5 at high effort (new self-contained module with a fixture repo; the import gotcha is spelled out)

### Validation Plan

- Tests to add/update: `test_code_metrics.py` covering each criterion above; the two `test_finalize.py` assertions.
- Commands to run: `cd skills/project-manager/tests && python3 -m unittest test_code_metrics test_finalize test_plan`
- Lint (differential, via the `lint` skill): required.
- Manual checks: accept a slice in the README trial walkthrough and read `code` in `run.json`.

### Rollback Path

- Revert the slice commit; `code` is an unknown key that validation tolerates.

## Slice 5: Judgment inputs — criteria met, defects, strict panel order

### Intended Change

- `judgments._parse_developer_input`: a score record requires `criteria_met` (int, `0 ≤ criteria_met ≤ criteria_total` of the slice entry; the upper bound is checked in `record_developer_judgment` where the entry is available, and an entry lacking `criteria_total` is a named error, never a skipped check) and `defects` (object with exactly the keys `P0`, `P1`, `P2`, `P3`, each an int ≥ 0). An unavailable record refuses both. Stored verbatim on the judgment record.
- `judgments._parse_input` comparisons: `rank_groups` is replaced by `order` (list of ≥ 2 unique review ids, best first) and `close` (list, possibly empty, of ids each of which appears in `order` at position ≥ 1; a close member is too close to call against the member immediately above it). Stored as `{"order": [...], "close": [...], "assessment": "comparison"}`. The unavailable default at `judgments.py:89` is removed: `assessment` is required on every unavailable record. `_referenced_ids` reads `order`. `_input_matches` compares `order` and `close` instead of `rank_groups`, so a reversed order or a changed close flag is a conflict, not an exact retry. `_validate_comparable_panel` is unchanged in substance.
- New `judgments.panel_groups(entry, origin_index) -> list[list[str]]` per Decision 14. `unranked_code_review_ids` is redefined on it: for every slice and every origin that has reviews, the members of a group not covered by an active `order` whose id set includes the group or by an active unavailable comparison whose `review_ids` include the group. Its printed line in `cli.py` and rendered line in `state.py` are reworded to "code panels without an order".
- Rendering that read `rank_groups`: `state._judgment_attempt_label` (`state.py:643`) and `state._render_reviewer_judgments` (`state.py:715-721`) now read `order`/`close` and render `a > b ≈ c` (`≈` marks a close call against the member before it); the "(singleton/unranked)" rendering is dropped since a comparison has ≥ 2 members.
- `references/review-judgments.md` is rewritten for these inputs: the 0–2 rubric table unchanged; a Developer example with `criteria_met` and `defects` and one sentence defining each (distinct defects PM confirmed in this submission, code or scope, whether PM found them or confirmed a reviewer's finding; `criteria_met` counts the slice's checkbox criteria this submission met); the comparison example with `order` and `close` and one sentence each; the unavailable examples for both skills with `assessment` stated; deletion of tie, chosen-panel, singleton/unranked and "do not commission extra reviews to fill a ranking" passages. The gate is not described here (Slice 6) beyond "see SKILL.md".
- `SKILL.md` judgments section (`:36` and `:40`): the sentence "Also record the usefulness of all code reports in your chosen panel best first, allowing ties …" becomes "order every intact code report that shares a commission context, strict best-first, flagging close calls; a lone report needs only its rating"; "a single reviewer can be excellent while remaining singleton/unranked" becomes "a lone report is rated, never ordered"; "the panel you chose" and "Code reports outside recorded panels are informational coverage" are deleted (Slice 6 adds the gate wording). `run-state.md`: the example record uses `order`/`close`; the developer-judgment description names the two new fields.

### Acceptance Criteria

- [ ] `judge-developer` with a score refuses a missing or out-of-range `criteria_met`, a missing `defects`, a `defects` with a missing, extra or negative key, and stores both on the record; an unavailable record carrying either is refused; an entry lacking `criteria_total` is a named error.
- [ ] `judge-reviews` accepts `order` with ≥ 2 unique known ids and `close ⊆ order[1:]`; refuses `rank_groups`, a singleton `order`, a duplicate, a `close` member not in `order` or at position 0.
- [ ] Re-submitting an identical `order`/`close` is an exact retry; re-submitting with the order reversed or a close flag changed is refused as a conflict.
- [ ] Any unavailable record without `assessment` is refused for both skills.
- [ ] `panel_groups` returns only groups of ≥ 2 intact code-review reviews of the given origin sharing one `_context_key` and lacking an active unavailable rating; `unranked_code_review_ids` lists exactly the members of such groups without covering coverage, so a lone code review is never listed.
- [ ] `status` and `run-report.md` render an order as `a > b ≈ c` and no "singleton/unranked" text.
- [ ] Supersession works unchanged for the new shapes.
- [ ] `review-judgments.md` has one worked example each for a Developer score, a Developer unavailable, a reviewer rating, a panel order, and an unavailable comparison, and no tie or chosen-panel language; `SKILL.md:36,40` carry the new wording and none of the deleted phrases.
- Behaviour that must not change: rating records; `_context_key`; overlap refusal; event publication.

### Authorized Surface

- Files allowed to change:
  - `skills/project-manager/scripts/pm_lib/judgments.py`
  - `skills/project-manager/scripts/pm_lib/state.py`
  - `skills/project-manager/scripts/pm_lib/cli.py`
  - `skills/project-manager/SKILL.md`
  - `skills/project-manager/references/review-judgments.md`
  - `skills/project-manager/references/run-state.md`
  - `skills/project-manager/tests/test_judgments.py`
  - `skills/project-manager/tests/pm_test_helpers.py`
- Functions/classes/components allowed to change: `_parse_input`, `_parse_developer_input`, `_referenced_ids`, `_input_matches`, `record_developer_judgment` (upper-bound check), new `panel_groups`, `unranked_code_review_ids`, `_judgment_attempt_label`, `_render_reviewer_judgments`, the `status` lines in `cli.py` that print those helpers, and the doc passages named above.
- Tests allowed or expected to change: `test_judgments.py` (all `rank_groups` uses, the unavailable-default test, new field tests, retry-conflict test, `panel_groups` test); `pm_test_helpers.py` (promote the judgment input writers from `test_judgments.py` into shared helpers `judge_reviews(test, token, run_dir, data)` and `judge_developer(test, token, run_dir, data)` for Slice 6's use).

### Explicit Non-Goals

- No gate (Slice 6); no change to when a judgment may be recorded.
- No change to the 0–2 scale or to rating records.
- No "mixed" or half-win semantics.

### Risk Flags

- Risky surfaces touched: judgment input contract (public CLI input shape)
- Approval needed before implementation: no
- Difficulty: moderate
- Recommended Developer: claude-sonnet-5-5 at high effort (input validation, one shared predicate and rendering changes with many existing tests to update)

### Validation Plan

- Tests to add/update: one test per refusal listed in the criteria; the render test for `a > b ≈ c` in whichever module already asserts the judgments section of the report.
- Commands to run: `cd skills/project-manager/tests && python3 -m unittest test_judgments test_review test_state test_finalize`
- Lint (differential, via the `lint` skill): required.
- Manual checks: read the rewritten `review-judgments.md` end to end against the code's validation messages.

### Rollback Path

- Revert the slice commit; runs with `order` records in between must be stopped first.

## Slice 6: Judgment gate on every exit from a submission

### Intended Change

- `judgments.current_submission_gaps(state, events, repo, slice_id) -> list[str]` (pure apart from one `git_head` read): with `origin = _scan_latest_developer_origin(events, slice_id)`:
  1. no origin → `"no launch, relaunch or steer event for {slice_id}"`;
  2. no active developer judgment whose `submission.origin_event.index == origin.index` → `"Developer judgment for event {index} (judge-developer)"`; one whose `submission.head != git_head(repo)` or `submission.grants_seen != len(slice_grants(state, slice_id))` → `"Developer judgment for event {index} is stale (head or grants changed); record a superseding judgment"`;
  3. for each review in the entry with `origin_event.index == origin.index` and `_artifact_is_intact(review)`: no active rating-kind record (score or unavailable) referencing it → `"rating for {review_id} ({skill})"`;
  4. for each group from `panel_groups(entry, origin.index)`: no active `order` whose id set includes the group and no active unavailable comparison whose `review_ids` include the group → `"panel order for {ids} (judge-reviews order, or one unavailable comparison covering all of them)"`.
- `slice_ops._require_judged(repo, run_dir, token, state, slice_id, *, ratcheted: bool)`: calls the helper through a lazy `from . import judgments` inside the function; on gaps, `save_state(run_dir, state, token)` only when `ratcheted` is True, then `raise PmError("refused: the current submission of {slice_id} is unjudged — " + "; ".join(gaps))`.
- Call sites, each after the risk ratchet (whose return value is `ratcheted`) and after any budget block, before evidence collection, rotation or session changes: `finalize_accept` after `apply_risk_ratchet` (`:1398`) and before `_collect_finalize_evidence`; `finalize_steer` after the budget block (`:1540`) and before `current["attempts"] = attempts`; `finalize_stop` after the ratchet (`:1648`) and before `_collect_finalize_evidence`; `start_slice` only when `relaunch`, after the budget block (`:780`) and before `_rotate_prior_attempt`.
- `pm stop` stays ungated. A fresh launch after `finalize --stop` is ungated (new window).
- `SKILL.md` judgments section: replace the "never … acceptance gates" sentence and the recovery paragraph with: every exit from a submission (`finalize --accept`, `--steer`, `--stop`, and a relaunch including resume after `pm stop`) is refused until the current submission has a Developer judgment matching the current head and grants, a rating or `unavailable` for every intact review of this submission, and a full order (or one unavailable comparison) for every panel; a dead or paused session's submission is judged `unavailable`; after a budget kill `finalize --stop` is the exit and is gated like the rest; a head that moved since the judgment is corrected by supersession; `pm stop` remains the ungated emergency exit; reviewer ratings may still be recorded after a decision. Delete the gate-contradicting sentences named in HLP W4. `run-state.md`: replace "Judgments do not change … acceptance rules" with one sentence pointing at the gate. `README.md`: the `finalize` and `start-slice` rows mention the gate; the exit-code line adds "or the judgment gate" to code 2; the "Verify your setup" walkthrough records a `judge-developer` (score, `criteria_met`, `defects`) before its `finalize --accept`.
- Tests: every test that decides a submission (accept, steer, stop, or relaunch of a live slice) now records the judgments first through the shared helpers from Slice 5 (one `judge_developer` with `criteria_met`/`defects`, plus ratings/orders where reviews exist); hand-built `current_slice` fixtures gain a launch event and a `developer` identity; new gate tests cover each gap kind, relaunch gating, earlier-origin gaps ignored, a two-report panel refused until ordered, the ratchet-persisted refusal, the no-write refusal, and budget exhaustion still killing the session.

### Acceptance Criteria

- [ ] Within budget, each of the four commands is refused with exit 2 and a message naming every gap kind present, before writing evidence, rotating artifacts, killing a session, or changing `attempts`.
- [ ] A refusal after `--risk elevated` leaves the ratchet persisted in `run.json`; a refusal without `--risk` leaves `run.json` byte-identical.
- [ ] With a Developer judgment, a rating for every intact current-origin review, and an order (or unavailable comparison) for every panel, each command proceeds exactly as before.
- [ ] A relaunch of a live slice (and a resume after `pm stop`) is gated; a fresh launch after `finalize --stop` is not; `pm stop` is not.
- [ ] Gaps on an earlier origin (a superseded steer's submission) never refuse.
- [ ] A Developer judgment recorded at an older head is refused as stale, and a superseding judgment clears it.
- [ ] Past the budget, `finalize --steer` and a relaunch kill the session and write the budget `stop` event whether or not the submission is judged, and never reach the gate.
- [ ] A review whose artifact is missing or altered is not demanded by the gate.
- [ ] A panel of two intact code-review reports sharing a `_context_key` is refused until an `order` names both or an unavailable comparison covers both; two reports with different keys are two lone reports and need only ratings; an `order` that names the group plus a since-unavailable member still covers it.
- [ ] The README walkthrough runs end to end as written.
- Behaviour that must not change: the floor; the elevated-review check; `pm stop`; budget accounting; every judgment-recording rule from Slice 5.

### Authorized Surface

- Files allowed to change:
  - `skills/project-manager/scripts/pm_lib/judgments.py`
  - `skills/project-manager/scripts/pm_lib/slice_ops.py`
  - `skills/project-manager/SKILL.md`
  - `skills/project-manager/README.md`
  - `skills/project-manager/references/run-state.md`
  - `skills/project-manager/references/review-judgments.md`
  - `skills/project-manager/tests/test_finalize.py`
  - `skills/project-manager/tests/test_slice_ops.py`
  - `skills/project-manager/tests/test_judgments.py`
  - `skills/project-manager/tests/pm_test_helpers.py`
- Functions/classes/components allowed to change: new `current_submission_gaps`; new `_require_judged`; `finalize_accept`, `finalize_steer`, `finalize_stop`, `start_slice` (one call each); the doc passages named above; test helpers for recording judgments with launch events and `developer` identity on hand-built `current_slice` fixtures.
- Tests allowed or expected to change: every `test_finalize.py` and `test_slice_ops.py` test that decides a submission, plus new gate tests in `test_finalize.py`.

### Explicit Non-Goals

- No gating of `pm stop`, `send`, `review`, `grant`, `approve`, or a fresh launch.
- No lock-held re-read inside the finalize paths (the single-controller rule in `run-state.md` already covers concurrent mutating commands).
- No change to judgment input shapes.

### Risk Flags

- Risky surfaces touched: the decision control flow of four commands; ordering against the budget kill and the risk ratchet
- Approval needed before implementation: no
- Difficulty: hard
- Recommended Developer: claude-opus-5-5 at high effort (cross-cutting: four command paths, ordering against budget kills and state saves, many test updates)

### Validation Plan

- Tests to add/update: as above; the new gate tests use fake harnesses that write `result.json` and exit, and a two-report panel built with `--reviewer-command` fakes as in `test_review.py`.
- Commands to run: `cd skills/project-manager/tests && python3 -m unittest test_finalize test_slice_ops test_judgments test_review`
- Lint (differential, via the `lint` skill): required.
- Manual checks: README trial walkthrough end to end, including one deliberate accept without the judgment to read the refusal message.

### Rollback Path

- Revert the slice commit; decisions become ungated again, with no state change required.

## Slice 7: Ledger rows and the per-run ledger file

### Intended Change

- `state.parse_event_ts(raw) -> datetime | None`: the parsing loop body split out of `run_elapsed`, which now calls it; tz-aware UTC or None. The existing `TestRunElapsed` tests cover it; no new test.
- New `pm_lib/ledger.py`:
  - `ledger_dir() -> Path`: `PM_LEDGER_DIR` (stripped, if non-empty) else `Path(__file__).resolve().parents[2] / "ledger"`. `host_name() -> str`: `socket.gethostname().split(".")[0].lower()`.
  - `run_rows(state: dict, events: list[dict]) -> list[dict]`, pure, no git, no plan file. **Every event-derived field below considers only events whose `slice` equals the row's slice id**, with one exception named under `developer_s`. One row per window; attested slices and slices with no `launch` event produce none. Window: from a slice's `launch` event (index `open`) to the next `launch` for the same slice (exclusive) or the end of the log. Row fields, all present, `null` where unknown:
    - `repo_name`, `run_id`, `slice_id`, `window_open` (int), `difficulty`, `criteria_total` (from the signed entry; `null` if absent);
    - `outcome` ∈ `accepted|exhausted|stopped|open` with the HLP §3.2 precedence over the window's `accept`, `stop` with `data.cause == "developer"`, and `slice-stop` events; `cause` (`slice-stop` `data.cause`, or `"developer"` for exhausted, else `null`); `decided_at` (the `accept` ts, the first budget `stop` ts for exhausted, the `slice-stop` ts for stopped; `null` for open); `elapsed_s` (decided_at minus the launch ts, `null` for open);
    - `steers`, `relaunches`, `nudges` (`steer`, `relaunch`, `send` events in the window);
    - `code`: the entry's `code` block for accepted windows, else `null`;
    - `submissions`: one per `launch`/`relaunch`/`steer` event in the window, in order, each `{origin: int, kind, developer: {tool, model, effort}` from the `data.developer` of the latest `launch`/`relaunch` for this slice at or before it, `judgment`: exactly one of `{score, criteria_met, defects}`, `{status: "unavailable"}` or `null`, from the active developer judgment on that origin, `developer_s`: seconds from the origin to the next `floor`, `launch`, `relaunch`, `steer`, `stop`, `slice-stop` or `accept` event for this slice, where a `stop` whose `slice` is `null` (a run-wide stop) also ends it, or `null` if none}`;
    - `reviews`: one per review on the signed entry whose `origin_event.index` is in the window: `{review_id, skill, tool, model, effort, command_override, origin, rating: {score}|{status: "unavailable"}|null}` from the active rating-kind judgment, plus one `{review_id: null, failed: true, skill, tool, model, effort, command_override, origin, reason}` per `review-failed` event in the window;
    - `comparisons`: each active `order` judgment all of whose referenced reviews are in the window as `{origin, order, close}`; each active unavailable comparison projected per window as `{status: "unavailable", review_ids: <its referenced ids that lie in this window>}`, so a cross-origin unavailable record appears, partially, on every window it touches.
  - `write_run_file(run_dir: Path, token: str) -> Path`: under `_advisory_lock(run_dir / ".lock")`, `_load_state_unlocked(run_dir, token)` (MAC-verified), `read_events`, then `_atomic_write_bytes` of `json.dumps({"run_id", "events": len(events), "rows": run_rows(...)}, sort_keys=True, indent=2) + "\n"` to `ledger_dir() / host_name() / f"{run_id}.json"`.
- `cli.main`: per Decision 9, one private function `_write_ledger(args) -> str | None` runs after the handler returns or raises `PmError`: if `hasattr(args, "run")` and a token resolves, `repo = _repo_from_cwd()`, `run_dir = resolve_run_dir(repo, args.run)`; return silently if either raises `PmError` or `run_dir / "run.json"` is absent; call `write_run_file`; **catch `Exception`** and return `f"{type(exc).__name__}: {exc}"`, which `main` prints as `pm: ledger: not written: …` to stderr. The handler's exit code is returned unchanged except that `status --report` returns 2 after a hook failure.
- `tests/pm_test_helpers.py`: pin `PM_LEDGER_DIR` at import to a per-process `tempfile.mkdtemp()` beside the existing `PM_TMUX_SOCKET` pin, removed by the existing `atexit` hook.
- `.gitignore`: the two entries from Decision 4.
- `README.md`: a short "Ledger" section (location, `PM_LEDGER_DIR`, per-host folders, that the file is regenerated after every token-bearing command and never edited by hand); the walkthrough exports `PM_LEDGER_DIR` to a scratch path; the maintainer map gains `code_metrics` and `ledger`. `run-state.md`: one paragraph on the per-run ledger file as a derived artifact. `review-judgments.md`: the harvest paragraph at `:112` now points consumers at the per-run ledger file instead of `run.json`. `SKILL.md` step 5: `status --report` also confirms the ledger file was written (a non-zero exit names the failure).

### Acceptance Criteria

- [ ] `run_rows` on a fixture `state`/`events` with two slices launched under different `data.developer` identities attributes each row's submissions to its own identity, and events of one slice never appear in another slice's row.
- [ ] A window with a launch, a steer, and a relaunch under a changed `--model` yields three submissions with two identities (a mixed window).
- [ ] Outcomes: accept → `accepted`; `finalize --stop --cause plan` → `stopped`/`plan`; budget `stop` then `finalize --stop --cause environment` → `exhausted`/`developer` with `decided_at` the budget stop; `pm stop` then nothing → `open`; `pm stop` then relaunch then accept → one window, `accepted`, two submissions.
- [ ] A `finalize --stop` followed by a fresh launch of the same slice yields two rows with different `window_open`.
- [ ] An `unavailable` Developer judgment appears on its submission as `{status: "unavailable"}` and a scored one as `{score, criteria_met, defects}`.
- [ ] `developer_s` ends at the next `floor` event, not at the decision, when a bare `finalize` preceded the accept (fixture timestamps differ by whole seconds).
- [ ] Reviews carry their rating; a rating recorded after the decision appears after the next hook write; a `review-failed` event appears as a failed review entry; an unavailable comparison naming reviews from two windows appears on both, each with only its own ids.
- [ ] `run_rows` is deterministic: the written file bytes are identical across two writes with no new events.
- [ ] After `finalize --accept` (via `cli.main`) the file `PM_LEDGER_DIR/<host>/<run_id>.json` exists with `events == len(read_events(run_dir))`; after a tokenless `status --report` it is unchanged (`cli.main` with the env var unset and no `--token`); after `judge-reviews` it is rewritten.
- [ ] With `PM_LEDGER_DIR` pointing beneath a regular file (so no process can create it), `finalize --accept` still exits 0 and prints `pm: ledger: not written:` to stderr; `status --report` exits 2 with the same line; with `run_rows` monkeypatched to raise `KeyError`, `finalize --accept` still exits 0 with the same line.
- [ ] `stop --scavenge` with the run directory deleted prints no ledger line.
- [ ] `TestRunElapsed` passes unchanged through `parse_event_ts`.
- Behaviour that must not change: every command's stdout and exit code apart from the cases above; lock discipline (the hook holds the lock only for its own read-and-write).

### Authorized Surface

- Files allowed to change:
  - `skills/project-manager/scripts/pm_lib/ledger.py`
  - `skills/project-manager/scripts/pm_lib/state.py`
  - `skills/project-manager/scripts/pm_lib/cli.py`
  - `skills/project-manager/README.md`
  - `skills/project-manager/SKILL.md`
  - `skills/project-manager/references/run-state.md`
  - `skills/project-manager/references/review-judgments.md`
  - `.gitignore`
  - `skills/project-manager/tests/pm_test_helpers.py`
  - `skills/project-manager/tests/test_ledger.py`
- Functions/classes/components allowed to change: new module `ledger`; `run_elapsed` (extraction) and new `parse_event_ts`; `main` and new `_write_ledger` in `cli.py`; the test-helper env pin; the doc passages named above.
- Tests allowed or expected to change: new `test_ledger.py` (`unittest.TestCase` with hand-built `state`/`events` for `run_rows`; `TmuxRunTestCase` for the hook through real commands).

### Explicit Non-Goals

- No renderer (Slice 8).
- No append log, no second lock, no repair, no sweep, no host field in rows.
- No reading of the plan file or git in `run_rows`.
- No change to which commands exist or what they print on success.

### Risk Flags

- Risky surfaces touched: CLI dispatch (`main`), a new on-disk artifact outside the repo, lock usage
- Approval needed before implementation: no
- Difficulty: hard
- Recommended Developer: claude-opus-5-5 at high effort (the pure row function is the ledger's whole semantics; the hook touches dispatch)

### Validation Plan

- Tests to add/update: `test_ledger.py` as enumerated; the hook tests drive `cli.main` with a fake harness.
- Commands to run: `cd skills/project-manager/tests && python3 -m unittest test_ledger test_state test_finalize test_slice_ops`
- Lint (differential, via the `lint` skill): required.
- Manual checks: README trial walkthrough with `PM_LEDGER_DIR` set; inspect the written JSON by eye; confirm `git status` shows nothing under `skills/project-manager/ledger/` after a run without `PM_LEDGER_DIR`.

### Rollback Path

- Revert the slice commit; the hook and module disappear; ledger files already written are inert.

## Slice 8: `pm ledger render`

### Intended Change

- `pm_lib/ledger.py` gains `load_run_files(root: Path)` and `render_leaderboard(runs) -> tuple[str, list[str]]` (markdown text, error strings); `cli.py` gains the `ledger` subparser with the `render` subcommand and `--out PATH` (default `ledger_dir() / "leaderboard.md"`), handled by `_run_ledger_render`; it needs no run and no token, so it carries neither `--run` nor `--token` and the hook never fires for it. Exit 0 when rendered, 2 when no files were found, naming the directory searched.
- Loading: every `<root>/*/*.json`, read in sorted path order. Per `run_id`, keep the copy with the largest `events`; byte-identical copies collapse; two copies with the same `events` but different bytes, and any unparsable file or one lacking `run_id`/`events`/`rows`, go to the errors list (sorted by path) and are excluded.
- Statistics, shared by every table: a group's cell is computed over the values present for that group with `null` values (a `null` `developer_s`, `elapsed_s`, `complexity`, or a `code` block with `error`) excluded, and every cell prints its own n; a cell with zero values prints `n/a`; "As of" is the latest `decided_at`, or `unknown` when no window is decided.
- Developer tables (overall, then one per difficulty band), rows grouped by exact `(tool, model, effort)`, per HLP §4 W5: windows counted are `accepted`, `stopped` with cause `developer`, and `exhausted`; submissions counted are scored ones only; "first submission" is the window's first scored submission; submission-level columns credited per submission identity: PM 0–2 mean; first-submission criteria Σk/Σn; first-submission defects as mean P0+P1 and mean P2+P3 per first submission; median `developer_s`. Window-level columns only for windows whose every scored submission shares one identity (Decision 15): windows n; accepted share; accepted on first submission (an `accepted` window with exactly one scored submission) share; submissions per window mean; steers+nudges mean; median `elapsed_s`; median Δ code lines per category and kind from `code.lines`; median Δ complexity sum (`after.sum − before.sum`). Windows with mixed identities or no scored submission are counted in a footnote per table. Sort by PM mean descending, then first-submission acceptance share descending, nulls last, then by the group key tuple. Rows with fewer than `MIN_WINDOWS = 3` windows go under "Insufficient data", in group-key order, unranked.
- Reviewer tables, one per skill, grouped by `(tool, model, effort, command_override)`: commissions n (every review entry for the group: rated, unavailable, unrated or failed); usable share rated/(rated+unavailable+failed), unrated reviews excluded from both sides; PM 0–2 mean; panels n (orders the reviewer appears in); pairwise win share: over every pair `(x above y)` implied by every `order` the reviewer appears in, wins/(wins+losses), a close call still a full win; close-call win share: over the adjacent pairs `(x, y)` where `y ∈ close`, the reviewer's wins/(wins+losses) counting its appearances as either member. Code-review sorted by pairwise win share, drift-audit by PM mean, with the same null-last and group-key tie-breaks. Rows under `MIN_COMMISSIONS = 3` go under "Insufficient data".
- Presentation per HLP §4 W5: each column header names its statistic; columns derived from PM judgment carry "(PM)" in the header; no composite score; a "History" link to `historical-pre-ledger.md` if it exists in the skill's own `ledger/` directory (`Path(__file__).resolve().parents[2] / "ledger"`, not `ledger_dir()`, so `PM_LEDGER_DIR` does not hide it), written relative to the output's directory; an "Errors" section listing excluded files; no wall-clock stamp anywhere.
- `README.md` CLI table and Ledger section: the `ledger render` row and one paragraph on cross-host merging (copy the other host's folder in). `CHANGELOG.md` `[Unreleased] / Added`: one entry for the whole ledger feature (Slices 1, 3–8), naming the `lite-2` schema, the gate, the plan fields and the renderer.

### Acceptance Criteria

- [ ] Rendering the same set of files twice gives identical bytes; rendering after copying one host folder under a second host name gives the same bytes (duplicates collapse); rendering with the files supplied in a different order gives the same bytes.
- [ ] Two copies of one run with different `events` keep the larger; two with equal `events` and different bytes are listed under Errors and excluded; a malformed file is listed under Errors.
- [ ] A fixture of rows exercising every column gives the hand-computed values: PM mean, Σk/Σn, defect means, medians, accepted share, first-submission acceptance, pairwise and close-call win shares, each with its own n; a column with no values prints `n/a`.
- [ ] `open` windows and `stopped` windows with cause `plan` or `environment` are absent from Developer columns and present in reviewer counts.
- [ ] A mixed window and a window with no scored submission are absent from window-level columns and counted in the footnote; scored submissions of a mixed window still count at submission level.
- [ ] A group below the minimum n appears under "Insufficient data" without a rank; two groups tied on every sort key appear in group-key order.
- [ ] A reviewer with one rated, one unavailable, one unrated and one failed commission shows commissions 4 and usable share 1/3.
- [ ] `pm ledger render --out` writes there; without files the command exits 2 naming the directory searched.
- [ ] "As of" equals the latest `decided_at` in the kept files, or `unknown` with none.
- Behaviour that must not change: `run_rows`; the hook; every other command.

### Authorized Surface

- Files allowed to change:
  - `skills/project-manager/scripts/pm_lib/ledger.py`
  - `skills/project-manager/scripts/pm_lib/cli.py`
  - `skills/project-manager/README.md`
  - `CHANGELOG.md`
  - `skills/project-manager/tests/test_ledger.py`
- Functions/classes/components allowed to change: new `render_leaderboard`, `load_run_files` and their private helpers (one small markdown-table helper is expected); the `ledger` subparser and `_run_ledger_render`; the README passages; the CHANGELOG entry.
- Tests allowed or expected to change: `test_ledger.py` (new render tests on hand-built row files; no whole-leaderboard golden file).

### Explicit Non-Goals

- No HTML, no Bradley–Terry, no randomisation, no configuration file, no sweep, no token figures.
- No change to row semantics or the per-run file.
- No historical table generation (operator task, below).

### Risk Flags

- Risky surfaces touched: new public CLI command (`ledger render`, `--out`)
- Approval needed before implementation: no
- Difficulty: moderate
- Recommended Developer: claude-opus-5-5 at medium effort (self-contained renderer, but many columns whose statistics must be exactly as specified)

### Validation Plan

- Tests to add/update: `test_ledger.py` render tests as enumerated, each asserting specific cell values or section presence, not whole-document equality.
- Commands to run: `cd skills/project-manager/tests && python3 -m unittest test_ledger`
- Lint (differential, via the `lint` skill): required.
- Manual checks: render the files produced by a README trial run and read `leaderboard.md`; confirm the "Insufficient data" placement for a one-run ledger.

### Rollback Path

- Revert the slice commit; the per-run files remain and can be rendered by a later version.

## Operator tasks after the run (not slices)

1. **Historical table (HLP W7).** Generate `skills/project-manager/ledger/historical-pre-ledger.md` once from `ai-agent-bench/archive/2026-10-09-pm-ledger-census/` (era-D runs only, identities resolved, the brief's eligibility rules, the same column families where the old data supports them), label it frozen and pre-ledger, and commit it (it is tracked under Decision 4). Never regenerate it.
2. **Bench freeze.** Add the "frozen" note to `ai-agent-bench/README.md`.
3. **Live install.** Finish or stop every in-flight run on both machines (check the Mac Studio's `mimic` first), then pull the live `ai-agent-coder`. Plans written before Slice 1 fail `check-plan` until each slice has `Difficulty:` and a checkbox criterion; make that edit before `init`, never mid-run.
4. **First `lite-2` smoke run** on a small real plan with two Developer models across slices, one steer and one two-report panel; then `pm ledger render` and read the result. Confirm `skills/project-manager/ledger/` is writable where PM runs on both machines, or set `PM_LEDGER_DIR`.
5. **Reassess the judgment burden after ~10 runs** (HLP §8).

## Review record

Round 1 (Codex `gpt-6.1-sol` high: NOT FIT, 3 P0 / 6 P1 / 5 P2 / 1 P3; Claude `claude-opus-5-5` high: FIT WITH FIXES, 1 P1 / 11 P2 / 11 P3). Every finding was verified against the code and adopted, in particular: the exact-retry matcher's `rank_groups` whitelist (Slice 5); the hook's `Exception` boundary (Slice 7, Decision 9); explicit per-slice event filtering in rows (Slice 7); gate refusal persisting state only after a ratchet (Slice 6, Decision 5); an `order` covering a panel by inclusion, with one shared panel predicate (Decision 14); reaped detection limited to failed exits (Slice 3); the stop cause stored once (Slice 3); the README walkthrough fixed in the gate slice (Slice 6); a cached code-health loader with a patchable path and a loud metric diagnostic (Slice 4); pinned renderer statistics, null handling and tie-breaks (Slice 8); the two HLP limits written into `run-state.md` (Slice 3); surface and fact corrections throughout. Bloat removed on their advice: `code.method` and `files_changed`, the identical-dicts and even/odd-median tests, a duplicate accept-level error test, the `parse_event_ts` test, `test_review.py` from Slice 5's surface, and the per-slice model table (now each slice's `Recommended Developer:` line).

## Next Chat Prompts

### Mode A — Assisted run (default, checkpointed)

```md
Plan file: docs/implementation-plan-pm-model-ledger.md
Slices or batch this session: Batch A (Slices 1–2)

Read the full plan file first. If a selected slice or batch receipt is incomplete or the plan state is unclear, stop and tell me before coding.

Work on the current feature branch for this plan; if none exists, create one and tell me the name.

Use orchestrator as the controlling skill. Act as the Developer: keep implementation, validation, Git operations, and commits local. Use a read-only Reviewer only for investigation, evidence gathering, the hostile drift-audit skill, and an independent code-review skill pass. If no Reviewer is configured or available, perform Developer self-audit and record that provenance explicitly.

For each selected slice or batch, in plan order:
1. Restate the frozen contract (authorized surface + non-goals) from the plan.
2. If any included slice's Risk Flags mark approval-needed, stop and get my approval before coding.
3. apply the scoped-implementation skill against the selected contract.
4. apply the drift-audit skill using a read-only Reviewer when available; otherwise perform Developer self-audit. Report the authorization gate result and who performed it before any quality review.
5. If the gate passes: for a broad or structural change, first run the code-health skill differentially against the slice's starting commit and supply its report as review evidence. Then apply the code-review skill using a read-only Reviewer when available; otherwise perform Developer self-audit through the code-review skill. Record who performed it. If the drift gate fails, fix the drift and re-audit.
6. Surface drift and review findings to me, fix them, then re-run the relevant gate. If consecutive reviews return only minor findings and have clearly converged record residuals in the slice summary and proceed.
7. Ask me before committing. On my approval, commit the selected slice or batch with the commit skill.

After the selected slice(s) or batch are committed, use the handoff skill to record state, audit provenance (Reviewer tool/label or Developer self-audit and fallback context), and the next slice or batch to resume from. Do not continue past the selected scope.

Confirm before starting: plan file read, selected slice(s) or batch, branch, and the first slice. Then begin.
```

### Mode B — Supervised autonomy (intended)

```md
Plan file: docs/implementation-plan-pm-model-ledger.md
Repo: /Users/dcroton/Local/git-repos/ai-agent-coder
Developer: harness claude, model and effort from each slice's `Recommended Developer:` line (pass them with `start-slice --model <id> --effort <level>`)
Reviewer: harness codex model gpt-6.1-sol

Use the project-manager skill. You are the PM: the accountable supervisor of this run — you never write slice code yourself.

Start the run for this plan and repo on the Developer harness above, with the Reviewer harness/model as your default for commissioned reviews — turn it into a wider review panel yourself, per slice, if the risk warrants it. Keep the run token the toolkit gives you to yourself; never pass it to a Developer or Reviewer session.

Then, slice by slice, in plan order:
1. Launch a fresh Developer session scoped to that slice's frozen contract, with the model and effort the slice's `Recommended Developer:` line names.
2. Wait on it with a single long `observe --wait` rather than repeated checks; nudge it only if it genuinely stalls, and otherwise let the session's own signal — result, death, or a dialog marker — end the wait.
3. Assess what it produced against the plan, the diff, and the validation evidence; run lint, investigate differential code-health when structure materially changed, and commission an independent review when risk warrants it (Slices 2–8 are elevated, so both reviews are mandatory there; commission code-review on Slice 1 too). A review blocks until it returns or its timeout kills it; leave it to run rather than watching it.
4. Record your decision: accept, send it back for correction, or stop for a human — whichever the evidence and the plan's gates call for.

Stop the run and tell me whenever the plan or the mechanical floor requires a human decision, rather than making that call yourself.

Confirm before starting: plan file read, Developer and Reviewer harness/model, and the first slice. Then begin.

When every slice is decided, report from the run record: total run time (double check this), what was accepted and on what evidence, what stopped and why, and any residual risk I should know about.
```
