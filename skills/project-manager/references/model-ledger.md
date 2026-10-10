# Model assessment and the ledger

How a PM run scores the Developer and Reviewer models it used, where that record lives, and how to turn many runs into one leaderboard. This is the user's guide; the exact file shapes are in [run-state.md](run-state.md) and the exact judgment inputs in [review-judgments.md](review-judgments.md). `pm` below stands for `python3 skills/project-manager/scripts/pm.py`.

## The idea in one paragraph

Every run already records what each model did: which model was launched for which slice, what it delivered, what the independent reviewers found, and what the PM decided. The ledger adds the PM's own structured judgment of each piece of work and writes the whole account, per run, to one JSON file outside the repository. `pm ledger render` then reads every run's file and builds a leaderboard. Nothing in the leaderboard is a copy of anything else, nothing is blended into a composite score, and every number that came from the PM's judgment says so in its column header.

## What is measured

The unit is a **slice window**: one slice from its launch to its decision (accepted, exhausted, stopped) or, if undecided, to the end of the run. Inside a window, every launch, relaunch and steer is a **submission**, and each submission has an identity: the Developer tool, model, effort and optional model tag of the session that produced it. Reviews have the same kind of identity, plus whether a custom command ran the reviewer.

A Developer identity is therefore the exact strings passed at launch. Pass full model ids (`claude-opus-5-5`, not `opus`), because the leaderboard groups by the string and an alias's meaning drifts.

## What a run records automatically

Nothing below needs a command of its own; it happens inside the commands you already run.

| When | What is recorded |
|---|---|
| `init` | the repository name, the run tag (`--run-tag`), the default Developer model and tag |
| `start-slice` | the submission's identity on the launch or relaunch event; attempt counts |
| `send`, `finalize --steer` | nudges and steers, counted per window |
| `review` | the review's identity, its report and hash; a timeout or non-zero exit is a `review-failed` entry |
| `finalize --accept` | the outcome, the decision time, and the code metrics of the accepted range (net lines by category and kind, Python cyclomatic complexity before and after) |
| `finalize --stop --cause …`, budget exhaustion | the outcome and its cause |
| every command that carries the run token | the run's ledger file is regenerated from signed state |

The per-run file is `<ledger dir>/<host>/<run-id>.json`. The ledger directory is `skills/project-manager/ledger/` beside the skill, or wherever `PM_LEDGER_DIR` points; `<host>` is the machine's short host name. A failed write prints `pm: ledger: not written: <reason>` and never blocks the command; `status --report` exits 2 after one, so the finish step cannot miss it.

## What the PM records by hand

Two kinds of judgment. The **judgment gate** refuses `finalize --accept`, `--steer`, `--stop` and a relaunch until the current submission has its Developer judgment, a rating for every review commissioned on it, and an order for every panel; `pm stop`, the emergency exit, is not gated.

