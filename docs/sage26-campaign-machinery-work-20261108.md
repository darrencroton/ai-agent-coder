# Campaign machinery work after campaign sage26 (2026-11-08)

**Status:** Open. The toolkit changes the sage26 post-campaign review recommends, written as a stand-alone entry point: a fresh session should be able to plan and implement from this document and the repository alone, without the campaign's dossier. Nothing here has been implemented; every item names its target file and the evidence it rests on, with the evidence stated inline.

**What the campaign was.** Campaign sage26 (2026-11-03 to 11-08) was the second run of the Project Manager's campaign mode (Mode C; the first was `shakedown`, reviewed in [`shakedown-review-20261025.md`](shakedown-review-20261025.md)) and the exercise it was built for: a Campaign Manager (CM, a `project-manager` seat in campaign mode) imported SAGE26 into Mimic as `models/sage26` against an owner-approved brief with five targets, through 54 child-PM runs (21 change nodes, 22 ratchets, 6 evidence nodes, 2 experiments, 3 instrument nodes), about 103 plan-review rounds and one acceptance audit on a different model. It met every target: bit-for-bit parity with SAGE26 under a frozen core-alignment set, both drivers identical, `sage16` unchanged, Mimic's gates green, documented and plotted like `sage16`; the owner signed it off and the import is merged. The toolkit used was this repository at `feature/pm-campaign-mode` `b496bf5` (the branch is still unmerged to `main`). The campaign's record is the dossier at `/Users/dcroton/Local/git-repos/campaigns/sage26/` (a git repository; its bare archive copy is `/Volumes/Internal/results/sage26-campaign/dossier.git`), whose `post-campaign/axis2-machinery-retrospective.md` is the review this document distils; its `evidence.md` carries 24 `Machinery:` entries the CM wrote as it met each obstacle. Figures below come from the toolkit's ledger (54 `campaign/sage26` run files under `skills/project-manager/ledger/<host>/`) and the dossier's retrospective.

**The lens.** The CM and the child PM are responsible seats that hold the authority to act. The question for each item is whether the seats had the tools to act well, not which rule would have stopped each bump. Every recommendation is a capability (C) or a tool change (T) first; seat changes (S) and the three rules (R) are marked. Judge every change against [`VISION.md`](VISION.md): no mechanism for its own sake, no second way to do what one command already does.

---

## Where the cost went

