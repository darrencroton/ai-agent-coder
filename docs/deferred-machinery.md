# Deferred machinery

**Status:** Living. This document holds every toolkit change a post-campaign review proposed and did not build, with the evidence it rested on, why it was held, and what would reopen it. It exists because campaigns throw up an almost unbounded variety of issues, and the toolkit's job is to let the Campaign Manager (CM) and the child PM act well on all of them, not to patch each one. A proposal held once for lack of evidence can be the right change after the next campaign, and this is where that evidence accumulates instead of being rediscovered.

**How it is used.** Each post-campaign review reads this document before proposing machinery, and appends or revises entries when it closes: a new proposal that was not built gets an entry; an existing entry whose evidence grew gets a line under *History*; an entry that was built names the change and moves to *Built*; one closed for good says why and moves to *Closed*. An entry is judged by the same lens as the reviews: [`VISION.md`](VISION.md) (no mechanism for a hypothetical need, one source of truth, mechanise the floor and judge the rest, honest about which layer enforces what), and the question is whether the seats lacked a tool to act well, not which rule would have stopped a bump.

The reviews this document distils are the shakedown review (2026-10-25) and the sage26 machinery work document (2026-11-08). Both are superseded by this document and the changes they led to, and are archived under `archive/docs/` of the owner's checkout; git history keeps them, last tracked at commit `1ab21d3` as `docs/shakedown-review-20261025.md` and `docs/sage26-campaign-machinery-work-20261108.md`. The campaigns' own records are their dossiers (`~/Local/git-repos/campaigns/<id>/`).

## Entry format

- **Proposed:** the change, in outcome terms, with the command or contract shape it would take.
- **Evidence:** the campaign, the numbers, and where in the dossier or ledger they are.
- **Held because:** the reason, against the vision.
- **Done instead:** what was built or written in its place, if anything.
- **Revisit when:** the concrete trigger that would reopen it.
- **History:** dated decisions.

---

## Open

### Templated nodes: `pm.py new-node --template <kind> --head <sha>`

- **Proposed:** a node generated from a reviewed template by named substitutions, carrying the template's digest, with the substitution diff recorded as its plan review and a receipt writer driven by the runner's `context.txt` and status files; a substitution outside the template's named fields refused.
- **Evidence:** sage26 ran 22 ratchet nodes (of 54) on one reviewed template through the CM's own `make_rnode.py` (26 invocations) and `rnode_receipt.py` (21), each differing from the template only in named substitutions, with the substitution diff recorded as its review.
- **Held because:** one campaign's shape. A ratchet at every head is sage26's method, and the generator took the CM a day to write in the dossier. A toolkit node kind, a template digest and a substitution-diff review are a new concept with its own state, and principle 9 says no mechanism for a need one campaign has shown.
- **Done instead:** the contract now allows the review shape without the tooling: a node generated from a template the owner approved passes the plan-review rule with the template reviewed once and its substitution diff recorded as the instance's review (`references/campaign-mode.md` *Nodes*). The generator stays a dossier script.
- **Revisit when:** a second campaign writes the same generator, or a campaign's templated nodes again outnumber its change nodes. The design then starts from sage26's `make_rnode.py` and `rnode_receipt.py` in its dossier.
- **History:** 2026-11-08 proposed (sage26 item 8); 2026-11-09 held, contract clause written.

### Host-check receipts: `pm.py host-check <runner> --at <commit>`

