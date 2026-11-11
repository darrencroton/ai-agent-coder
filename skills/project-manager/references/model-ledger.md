# Model assessment and the ledger

How a PM run scores the Developer and Reviewer models it used, where that record lives, and how you turn many runs into one leaderboard. This is the user's guide. Full command syntax is in the [README](../README.md#cli), the file shapes are in [run-state.md](run-state.md), and the judgment input formats are in [review-judgments.md](review-judgments.md). `pm` below stands for `python3 skills/project-manager/scripts/pm.py`.

## The idea in one paragraph

Every run already records what each model did: which model was launched for which slice, what it delivered, what the independent reviewers found, and what the PM decided. The ledger adds the PM's own structured judgment of each piece of work and writes the whole account, per run, to one JSON file outside the repository. `pm ledger render` then reads every run's file and builds a leaderboard. Nothing in the leaderboard is a copy of anything else, nothing is blended into a composite score, and every number that came from the PM's judgment says so in its column header.

## Commands at a glance

| Command | What it does for the ledger |
|---|---|
| `init --model M --run-tag TEXT --model-tag TEXT --pm-model TEXT` | names the default Developer model, labels the whole run, sets the run's default model tag, and names the model in the PM seat |
| `start-slice --model M --model-tag TEXT` | launches a Developer; the model and tag override the run defaults for this launch only |
| `review --tool T --model M --model-tag TEXT` | commissions a Reviewer with its own identity and tag |
| `judge-developer --file F` | records the PM's score of the current Developer submission |
| `judge-reviews --file F` | records the PM's rating of a review (with its confirmed-defect count), or the order of a review panel |
| `status --report` | regenerates the run report and confirms the run's ledger file was written |
| `ledger render [--out PATH] [--run-tag TEXT …]` | builds the leaderboard from every run's file |
| `ledger tag RUN_ID [--run-tag TEXT] [--model-tag OLD=NEW …]` | changes the tags on a finished run's file |

The tag and `--pm-model` flags are optional. `ledger render` and `ledger tag` need no run and no token, so you can use them any time, including long after the runs and their repositories are gone.

## What is measured

The unit is a **slice window**: one launch of a slice, up to the slice's next fresh launch or the end of the run. A slice stopped with `finalize --stop` and launched again therefore has two windows, and a rating recorded after the decision still belongs to its window. A window's outcome is its decision (accepted, exhausted, stopped), or open if there was none. Inside a window, every launch, relaunch and steer is a **submission**, and each submission has an identity: the Developer tool, model, effort and optional model tag of the session that produced it. Reviews have the same kind of identity, plus whether a custom command ran the reviewer.

An identity is the exact strings passed at launch. Pass full model ids (`claude-opus-5-5`, not `opus`), because the leaderboard groups by the string and an alias's meaning drifts.

## What a run records automatically

Nothing below needs a command of its own; it happens inside the commands you already run.

| When | What is recorded |
|---|---|
| `init` | the repository name, the run tag, the default Developer model and tag, and the PM's model (`--pm-model`: one non-blank line, no `=`) |
| `start-slice` | the submission's identity on the launch or relaunch event; attempt counts |
| `send`, `finalize --steer` | nudges and steers, counted per window |
| `review` | the review's identity, its report and hash; a timeout, non-zero exit or empty report is a `review-failed` entry carrying its failure class (`limit`, `auth`, `capacity`, `timeout`, `empty`, or `exit <N>` when nothing recognised it), one entry per commission, including each bounded re-commission |
| `finalize --accept` | the outcome, the decision time, and the code metrics of the accepted range (net lines by category and kind, Python cyclomatic complexity before and after) |
| `finalize --stop --cause …`, budget exhaustion | the outcome and its cause |
| every command that carries the run token | the run's ledger file is regenerated from signed state |

The per-run file is `<ledger dir>/<host>/<run-id>.json`. The ledger directory is `skills/project-manager/ledger/` beside the skill, or wherever `PM_LEDGER_DIR` points; `<host>` is the machine's short host name. A failed write prints `pm: ledger: not written: <reason>` and never blocks the command. `status --report` exits 2 if its own write fails, so the finish step cannot miss it.

## What the PM records by hand

Two kinds of judgment, each written as a small JSON file and recorded with its command. The input formats are in [review-judgments.md](review-judgments.md).

