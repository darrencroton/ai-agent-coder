# Shakedown campaign review (2026-10-25)

A review of the first campaign-mode (Mode C) run, `shakedown`, against the toolkit at `feature/pm-campaign-mode` `d565323`. It covers the whole history, not the final logs alone: the dossier's 32 commits, both child runs' state roots (`events.jsonl`, assessments, run reports, HANDOFFs, panes), the four CM session transcripts, the owner guide and preflight script, and the toolkit code behind every `Machinery:` claim the CM recorded. Dossier: `~/Local/git-repos/campaigns/shakedown`; the CM's own consolidation is its `lessons.md` (40 entries), which this review re-evaluates rather than repeats. Timings to the minute that the dossier does not record (owner latency, the moment a question was answered) come from the CM transcripts.

The lens is the one the owner set: KISS and DRY for equivalent function, the CM and the PM are responsible seats that can manage many things, and the question is what the *core* workflow and machinery must change before the sage26 campaign relies on it, judged against `docs/VISION.md`.

This document was itself reviewed by an independent two-seat panel (codex `gpt-6.1-sol` and claude `claude-opus-5-5`, both high effort, read-only, through the `orchestrator` skill); the corrections it made are folded in, and the panel's reports are under `.orchestrator/runs/shakedown-review-*/`.

Severity: **P1** fix before a real campaign; **P2** worth fixing now, cheap; **P3** cosmetic or deferrable; **Declined** a suggestion from the run that should not be built, with the reason.

## Verdict

The campaign machinery works end to end. Two child PMs ran unattended to `complete` with zero stops, the floor and the protected paths held (T3), the question channel carried the one question a child asked, the restart reconciled from the dossier alone (T4), both cleanups left no worktree, no prunable entry and no live server (T5, E24), and the independent audit on a different model caught two real CM process deviations that the CM itself had rationalised. The design's load-bearing ideas held under a real product.

What the run also showed is where the cost went. The measurement that the campaign existed to make took 2m35s. The evidence node that made it cost the CM four plan-review rounds (about half an hour) and the child PM ten commissioned reviews (about 50 of its 60 minutes went to the two review rounds and their assessment). Both trace to two small things rather than to the design: a Risk Flags line the plan parser read as "risky surfaces touched" because prose followed the word `none`, and no convention for how an evidence node pins its commands. The one P1 is a toolkit defect unrelated to campaign mode: Developer sessions get a `TMPDIR` inside the product worktree, which broke a product test suite and forced a plan-level workaround.

Of the CM's 40 machinery entries, this review keeps six toolkit items (one P1, three P2 of which one is applied here, two P3, one of them folded into A3), four contract or template items (three P2, one P3), declines seven as machinery the seats should carry or that would add mechanism against the vision, and treats the rest as discipline, environment or out-of-scope items the system already handles.

## How the run went

Times are local (+11:00), from dossier commits, `events.jsonl` and the CM transcripts.

| When | What | Notes |
|---|---|---|
| 11:10–11:16 | CM session 1: preflight, reads, `package` plan drafted, T3 run | preflight exit 0 in under a minute |
| 11:16–11:24 | `review-package`: 2 codex rounds (7 findings, then clean) | one real brief conflict found (`check-docs` reads excluded `docs/dev/`) |
| 11:25→11:37 | Q1 to the owner, six parts | two parts (trust, "is a check command an instrument") were defaults the CM could have taken |
| 11:38–11:54 | `package` child: 14m32s, one submission, accepted | Developer done in 3.5 min; drift audit failed on an expired OpenCode key; child asked the CM once; the CM answered within a minute (D8) |
| 11:55–12:00 | CM drift audit, merge `bfd53c01`, cleanup, restart pause | cleanup followed the contract's order; empty patches, no `--force` |
| 12:11–12:13 | CM session 2 (a `/clear`, same process): reconcile | preflight, `git worktree list`, `work.md` *Now* were enough |
| 12:13–12:42 | `measure-1` planned; 4 review rounds (9, 9, 4, 2 findings) | prose commands replaced by a pinned runner after round 2; launched on a `revise` round |
| 12:43–13:44 | `measure-1` child: 59m39s | runner 2m35s; two rounds of drift + 4 code reviewers (10 reviews) around one report-only steer |
| 13:44–13:52 | judge, receipt, cleanup, audit (codex): `reject` on two process points | the `docs/dev` listing and the launch before a clean review |
| 13:55→14:18 | Q3 to the owner; sign-off | retrospective round 5 of the plan review came back clean meanwhile |