- **Proposed:** a pinned runner or gate run on the host, outside any sandbox, in a sparse throwaway clone at a commit, with a receipt-grade hashed log and a `context.txt`; its record naming which of two uses it is (the evidence for a measurement the sandbox cannot make, or the CM's own verification before a merge).
- **Evidence:** sage26 made seven aligned rebuilds, two converter host runs, five T2 reruns and two judge-script runs, each a hand-written `verify.sh` derived by `sed` from the first. Two were the evidence (the sandbox denied `os.sysconf("SC_SEM_NSEMS_MAX")` behind `ProcessPoolExecutor`, so two converter cases needed a host run per measurement, an owner-accepted denial); the rest were the CM's own verification before a merge.
- **Held because:** it creates a second evidence path outside both the sandbox and plan nodes, which blurs who measures (`references/campaign-mode.md` *Validation*: the CM measures through evidence nodes, run by a child PM at a recorded SHA). The CM's pre-merge verification needs no command: it reuses the evidence-runner skeleton and is recorded as verification. The accepted-denial use is campaign-specific so far.
- **Done instead:** one sentence in *Validation*: a check the CM runs on the host is its own verification, recorded in the dossier, and never the receipt.
- **Revisit when:** a campaign again records a measurement as impossible in the sandbox (an accepted denial in the brief) and needs it more than once or twice. The change is then a contract amendment first, approved by the owner before anything is built: a host check is the receipt only for such a measurement, and otherwise stays verification.
- **History:** 2026-11-08 proposed (sage26 item 9); 2026-11-09 held, one sentence written.

### Token spend per review commission in the ledger

- **Proposed:** the ledger records token spend per commission where the harness reports it, else null; the leaderboard renders cost beside ratings.
- **Evidence:** sage26's spend was not recorded at all (the ledger has no token or cost field); the owner's budget note reserved one provider for diversity on the most important tasks with no measured spend to steer by.
- **Held because:** the one-shot reviewer path captures the report as plain text, and each harness reports usage in its own place and shape, so recording it means a per-harness parser for a number the leaderboard would show as context, not a score.
- **Done instead:** the failure class (limit, auth, capacity, timeout, empty) is recorded per commission and rendered by class, which answers the budget question the campaign actually had (which seats fail on limits).
- **Revisit when:** a harness exposes usage in a stable machine-readable form on the path the toolkit already captures, or an owner needs spend per seat for a budget decision the class column cannot inform.
- **History:** 2026-11-08 proposed (sage26 item 14, second half); 2026-11-09 held.

### Local-model signals in the ledger: floor-check pass rate and harness telemetry

- **Proposed:** record each submission's floor-check outcome in its ledger row (the `floor` event already carries the failing facts) and render a first-check pass rate; and capture harness-level signals (tokens per second, context overflows, malformed tool calls).
- **Evidence:** the ledger review of 2026-11-12 (70 runs, 150 windows, every Developer a frontier model): 148 windows accepted, 13 nudges plus relaunches in total, no Developer scored 0. The owner intends to start running weaker, slower local models, where these signals may discriminate.
- **Held because:** a floor failure already forces a steer, so it shows in first-pass acceptance and interventions. Harness telemetry needs a per-harness parser (the reason the token-spend entry is held), and its symptoms already surface as time, interventions and non-accepts. No local Developer run exists yet, and `VISION.md` principle 9 allows no mechanism for a need not yet shown.
- **Done instead:** the board keeps Accepted, Interventions and Δ complexity visible, because local models are where they will discriminate.
- **Revisit when:** the first local-model runs show a failure the board cannot explain.
- **History:** 2026-11-12 proposed (ledger review) and held.

### Seat advice from sage26, for the next campaign's brief

- **Proposed:** nothing in the toolkit (`VISION.md`: every supported tool is eligible for any seat, and enforcement differences are documented without becoming rankings). The record is kept here so the next brief and launcher can start from it.
- **Evidence:** the sage26 ledger (54 runs, 100 windows; a copy is at `/Volumes/Internal/results/sage26-campaign/toolkit-ledger/`, rendered by `pm ledger render --run-tag campaign/sage26`). Code review: claude opus-5-5 high 146 commissions, 3 failed, mean 1.88, won 181 of 229 panel pairs, median 245 s; codex gpt-6.1-sol high 93, 14 failed on usage limits, 1.24, won 23 of 157; opencode deepseek-v4.1-flash max 86, 17 failed, 1.61, median 1,033 s; opencode qwen3.8-flash xhigh 70, 0 failed, 1.46; opencode adacs/qwen 13, 1 failed, 1.00, won 0 of 24. Drift audit: sonnet-5-5 high 79, 0 failed, 1.82, median 120 s; deepseek 68, 1 failed, 1.91, median 793 s. Developers: opus high on hard slices 11 of 57 accepted without a steer; sonnet medium on easy slices 28 of 32. Plan review, surveys and the acceptance audit ran on codex outside the ledger and found the real defects (two runner defects, one plan-wording defect, two scope blockers).
- **The advice, as the sage26 review gave it:** code panel opus high first seat, deepseek-v4.1-flash max second with one retry then qwen3.8-flash xhigh, adacs/qwen dropped, codex sol only as a third seat on instrument nodes and for the acceptance audit; plan review on a Claude seat other than the plan's author, codex sol for instrument plans and the first round of each physics node; drift audit sonnet high; Developers opus high hard, sonnet high moderate, sonnet medium easy and evidence nodes.
- **Revisit when:** every campaign; the next campaign's ledger supersedes these figures.
- **History:** 2026-11-08 recorded (sage26 item 13).

---

## Closed

Proposals declined with a reason that does not depend on evidence growing. Each stays here so a later review does not re-propose it without saying why the reason no longer holds.

### A seat probe before reviews or review clusters

- **Proposed:** a probe of each reviewer seat in `campaign-preflight` or before a review cluster, so a dead key or an exhausted limit is found before a commission.
- **Evidence:** shakedown: an expired OpenCode key found at the first review; sage26: 35 of 408 code-review commissions failed, 14 on usage limits.
- **Closed because:** composing a one-shot prompt per harness duplicates the reviewer launch path (one source of truth), and a probe that passed would not have prevented the capacity exits and timeouts minutes later. The failure class and bounded re-commission in `pm.py review --fallback` address what actually happened.
- **History:** 2026-10-25 declined (shakedown D3); 2026-11-08 declined again (sage26).

### A dossier sidecar and renderer

- **Proposed:** a machine-readable sidecar for `work.md`'s node table and a renderer, after scripted edits mangled rows.
- **Evidence:** shakedown: the 16-column table lost cells twice; sage26: about 550 inline edits of the dossier's four files.
- **Closed because:** table state is the CM's own record and a schema is a second source of truth; the shakedown's "trim the table rather than add tooling" stands. What a tool can own without a schema, numbering and timestamps, became `pm.py dossier add-decision`, `add-evidence` and `set-head`, and the table was trimmed.
- **History:** 2026-10-25 declined; 2026-11-09 numbering and stamps built, sidecar closed.

### A `campaign.yaml` holding seats and access rules

- **Closed because:** the brief's digest is the access rules' only protection, and a second file would be a second copy; the seat lines the launcher lacked were added to the existing campaign launcher instead.
- **History:** 2026-11-08 declined (sage26).

### A `pm.py notify` command

- **Closed because:** a wrapper would not fix a broken notifier; one launcher line names the notification command and the events.
- **History:** 2026-11-08 declined (sage26).

### A sanctioned state-changing task node for one-commit closing work

- **Closed because:** the dividing line (anything that changes state is a plan node) stays; the one case was ruled acceptable by the owner after verification.
- **History:** 2026-11-08 declined (sage26).

### `launch-child --plan-review <output>` refusing unless the review begins `RESULT: clean`

- **Closed because:** it couples the toolkit to a delegate prompt's output format and mechanises a judgement gate the acceptance audit already backstops (principle 2 cuts both ways: no determinism dressed over judgement). `pm.py review-plan` now builds and records the round, and fails on an empty or incomplete report, but the clean-or-revise reading stays the CM's.
- **History:** 2026-10-25 declined (shakedown D2).

### `launch-child` scrubbing or recording the inherited environment

- **Closed because:** the sensitive variable set is product-specific; the evidence-runner skeleton records and removes the make, git and Python variables, which is the right layer.
- **History:** 2026-10-25 declined (shakedown D4).

### Renaming node worktrees `<campaign>-<node>` by default

- **Closed because:** the worktree path is the brief's, under the owner's trusted parent.
- **History:** 2026-10-25 declined (shakedown D5).

### A "none recorded" line from `stop-child` when no reviewer process groups are recorded

- **Closed because:** an unreadable state root already prints that the groups are unknown, a bare "none" would misreport a single unreadable `run.json`, and *Cleanup* already mandates the by-hand reviewer check.
- **History:** 2026-10-25 declined (shakedown A6/D6).

### Copying the ledger file into `validation/<node>/`

- **Closed because:** the ledger is the toolkit's model-performance record, kept in the toolkit checkout by design (`PM_LEDGER_DIR`), survives worktree removal and is not campaign evidence.
- **History:** 2026-10-25 declined (shakedown D7).

### Cosmetic, deferred until the code is next touched

- Child session names double the word (`campaign-campaign-<id>-<node>-<hex>`), since the prefix is added to a branch that by convention starts `campaign/`; it costs 9 of the 32 characters kept for the socket name (shakedown A7).
- The run report counts attempts from 0 and the judgments from 1 (`status --report`; shakedown C).

---

## Built

Proposals that were first held or declined and later built when the evidence changed. Kept as the record that a decision here is not final.

### A standard evidence-node runner in the toolkit

- **Declined 2026-10-25** (shakedown D1): product-shaped, and principle 9. The convention (a pinned dossier script, a steer edits the report only) plus a worked example was the proportionate cost.
- **Built after the second shakedown** as the capture-only skeleton `references/evidence-runner.sh`, once the convention had settled and every runner was re-deriving the same provenance checks.
- **Extended 2026-11-09** with the four additions sage26's ten runners all made (a sparse-clone step, `require`, per-stage declared files, the heredoc exception). The skeleton stays capture-only: it judges capture, never outcome.

### Dossier bookkeeping commands

- **Declined 2026-10-25** in the form of a scripted table editor ("trim the table rather than add tooling").
- **Built 2026-11-09** in the smallest form: `pm.py dossier add-decision`, `add-evidence` and `set-head`, which own only numbering and timestamps; the Markdown stays the source and a value a tool cannot know stays the CM's to write. sage26's 550 hand edits were the evidence.