- **`judge-developer`**: a 0–2 score for the current submission, with `criteria_met` (how many of the slice's checkbox criteria it met) and `defects` (the distinct defects the PM confirmed, counted by severity P0–P3). A submission the PM cannot assess, such as a session that died before producing anything, is recorded as `unavailable` instead; `unavailable` is not a score of 0, and a submission with assessable work is scored even when its session has since died.
- **`judge-reviews`**: a 0–2 rating (or `unavailable`) for every review of the submission, and, when two or more code-review reports share a commission context, one strict best-first `order` with `close` calls for the ones too close to call. Give every scored rating its `confirmed` count, as [review-judgments.md](review-judgments.md) defines it.

The scale is the same everywhere: 0 unacceptable, 1 acceptable, 2 excellent. The **judgment gate** refuses `finalize --accept`, `--steer`, `--stop` and a relaunch until the current submission has its Developer judgment, a rating for every review commissioned on it, and an order for every panel; `pm stop`, the emergency exit, is not gated.

Either judgment is corrected by recording a superseding one, and reviewer ratings can also be recorded for the first time after the decision. Each judgment command regenerates the run's ledger file itself, so a correction is in the ledger as soon as it is recorded.

## Rendering the leaderboard

```bash
pm ledger render                                             # every run → <ledger dir>/leaderboard.md
pm ledger render --run-tag bench-q4                          # only runs tagged bench-q4 → <ledger dir>/leaderboard-bench-q4.md
pm ledger render --run-tag bench-q4 --out ~/boards/bench-q4.md   # the same, in a file of your choosing
```

**Each render replaces its output file.** Without `--out` an unfiltered render writes `<ledger dir>/leaderboard.md`, and a tag-filtered one writes `<ledger dir>/leaderboard-<tags joined by +>.md` beside it (tags sorted, characters other than letters, digits, `_`, `.` and `-` replaced by `_`), so a filtered view never overwrites the full board; the command says so. Give a board you want elsewhere its own `--out`. Nothing is lost by overwriting, because the board is rebuilt from the per-run files every time. A filtered board names its tags in a "Run tags:" line at the top, and runs without a run tag are left out of it. A "PM model:" header line counts the runs by the model that sat as PM, `unrecorded` for runs without one. Repeat `--run-tag` to include runs carrying any of several tags.

The output depends only on the files, never on the clock or the output location, so the same files always give the same bytes. The write is atomic, so a failed render leaves the previous board intact. The command exits 2, naming the directory it searched, when it finds no files or no run carries any of the given tags.

**The Developer table** is one table, grouped by difficulty band (hard, moderate, easy, then any other label) and ranked within each band only, since a rank across bands would follow each model's mix of slices. Rows group by exact tool, model, effort and model tag. A window counts for a Developer when it was `accepted`, `exhausted`, or `stopped` with cause `developer`; every other stopped window, and windows still open, are left out. Rows rank by first score (PM), then by first-pass acceptance. A window's *first* submission is its first scored one, so the score is not a mean blended with later, post-steer work. Columns:

- Accepted over windows; first-pass accept (accepted on the first submission); first score (PM), the mean; first-submission criteria met as Σk/Σn (PM); first-submission P0–1 and P2–3 defects, mean (PM).
- Interventions per window, the mean of steers, relaunches and nudges. Dev time per submission, from the submission's start to the next floor check, decision or submission (a proxy that includes whatever waiting happened before the PM's first check). Wall time per window. Both times are median minutes.
- Δ code lines (production and test, code lines only) and Δ complexity (Python only), medians, describing the accepted diff after any steering; comparable only between Developers given the same slices.

The first-submission columns and Dev time credit each submission's own identity; the others count a window only when all its scored submissions share one identity, and a footnote (printed only when non-zero) counts the windows left out.