| Measure | Value |
|---|---|
| Campaign time, brief to last evidence | 5 d 2 h |
| Child run time | 145 h 7 m over 54 runs (physics change nodes 116.9 h) |
| Slices | 100, all accepted: 43 first time, 36 after one steer, 14 after two, 7 at the ceiling of three attempts |
| Plan-review rounds | about 103 on codex, each a hand-built delegate request (106 inline Python request builders in the CM's transcript, one saved builder written on day five) |
| Owner decisions | 29 of 71 recorded decisions; 7 forced by harness or sandbox stops, 4 of those by the `docs/dev` exclusion |
| Reviewer-seat failures | 36 of 408 code-review commissions (codex 14, usage limit; opencode deepseek 19, capacity and timeouts; claude 3, OAuth expiry) |
| Hand work in the dossier | about 550 inline string-replacement edits of `work.md`, `plan.md`, `evidence.md`, `decisions.md`; 16 hand copies of child output to the results volume |
| Spend | not recorded: the ledger has no token or cost field |

The machinery worked: no node merged without its ratchet, the protected paths held, every runner was fail-closed and both refusals (one ratchet, one evidence launch) stopped before a wrong measurement, evidence at two heads around a documentation-only merge let the owner sign off without a fresh look at the science, and the acceptance audit on another model found two real scope readings. What cost time was hand work the toolkit could carry (plan-review requests, dossier bookkeeping, archiving with outputs, host verification) and one enforcement mechanism that fought git (the `docs/dev` exclusion).

---

## The work, prioritised

Priority is by cost removed and risk reduced. Each item: the evidence, the change, the target files (paths as of `b496bf5`), and what accepts it.

### 1. Mechanical in-repository exclusions (C, T)

**Evidence.** The brief excluded `docs/dev/` from every seat. Node worktrees held it on disk and the sandbox denied reading it, which made every working-tree `git diff` print the committed content as "deleted" files; two codex reviewers sent that to their provider (dossier `evidence.md`, the Machinery entry on the leak). The fix was a sparse checkout in each node's `setup.sh` plus a dossier history rewrite; then 37 clones nested under `output/` held readable copies, fixed by a deny at any depth, which then made every full checkout fail inside the sandbox, so every runner had to clone sparse. Four owner rulings, a 400-word access paragraph pasted into every launcher, two breaches. Cause: an exclusion enforced by a read denial inside a git worktree, the one place a denial changes git's output rather than silencing it.

**Change.** `launch-child` applies a sparse checkout for every excluded path inside the product repository before the sandbox starts, adds the deny-at-any-depth entry to the derived policy, and refuses a launch whose worktree still holds the path. The derived policy also grants the child its own transcript directory (`~/.claude/projects/<mangled worktree path>`, read and write), which the campaign carried as a 47-entry literal grant list in the base policy re-pinned into the brief.

**Targets.** `skills/project-manager/scripts/pm_lib/campaign.py` (`launch_child`), `pm_lib/sandbox.py` (the derived policy), `skills/project-manager/references/sandbox.md` *What the toolkit adds*, `references/campaign-mode.md` *Launching and watching a child*, the brief template's *Exclusions* section in `references/dossier-template.md` (the field the launcher reads).

**Accepts.** A campaign-mode trial (the PM README's *Verify campaign mode*) with one excluded product path: the node worktree has no such path on disk, a `git status`/`git diff` in it is clean, a nested clone under `output/` is refused by policy, and the child's transcript is readable by the CM without a base-policy entry.

### 2. Working-tree-safe lint and health (T)

**Evidence.** `lint.py check --base` and `health.py analyze --base` both diff the working tree with no exclusion pathspec, and `lint.py` writes the whole base tree to `$TMPDIR` (`skills/lint/scripts/lint.py`, the base-tree materialisation around lines 870–910; `skills/code-health/scripts/health.py` around line 1075). Under the exclusion both printed the excluded content; one Developer ran `lint.py` against a plan's ban because the Developer prompt names it as the lint step (`references/developer-prompt.md`, the lint step around lines 46–50), outranking the plan.

**Change.** Both scripts take an exclusion pathspec (or diff commit to commit), and `lint.py` materialises only the files it lints. The Developer prompt's lint step honours a plan-level override instead of naming the script verbatim.

**Targets.** `skills/lint/scripts/lint.py` (around lines 630–700 and 870–910), `skills/code-health/scripts/health.py` (around 1075), `skills/project-manager/references/developer-prompt.md`.

**Accepts.** `lint.py check --base <commit> --exclude docs/dev` on a tree with `docs/dev` present prints nothing from it and writes nothing from it to `$TMPDIR`; the same for `health.py`.

### 3. Plan review as a command (C)

**Evidence.** About 103 plan-review rounds, each composed by hand from the node's plan, policy, decisions list, files and the previous round's report and dispositions; 106 inline request builders in the CM's transcript. The largest single class of CM hand work.

**Change.** `pm.py review-plan <node> --round N [--seat <tool/model/effort>]` builds the orchestrator request from the node directory (plan, policy, the decisions it cites, the files it names, the prior round's report and the CM's dispositions), launches it through `delegate_jobs.py`, records the round under `nodes/<node>/reviews/` and in the node's `work.md` row, and fails on an empty report.

**Targets.** `pm_lib/campaign.py`, `pm_lib/cli.py` (a new subparser beside `launch-child`), `references/campaign-mode.md` *Nodes*.

**Accepts.** One command replaces the hand-built request for a node in the trial; the recorded round has the request, the report and the dispositions, and an empty report is an error.

### 4. Dossier bookkeeping commands (C)

**Evidence.** `work.md` was edited 205 times, `plan.md` 197, `evidence.md` 59 and `decisions.md` 83 by Python heredocs; the budget table and one node row fell stale until the close; evidence and decision numbers were assigned by hand.

**Change.** `pm.py dossier set-row <node> <column> <text>`, `add-decision`, `add-evidence`, `set-head`, each numbering and timestamping itself and rendering the Markdown tables from a sidecar (YAML or JSON) so a row can never fall stale against the archive; `work.md` *Now* kept by the tool.

**Targets.** `pm_lib/campaign.py`, `references/dossier-template.md` (the sidecar and the rendered sections).

**Accepts.** The rendered `work.md` is byte-reproducible from the sidecar; adding a decision never needs a hand edit.

### 5. Archive with the child's outputs (C)

**Evidence.** `stop-child` removes the sandbox temp directory with whatever the child left there, and the CM's own `archive_node.sh` (71 lines, run 54 times) kept only the PM state and named artifact directories, so the CM copied child output to the results volume by hand 16 times and outlier CSVs 5 times before archiving.

**Change.** `stop-child --archive` (or `pm.py archive-child`) does the CM's script's work (copy HANDOFF, `.pm/campaign`, `.pm/runs`, the state root and named artifact directories into `validation/<node>/`; capture uncommitted work as patches; remove the worktree, keep the branch) plus the child's sandbox scratchpad and `output/` copied to a results root the brief names, before the temp directory goes.

**Targets.** `pm_lib/campaign.py` (`stop_child`), `references/campaign-mode.md` *Cleanup*, the brief template (a results-root line).

**Accepts.** After `stop-child --archive` in the trial, the results root holds the child's `output/` and the dossier holds its state, and no hand copy is needed.

### 6. Orchestrator failure detection and seat fallback (T)

**Evidence.** The orchestrator reported a clean exit with an empty report when codex hit its usage limit, so the failure was found by reading; deepseek timed out at 3,600–5,400 s five times and returned capacity errors eleven times; the ledger stores only `exit 1`, `exit -15`, `timeout` and `empty report`, so a usage limit is indistinguishable from any other `exit 1`.

**Change.** `wait` and `extract` fail on an empty report or a usage-limit message (the `incomplete` state at `skills/orchestrator/scripts/delegate_jobs.py` lines 390 and 1137 exists for a violated `SECTION:` contract; extend it to these two cases); `fallback_tool`/`fallback_model` in the delegate policy so a PM re-commissions without a hand-written second request; a `preflight --tool` probe and a seat health check before a review cluster.

**Targets.** `skills/orchestrator/scripts/delegate_jobs.py`, `skills/orchestrator/references/delegate-contract.md`.

**Accepts.** A delegate whose output is a usage-limit message ends in `failed`, not `completed`; a policy with a fallback re-launches on that failure once and records both attempts.

### 7. The evidence-runner skeleton gains the campaign's runner pattern (T)

**Evidence.** Every evidence node's `run.sh` (ten runners, 9,685 lines in all) re-implemented the same things over the skeleton: re-execute itself without inherited make variables, check every pin, make a sparse clone of the measured commit under `output/`, run the measurement, write `context.txt`, per-step logs and status files, a manifest, and refuse to run twice. The skeleton's manifest hid a stage failure once (the Machinery entry on the manifest); the clean-tree check tripped on the shell's transient heredoc files once (a ratchet refused and rerun).

**Change.** `skills/project-manager/references/evidence-runner.sh` gains the environment re-exec, pin checks, the sparse-clone step, `measure`/`require`/`pin`/`nonempty` helpers, a manifest stage that lists to a file and checks each stage, a `context.txt` contract a receipt writer can read, and a clean-tree check that ignores `sh-thd-*` files. The runner-specific part shrinks to the measurement commands.

**Accepts.** The campaign's e02 runner re-expressed on the new skeleton is under 150 lines and produces the same `context.txt` and manifest.

### 8. Templated node kinds (C)

**Evidence.** 22 ratchet nodes ran on one reviewed template through the CM's `make_rnode.py` (26 invocations) and `rnode_receipt.py` (21), each differing from the template only in named substitutions, with the substitution diff recorded as its review.

**Change.** `pm.py new-node --template ratchet --head <sha>` (and `evidence-at-head`): a node generated from a reviewed template by named substitutions, with the substitution diff as its plan review and a receipt writer driven by the runner's `context.txt` and status files.

**Targets.** `pm_lib/campaign.py`, `pm_lib/cli.py`, `references/campaign-mode.md` *Nodes*.

### 9. Host-check receipts (C)

**Evidence.** Seven aligned rebuilds, two converter host runs, five T2 reruns and two judge scripts, each a hand-written `verify.sh` derived by `sed` from the first: a pinned runner or gate run on the host, outside any sandbox, in a sparse throwaway clone at a commit, with a receipt-grade log. The sandbox could not read one sysctl (`os.sysconf("SC_SEM_NSEMS_MAX")` behind `ProcessPoolExecutor`), so two converter cases needed a host run per measurement.

**Change.** `pm.py host-check <runner> --at <commit>`: the same sparse clone and capture, log hashed, `context.txt` written; the campaign reference says when a host check is the evidence (a sandbox denial the brief accepts) and when it is only the CM's verification.

**Targets.** `pm_lib/campaign.py`, `references/campaign-mode.md` *Validation*.

### 10. The launcher as data (C)

**Evidence.** The CM's `fill_launcher.py` filled the Mode B launcher from the toolkit's SKILL.md with a node's paths, seats and the access paragraph, 34 invocations, rewritten 10 times as seat decisions changed; the task-node, plan-review and audit seats were chosen by the CM because the brief did not name them.

**Change.** Seats, run settings and the brief's access rules live in the dossier (a `campaign.yaml`) and `launch-child` renders the launcher, so a seat change is one edit and the access rule is never pasted by hand; task-node, plan-review and audit seats are named there.

**Targets.** `pm_lib/campaign.py`, `references/campaign-mode.md` *The campaign launcher*, the brief template.

### 11. Sandbox reference updates (T)

**Evidence.** Facts the seats learned the hard way: srt injects `GIT_CONFIG_PARAMETERS` (one evidence launch measured nothing because the runner's environment check refused it); the sysctl denial above; packed-refs writes denied in a linked worktree (a misleading error in panes); a full checkout fails under a deep deny; transient `sh-thd-*` files.

**Change.** `references/sandbox.md` *Per-harness facts*, *Troubleshooting* and *What it is not* carry each, and the runner skeleton's environment check allows `GIT_CONFIG_PARAMETERS`.

### 12. `answer-child --hold`, `--follow-up`; `wait-child` marker echo (T)

**Evidence.** The CM wanted to hold an answer while it checked something and to send a follow-up to an answered question; `wait-child` once matched a marker that was the child's own command echo.

**Targets.** `pm_lib/cli.py` (the `answer-child` parser), `pm_lib/campaign.py` (`answer_child`, `wait_child`).

### 13. Seats (S)

**Evidence** (the ledger, 54 runs, 100 windows; the PM's 0–2 rating): claude opus-5-5 high as code reviewer 146 commissions, 3 failed, mean 1.88, won 181 of 229 panel pairs, median 245 s; codex gpt-6.1-sol high 93, 14 failed (usage limit), 1.24, won 23 of 157 pairs; opencode deepseek-v4.1-flash max 86, 17 failed, 1.61, median 1,033 s; opencode qwen3.8-flash xhigh 70, 0 failed, 1.46; opencode adacs/qwen 13, 1 failed, 1.00, won 0 of 24 pairs. Drift audit: sonnet-5-5 high 79, 0 failed, 1.82, median 120 s; deepseek 68, 1 failed, 1.91, median 793 s. Developers: opus high on hard slices 11 of 57 accepted without a steer; sonnet medium on easy slices 28 of 32 (1.81). Plan review, surveys and the acceptance audit ran on codex outside the ledger and found the real defects (two runner defects, one plan-wording defect, two scope blockers at the audit): the place a different model earned its spend.

**Change.** Code panel: opus high first seat; second seat deepseek-v4.1-flash max with one retry then qwen3.8-flash xhigh, or qwen where speed matters; drop adacs/qwen; codex sol only as a third seat on instrument nodes and for the acceptance audit. Plan review: a Claude seat different from the plan's author by default; codex sol for instrument plans and the first round of each physics node. Drift audit: sonnet high. Developers: opus high hard, sonnet high moderate, sonnet medium easy and evidence nodes. The owner's budget note stands: codex is limited and reserved for diversity on the most important tasks.

**Targets.** The campaign launcher (item 10) and the brief template's seat lines.

### 14. Ledger (T)

**Change.** Record the failure class (limit, auth, timeout, capacity, empty), token spend per commission where the harness reports it, the plan-review and task-node seats (today outside the ledger), and re-render the campaign leaderboard at every node close (the campaign's board was quoted stale in a seat decision: rendered at 40 runs when 54 existed).

**Targets.** `pm_lib/ledger.py`, `pm_lib/leaderboard.py`, `references/model-ledger.md`.

### 15. The final evidence bundle is every receipt (C, R)

**Evidence.** The claims read at the end raised 357 flags and took most of the CM's last day, because the final evidence node's bundle was two receipts while the documents cited every node's measurement, and the documentation nodes wrote statements without a record citation per statement.

**Change.** `references/campaign-mode.md` *Validation*: the final evidence node's bundle is every receipt and every accepted node's verification entry; the brief template's T5 wording asks that each measured statement in a documentation node name its record as it is written.

### 16. Scope readings go to the owner when made (R)

**Evidence.** The acceptance audit's two blockers were both CM readings of target wording that narrowed what a target reports or claims, recorded as CM decisions; the owner ruled both at sign-off.

**Change.** `references/campaign-mode.md` *Questions to the owner*: a reading that narrows a target's scope is an owner question when it is made. One of the few places a rule is right: a seat cannot know the owner's intent for its own wording.

### 17. A sanctioned route for a verified one-commit closing task (C or R)

**Evidence.** The owner asked for the measured aligned tree to be kept as a branch; the CM had a delegate make it (campaign mode's line is that anything changing state is a plan node), then verified the tree byte for byte and disclosed it; the owner ruled it acceptable.

**Change.** Either a `task` node kind that may change state when its result is verified from commits by a pinned check, or the brief template says closing work is a plan node. `references/campaign-mode.md` *Nodes*.

### 18. Side records the owner asks for (C)

**Evidence.** The owner asked for `sage16 lesson:` entries during the campaign; none were written, because a side record has no node, no receipt and no table row to fall stale. The review reconstructed them afterwards.

**Change.** `references/dossier-template.md`: a *Side records* section the close-out checklist in *Acceptance and retrospective* reads.

### 19. Notification hook (T)

**Evidence.** The owner's `ping-me` was called 53 times and dropped on the last day for unrelated problems; the owner's global instructions still name it.

**Change.** `pm.py notify <event>` runs an owner-configured command; the campaign launcher says which events notify. Keep `ping-me` as that command when it is repaired.

### 20. Brief template (T)

**Change.** A spend line the ledger can measure (item 14); the results root for child outputs (item 5); the side-record section (item 18); the scope-reading rule (item 16); the task-node, plan-review and audit seat lines (items 10, 13).

---

## What not to change

- The brief's structure: targets with tolerances, "who supplies the evidence", reserved decision classes, the no-loosening rule. The audit found no loosened target; every reserved decision was the owner's.
- Capture-only, pinned, fail-closed runners written by the CM, not by the measured party.
- Evidence at two heads around a documentation-only merge; the CM's own host re-verification before every merge; the acceptance audit on a different model.
- The sandbox and the exclusion list themselves; only their enforcement mechanics move into the toolkit (items 1, 2).
- Serial merges with `--no-ff` and a ratchet at every head.
- Campaign mode's dividing line (anything that changes state is a plan node), unless item 17 chooses the sanctioned task kind deliberately.

---

## Order and acceptance

Items 1 to 7 remove most of the campaign's friction and are the set to do before the next campaign; 8 to 12 are the next tier; 13 and 20 are edits to the brief template and launcher; 14 to 19 are small. Each item is a frozen implementation-plan slice with its own acceptance above; the whole is accepted when the PM README's *Verify campaign mode* trial runs end to end on the new commands with an excluded product path, and the shakedown dossier's targets (`/Users/dcroton/Local/git-repos/campaigns/shakedown/`, the comparison baseline) still pass. Merge `feature/pm-campaign-mode` to `main` first or with this work; the owner's call.
