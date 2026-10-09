# PM model ledger: high-level plan

2026-10-10 · Follows the PM model ledger decision brief (2026-10-09, kept in the operator's notes, outside this repo) and the decisions in its §12. This plan is the input for a frozen implementation plan. It fixes *what* is built and the rules it must obey, and leaves exact code shapes to that plan. It was reviewed by an independent panel and then simplified; see §10.

---

## 1. Goal

Every PM-directed run, in any repo and on either machine, leaves a deterministic, structured account of how each Developer and Reviewer performed on each slice. A manual command turns those accounts into one leaderboard that informs model choice.

- **Nothing to remember:** neither PM nor the operator has to remember anything for the data to be captured.
- **No free text:** no metric is calculated from free text.
- **No new copies:** apart from the Developer identity on launch events (needed for submissions PM never judged), nothing is stored that PM's state or event log already holds.

**Done looks like:**

- **PM can't skip the inputs:** it cannot move past a Developer submission until the judgments the leaderboard needs exist.
- **Each run keeps its own ledger file:** a file derived from the run's state is kept current after every relevant PM command, with no extra step by PM.
- **The leaderboard is reproducible:** `pm ledger render` builds it from those files alone, byte-identical for identical inputs.
- **The old rating is retired:** `pm rate` and `model-performance.md` are gone.
- **The bench is frozen:** a static historical table preserves what the 45 judged runs (14 Sep onwards) already showed.

## 2. Decisions adopted

| # | Topic | Choice | Consequence for this plan |
|---|---|---|---|
| 1 | Measurement location | **Hybrid**: PM captures structured data at the source; a reader aggregates | PM gains a gate, a few counts and a row writer; no external log scraping |
| 2 | Packaging | **Inside PM** | A `pm_lib/ledger.py` module and one `pm ledger render` subcommand |
| 3 | Trigger | **Recording automatic, leaderboard manual** | Ledger files are rewritten inside PM commands; render is run by hand |
| 4 | Storage | **Gitignored directory in the PM skill dir**, one folder per host | `skills/project-manager/ledger/<host>/<run_id>.json`; cross-host merge by copying the other host's folder in |
| 5 | History | **Start fresh, plus a frozen historical table** | No legacy readers in PM; a one-off static table from the archived census |
| 6 | PM changes | Brief §7 items **1–5 and 7**; not 6 | No session ids, no tokens. KISS, DRY, deterministic, robust |
| 7 | 1–5 end-of-run block | **Retire** | `pm rate`, `model-performance.md` and its rubric are removed (rubric archived) |
| 8 | Reviewer ranking | **Strict order with close-call flags, no randomisation** | Ranking ties are replaced by an order plus close flags |
| 9 | Plan difficulty | **Required, three levels: easy / moderate / hard**; recommended model/effort optional | New machine-consumed line in the plan template; `check-plan` refuses a slice without it |
| 10 | Tokens/cost | **Never** | Out of scope permanently |
| 11 | Old bench | **Freeze** `ai-agent-bench` | README note only; no further maintenance |

## 3. Design

### 3.1 Principle: derive, don't copy

**Already recorded.** PM's signed `run.json` and its append-only `events.jsonl` already hold almost everything the leaderboard needs:

- judgments, with the identity and commit of each judged submission;
- reviews, with tool, model, effort and commit;
- launches, relaunches, steers, nudges, floor checks and decisions, each with a timestamp.

The ledger therefore stores no second copy of any of this. The one deliberate exception is the Developer identity on launch events. Judgments already snapshot it, but a submission that ends without a judgment (e.g. one paused by `pm stop`) would otherwise have none, so launch data is the single source of identity for rows.

**Three small additions.** The plan adds only what is genuinely missing, each written once, at the moment it is known:

| Fact | Written where | When |
|---|---|---|
| Developer identity per launch | `data` on the `launch`/`relaunch` event | `start-slice` |
| Stop cause; failed review commissions | `data` on the `slice-stop`/`stop` event; a new `review-failed` event | `finalize --stop`, budget exhaustion, `review` |
| Code metrics (ΔLOC, complexity) | `code` on the signed slice entry, beside `commit` | `finalize --accept`, while git is guaranteed present |

**The row writer.** A pure function `run_rows(run.json, events.jsonl)` turns one run into leaderboard rows. Its output is the whole run's rows plus the number of events it was built from, written to `ledger/<host>/<run_id>.json` with sorted keys.

- **When it runs:** after every token-bearing PM command, from a single post-command call in `cli`, not a hand-maintained list. That covers every decision, judgment, `review`, `start-slice` (including its budget-exhaustion stop) and `pm stop`.
- **Verified state only:** it runs only when the state load was MAC-verified. A tokenless `status --report` never writes.
- **Concurrency:** it reads state and events and writes the file while holding the run's existing `.lock`, using `state._atomic_write_bytes` (a unique temporary file, then rename). Commands that overlap, such as a judgment during a running review, can't leave an older snapshot in place.
- **On failure:** it prints a loud error, and never blocks or undoes the command that triggered it. The next write heals it. `status --report` (the finish step) also exits non-zero naming the failure, so a run whose writes all failed, e.g. because of an unwritable directory, is noticed before its worktree is removed.
- **Late changes flow through:** a resumed run, a late reviewer rating or a judgment correction is reflected in the next write.

**Why a copy outside the repo is still needed.** PM state for a linked worktree lives under `.git/worktrees/<name>/pm` and is deleted by `git worktree remove`. Repos also get deleted. The per-run file is the durable copy. It is regenerated, not appended. So it needs no new lock (it reuses the run's lock), no append log and no repair logic.

### 3.2 Unit: per slice, per submission, never per run

Real plans assign different Developers to different slices by difficulty, and PM can switch models on a relaunch. Every calculation therefore works per slice and per submission. The run's default `harness` is never used for attribution.

- **Submission:** one origin event, which is a `launch`, `relaunch` or `steer`. Its Developer identity comes from the `data` on the latest launch or relaunch event at or before that origin. A steer keeps the identity of the session it steers.
- **Slice window:** all submissions of one slice from its `launch` event to its decision.
  - The window opens on a slice's first launch in the run, or on a fresh launch after `finalize --stop`.
  - Relaunches and steers stay inside the window.
- **Window outcome** is derived from events:

  | Outcome | When |
  |---|---|
  | `accepted` | `accept` |
  | `exhausted` | the window contains a budget-exhaustion `stop` event (marked by its `data`, never by note text) and no `accept`. This holds whatever cause a later `finalize --stop` gives, so exhaustion always counts against the Developer |
  | `stopped` with a cause | `slice-stop` |
  | `open` | anything else, e.g. paused by `pm stop` or still running |

  Precedence, top to bottom: `accepted`, `exhausted`, `stopped`, `open`.

- **Row key:** `(run_id, slice_id, window_open_event_index)`. Run ids are already globally unique (timestamp plus random nonce).
- **Attested slices** produce no row.

## 4. Workstreams

Sizes: **S** a few dozen lines plus tests, **M** a focused change across a few `pm_lib` modules.

### W1. Plan covariates (S)

**implementation-plan skill:**

- **Difficulty line (required):** add `Difficulty: easy | moderate | hard` to the slice template's `Risk Flags` block, documented under "Machine-Consumed Fields".
- **Recommended Developer line (optional):** a free-text line for a recommended model or effort, for the operator and PM to read. PM never parses it.
- **Anchors.** Rate the slice, never the model, and set it before any attempt:
  - **easy**: a tightly specified or mechanical change, typically in one module; follows an existing pattern; no new interfaces; little room for a wrong reading.
  - **moderate**: new behaviour across a few files within the existing design; some judgement inside the contract; ordinary test work.
  - **hard**: cross-cutting, or new interfaces or algorithms; subtle state, concurrency, numerics, persistence or schema; a wrong reading is likely or costly.
- **Checkbox criteria (required):** the skill already asks for `- [ ]` checklist criteria. They become required, because they are the denominator of `criteria_met`.

**PM:**

- `PlanSlice.difficulty` reads its line through the existing `_risk_flag` helper.
- `criteria_total` counts the `- [ ]`/`- [x]` lines in `Acceptance Criteria`.
- `check-plan` reports a missing or unrecognised difficulty, or zero checkbox criteria, as an **error**, so `init` refuses the plan.
- `init` stores `difficulty` and `criteria_total` in each signed slice entry, beside `plan_risk`, following the same pattern.

### W2. Event data and code metrics (S–M)

- **Schema `lite-2`.** `_validate_shape` already refuses any other schema, so only `SCHEMA` changes. Code that only tolerates older records is removed: `assessment_of`'s fallback, `historical_review_count` and its callers, and the missing-`grants_seen` default.
- **Event `data`.** `append_event` gains an optional `data` object, serialised with the event.
- **Launch identity.** `launch` and `relaunch` events carry `data.developer = {tool, model, effort}`. That is the same value already set in `current_slice.developer`.
- **Stop cause:**
  - `finalize --stop` takes a required `--cause plan|developer|environment`, carried on its `slice-stop` event.
  - Both budget-exhaustion paths (relaunch and steer) put `cause: developer` on their `stop` event.
  - A plain `pm stop` needs nothing: its window stays `open`.
- **Failed commissions.** `review` writes a `review-failed` event, with `data = {skill, tool, model, effort, command_override, origin_event_index, reason}`, when either:
  - the reviewer timed out. This replaces today's free-text timeout `review` event rather than adding a second one;
  - it exited non-zero (today this leaves nothing).

  A reviewer that PM reaped is not a failure and is not recorded. It is recognised at the existing state reload after the wait, before `review` removes its own process group: the group is already missing from `current_slice.reviewer_pids`, because `_reap_reviewers` clears that list on every reaping path. A crash or an out-of-memory kill is still recorded. An empty report is not a failure either, because the gate already makes PM rate it or mark it `unavailable`.
- **Code metrics at accept.** `finalize --accept` computes a `code` block over `before_head..commit` and stores it on the signed slice entry beside `commit`:
  - **Files and line changes.** File list from `git diff --no-renames -z --name-only`; pinned options make it independent of the user's git config. For each touched file, the net line change between before and after uses code-health's `language_for`, `category_for` and `line_counts`. That gives its six categories (production, test, documentation, configuration, data, other) by its lexical kinds (code, comment, blank).
  - **Complexity.** Python function cyclomatic complexity, as sum and max over the touched Python files before and after, from code-health's AST collector. A parse error makes the figure `null` with a reason, never a partial sum. Lizard is never used, so the result is deterministic.
  - **Unborn branch.** When `before_head` is absent, the diff runs against git's empty-tree object.
  - **Finding code-health.** `health.py` is found from `pm.py`'s real path, like the ledger directory. It is imported inside the block's error handling, so a missing code-health gives `{error}`. It is stdlib-only and safe to import.
  - **Failures.** A metric that can't be computed is `null` with a named reason. The block is computed after the floor and the gate. Any exception becomes `{error: reason}`, so it never blocks the accept.

### W3. Judgment changes (S)

In `judgments.py` and `references/review-judgments.md`. The 0–2 scale is unchanged.

- **Developer judgment** (score records; `unavailable` records are exempt) gains two fields:
  - `criteria_met`: how many of the slice's checkbox criteria this submission met, `0 ≤ criteria_met ≤ criteria_total`.
  - `defects`: distinct defects in this submission that PM confirmed, by severity `{P0, P1, P2, P3}`. This covers code and scope defects, whether PM found them or confirmed a reviewer's finding.
- **Panel comparison.** The ranking with ties is replaced by:
  - `order`: every panel member exactly once, best first;
  - `close`: the members too close to call against the one ranked immediately above.

  Comparability stays exactly the existing `_context_key`, and reports are not shuffled.
- **Unavailable records** must state their `assessment` (`rating` or `comparison`). The current default for code-review unavailable input, which is comparison, is removed, so the gate's checks are unambiguous.
- The reference doc is rewritten for these inputs, with one worked example each.

### W4. Judgment gate (S–M)

**Covered commands:** `finalize --accept`, `--steer` and `--stop`, and a `start-slice` relaunch of a live slice. A relaunch includes resuming after `pm stop`, where the paused submission is typically judged `unavailable`. Each refuses, naming every gap, unless the **current submission** has:

1. **A Developer judgment.** It may be a score, or `unavailable` (e.g. for a session that died), and its recorded `head` and `grants_seen` must match the current submission.
2. **An active rating-kind record (a score or `unavailable`) for every intact review** whose origin is the current submission.
3. **A strict order for every panel.** A panel is two or more intact code-review reports on the current submission that share one `_context_key` and lack an active `unavailable` rating. Either the order includes every member, so PM no longer chooses which reports form a panel, or one `unavailable` comparison covers the whole panel.

**What the gate leaves alone:**

- **Earlier submissions.** Gaps there stay informational in `status`. Only the current submission can still be judged, so gating on them would trap the run.
- **The run-wide gap lists.** The gate is a new helper scoped to the current origin; it doesn't use those lists as predicates.
- **The budget kill.** In `--steer` and relaunch, the existing budget check and its mandatory session kill run *before* the gate. A missing judgment never keeps a session alive past its budget.
- **`pm stop`.** It stays ungated as the emergency exit.

**How PM can catch up:**

- **Reviewer ratings** can still be recorded at any time, including after the slice is decided, and they reach the ledger at the next write.
- **A Developer judgment** can be corrected through supersession. Every other exit from a submission is gated, including a resume. So a Developer judgment can only be missing for a submission ended by a `pm stop` that was never resumed.
- **Docs that contradict the gate and the strict order** are rewritten:
  - SKILL.md's judgment section ("allowing ties", "never … acceptance gates", "the panel you chose", the `rate` narrative);
  - `references/review-judgments.md` (its tie, chosen-panel and historical-coverage passages);
  - `references/run-state.md` ("Judgments do not change … acceptance rules").

  W2's removal of legacy code also covers the historical/unjudged messages in `judgments.py`.

### W5. Ledger rows and leaderboard (M)

**Location:** `skills/project-manager/ledger/`, gitignored, resolved from `pm.py`'s real path so every harness's symlink reaches the same directory. `PM_LEDGER_DIR` overrides it. Test helpers and the README walkthrough always set it, so fake-harness and trial runs never reach the real ledger.

**Rows: `run_rows`** emits one row per window. It is pure over `run.json` and `events.jsonl`, with no git and no plan file. Each row holds:

- **Covariates:** repo name, run id, slice id, window open index, difficulty, criteria total. The host is the folder name, not a row field, so the same run copied between hosts gives identical rows.
- **Outcome:** outcome and cause, plus `decided_at` (the timestamp of the decision event).
- **Window figures:**
  - wall time `elapsed_s`, from window open to decision (decided windows only);
  - counts of steers, relaunches and nudges (`send` events);
  - the `code` block, accepted windows only.
- **Submissions,** one per origin. Each holds:
  - its identity;
  - its judgment: score or `unavailable`, plus `criteria_met` and `defects`;
  - `developer_s`: the time from the origin to the next `floor`, origin, `stop` or decision event. That is Developer working time, without review, PM or pause time.
- **Reviews,** one per review recorded in signed state (intactness is a judge-time and gate-time check, not a row filter):
  - review id, skill, tool, model, effort, `command_override` and origin index;
  - its rating or `unavailable`.

  The window's `review-failed` events are included.
- **Comparisons:** each active one as `{origin index, order, close}`.

Timestamp parsing reuses `run_elapsed`'s rules, split into a helper.

**Render: `pm ledger render [--out PATH]`**

- **Reads** every `ledger/*/*.json`.
- **Duplicates:** if the same run appears in two folders, the copy built from more events wins, and identical copies collapse. Differing copies with the same event count, and malformed files, are listed in an "errors" section and excluded.
- **Output:** `leaderboard.md`, by default in the ledger directory.
- **Deterministic:** rows are sorted and there is no wall-clock stamp. "As of" is the latest `decided_at`.

**Developer tables.** Rows are grouped by exact `(tool, model, effort)`, overall and per difficulty band.

- **Which windows count:** `accepted`, `stopped` with cause `developer`, and `exhausted`. Windows that are `open` or stopped for `plan` or `environment` are excluded from Developer columns, but still count for reviewers.
- **Which submissions count:** only scored submissions. An `unavailable` submission (operational: a paused or dead session with nothing to assess) is left out of every Developer column, including "first submission" and submission counts, so a pause or crash isn't charged to the model.
- **Submission-level columns,** credited to each submission's own identity:
  - PM 0–2 mean;
  - criteria met on the first submission (Σk/Σn);
  - first-submission defects, P0–P1 and P2–P3;
  - median Developer time per submission.
- **Window-level columns,** credited only when every submission in the window shares one identity. Mixed windows are left out and their count is footnoted:
  - windows (n);
  - accepted share;
  - accepted on first submission;
  - submissions per window;
  - steers and nudges;
  - median wall time;
  - median ΔLOC by category and kind;
  - median Δ complexity.
- **Sort:** PM mean, then first-submission acceptance.

**Reviewer tables.** One table per skill, grouped by `(tool, model, effort, command_override)`:

- commissions (n);
- usable share, rated / (rated + unavailable + failed);
- PM 0–2 mean;
- panels (n);
- pairwise win share, where a close call still counts as a full win, so a consistently "almost as good" reviewer ranks lower rather than tying;
- close-call win share: the reviewer's wins among adjacent close pairs. About 50% means those calls are noise.

Code-review tables sort by pairwise win share, drift-audit tables by PM mean.

**Presentation:**

- **Statistics:** every column states its statistic and n: share with n, Σk/Σn, median or mean.
- **Small samples:** rows below a minimum n (a constant, e.g. fewer than 3 windows) are listed under "insufficient data" without a rank.
- **Labels:** columns from PM judgment (ratings, criteria, defects, panel order) are labelled as such. No composite score.
- **History:** a link to the historical table, if it exists.

### W6. Retire `pm rate`, update docs (S)

**Removed:**

- the `rate` subcommand and `write_model_performance`;
- the `model-performance.md` section of `run-report.md`;
- SKILL.md step 5's rating instruction.

Move `references/model-performance-rubric.md` to `archive/`. Step 5 becomes: run `status --report` and quote the total run time from it.

**Updated:**

- **SKILL.md, model ids:** launch Developers and Reviewers with full model ids (e.g. `claude-opus-5-5`, not `opus`), because the leaderboard groups exact strings and an alias's meaning drifts.
- **SKILL.md, cause:** `finalize --stop` requires `--cause`.
- **References:** `references/run-state.md` and `references/review-judgments.md` gain the `lite-2` fields, the event `data`, and no historical-record language.
- **README:** the CLI table, plus a walkthrough that sets `PM_LEDGER_DIR` and records judgments before decisions.

### W7. Historical table and bench freeze (operator task, not PM code)

- **Historical table.** Generate `ledger/historical-pre-ledger.md` once from the archived census (`ai-agent-bench/archive/2026-10-09-pm-ledger-census/`):
  - era-D runs only (14 Sep onwards, when judgments were first recorded);
  - identities resolved;
  - the brief's eligibility rules;
  - the same Developer and Reviewer column families, where the old data supports them.

  Label it frozen and pre-ledger, and never regenerate it.
- **Bench freeze.** Add a short "frozen" note to `ai-agent-bench/README.md` saying what the bench remains valid for, and that the PM ledger supersedes it. Its tools are expected to stop working against `lite-2` runs; that is accepted.

## 5. Rules the implementation must keep

- **Deterministic:** rows are a pure function of `run.json` and `events.jsonl`, and the leaderboard is a pure function of the rows. Identical inputs give identical bytes. Git is read once per accepted slice, at accept.
- **One source per fact:**
  - nothing in a row is stored anywhere else as a second copy;
  - each new fact is written once, where it is born (§3.1).
- **Never a silent zero:** missing data is `null` with a reason. Model and effort strings are kept verbatim, never mapped.
- **Never blocking:** only the W4 gate can refuse a command. Metric or ledger-write failures are loud but never block or undo.
- **Judgment is labelled:** counts entered by PM are PM judgment. They sit in labelled columns and are never blended into a deterministic number.
- **DRY:** reuse:
  - `_context_key`, `_risk_flag` and `run_elapsed`'s parsing;
  - code-health's classifiers and AST complexity.

  Add no second line classifier and no report parser.
- **KISS:** none of the following:
  - a configuration file;
  - a sweep;
  - a separate record command;
  - append logs, new locks or repair logic;
  - Bradley–Terry;
  - tokens;
  - randomisation;
  - HTML.

## 6. Out of scope

- Token usage and cost.
- Session ids.
- Parsing review markdown.
- Reviewer precision and recall counts.
- A sweep across repos.
- Backfilling old runs into the ledger.
- Head-to-head Developer runs.
- Tracking review commissions from launch. If the controlling `pm review` process itself dies, the commission leaves no record; that is a documented limit.
- A recorded PM-seat identity (see §8).
- Changes to the orchestrator skill. PM's reviews don't use it.

## 7. Rollout and validation

**Rollout:**

- **Schema:** `init` writes `lite-2`, and every command refuses a `lite-1` run with the existing named error. There are no dual-schema paths.
- **Before upgrading a host,** finish or stop its in-flight runs on the old code. Check the Mac Studio's `mimic` first.
- **Plans** written before W1 fail `check-plan` until each slice has a `Difficulty:` line and checkbox criteria. Make that edit before `init`, never mid-run: the plan digest is a floor fact.
- **Implementing this plan:** use a stable toolkit, either a `lite-1` PM run whose Developer works in a separate worktree, or Mode A, so the installed supervisor never changes under its own run.
- **First records:** that run produces no ledger rows. The first come from a fresh `lite-2` smoke run after merge, on a small real plan with two Developer models across slices, one steer and one two-report panel.

**Validation:** one focused test per behaviour; no golden-file snapshot of the whole leaderboard.

| Area | What is checked |
|---|---|
| Plan | `check-plan` errors on a missing difficulty and on zero checkbox criteria; `init` stores both |
| Events | Launch identity, stop cause, budget-exhaustion cause and `review-failed` data are written; a timeout writes one event; a reaped reviewer writes nothing |
| Code block | A fixture repo gives the expected ΔLOC by category and kind, and the expected complexity. A parse error gives `null`. An injected exception still lets the accept complete |
| Gate | Each missing item for the current origin is refused, including on a relaunch. Earlier-origin gaps are ignored. A full panel order is enforced. Budget exhaustion still kills the session |
| Rows | Per-slice identity across slices with different Developers. A mixed window. Window outcomes for accept, slice-stop, exhaustion (including exhaustion then `finalize --stop`) and pause/resume. An `unavailable` submission is excluded. A late reviewer rating appears after the next write. Writing is skipped without a verified token |
| Render | Same input gives identical bytes; duplicate copies collapse; conflicts and malformed files go to the errors section |
| Floor | Existing floor tests pass unchanged |

## 8. Risks and open items for the implementation plan

- **Judgment burden.** W3 adds `criteria_met` and four severity counts per scored submission. If PM's counts prove careless (e.g. defects always 0), the leaderboard shows no spread. Reassess after ~10 runs; the 0–2 ratings stand on their own regardless.
- **Event log integrity.** The event log is unsigned, and the identity, cause and failure data in it are trusted as much as the attempt and steer counts already are. If a steer's event append fails right after the steer is delivered and saved (a local write failing immediately after a successful one), that submission merges into the previous one. It may surface as a `head` mismatch at the gate, but this is not guaranteed. This is accepted as a documented limit rather than guarded.
- **PM self-preference and PM identity.** Claude reviewers may be favoured, and the PM model is unrecorded. A later option is an optional `init --pm-model`; it is not in this plan.
- **Time figures are proxies.** Developer time runs to the next floor, origin, stop or decision event, so a session left overnight before PM looks inflates it. Wall time includes review time. Medians are used.
- **Host name.** Use the short, lower-cased `socket.gethostname()`. It only names the folder; a change just starts a new one, and render keeps the copy with more events.
- **Sandboxed PM harness.** If `skills/project-manager/ledger/` isn't writable where PM runs, writes fail loudly. `PM_LEDGER_DIR` is the remedy; confirm it on both machines.
- **Exposure.** Local models are mostly used as Developers on bench-style work today, so their rows will show "insufficient data" until they're used on real plans.

## 9. Suggested slice breakdown for the implementation plan

| Slice | Content | Depends on | Difficulty |
|---|---|---|---|
| 1 | W1: template line, anchors, checkbox criteria, `check-plan` errors, init fields | — | easy |
| 2 | W6: retire `pm rate`; SKILL.md, reference and README updates (except gate wording) | — | easy |
| 3 | W2: `lite-2` and dead-code removal, event `data`, launch identity, stop cause, `review-failed`, code block at accept | — | moderate |
| 4 | W3: `criteria_met` and `defects`, strict `order` + `close`, reference rewrite | 1, 3 | moderate |
| 5 | W4: current-origin gate on finalize and relaunch; SKILL.md gate wording; update existing finalize tests | 4 | moderate |
| 6 | W5: `run_rows`, per-run ledger file writes, `pm ledger render` | 3, 4, 5 | hard |

W7 and the `lite-2` smoke run follow slice 6 as operator tasks.

## 10. Review and simplification record

**Panel review, three rounds.** The reviewers were Codex `gpt-6.1-sol` high and Claude `claude-opus-5-5` high, and the review ended with no serious findings. Its main contributions survive in this version:

- the slice-window unit;
- per-submission identity;
- gating only the current submission;
- the full panel order, grouped by `_context_key`;
- budget exhaustion counted against the Developer;
- the code metrics pinned against git configuration;
- the corrected self-hosting rollout.

**Simplification pass.** Shaped by the user's choices, with an independent Opus brainstorm. It replaced stored, signed window records and a per-host append log with per-run files regenerated from state. That removed:

- provisional records and their replacement rules;
- the sync lock, torn-line repair and de-duplication;
- the locked launch-index allocation and both publication checks;
- the signed `launches` list and `reviews_failed` list;
- treating an empty report as a failure;
- the init snapshot of the recommended Developer;
- reviewer confirmed/rejected counts and recall, with their commit-matching rule and gate check;
- the floor-failure share, coverage table and "mixed" row.

**Kept at the user's request:**

- complexity (code quality, not just correctness);
- Developer defects by severity;
- Developer time.

**Rejected:** counting close calls as half wins, because it would let a consistently "almost as good" reviewer tie.

**Added:** relaunch gating, so a dead session's submission can't skip its judgment.

**Fresh review of the simplified plan** (Claude `claude-opus-5-5` high, through the orchestrator): FIT WITH FIXES. All 14 findings were verified against the code.

Adopted:

- an outcome precedence rule, so exhaustion always counts against the Developer;
- `unavailable` submissions excluded from Developer columns;
- explicit `assessment` on unavailable input;
- writing under the run lock;
- one post-command write hook covering every token-bearing command;
- writing only from MAC-verified state;
- `status --report` failing loudly on a ledger failure;
- reviews taken from signed state rather than filtered by intactness;
- host dropped from rows, with the copy built from more events winning;
- reaping recognised by `reviewer_pids` and a single timeout event;
- exact doc targets named;
- code-health location;
- unused row fields dropped.

Partly adopted: identity comes from launch data only, but no mismatch flag was added. Net ΔLOC is kept without churn, as the user accepted. The close-call column is kept, per decision 8.
