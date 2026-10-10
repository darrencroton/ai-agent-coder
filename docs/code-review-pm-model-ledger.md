# Code Review — PM model ledger (Slices 1–8, commits `4effa7b`…`e8bcd58`)

2026-10-10 · Post-plan review of the Mode B run `20261009T233338Z-b25f5d` that implemented [implementation-plan-pm-model-ledger.md](implementation-plan-pm-model-ledger.md) (the frozen plan) for [PM_MODEL_LEDGER_PLAN.md](PM_MODEL_LEDGER_PLAN.md) (the HLP). Reviewed with the `code-review` skill, with `code-simplifier` as a secondary lens, over the whole branch diff (`68d3d8e..e8bcd58`, 28 files, +5194/−416), the PM's HANDOFF.md, the run notes, and the run report. Supporting evidence: an independent, unanchored review of `ledger.py` against the Slice 7–8 contract (Opus, hand-recomputed fixture statistics), and a full-suite and lint run (Sonnet). Both were verified against the code before anything below was adopted.

## Executive summary

**Verdict: PASS WITH RISKS.** No P0 or P1 findings. The branch delivers every goal of the plan and the HLP: two plan covariates, the `lite-2` schema with structured event data, code metrics at accept, strict panel orders with `criteria_met`/`defects`, a judgment gate on every exit from a submission, a per-run ledger file regenerated after every token-bearing command, and a deterministic leaderboard. The HLP's §5 rules (deterministic, one source per fact, never a silent zero, never blocking, judgment labelled, DRY, KISS) are honoured in the code, not just in the docs. The branch is fit to push and install on the live checkout once the P2 items below are decided.

What stands out as good: the gate and the reaping race were both handled with real care (persist-then-kill, refusal without a write, ratchet consistency); the renderer uses exact `Fraction` arithmetic with explicit half-up rounding; `run_rows` is genuinely pure; `code_block` never raises and names its failures; the suite grew from 437 to 522 tests with behaviour-named tests and a snapshot-equality check on the gate's no-write promise.

Findings that should be acted on before the live install (all P2):

1. **A lost `launch` event wedges a slice** (`slice_ops.py:980`, PM decision D4). The event is appended outside the guarded window after `current_slice` is saved, and the gate then refuses every exit except `pm stop`. Reachable by an ordinary I/O failure. Let `finalize --stop` accept an origin-less submission, or append the event before the state save.
2. **Self-pairs in a panel order credit a win and a loss to the same reviewer identity** (`ledger.py:941–954`). Two commissions of the same `(tool, model, effort, override)` in one panel drag both the pairwise and close-call shares toward 50%, the HLP's "noise" signal. Skip pairs whose members resolve to the same group.
3. **The README walkthrough's cleanup line, `stop --scavenge`, kills every `pm-*` session on the default tmux server.** It is the probable cause of the Slice 7 attempt-0 death. Scope the walkthrough's cleanup to its own run and document `PM_TMUX_SOCKET`.
4. **Lint configuration and code disagree on line length** (88 vs ~120 columns), so the differential `ruff format` check is blind to new findings in every PM file. Align one to the other.

Everything else is P3: hardening of edge cases no writer produces, documentation follow-ups the PM already recorded, test gaps on secondary behaviours, and a set of simplifications (below) that would make the next maintainer's life easier without changing behaviour. The PM's five delegated decisions (D1–D5) are all endorsed; D4 is the one whose recommended follow-up is promoted to P2 above.

## Authorization status