Owner attention: about six touches over 3h10m, two of them not needed (Q1 e and f) and one a usability gap (`tmux ls` shows no child; a bare `attach` lands on a Developer).

What worked and needs no change: `campaign-preflight` and the digest check; `launch-child --setup` with the owner's script; the private server per child and `stop-child`'s ownership mark; `wait-child`'s signals and `answer-child`; the floor and `check-plan --policy`; the merge and cleanup procedure; reconcile from `work.md` alone; the child PMs' own judgement (a clean seat substitution; a real report gap found through an empty reviewer's stderr); the dossier as an audit trail, which let a different model reject the CM's own account.

## Findings

### A. Toolkit defects (code)

**A1 (P1). Developer sessions get a `TMPDIR` inside the product worktree.** `scripts/pm_lib/slice_ops.py:907-917` creates `<artifact_dir>/tmp` under `<worktree>/.pm/runs/<run>/slices/<slice>/` and exports it as `TMPDIR` to every Developer session. It is a vestige: the Master Controller set `TMPDIR={slice_tmp_dir}` beside its per-slice reviewer tool-home isolation (`PM_SLICE_TMP_DIR`, `PM_TOOL_HOME_ROOT`, see `dc0aa25`); that machinery was deleted in `f9d36a3` and only this line came back in `b41acaa`, with nothing left that depends on it (reviewers now run as one-shot subprocesses in `review.py` with no `TMPDIR` of their own). Effect in the run: two of Mimic's `tests/integration/test_parity_gate_helpers.py` tests failed in the `package` Developer's first integration run; the Developer reran with `TMPDIR` outside the repo and the child PM's HANDOFF named the cause; the CM then had to write a workaround into the `measure-1` plan (D11) and left 28 MB of residue in `/tmp` that no cleanup covers. In general, a suite that assumes temp space lies outside the checkout breaks under PM, and floor fact 7 ignores `.pm/` so nothing notices. **Fix:** delete the override (the `tmp_dir` lines and the `"TMPDIR"` entry). The Developer then inherits the launching environment's `TMPDIR` like every other process, and there is no toolkit-owned temp directory to clean up. The trade-off is accepted knowingly: parallel children share one temp root, which is what a suite's `mkdtemp` is for. This supersedes `lessons.md`'s "per-node TMPDIR that `stop-child` removes". A later sandbox may set its own.

**A2 (P2, fixed here at the owner's request). Every private tmux server left its socket file behind.** tmux never unlinks a `-L` socket file: not on `kill-server`, not when the last session ends (verified on 3.5a in this session). So `stop-child`, a failed launch's cleanup and every test process's `pm-tests-<pid>` server each left a stale file in `/tmp/tmux-<uid>/`. At the start of this review that directory held 537 files and one live server: 472 `pm-tests-*` from the test suite, 50 `pm-child-feat-x-y-*` from `test_campaign`, 2 from the campaign's children, the rest probes (the dossier's E24, taken a few hours earlier, saw the same 537 files with two servers live). A stale file breaks nothing: `list_sessions_on` reads it as "no server", and child sockets carry a random suffix. It is an operator nuisance, which is why the CM rated it P3 and why it is P2 here only because the owner asked for it to be fixed. **Fix applied:** `sessions.kill_server` removes the socket file, best effort, once its re-probe confirms no server remains and the inode is still the one seen before the kill (so a server started under the same name in between keeps its socket, bar the instant between that check and the unlink, which unique socket names make moot), and the test suite's `atexit` teardown now calls `kill_server` instead of a raw `kill-server`. A regression test starts a real server and checks the file is gone. This does not address the separate observation of a live `pm-tests-74724` server from 23 Oct: `atexit` cannot run for a killed process, and the keepalive session ends only itself once the test process's pid is gone, so a server keeps running while any other session is left on it, which is what E24 saw (`pm-tests-74724` still hosting a Developer-shaped session). It is rare, visible with one `tmux -L` probe, and not worth machinery, so nothing further is proposed.

**A3 (P2). The derived risk is invisible, and prose after `none` makes a slice elevated.** `plan.py:156-171` derives `plan_risk` as elevated when `Risky surfaces touched:` is anything but exactly `none` (lowercased, trailing period stripped). The `measure-1` plan said `Risky surfaces touched: none. The slice adds one Markdown report …`, so a report-only slice became elevated, both mandatory reviews ran, and the launcher's four-reviewer panel turned that into ten reviews across two rounds. The `package` plan had the same shape (`none of the runtime; …`) but was audit-required anyway. Nothing printed the derived risk or its reason: `check-plan` reports slice count, approval gates and protected paths, and the CM only saw `plan_risk=elevated` in the child's assessment after the fact. The parser's strictness is right (silence is not "none"); the silence about it is the defect, and the plan skill's *Machine-Consumed Fields* documents exactness for the approval, audit and difficulty flags but not for this one. **Fix:** (a) `check-plan` prints one line per slice, `Slice N: risk standard|elevated (…reason…)`, naming the flag that raised it; (b) one bullet in `implementation-plan`'s *Machine-Consumed Fields*: the value is exactly `none` or a list of surfaces, and anything else, including prose after `none`, reads as touched. About 15 lines plus a test. This makes the derivation visible before `init` freezes it; writing the flag correctly, and sizing the review panel, stay with the CM and the launcher.

**A4 (P2). A reviewer that exits 0 with an empty report is recorded as a review.** `review.py:519-575` fails a review on timeout or non-zero exit only; the report file is mirrored and hashed but never checked for content, and the freshness check (`slice_ops.py:1516-1524`) looks at commit, grants, existence and hash, so a blank file satisfies a mandatory-review requirement. In `measure-1`, one opencode reviewer exited 0 mid-investigation with an empty report; the child PM judged it `unavailable` and read its stderr, which is the system coping. **Fix:** an empty or whitespace-only report is `review-failed` with reason `empty report`, pointing at its stderr, exactly as a non-zero exit is. About 10 lines plus one test.

**A5 (P3). `check-plan` prints its clean-looking summary after an `ERROR:` line** (`cli.py:396-405`; the exit code is already 2); `init` returns straight after the errors. Make `check-plan` do the same. Two lines; fold into A3.

**A6 (Declined, see D6).** `stop-child` prints nothing when a readable state root records no reviewer process groups. The ambiguity the CM felt is smaller than it looks: an unreadable state root already prints "recorded reviewer process groups unknown" (`cli.py:1061-1062`), a bare "none" line would misreport a single unreadable `run.json` (`campaign.py:186-193`), and *Cleanup* already mandates the by-hand reviewer check.

**A7 (P3). Child session names double the word:** `campaign-campaign-shakedown-package-32e421`, since the prefix is added to a branch that by convention starts `campaign/`. Cosmetic; it costs 9 of the 32 characters kept for the socket. Defer unless `child_names` is touched for another reason.

### B. Contract and template gaps (campaign-mode.md, the launcher, the dossier template)

**B1 (P2). The CM's own decision authority has no form, so it over-asks and the audit then counts owner rulings as missing permissions.** Goalpost rule 1 ("every entry in `decisions.md` names the `brief.md` permission it uses; a decision no permission covers is a question to the owner") is written for goalposts but reads as covering every decision. The CM read it that way (Q1 asked about directory trust and whether a check command is an instrument; the owner's first feedback was that moving forward on sensible choices is the CM's job), and the auditor noted the same reading in an aside (D5, D6, D8, D9, D11 cite owner rulings or the owner's standing instruction rather than the brief). Mode B has an explicit rule for this (*Reading a defective plan*); Mode C has none. **Fix in `campaign-mode.md`, a few lines of prose that point at the Mode B rule rather than restate it:** the CM applies *Reading a defective plan* to the brief, with the brief's targets, tolerances, reserved classes, protected paths, allowed paths and exclusions as the stops, records each such decision with `Actor: CM` and its grounds, and may cite an owner ruling already recorded in `decisions.md` (`Actor: owner`, `D<n>`) as a decision's permission. The dossier template's `Permission used` line gets the same alternative. No toolkit change.

**B2 (P2). Nothing grants a campaign's commits.** The Mode B launcher grants Developer commit authority in the confirm step, which the campaign-child paragraph tells the child to skip, so a child inherits none; and nothing grants the CM its own dossier commits or its local merges, which the owner's global "ask before committing" rule otherwise forbids. The CM had to ask (D6) and add a launcher line by hand. **Fix:** one line in the campaign launcher (`campaign-mode.md`, the owner pastes it, so it is the owner's grant): standing authorisation for Developers' slice commits on node branches, the CM's dossier commits and the CM's local `--no-ff` merges, nothing pushed; and one bullet in the campaign-child paragraph (`implementation-plan`'s SKILL.md) that passes the Developer part down and names the merge as the CM's.

**B3 (P2). Evidence nodes have no convention, so the CM invented one over four review rounds.** Three things the run had to work out: that an evidence node's commands must run exactly, which prose cannot guarantee and a pinned script can (D12, `measure.sh`); that a steer may edit the report only and never re-measure; and that the Mode B launcher's generic "full test suite" step (`implementation-plan` SKILL.md, per-slice step 2) means the runner's files for such a node. Each review round found real defects in hand-typed shell (quoting, exit capture, missing-artifact semantics, inherited environment). **Fix, two rules in `campaign-mode.md` *Nodes* and one line in the campaign-child paragraph:** an evidence node's measurements are a script committed in the dossier at `nodes/<node>/` and pinned by SHA-256 in the plan, which the receipt's *Command* cites; a steer edits the report only, and a correction that would re-measure is a new node or a stop; and for an evidence node the child PM's "full test suite" is the runner's files, where a measured `fail` or `error` faithfully reported is acceptable slice work. The run's `nodes/measure-1/measure.sh` is a worked example to point at; how the script captures and what it records is the plan's business. *Trust before use* is untouched: any check that produces a verdict is an instrument needing its controls wherever it lives (this run's copy check is exactly that, D10); a capture-only runner is not. **Declined (see D1):** a generic runner in the toolkit.

**B4 (P3). Small fixes, one line each:**

- `dossier-template.md` layout: add `retrospective.md` (campaign-mode.md requires a retrospective and names no file).
- `campaign-mode.md` *Launching and watching a child*: the watch command is `tmux -L <socket> attach -t <child session>`; a bare `attach` picks the most recent session, which is usually a Developer. `launch-child` prints that line beside the session and socket it already prints (two lines of code).
- `campaign-mode.md` *Nodes*: an audit task node may take the dossier as its repository (the whole dossier is its material; inlining it is not faithful).

### C. Working as designed, or the seat's responsibility (no change)

- **The CM launched `measure-1` on a `revise` round.** A CM error under convergence pressure, as the CM itself wrote. The independent audit on another model caught it and the owner disposed of it: that is the acceptance design working. The contract already says "revise it until both are clean" and "a node launches only when both are clean"; another clause adds nothing. See D2 for why not a flag.
- **The CM listed `docs/dev/` once.** A seat breach, recorded by the seat, caught by the audit. Nothing mechanical in the current threat model prevents a read; the owner's planned sandbox is the layer for that, and the related brief conflict (a mandated gate that reads an excluded path, D5) is deferred to it as well.
- **The expired OpenCode key.** The child found it at its first review, used the question channel exactly as designed, the CM answered within a minute, and the owner renewed the key. The "one question per node" rule that made this costly is the brief's, not the toolkit's. Probing a seat after an outage is a seat habit, not a contract step (D3).
- **The 30-minute `wait-child` that returned with no signal, and the "an hour or more" estimate.** Sizing is the CM's; the toolkit said "no signal" and the CM waited longer, which is the documented response.
- **Inherited `VIRTUAL_ENV`, `MAKEFLAGS`, `TEST_SUMMARY`.** Only `VIRTUAL_ENV` was actually set; the owner's shell is where it came from, the product's Makefile is what reads it, and the plan's runner recorded and scrubbed it, which is the right layer. The toolkit cannot know which variables a product is sensitive to. One line in the owner guide ("start the CM from a clean shell") is the owner's call.
- **zsh word-splitting, directory trust, `tmux ls` showing nothing.** Environment facts; the first is a seat lesson the `package` child recorded in its HANDOFF, the second fails closed by design, the third is addressed by B4's attach line.
- **Node worktrees register in the main checkout's git dir; the state root path is not predictable from the node id.** Git worktree semantics; `campaign-mode.md` already says to find the git dir with `rev-parse --absolute-git-dir`, and the CM did.
- **The T4 restart was a `/clear`, not a new process.** The owner's test procedure; `lessons.md` already carries it for the next shakedown.
- **`work.md`'s 16-column node table lost cells twice under scripted edits** (once noticed by the CM, once by the audit). The CM's edits, but worth watching: if the next run mangles it again, trim the table (socket, session and worktree are also in each worktree's `child.json`) rather than add tooling.
- **The run report counts attempts from 0 and the judgments from 1.** A real cosmetic inconsistency in `status --report`; relabel the column when that code is next touched, not before.
- **A "Resume: yes/no" launcher line.** Not needed: `work.md` *Now* says what state the campaign is in, and reconcile reads it first.

### D. Declined

- **D1. A standard evidence-node runner in the toolkit** (`lessons.md` top priority 1). Product-shaped (rows are Mimic gates), and VISION principle 9 says no mechanism for a hypothetical need. The convention (B3) plus a worked example gives the same convergence with no toolkit code; each campaign still writes and reviews its own script, which is the proportionate cost.
- **D2. `launch-child --plan-review <output>` refusing unless it begins `RESULT: clean`** (top priority 3). Couples the toolkit to an ad-hoc delegate prompt's output format that no skill defines, and mechanises a judgement gate that the audit already backstops. Principle 2 cuts both ways: no determinism dressed over judgement.
- **D3. A seat probe in `campaign-preflight`** (top priority 5). Composing a one-shot prompt per harness and model is a second copy of the reviewer launch path, and making it a reconcile step would move the same cost into prompt ceremony. Probe after an outage, as the CM did.
- **D4. `launch-child` scrubbing or recording the inherited environment.** See C; the sensitive set is product-specific.
- **D5. Renaming node worktrees `<campaign>-<node>` by default.** The worktree path is the brief's, under the owner's trusted parent; the toolkit should not second-guess it.
- **D6. A "none recorded" line from `stop-child`.** See A6.
- **D7. Copying the ledger file into `validation/<node>/`.** The ledger is the toolkit's model-performance record, kept in the toolkit checkout by design (`PM_LEDGER_DIR`); it survives worktree removal and is not campaign evidence.
- **Out of scope here, forward to the `orchestrator` skill:** a continuation needs a new policy file per plan digest; the opencode adapter records no session id; the codex sandbox denies the Xcode git shim's cache writes (a child PM should not read "could not run" as a finding, which SKILL.md already says).
- **Not measured, not a defect:** spend in tokens or money. The retrospective says so plainly.

## Against the vision

Rung 3 held as described: every child kept the Mode B floor unchanged; the CM judged from the children's evidence, never from narration; the owner approved the brief, the reserved node and the sign-off and answered gates, and routine nodes asked nothing; the audit was an independent check on the CM rather than on the product alone. Principle 5 (fail closed) showed in the right places: the digest check, the trust dialog, `stop-child`'s ownership mark.

Three principles are where the findings sit. Principle 2 (mechanise the floor; judge the rest; never blur which is which): A3 is a mechanical derivation acting on judgement silently, A4 is a mechanical freshness check satisfied by nothing, and D2 would blur it the other way. Principle 8 (one source of truth): the exclusions are copied into every plan's non-goals, every task prompt and every child launcher because the brief required exactly that, which is the owner's call; what the CM added beyond it was campaign detail in the launcher that the plan already carried (the setup links, the `docs/dev` ruling), and B2 puts the one item that does belong in the campaign-child paragraph into its only copy. Principle 9 (minimise the whole system): D1 to D3 are the suggestions that would have added mechanism where a convention or a seat's habit does the same work.

## Changes made in this session

Applied in this checkout, not committed, on `feature/pm-campaign-mode`:

- `skills/project-manager/scripts/pm_lib/sessions.py`: `socket_path()`, `_socket_identity()` and `_remove_socket_file()`; `kill_server` records the socket inode before the kill and removes the file once the re-probe returns no sessions and the inode is unchanged.
- `skills/project-manager/tests/pm_test_helpers.py`: the `atexit` teardown calls `sessions.kill_server(TEST_TMUX_SOCKET)`.
- `skills/project-manager/tests/test_sessions.py`: a live-tmux test that a confirmed kill removes the file; the existing "sessions remain" test now also asserts the file is left alone.
- `skills/project-manager/README.md` (the `stop-child` row) and `CHANGELOG.md` (*Fixed*).

Verification: the full project-manager suite (17 modules, 705 tests) passes under CONTRIBUTING's parallel loop with the final code, 17 of 17 modules green and no socket file or live server left behind. An earlier parallel run, on an interim version of the fix without the inode guard, saw two launch-time race failures (`test_ledger` "can't find pane", `test_finalize`'s hung-reviewer reap; both pass alone on the modified and the unmodified tree) and left one `pm-tests-<pid>` file whose module could not be identified afterwards; the final version was then run twice more (the full loop with per-module pids recorded, and `test_sessions` plus `test_campaign` alone) and left nothing. `lint.py check --base HEAD` passes with no new findings.

The backlog of dead files from before the fix is the owner's to sweep. The command below removes only socket inodes, never `default`, and only when tmux answers with its "no server" phrases, so a server that fails to answer for another reason (a protocol mismatch after a tmux upgrade, say) is left alone. It has the same probe-to-unlink instant as `kill_server`, which matters only if a server is being started on that name at that moment. A dead socket inode holds no data, which is why it is removed rather than moved to the trash:

```sh
d="/tmp/tmux-$(id -u)"; for s in "$d"/*; do
  [ -S "$s" ] && [ "$(basename "$s")" != default ] || continue
  out=$(tmux -S "$s" list-sessions 2>&1 >/dev/null) && continue
  case "$out" in *"no server running"*|*"No such file"*) rm -f "${s:?}";; esac
done; ls "$d"
```

## Recommended change set for the next session

In order, each small enough to review in one sitting, all on this branch before pulling to the live checkout:

1. A1: drop the Developer `TMPDIR` override (code, 3 lines removed).
2. A3 + A5: `check-plan` prints each slice's derived risk and reason and returns 2 straight after errors; the plan skill documents the exact-`none` rule (code ~15 lines, doc 1 bullet, 1 test).
3. A4: an empty reviewer report is a failed review (code ~10 lines, 1 test).
4. B1: the CM's delegated-decision rule by reference to Mode B's, and owner rulings as permissions (`campaign-mode.md`, `dossier-template.md`).
5. B2: commit authority in the campaign launcher and the campaign-child paragraph (`campaign-mode.md`, `implementation-plan` SKILL.md).
6. B3: the two evidence-node rules and the launcher line (`campaign-mode.md`, `implementation-plan` SKILL.md).
7. B4: the three one-line items.

Then the child-PM sandbox the owner plans, and the second shakedown. Its checklist is already in the dossier's `lessons.md` *For the next shakedown*; two amendments from this review: the TMPDIR check becomes "the Developer's `TMPDIR` is the launching environment's, not under the worktree", and "an evidence node run by the standard runner" becomes "an evidence node run by a dossier-pinned script under the B3 convention, at standard risk".