- **`judge-developer`**: a 0–2 score for the current submission, with `criteria_met` (how many of the slice's checkbox criteria it met) and `defects` (the distinct defects the PM confirmed, counted by severity P0–P3). A submission the PM cannot assess, such as a session that died before producing anything, is recorded as `unavailable` instead; `unavailable` is not a score of 0, and a submission with assessable work is scored even when its session has since died.
- **`judge-reviews`**: a 0–2 rating (or `unavailable`) for every review of the submission, and, when two or more code-review reports share a commission context, one strict best-first `order` with `close` calls for the ones too close to call.

The scale is the same everywhere: 0 unacceptable, 1 acceptable, 2 excellent. Either judgment is corrected by recording a superseding one; reviewer ratings can also be recorded for the first time after the decision. Each judgment command regenerates the run's ledger file itself, so a correction is in the ledger as soon as it is recorded.

## Rendering the leaderboard

```bash
pm ledger render                      # <ledger dir>/leaderboard.md from every run on every host
pm ledger render --out ~/boards/all.md
pm ledger render --run-tag bench-2026-q4 --run-tag bench-2026-q3   # only runs carrying one of these tags
```

`render` needs no run and no token, so you can run it any time, including long after the runs are over. Its output depends only on the files' contents, never on the clock or their order, so the same files always give the same bytes.

**Developer tables**, one overall and one per difficulty band, group rows by exact tool, model, effort and model tag. A window counts for a Developer when it was `accepted`, `exhausted`, or `stopped` with cause `developer`; windows stopped for a `plan` or `environment` cause, and windows still open, are left out. Only scored submissions count. Columns:

- Score (PM), mean; first-submission criteria met as Σk/Σn; first-submission P0+P1 and P2+P3 defects, mean; Developer time per submission, median (from the submission's start to the next floor check, decision or submission: a proxy that includes whatever waiting happened before the PM's first check).
- Windows; accepted share; accepted on the first submission; scored submissions per window; steers plus nudges per window; wall time per window, median; Δ lines per category (code/comment/blank medians); Δ complexity. These window-level columns count a window only when all its scored submissions share one identity; mixed windows are counted in a footnote.

**Reviewer tables**, one per skill, group rows the same way plus whether a custom command ran the review: commissions; usable share (rated over rated plus unavailable plus failed); Score (PM), mean; panels; pairwise win share across every panel order the reviewer appeared in (a close call is still a full win); close-call win share. Two commissions of one configuration in the same panel are never paired against each other. Code-review tables rank by pairwise wins, drift-audit tables by PM score.

Rows with fewer than three windows (Developers) or three commissions (Reviewers) are listed unranked under "Insufficient data". The "As of" line is the latest decision in the kept files. An "Errors" section names any file that was skipped and why.

## Tags

Two optional labels separate what the identity strings cannot.

- A **model tag** names a configuration that tool, model and effort do not distinguish: one local model at two temperatures, or the same model behind two system prompts. Set a run default at `init --model-tag`, override it per launch with `start-slice --model-tag`, and label a reviewer per commission with `review --model-tag`. The tag is part of the group key; a row without one reads `untagged`.
- A **run tag** labels a whole run at `init --run-tag`, so `render --run-tag` can build a leaderboard from a chosen set of runs.

Tags can be changed later, on a finished run's files:

```bash
pm ledger tag 20261010T060837Z-9153ab --run-tag bench-2026-q4
pm ledger tag 20261010T060837Z-9153ab --model-tag untagged=temp-0.2 --model-tag hot=temp-0.9
```

`tag` rewrites every host's copy of that run's file at once, so copies still collapse at render; `untagged` as the old value selects identities that had no tag, and all renames apply together, so a swap works. Retag last: the per-run file is derived from the run's signed state, and any later token-bearing command on that run (a late rating, a superseding judgment, `status --report`) regenerates it with the original tags and no warning. If that happens, run the same `tag` command again.

## Common situations

**Compare two Developer models on real work.** Give each model the same plans, one run per model, tagged at `init --run-tag` so the comparison can be rendered on its own. Read the overall table for the PM score and acceptance columns, then the per-difficulty tables, since a model that is strong on easy slices may not be on hard ones. Models that merely happened to get different slices in ordinary runs are compared on different work, so read those rows as context, not as a verdict.

**Benchmark one local model at several settings.** Launch each configuration with its own `--model-tag` (`temp-0.2`, `temp-0.8`), or set a run default at `init`. The leaderboard shows one row per tag.

**Keep a benchmark separate from production runs.** Tag the benchmark runs at `init --run-tag bench-…`, then render twice: once with the tag filter for the benchmark board, once without it for everything.

**Combine two machines.** Point `PM_LEDGER_DIR` on each machine at one shared folder (an absolute path outside any supervised repository); each host writes only its own subfolder, so they never write the same file, and `render` on either machine sees both. A synced folder is eventually consistent, so render after it has settled. Prefer this to symlinking the skill's own `ledger/` directory, which the toolkit repository would show as an untracked path. Without a shared folder, copy the other host's subfolder in and render; a run present in both is counted once, the copy with more events wins, and two copies that disagree at the same event count go to "Errors".

**A rating was missed, or a judgment was wrong.** Record the reviewer rating, or a superseding judgment; the command regenerates the run's file. Render again. If the run had been retagged, retag it again afterwards.

**Rebuild a leaderboard after the repositories are gone.** Only the per-run files are needed. Keep the ledger directory outside every repository and treat it as data.

**The finish step says the ledger was not written.** Fix the cause named in the message (usually an unwritable `PM_LEDGER_DIR`, which must be an absolute path outside the repository), then run `status --report` again; the next write regenerates the whole file.

## What the numbers do and do not mean

- Every column derived from the PM's judgment is marked (PM). They are the PM's opinion of the work against its instructions and evidence, labelled as such, and never mixed into a measured figure.
- Time figures are proxies: Developer time runs to the next floor check or decision, so a session left overnight before the PM looks inflates it, and wall time includes review time. Medians are used for that reason.
- Code metrics count lines lexically (binary files included) and skip symlinks; they are context for a window, not a score.
- A Reviewer's panel figures only exist where the PM commissioned two or more code reviews on one submission; a lone report is rated but never ordered against another, so a reviewer only ever used alone has a score and no pairwise figures.
- The comparisons are observational. Each model's rows come from whatever work it was given; the three-window minimum decides only whether a row is ranked, not whether a difference means anything; and every (PM) column reflects the model in the PM seat, including any leaning towards its own vendor. Compare on the same plans, treat small n as anecdote, and note who sat as PM.
- The leaderboard is only as good as the PM's judgments. If scores are always 2 and defects always 0, the spread is in the acceptance and time columns, and the judgment burden is worth revisiting.