- Each slice was drift-audited by a commissioned reviewer under PM supervision (verdicts recorded in HANDOFF.md's slice log; every final audit PASS). This review did not re-audit authorization and proceeds to quality.
- Note for the record: the human authorised the PM to resolve natural plan defects mid-run. Five such decisions were taken (D1–D5). They are assessed in their own section below.

## Goals check (plan and HLP)

| Goal | Where | Status |
|---|---|---|
| Difficulty and checkbox criteria parsed, required by `check-plan`, stored on slice entries | `plan.py:135–145, 412–415`, `slice_ops.py:377–378` | Met. `check-plan` on the plan itself: 0 errors, the expected batch warning. |
| `pm rate` and `model-performance.md` retired, rubric archived | `cli.py`, `slice_ops.py`, `state.py`, docs | Met. `git grep` for the retired tokens under `skills/` is empty. |
| `lite-2`: event `data`, launch identity, stop causes, `review-failed`, `repo_name`, legacy tolerance removed | `state.py:373–409`, `slice_ops.py:807–816, 973–981, 1584–1591, 1754–1761`, `review.py:506–553`, `git_ops.py:219–226` | Met. The reaped-vs-failed distinction is correct given persist-before-kill (D2). |
| Code metrics at accept, never blocking | `code_metrics.py`, `slice_ops.py:1478–1482`, `cli.py:605–606` | Met. Literal pathspecs, symlink = absent (D3), BOM tolerated, non-UTF-8 names round-trip. |
| `criteria_met`, `defects`, strict `order`/`close`, one shared panel predicate | `judgments.py:136–173, 409–461, 468–541` | Met. Exact-retry matcher compares the new fields; `rank_groups` is refused by name. |
| Judgment gate on accept/steer/stop/relaunch, after budget kill and ratchet, before any write | `slice_ops.py:81–121`, call sites `:817–819, 1438, 1594, 1719` | Met. Snapshot-equality test proves the no-write refusal; ratchet-only persistence tested. |
| Per-run ledger file after every token-bearing command, under the run lock, loud never blocking | `ledger.py:322–361`, `cli.py:839–893` | Met. Structural predicate (`hasattr(args, "run")`) confirmed against every subparser. |
| Deterministic leaderboard with the HLP W5 columns, labelled PM columns, no composite, no clock | `ledger.py:364–1076` | Met. Independent hand computation of every fixture cell agrees with the tests. |
| Docs: SKILL, README, run-state, review-judgments, implementation-plan template, CHANGELOG | diff | Met with the P3 follow-ups listed below. |

## Findings

Ordered by severity. `P0`/`P1`: none.

### P2

1. **[P2] `slice_ops.py:980` — a lost `launch`/`relaunch` event leaves a submission no command can decide (D4).**
   `start_slice` saves `current_slice` inside its guarded block and appends the launch event afterwards. If the append fails (disk full, interrupt, read-only state dir), the slice has a live session and a `current_slice` but no origin event. `current_submission_gaps` reports "no launch, relaunch or steer event", and `judge-developer` cannot record anything either (`_origin_from_current` needs the origin), so `finalize --accept/--steer/--stop` and a relaunch are all refused. Only `pm stop` works, and recovery is a new run attesting accepted slices. The PM documented the exit in SKILL.md (D4) but left the code. Fix direction, in order of preference: (a) in `_require_judged`, treat the "no origin" gap as refusing accept/steer/relaunch but not `finalize --stop`, so the stop record can be written and a fresh launch opens a new window; or (b) append the launch event inside the guarded block before `save_state`, accepting that a failed save then leaves an event without state (the next launch's event is a second `launch`, which `run_rows` already handles as a new window). Add a test that deletes the launch event and asserts `finalize --stop --cause environment` succeeds.

2. **[P2] `ledger.py:941–954` (`_credit_order`) — self-pairs count as a win and a loss for the same reviewer identity.**
   Reviewer groups are keyed by `(tool, model, effort, command_override)`. When PM commissions the same configuration twice to form a panel (a plausible way to get a panel at all, and exactly what a benchmark of one local model would do), every pair between the two copies adds one win and one loss to the same group in both the pairwise loop and the close-call loop. Reproduced by the independent reviewer: three windows of `[codexA, codexB, claude]` with `close=[codexB]`, where codex beats claude every time, render as "Pairwise 9/12 (75%)" and "Close-call 3/6 (50%)". The HLP reads a close-call share near 50% as "those calls are noise", so the distortion points the wrong way. The contract text does not settle self-pairs and no test covers them. Fix: skip a pair in both loops when both members resolve to the same group (keep the `panels` credit), add the test, and state the rule in the README's ledger paragraph. The leaderboard is recomputed from rows, so this changes no stored data.

3. **[P2] `skills/project-manager/README.md:187–196` — the "Verify your setup" walkthrough ends with `stop --scavenge`, which without a run id sweeps every `pm-*` session on the default tmux server.**
   HANDOFF.md records this as the probable cause of the Slice 7 attempt-0 session death: a Developer ran the walkthrough inside a PM run and killed its own session. The walkthrough is the first thing a new operator runs, and nothing in it mentions the blast radius. Fix: end the walkthrough with the trial run's own `stop --reason cleanup` (scoped by run id), add one line exporting `PM_TMUX_SOCKET=pm-trial` beside the `PM_LEDGER_DIR` export, and keep `stop --scavenge` documented only in the CLI table with its "every pm-* session" caveat.

4. **[P2] `skills/lint/config/ruff.toml` vs `pm_lib` — line-length mismatch blinds the differential format check.**
   The lint skill formats at ruff's default 88 columns while `pm_lib` and its tests are written at ~120, so every touched PM file carries pre-existing format findings and the file-level differential check reports new ones as "not new". The run worked around it by hand (`ruff format --diff` per hunk), which is exactly the manual step the lint skill exists to remove. Fix: either add a `[format] line-length = 120` (and `[lint] line-length`) to the skill's default config or a project-level `ruff.toml` for this repository, or reformat `pm_lib` to 88. The first is cheaper and matches the code as written.

### P3

5. **[P3] `slice_ops.py:1800–1808` (`_reap_reviewers`) — kills the union of the caller's snapshot and the on-disk set, so a pgid the reviewer already deregistered can be SIGKILLed after PID reuse.**
   The on-disk set under the lock is authoritative: a reviewer that registered is there, and one that deregistered is gone. The snapshot adds only pgids that have exited (or that a lost write erased, which the plan accepts). The hazard pre-exists the slice and the window is small, but the fix is a simplification: kill only `locked_current["reviewer_pids"]`. Capped at P3 because reaching it needs a reviewer to exit and the OS to reuse its pgid inside the caller's own window.

6. **[P3] `slice_ops.py:1851` (`stop`) — the `if current.get("reviewer_pids")` guard skips the reap when the snapshot is empty, so a reviewer that registered after `stop` loaded state keeps running against a stopped run.**
   The guard's stated purpose (not adding an empty key to a `current_slice` that never carried one) is moot under `lite-2`, where every launch writes `reviewer_pids: []`, and `_reap_reviewers` now writes the key under the lock anyway. Drop the guard; the helper handles an empty snapshot.

7. **[P3] `judgments.py:230–238` (`assessment_of`) — `value not in {…}` raises `TypeError`, not `PmError`, on an unhashable stored value.**
   Unreachable with a valid MAC. One-line guard: `if not isinstance(value, str) or value not in …`.

8. **[P3] `judgments.py:70–100` — an unavailable *comparison* naming a single `review_id` is accepted.**
   It can never cover a panel (panels have two or more members), yet it holds active comparison coverage over that id, so a later `order` naming it is refused as overlapping until superseded. Require two or more ids for an unavailable comparison, matching the `order` minimum.

9. **[P3] `ledger.py:63–65` (`host_name`) — an empty first label (hostname `""` or `.local`) writes the file to `ledger_dir()/run.json`, which `load_run_files` never reads.**
   Fall back to a fixed folder name such as `unknown-host`.

10. **[P3] `ledger.py:390–405` (`load_run_files`) — a dangling symlink or an unreadable host directory is skipped silently rather than listed under Errors.**
    `is_file()` is false for a dangling link, and pathlib's glob swallows the `OSError` from an unreadable directory. Iterate `root.iterdir()` and record both cases as `(label, None)`.

11. **[P3] `ledger.py:529–533` (`_number`) — `float(Fraction)` overflows above ~1e308 and loses the `.5` above 2^53.**
    Only a hand-edited file reaches it (every integer here is a count or a second). A median of ints has denominator 1 or 2, so format it exactly (`f"{n // 2}.5"`).

12. **[P3] `ledger.py:582–584, 1057–1061` — difficulty bands and group keys that differ only in type (`1` vs `"1"`) collide on the sort key and label, and the band tie is broken by set-iteration order.**
    The writer never emits these; a hand-edited file could. Either fold the type name into `_key_part` or have `_row_shape_error` require `str | None` for `difficulty` and the identity fields.

13. **[P3] `git_ops.py:219–226` (`git_common_dir_name`) — Decision 3's algorithm names `modules` for a submodule checkout and an arbitrary directory under `--separate-git-dir` or `GIT_DIR`; `--path-format=absolute` needs git ≥ 2.31, which is undocumented.**
    The ledger groups nothing by `repo_name` today (it is a covariate), so this is a labelling risk, not a statistics one. Document the git minimum in the README and note the two layouts in `run-state.md`'s `repo_name` bullet.

14. **[P3] `code_metrics.py:149–160` — binary files are line-counted as text, as the contract specifies.**
    A slice that adds a PNG or a wheel will show a large `data`/`other` line delta. The contract chose this; the ledger may later want `git diff --numstat` `-` markers to exclude binaries. Record as a known limit in `run-state.md`'s code-metrics bullet.

15. **[P3] Row outcome for `pm stop --slice-status stopped` is `open` with no cause, forever.**
    `stop` writes a `stop` event with no `data`, and `run_rows` derives `stopped` only from `slice-stop`. A slice ended this way never resumes, is left out of Developer tables (lenient to the Developer), and carries no cause. This is a gap in the HLP §3.2 table rather than an implementation defect; decide whether `--slice-status stopped` should carry a `--cause` or be treated as `stopped`/`null`.

16. **[P3] Test gaps worth closing (none protect a found defect).**
    Secondary Developer sort key (tie on PM mean, different first-submission share); drift-audit sort by PM mean across two groups and a code-review group with no panels sorting last; a `.5` median and a negative Δ-lines median; render-side exclusion of `complexity: null`; an `order` only partly inside a window being dropped; `load_run_files` depth (top-level and two-levels-down files excluded); self-pairs (finding 2); the accept-path reap through a real `pm review` child (today only the `pm stop` path proves it); `_kill_reviewer_pgid` asserting the persisted list is already `[]` when called.

17. **[P3] Documentation follow-ups.**
    - README `start-slice` row (`README.md:33`) lacks the full-model-id sentence that the `init` and `review` rows carry (D1).
    - `run-state.md:91` should say a symlink on either side counts as an absent file (D3) and that binaries are counted as text (finding 14).
    - README's Ledger section should say `PM_LEDGER_DIR` must be an absolute path outside the supervised repository; a relative or in-repo value writes into the worktree and can trip the clean-worktree floor fact.
    - `review-judgments.md:129` "survives `git worktree remove`" holds only when the ledger directory is outside the removed checkout; qualify it.
    - Plan text (not code): Slice 4's "contents from `git show <rev>:<path>`" should read "the file's text, or empty if no file exists at that path on that side" (D3); the `implementation-plan` SKILL.md named `criteria_met` before Slice 5 defined it (harmless now).
    - `_review_ids_without_coverage`'s `skill` parameter is dead since `unranked_code_review_ids` was redefined; remove it.
    - A wrong-token command now prints a second stderr line from the hook (`pm: ledger: not written: PmError: …`). Allowed by Decision 9; consider suppressing the hook when the handler itself failed on the token.

## Contract defects (plan text)

- Slice 4: "before and after contents from `git show <rev>:<path>`" conflicts with "the methods are code-health's" (which skips symlinks) and mis-reads a directory/file replacement and `:`-prefixed names. The implementation's reading (literal `ls-tree` lookup, blob text or empty) is the coherent one. D3 resolved it correctly.
- Slice 8: `render_leaderboard(runs) -> tuple[str, list[str]]` cannot write a History link "relative to the output's directory" with one argument. D5's optional `out_dir` keyword is the coherent reading.
- Slice 3: "reaped" was defined against persisted `reviewer_pids`, but every reaping path cleared them only in the caller's later unlocked save, so the check raced. D2's persist-before-kill is the only reading that makes the rule true.
- Slice 6 / Decision 13: a missing origin is a gate gap and `judge-developer` cannot fill it, but `start_slice` can lose the event. D4 named it; finding 1 is the code fix.
- Slice 2: "the README `--model` rows" (three) versus the Intended Change's explicit two. D1 chose the explicit list; the third row is finding 17's first bullet.

## PM delegated decisions (D1–D5)

| Decision | Assessment |
|---|---|
| D1 — `start-slice` README row left without the model-id sentence | Correct reading of the frozen text; the omission is a one-line follow-up (finding 17). Endorsed. |
| D2 — `_reap_reviewers` persists cleared pids under the lock before `killpg`, kills the union | Endorsed; it is the only way the plan's reaped rule can hold. The union part is where finding 5 suggests killing only the on-disk set. The regression test pins the union; adjust it with the change. |
| D3 — symlink = absent file side; literal `ls-tree` + `cat-file` instead of `git show` | Endorsed. This matches code-health, which is the stated method, and closes two real misreads (tree listing as text, pathspec parsing). Tests cover each case from git objects so none can skip. |
| D4 — document the lost-launch exit in SKILL.md, no code change | Endorsed as a slice-local call; the code fix is promoted to P2 here because the failure is reachable and the recovery cost (a new run) is high. |
| D5 — `render_leaderboard(runs, out_dir=None)` | Endorsed. The CLI passes `out.parent.resolve()`, so a symlinked output path links relative to the directory it was named in; tested. |

The decisions were slice-local or narrow, each was recorded in three places, and none waived a floor fact or widened a surface without `grant`. The delegation worked as intended.

## HANDOFF residuals: dispositions

Each residual the PM listed, with what this review recommends.

| Residual (origin) | Disposition |
|---|---|
| Post-reap save window (Slice 3, codex P2) | Accept as the plan's documented lost-write class. Add one sentence to `run-state.md`'s known limits that it also hides `review-failed` telemetry. Decision-commit / review-admission coordination belongs to a future plan. |
| `stop()` empty-snapshot guard | Fix (finding 6). |
| Kill only the on-disk set | Fix (finding 5). |
| Pin persist-before-kill in the unit test | Fix with finding 5 (finding 16). |
| `_reap_reviewers` lock timeout after irreversible steps | Accept. The lock is held briefly by every other holder; a retried finalize completes the decision, as the docstring says. Do not reorder reaping ahead of the assessment write: a reap that then fails would strand the reviewers the assessment already assumed gone. |
| Accept-path reap not tested through a real `pm review` child | Add the test (finding 16). |
| `assessment_of` hashability | Fix (finding 7). |
| `repo_name` under submodules / `GIT_DIR`; git ≥ 2.31 | Document (finding 13). |
| Test placement (`TestReapReviewersUnionsPersistedPids` under the wrong banner; strict `assessment_of` test in `test_state.py`; blank line) | Move with the next edit to those files; no separate commit needed. |
| Symlinks-absent note in `run-state.md`; binaries as text; `code_metrics` coupling to `health.py` internals | Document the first two (findings 14, 17). The coupling is deliberate (Decision 8) and degrades loudly to `{error}`; add one line to the `code-health` skill's README asking that the PM suite be run when `SourceFile`, `python_structure` or `category_for` change. |
| Unavailable comparison naming one review | Fix (finding 8). |
| `review-judgments.md` panel definition should add "and have no active unavailable rating" | Add; it matches `panel_groups`. |
| Dead `skill` parameter | Remove (finding 17). |
| Run report's Developer-judgment section does not print `criteria_met`/`defects` | Add them to `_render_developer_judgments`; the report is the human-facing artifact and these are the two new facts PM records. Small, worth doing. |
| D4 code follow-up | Finding 1. |
| Stale-head check applies to `unavailable` Developer judgments | Accept; supersession is the documented remedy and the case is rare (a dead session whose head then moves). |
| `start-slice --risk elevated` refused relaunch leaves `current_slice["risk"]` at `standard` | Accept; `new_current` is rebuilt from the entry on the next launch and nothing reads the stale value. |
| `PM_LEDGER_DIR` must be absolute and outside the repo | Document (finding 17). |
| Empty `gethostname()` | Fix (finding 9). |
| "survives `git worktree remove`" qualification | Document (finding 17). |
| Second stderr line on a wrong token | Accept, or suppress the hook when the handler failed on the token (finding 17). |
| Self-pairs | Fix (finding 2). |
| `Difficulty: unknown` table for null/off-list rows | Accept; it is honest output for a hand-edited file and never appears for files the writer produced. |
| Interpreter-dependent `RecursionError` text in Errors | Accept; fix only if a cross-Python byte-identical Errors section is ever required (use a fixed message then). |
| A run whose top-`events` copies conflict is hidden entirely | Accept; contract-literal and loud (both copies named under Errors). Falling back to an older copy would silently render stale rows. |
| Untested secondary sort key, drift sort, `PM_LEDGER_DIR` cannot hide the History link | Add (finding 16). |
| Median Δ code lines per kind separately | Accept and document in the README's ledger paragraph; the alternative (median of a tuple) has no meaning. |
| Plan-template observations (`_risk_flag` unanchored substring; `criteria_total` counts code-fence and placeholder checkboxes) | Record as `implementation-plan` skill follow-ups: anchor `_risk_flag` to line start, and strip fenced blocks before counting. Neither affected this run. |
| README walkthrough `stop --scavenge` | Fix (finding 3). |
| Lint line length | Fix (finding 4). |
| Reviewer sandboxes cannot run the suite | Operational; PM runs it. Note it in the PM SKILL step 3 so a future PM does not read a reviewer's "could not run tests" as a finding. |
| Environment-dependent `TestGitCommonDirName` cleanup | Already fixed in attempt 2; the lesson is in the run notes. |

## Simplification recommendations (behaviour-preserving)

None of these change outputs; each is a candidate for one small cleanup slice. Ordered by value.

1. **Promote the shared judgment helpers to public names.** `ledger.py` and `slice_ops.py` now import `judgments._active_judgments`, `_active_developer_judgments`, `_developer_origin_index`, `_referenced_ids` and `_read_events_or_raise`. Five underscore-prefixed names used across three modules are a public API in all but spelling; rename them (drop the underscore) so the dependency is honest and a later rename cannot silently break the ledger.
2. **One panel-coverage predicate.** `unranked_code_review_ids` (`judgments.py:463`) and `current_submission_gaps` (`:793`) both build `covers` from active comparison records and test `any(set(group) <= covered …)`. Extract `panel_is_covered(group, entry)` so `status`, the report and the gate cannot drift apart, which was Decision 14's whole point.
3. **One "active Developer judgment for an origin" lookup.** `ledger._developer_judgment` (`ledger.py:92`) and the gate's `next(…)` (`judgments.py:752–760`) do the same scan; one `active_developer_judgment(entry, origin)` in `judgments.py` serves both.
4. **Collapse `_require_judged`'s two ratchet-save branches** (`slice_ops.py:106–121`) into one: compute gaps and raise inside a `try`, and in a single `except PmError:` persist when `ratcheted`, then re-raise. Same behaviour, one path.
5. **Use `decimal` for half-up rounding.** `_two_decimals` (`ledger.py:554–558`) and `_share_cell`'s `int(percent + ½)` hand-roll ROUND_HALF_UP; `Decimal(value.numerator) / Decimal(value.denominator)` with `quantize(Decimal("0.01"), ROUND_HALF_UP)` is the stdlib statement of the same rule and also fixes finding 11's `_number`.
6. **Split the renderer out of `ledger.py`.** The module is 1076 lines holding two concerns with a banner between them; the README maintainer map describes `ledger` only as "per-window rows and the per-run ledger file". A `leaderboard.py` (load, select, statistics, tables, document) keeps each module to one job and lets Slice 8's contract be read in one file.
7. **Split `_developer_groups`** (`ledger.py:711–782`) into a submission-level accumulator and a window-level accumulator; the function currently interleaves both with a `continue` in the middle, which is the hardest part of the renderer to read.
8. **`_parse_developer_input` defects check** (`judgments.py:516–528`): `if set(defects) != set(_DEFECT_KEYS): raise PmError(f"… (got {sorted(map(str, defects))})")` replaces the missing/extra detail builder with one line and an equally precise message.
9. **`_reap_reviewers`**: with finding 5, the snapshot merge loop disappears and the function becomes "clear under lock, kill what was cleared".
10. **`stop()` guard** (finding 6): removing it deletes a comment that explains a case `lite-2` cannot produce.

## Open questions / assumptions

- The leaderboard's treatment of self-pairs (finding 2) and of `pm stop --slice-status stopped` windows (finding 15) are contract decisions; this review recommends a direction but the human owns the call.
- Drift audits were performed by commissioned reviewers under PM supervision and are taken as given.
- The live toolkit (`~/.claude/skills/project-manager`) still runs the pre-ledger code; nothing here was exercised against a real `lite-2` run beyond the suite's fake harnesses. The plan's operator task 4 (a small real smoke run) remains the first real exercise.

## Model and run tags (new feature raised during Slice 1; not part of this plan)

Direction for the follow-up plan, so it can be written against the code as it now stands:

- **Model tag**: a free-text `--tag` on `start-slice` and `review`, stored in `current_slice["developer"]` and on the review record (so it rides into `data.developer` on the launch event and into the row's `developer` and `reviews[].tag` without any new ledger plumbing). Grouping key becomes `(tool, model, effort, tag)` for Developers and `(tool, model, effort, command_override, tag)` for Reviewers, with `tag` null when absent, so existing rows keep their groups. This is the smallest change that separates two configurations sharing a model string (the local-LLM-at-two-temperatures case).
- **Run tag**: `init --tag` stored in signed state and copied once into the per-run file's top level (`{run_id, tag, events, rows}`), never per row. `ledger render --tag T [--tag U …]` keeps runs whose tag is in the set; no tag filter keeps every run. Tag groups (several tags as one) can be expressed by repeating `--tag`, which avoids a configuration file (HLP §5 KISS).
- Both are additive to the file format, and `_row_shape_error` already tolerates unknown keys.

## Coverage summary

- **Scope reviewed:** every file in `68d3d8e..e8bcd58` (production, tests, docs, `.gitignore`, CHANGELOG), the frozen plan, the HLP (§3–§5, §8), HANDOFF.md, the run notes and run report.
- **Requirements checked against:** the plan's acceptance criteria per slice; the HLP §5 rules; Decisions 3, 5, 8, 9, 12–15.
- **Dimensions checked:** requirements fit, functional correctness, boundary and malformed input, state and lock discipline, concurrency (reaping race, lost writes, hook under lock), interfaces (event shapes, row shapes, judgment inputs), numerical exactness and rounding, error handling, tests, observability, portability (git version, hostnames, symlinks), maintainability, documentation.
- **Validation run:** `check-plan` against the plan (0 errors); `git grep` for retired tokens and legacy phrases (empty); `git check-ignore` for the two ledger ignore rules (both match, the historical table is not ignored); independent re-derivation of every leaderboard fixture cell; the full suite and lint (results below).

## Validation results

Run at `e8bcd58` on macOS (Darwin 27.0.0), Python 3.14.7, git 2.54.0.

| Check | Command | Result |
|---|---|---|
| Full PM suite | `cd skills/project-manager/tests && python3 -m unittest discover -s . -p 'test_*.py' -v` | 522 tests, OK, 0 failures, 0 errors, 0 skips, 487.8 s, exit 0 |
| Ledger module alone (independent reviewer) | `python3 -m unittest test_ledger -q` | 32 tests, OK |
| Differential lint | `python3 skills/lint/scripts/lint.py check --base 68d3d8e` | verdict `pass`, no new findings; ruff-check 0, markdownlint 0, codespell 0; ruff-format 15 findings, all pre-existing (finding 4 explains why the differential check cannot see new ones in these files) |
| Ruff check, skill config | `ruff check --config skills/lint/config/ruff.toml skills/project-manager/scripts/pm_lib skills/project-manager/tests` | All checks passed |
| Retired tokens | `git grep -n "pm rate\|model-performance\|performance-rubric" -- skills` | empty |
| Legacy phrases | `git grep -n "historical/unjudged\|lite-1" -- skills/project-manager ':!skills/project-manager/tests'` | empty |
| Deleted judgment phrases | `git grep -i "singleton\|allowing ties\|chosen panel\|the panel you chose\|outside recorded panels\|acceptance gates" -- skills/project-manager` | only the two intentional `rank_groups` refusal mentions |
| Plan self-check | `pm.py check-plan --plan docs/implementation-plan-pm-model-ledger.md --repo .` | 8 slices, 0 errors, the expected batch warning |
| Ignore rules | `git check-ignore -v` on a host file, `leaderboard.md`, `historical-pre-ledger.md` | first two ignored by the new rules; the historical table is tracked |
| Worktree | `git status --porcelain` | clean |

Not run: a real `lite-2` run on the live checkout (operator task 4); a cross-Python render of a pathological file (finding 17's `RecursionError` note).

## Recommended next steps

1. One follow-up slice for the four P2 items (findings 1–4) and the three one-line P3 fixes that sit beside them (findings 5–7), with the tests in finding 16 that pin them.
2. One documentation pass for finding 17 and the HANDOFF dispositions marked "document".
3. The simplification items 1–5 as a `code-simplifier` slice after the above, run with the full suite as the unchanged-behaviour check.
4. Then the plan's operator tasks: historical table, bench freeze, live install, first `lite-2` smoke run.
5. A new implementation plan for model and run tags, using the direction above.