**The Reviewer tables**, one per skill, group rows the same way plus whether a custom command ran the review (`override` or `override unknown` is added to the label only when it applies). Columns: commissions; usable share (PM), rated over rated plus unavailable plus failed; failed commissions by class, for example `limit 14 · capacity 11 · unclassified 2` (classes in the order limit, auth, capacity, timeout, empty, unclassified, a zero class left out, `0` when none failed; `unclassified` is an `exit <N>` nothing recognised, or a reason recorded before the classes existed); score, mean (PM); confirmed defects per review, mean over the ratings that carry `confirmed` (PM); review time, median minutes (the review record's `at` minus `launched_at`, `n/a` without a `launched_at`, so a panel can be chosen with its cost beside its ratings). Code-review adds pairwise wins (PM), the share of panel pairings the reviewer was placed above (a close call is still a full win; two commissions of one configuration in a panel are never paired), ranks by it, and is followed by a head-to-head table of each ranked reviewer's wins–losses against every other across their shared panels, because a share depends on who a reviewer was panelled with. Drift-audit ranks by mean score.

Groups below three windows (Developers) or three commissions (Reviewers) are listed in the same table, unranked, with rank `–`. The "As of" line is the latest decision in the kept files. An "Errors" section names any file that was skipped and why.

The board leaves out, but the ledger file still records, comment and blank lines, the documentation, configuration, data and other categories, and panel close calls.

## Tags

Two optional labels separate what the identity strings cannot.

- A **model tag** names a configuration that tool, model and effort do not distinguish: one local model at two temperatures, or the same model behind two system prompts. It is part of the group key, so each tag gets its own leaderboard row.
- A **run tag** labels a whole run, so you can render a board from a chosen set of runs.

A tag is trimmed of surrounding spaces, and may not be blank, span lines, contain `=`, or be `untagged`, which `ledger tag` reserves for "no tag" as an old value. To change the tags on a finished run, use `ledger tag` with the run id:

```bash
pm ledger tag 20261010T060837Z-9153ab --run-tag bench-q4
pm ledger tag 20261010T060837Z-9153ab --model-tag untagged=temp-0.2 --model-tag hot=temp-0.9
```

`tag` rewrites every host's copy of that run's file at once, so copies still collapse at render. `untagged` as the old value selects identities that had no tag, and all renames apply together, so a swap works. It checks everything before writing anything, and restores any copies it already rewrote if a write fails.

**Retag last.** The per-run file is derived from the run's signed state, so any later token-bearing command on that run (a late rating, a superseding judgment, `status --report`) regenerates it with the original tags and no warning. If that happens, run the same `tag` command again.

## Common situations

**Compare two Developer models on real work.** Give each model the same plans, one run per model, and tag both runs so the comparison renders on its own.

```bash
pm init … --harness claude --model claude-opus-5-5 --run-tag cmp-oct
pm init … --harness codex --model gpt-6.1-sol --run-tag cmp-oct
pm ledger render --run-tag cmp-oct --out ~/boards/cmp-oct.md
```

Read each difficulty band for the PM score and acceptance columns, since a model that is strong on easy slices may not be on hard ones. Models that merely happened to get different slices in ordinary runs are compared on different work, so read those rows as context, not as a verdict.

**Benchmark one local model at several settings.** Give each configuration its own model tag, either for the whole run or per launch. The leaderboard shows one row per tag. Keep one tag per slice window: a relaunch under a different tag makes the window mixed, and mixed windows drop out of the window-level columns. The same holds for a relaunch under a different model; [SKILL.md](../SKILL.md) says how to hand a slice over. The Δ columns are comparable only between models given the same plans.

```bash
pm init … --model M --model-tag temp-0.2      # the run's default
pm start-slice --model M --model-tag temp-0.8 # this launch only
```

**Keep a benchmark separate from production runs.** Tag the benchmark runs at `init --run-tag`, then render the full board and the benchmark board to separate files, as shown in *Rendering the leaderboard*. If you forgot to tag a run, tag it afterwards with `ledger tag` (see *Tags*).

**Combine two machines.** Point `PM_LEDGER_DIR` on each machine at one shared folder outside any supervised repository. Use an absolute path, since a relative one resolves against wherever `pm` runs, usually inside the repository. Each host writes only its own subfolder, so they never write the same file, and `render` on either machine sees both. A synced folder is eventually consistent, so render after it has settled.

```bash
export PM_LEDGER_DIR=~/Sync/pm-ledger
```

Prefer this to symlinking the skill's own `ledger/` directory, which the toolkit repository would show as an untracked path. Without a shared folder, copy the other host's subfolder into your ledger directory and render. A run present in both is counted once: the copy with more events wins, and two copies that disagree at the same event count go to "Errors".

**A rating was missed, or a judgment was wrong.** Record the rating, or a superseding judgment, and render again; the judgment command regenerates the run's file, so a retagged run needs retagging again (see *Retag last*). Run the judgment from inside the supervised repository with the run's token exported as `PM_RUN_TOKEN`, or passed as `--token`. Add `--run` only when the run is no longer the repository's current one. This works after the run has finished, but not once its worktree is removed, because the run's state lives there.

```bash
pm judge-reviews --file rating.json --run RUN_ID
pm ledger render
```

**Rebuild a leaderboard after the repositories are gone.** Only the per-run files are needed, so `pm ledger render` works as long as the ledger directory survives. Keep it outside every repository and treat it as data.

**The finish step says the ledger was not written.** Fix the cause named in the message, usually an unwritable `PM_LEDGER_DIR`. Then run `pm status --report` again; the next write regenerates the whole file.

## What the numbers do and do not mean

- Every column derived from the PM's judgment is marked (PM). They are the PM's opinion of the work against its instructions and evidence, labelled as such, and never mixed into a measured figure.
- Time figures are proxies: Developer time runs to the next floor check or decision, so a session left overnight before the PM looks inflates it, and wall time includes review time. Medians are used for that reason.
- Code metrics count lines lexically (binary files included) and skip symlinks; they are context for a window, not a score.
- A Reviewer's panel figures only exist where the PM commissioned two or more code reviews on one submission; a lone report is rated but never ordered against another, so a reviewer only ever used alone has a score and no pairwise figures.
- The comparisons are observational. Each model's rows come from whatever work it was given; the three-window minimum decides only whether a row is ranked, not whether a difference means anything; and every (PM) column reflects the model in the PM seat, including any leaning towards its own vendor. Compare on the same plans and treat small n as anecdote.
- The leaderboard is only as good as the PM's judgments. If scores are always 2 and defects always 0, the spread is in the acceptance and time columns, and the judgment burden is worth revisiting.
