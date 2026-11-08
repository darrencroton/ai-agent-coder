# Campaign machinery work after campaign sage26 (2026-11-08)

**Status:** Open. The toolkit changes the sage26 post-campaign review recommends, written as a stand-alone planning input: a fresh session should be able to plan from this document and the repository alone, without the campaign's dossier. Nothing here is implemented, and nothing here is a frozen slice: each item states its evidence, the change in outcome terms, its target files and what would accept it, and the planning session settles the design choices it names before freezing slices. This document was reviewed by an independent two-seat panel (codex `gpt-6.1-sol` and claude `claude-opus-5-5`, both high effort, read-only, through the `orchestrator` skill) and the corrections are folded in; the panel's reports are under the campaign's review records.

**What the campaign was.** Campaign sage26 (2026-11-03 to 11-08) was the second run of the Project Manager's campaign mode (Mode C; the first was `shakedown`, reviewed in [`shakedown-review-20261025.md`](shakedown-review-20261025.md)) and the exercise it was built for: a Campaign Manager (CM, a `project-manager` seat in campaign mode) imported SAGE26 into Mimic as `models/sage26` against an owner-approved brief with five targets, through 54 child-PM runs (21 change nodes, 22 ratchets, 6 evidence nodes, 2 experiments, 3 instrument nodes), about 103 plan-review rounds and one acceptance audit on a different model. It met every target: bit-for-bit parity with SAGE26 under a frozen core-alignment set, both drivers identical, `sage16` unchanged, Mimic's gates green, documented and plotted like `sage16`; the owner signed it off and the import is merged. The toolkit used was this repository at `feature/pm-campaign-mode` `b496bf5` (the branch is still unmerged to `main`). The campaign's record is the dossier at `/Users/dcroton/Local/git-repos/campaigns/sage26/` (a git repository; its bare archive copy is `/Volumes/Internal/results/sage26-campaign/dossier.git`), whose `post-campaign/axis2-machinery-retrospective.md` is the review this document distils; its `evidence.md` carries 24 `Machinery:` entries the CM wrote as it met each obstacle. Figures below come from the toolkit's ledger (54 `campaign/sage26` run files under `skills/project-manager/ledger/<host>/`, local to the owner's checkout by design, `PM_LEDGER_DIR`; a copy is at `/Volumes/Internal/results/sage26-campaign/toolkit-ledger/`) and the dossier's retrospective. Every fact an item rests on is stated inline.

**The lens.** The CM and the child PM are responsible seats that hold the authority to act. The question for each item is whether the seats had the tools to act well, not which rule would have stopped each bump. Every recommendation is a capability (C) or a tool change (T) first; seat advice (S) and the two contract amendments (R) are marked. Judge every change against [`VISION.md`](VISION.md): no mechanism for its own sake, no second copy of a fact or a launch path, and nothing the shakedown review declined is re-proposed without saying why its reason no longer holds.

---

## Where the cost went

| Measure | Value |
|---|---|
| Campaign time, brief to last evidence | 5 d 2 h |
| Child run time | 145 h 7 m over 54 runs (physics change nodes 116.9 h) |
| Slices | 100, all accepted: 43 first time, 36 after one steer, 14 after two, 7 at the ceiling of three attempts |
| Plan-review rounds | about 103 on codex, each a hand-built orchestrator request (106 inline Python request builders in the CM's transcript, one saved builder written on day five) |
| Owner decisions | 29 of 71 recorded decisions; 7 forced by harness or sandbox stops, 4 of those by the `docs/dev` exclusion |
| Reviewer-seat failures | 35 of 408 child code-review commissions (codex 14, usage limit; deepseek 17: 11 capacity exits, 5 timeouts, 1 empty report; claude 3, OAuth expiry; adacs/qwen 1) and 1 of 147 drift audits (a SIGTERM) |
| Hand work in the dossier | about 550 inline string-replacement edits of `work.md`, `plan.md`, `evidence.md`, `decisions.md`; 16 hand copies of child output to the results volume |
| Spend | not recorded: the ledger has no token or cost field |

The machinery worked: no node merged without its ratchet, the protected paths held, every runner was fail-closed and both refusals (one ratchet, one evidence launch) stopped before a wrong measurement, evidence at two heads around a documentation-only merge let the owner sign off without a fresh look at the science, and the acceptance audit on another model found two real scope readings. What cost time was hand work the toolkit could carry (plan-review requests, dossier bookkeeping, archiving with outputs, host verification) and one enforcement mechanism that fought git (the `docs/dev` exclusion).

Two facts about where things run matter for every item below. Child code reviews and drift audits are launched by the PM's own `pm.py review` (`skills/project-manager/scripts/pm_lib/review.py`, a `subprocess.Popen` of the harness command; it shares no code with `skills/orchestrator/`, and `pm_lib/__init__.py` forbids importing from it), and their failure reasons are written there (`timeout`, `exit N`, `empty report`); the ledger only projects them. The CM's plan reviews, surveys and the acceptance audit run through `skills/orchestrator/scripts/delegate_jobs.py`, which already marks a clean exit with no `SECTION:` output as `incomplete` and fails `wait` on it, so the "clean exit, empty report" the campaign saw comes only from a request that declares no `SECTION:` contract (the orchestrator's investigation and plan-review templates declare one; its read-write template, and the drift-audit and code-review requests that state no `expected_output`, do not).

---

## The work, prioritised

Priority is by cost removed and risk reduced. Items 1 to 7 are the set to do before the next campaign; 8 to 12 the next tier; 13 to 18 are small. Paths are as of `b496bf5`.

### 1. Mechanical in-repository exclusions (C, T)

**Evidence.** The brief excluded `docs/dev/` from every seat. Node worktrees held it on disk and the sandbox denied reading it, which made every working-tree `git diff` print the committed content as "deleted" files; two codex reviewers sent that to their provider. The fix was a sparse checkout in each node's `setup.sh` plus a dossier history rewrite; then 37 clones nested under `output/` held readable copies, fixed by a deny at any depth, which then made every full checkout fail inside the sandbox, so every runner had to clone sparse. Four owner rulings, a 400-word access paragraph pasted into every launcher, two breaches. Cause: an exclusion enforced by a read denial inside a git worktree, the one place a denial changes git's output rather than silencing it. The exclusion's authority is the brief's *Exclusions* section, protected by the owner's digest (`references/campaign-mode.md`, goalpost rule 2); the mechanics belong in the launcher.

**Change.** `launch-child` reads the brief's *Exclusions* and, for every excluded path inside the product repository, applies a sparse checkout to the node worktree before the sandbox starts, adds the deny-at-any-depth entry to the derived policy, and refuses a launch whose worktree still holds the path. The derived policy grants the node's Claude transcript directory unconditionally (`~/.claude/projects/<mangled worktree path>`, read and write; `references/sandbox.md` explains why both): it is per node and inert when no seat uses Claude, and `launch-child` knows only the child PM's harness, not the seats', so a conditional grant could not be made (an OpenCode child PM with Claude Developers or reviewers is a supported combination). The campaign carried this as a 47-entry literal grant list in the base policy. The example policy in `references/sandbox.md` loses its per-node entries and its in-repository deny (both now derived), and the reference keeps its disclosed residual that in git history the exclusion rests on instructions. Design choices to settle: (a) the brief's *Exclusions* section is free text today (`- <path no seat may read>` and a `CM only:` line); the launcher needs a parseable form that says which entries are paths inside the product repository, so `references/dossier-template.md` is a target; (b) how the child receives the list, since a sandboxed child cannot read the dossier: `launch_child` writing the paths into the node's `child.json` is the obvious route; (c) `git sparse-checkout` in a linked worktree sets `extensions.worktreeConfig` in the product repository's shared config, a lasting change like the HANDOFF exclude line the contract already discloses; either disclose it or use `skip-worktree` bits.

**Targets.** `skills/project-manager/scripts/pm_lib/campaign.py` (`launch_child`), `pm_lib/sandbox.py` (the derived policy), `references/dossier-template.md` (*Exclusions*), `references/sandbox.md` (*What the toolkit adds*, *The example policy*), `references/campaign-mode.md` *Launching and watching a child*.

**Accepts.** In the offline trial of *Order and acceptance*: the node worktree has no excluded path on disk, `git status` and `git diff` in it are clean, a nested clone under `output/` is refused by policy, and a unit test of the derived policy shows the node's transcript grant. In the real-harness run: the sandboxed Claude child writes its transcript with no base-policy entry.

### 2. Exclusion-aware lint and health (T)

**Evidence.** `lint.py check --base` and `health.py analyze --base` diff the working tree with no exclusion pathspec, and `lint.py` writes the whole base tree to `$TMPDIR` so base-side tools find the base's own project configuration (`skills/lint/scripts/lint.py`, the `base_tree` materialisation; this whole-tree copy is deliberate and tested, `skills/lint/tests/test_lint.py`). Under the exclusion both printed excluded content, and the base copy wrote excluded blobs to `$TMPDIR`. One Developer ran `lint.py` against a plan's ban because the Developer prompt requires the lint command verbatim (`references/developer-prompt.md`, the lint step), a rule that exists to stop a substituted linter producing a false pass.

**Change.** Both scripts take an exclusion pathspec; `base_tree` skips paths matching it and keeps everything else (project configuration included); `health.py` likewise. The Developer prompt stays verbatim: the child PM renders the lint command (`pm_lib/prompts.py`) with an `--exclude` taken from the exclusion list `launch_child` gave it (item 1's design choice (b)), so no seat edits it. Once item 1 makes node worktrees sparse, the working-tree `git diff` no longer reports excluded paths as deleted; what item 2 still covers is the base-tree copy and any checkout that is not sparse.

**Targets.** `skills/lint/scripts/lint.py` (`base_tree` and the diff scope), `skills/code-health/scripts/health.py` (the `--base` diff), `pm_lib/prompts.py` or wherever the Developer prompt's command is rendered, `references/developer-prompt.md`.

**Accepts.** `lint.py check --base <commit> --exclude docs/dev` on a tree with `docs/dev` present reports nothing from it, writes nothing from it to `$TMPDIR`, and still finds the base's `ruff.toml`; `lint`'s existing tests pass; the same for `health.py`.

### 3. Plan review as a command (C)

**Evidence.** About 103 plan-review rounds, each composed by hand from the node's plan, policy, decisions list, files and the previous round's report and dispositions; 106 inline request builders in the CM's transcript. The largest single class of CM hand work.

**Change.** `pm.py review-plan <node> --seat <tool/model/effort>` builds the orchestrator request from the node directory (plan, policy, the decisions it cites, the files it names, the prior round's report and the CM's dispositions), on the orchestrator's existing plan-review template (`skills/orchestrator/references/templates.md`, the one with a `SECTION:` contract), launches it as a subprocess of `delegate_jobs.py` (a second documented cross-skill exception in `pm_lib/__init__.py`, path-based like `code_metrics`, never an import), numbers the round itself from `nodes/<node>/reviews/`, records the request, report and dispositions there, and fails on an empty or `incomplete` report. The CM passes `--seat` from its launcher's plan-review seat line (item 10); the launcher is pasted text, so no command reads it.

**Targets.** `pm_lib/campaign.py`, `pm_lib/cli.py`, `pm_lib/__init__.py` (the exception), `references/campaign-mode.md` *Nodes*.

**Accepts.** One command replaces the hand-built request for a node in the trial; the recorded round holds the request, the report and the dispositions; an empty report is an error.

### 4. Dossier bookkeeping commands, the smallest set (C)

**Evidence.** `work.md` was edited 205 times, `plan.md` 197, `evidence.md` 59 and `decisions.md` 83 by Python heredocs; evidence and decision numbers were assigned by hand; the budget table and one node row fell stale until the close because the CM did not update them. The shakedown review ruled "trim the table rather than add tooling" when a scripted edit mangled a row (`shakedown-review-20261025.md`, the `work.md` table item). About 550 edits in one campaign is the reason to revisit that ruling for the two things a tool can own without a schema: numbering and timestamps.

**Change.** `pm.py dossier add-decision` and `add-evidence` append an entry with the next number and a timestamp; `set-head` stamps a node row's head. No sidecar, no renderer: the Markdown stays the source, `work.md` *Now* stays the CM's own handoff, and a value a tool cannot know (a row's status) stays the CM's to write. Trim the node table as the shakedown review said.

**Targets.** `pm_lib/campaign.py`, `pm_lib/cli.py`, `references/dossier-template.md` (the trimmed table).

**Accepts.** A decision or evidence entry is never hand-numbered in the trial; the three commands are idempotent on a re-run with the same text.

### 5. Archive with the child's outputs (C)

**Evidence.** `stop-child` tears down the sandbox temp directory with whatever the child left there, and the CM's own `archive_node.sh` (71 lines, run 54 times) kept only the PM state and named artifact directories, so the CM copied child output to the results volume by hand 16 times and outlier CSVs 5 times before archiving. The brief already names the product's git-ignored output directories (`references/dossier-template.md`, *Allowed paths*), and *Cleanup* in `references/campaign-mode.md` already orders capture before removal and keeps the worktree when capture is incomplete.

**Change.** `stop-child --archive <results root>` does *Cleanup*'s capture (HANDOFF, `.pm/campaign`, `.pm/runs`, the state root and receipt artifacts into the dossier's `validation/<node>/`, uncommitted work as patches), copies the brief's named output directories and the sandbox scratchpad to the results root, and only then removes the worktree (branch kept) and the temp directory; a copy that fails or is incomplete leaves the source in place and stops, as *Cleanup* already requires. A nested clone under an output directory (item 7's sparse clone) is copied as files, not as the gitlink `git add -A` would record. The sentence "the toolkit never reads them" in *Child output* changes to say the archive step copies them.

**Targets.** `pm_lib/campaign.py` (`stop_child`), `references/campaign-mode.md` *Cleanup* and *Child output*.

**Accepts.** After `stop-child --archive` in the trial, the dossier's `validation/<node>/` holds the child's state and the results root its outputs and scratchpad; with the results root made unwritable, nothing is removed and the command exits non-zero naming what it could not copy.

### 6. Reviewer failure classes and a bounded re-commission, where the reviews run (T)

**Evidence.** 35 child code-review commissions failed (the table above); the ledger records only `exit 1`, `exit -15`, `timeout` and `empty report`, so a usage limit is indistinguishable from any other non-zero exit, and each failure was a hand re-commission by a child PM, sometimes with a seat change the owner had to rule (the campaign's D12, D51(2)). These failures happened in `pm.py review` (`pm_lib/review.py`), not in the orchestrator. The shakedown review declined a seat probe before reviews because it duplicates the launch path (its D3), and nothing in this campaign changes that reason: a probe that passed would not have prevented the capacity exits and timeouts minutes later.

**Change.** In `review.py`: classify a failed review from its captured stderr (usage limit, auth, capacity, timeout, empty) and record the class as the `reason`, so the ledger (item 14) carries it; and a bounded re-commission: `pm.py review` gains `--fallback <tool/model/effort>`, filled by the child PM from a new fallback-reviewer field in its launcher (the Mode B launcher in `skills/implementation-plan/SKILL.md` and the campaign launcher's child-seats line both gain it), and on a classified failure re-commissions by class: a transient class (capacity, timeout, empty) retries once after a short wait on the same seat and then once on the fallback; a limit or auth class, which no wait clears, goes straight to the fallback; all without a hand-written second request. Design choice: whether the wait and the retry count are fixed or options. In `delegate_jobs.py`: classify a usage-limit or auth message in the output of a request that declared no `SECTION:` contract as `failed`, the one case `incomplete` does not already catch. No probe.

**Targets.** `pm_lib/review.py` (the failure path), `pm_lib/ledger.py` (the reason field), `skills/implementation-plan/SKILL.md` and `references/campaign-mode.md` *The campaign launcher* (the fallback field), `skills/orchestrator/scripts/delegate_jobs.py`, `skills/orchestrator/references/delegate-contract.md`.

**Accepts.** A review whose primary seat fails twice on capacity and whose fallback then succeeds produces three commissions, and one whose primary seat fails on a usage limit produces two, every commission in the ledger with its class; a contract-less orchestrator request whose output is a usage-limit message ends `failed`.

### 7. The evidence-runner skeleton: the deltas the campaign's runners added (T)

**Evidence.** The skeleton (`references/evidence-runner.sh`) already has `measure`, `pin`, `nonempty`, the inherited-variable record and unset, the clean-tree and HEAD checks, `context.txt`, the refusal to run twice and the manifest. Every one of the campaign's ten evidence runners (9,685 lines in all) added the same four things on top: a sparse clone of the measured commit under `output/`, a `require` helper that stops the run when a precondition of the next stage is missing, a manifest stage that lists its files to a file and checks that each stage wrote what it declared (the skeleton's manifest once hid a stage that wrote nothing), and a clean-tree check that ignores the shell's transient `sh-thd-*` heredoc files, which the sandboxed shell leaves in the worktree while a heredoc runs (one ratchet refused and reran on one). The shakedown review's B3 made the runner the plan's business; four identical additions across ten runners is the evidence that these four belong in the skeleton.

**Change.** Add exactly those four to the skeleton: a sparse-clone step, `require`, the per-stage manifest check (it judges capture, not outcome, which keeps the skeleton capture-only) and the `sh-thd-*` exception, with one line each in the header. Nothing else.

**Targets.** `references/evidence-runner.sh`; one sentence in `references/campaign-mode.md` *Validation*.

**Accepts.** A fixture runner in the toolkit's own tests (two measured commands, one of which writes nothing) is refused at the manifest stage naming the empty stage, and runs clean when both write; `shellcheck` clean.

### 8. Templated ratchet nodes (C, R)

**Evidence.** 22 ratchet nodes ran on one reviewed template through the CM's `make_rnode.py` (26 invocations) and `rnode_receipt.py` (21), each differing from the template only in named substitutions, with the substitution diff recorded as its review.

**Change.** `pm.py new-node --template ratchet --head <sha>`: a node generated from a reviewed template by named substitutions, with the substitution diff as its plan review and a receipt writer driven by the runner's `context.txt` and status files. This amends the plan-review rule in `references/campaign-mode.md` *Nodes* (a plan node passes `check-plan` and a review) for the templated kind only: the template is reviewed once, the instance's review is the substitution diff; the owner approves the amendment with the first template.

**Targets.** `pm_lib/campaign.py`, `pm_lib/cli.py`, `references/campaign-mode.md` *Nodes*.

**Accepts.** A ratchet node generated in the trial carries its template's digest, its substitution diff and a receipt written from `context.txt`; a substitution outside the template's named fields is refused.

### 9. Host-check receipts (C, R)

**Evidence.** Seven aligned rebuilds, two converter host runs, five T2 reruns and two judge scripts, each a hand-written `verify.sh` derived by `sed` from the first: a pinned runner or gate run on the host, outside any sandbox, in a sparse throwaway clone at a commit, with a receipt-grade log. Two of them were the evidence (the sandbox could not read one sysctl, so two converter cases needed a host run per measurement, an owner-accepted denial); the rest were the CM's own verification before a merge. The contract's dividing line (`references/campaign-mode.md` *Nodes*: anything that executes or measures is a plan node; *Validation*: the CM measures through evidence nodes) does not cover either use today.

**Change.** `pm.py host-check <runner> --at <commit>`: the same sparse clone and capture, the log hashed, a `context.txt` written. Both uses execute and measure, which the dividing line reserves to plan nodes, so the item needs a contract amendment for the owner to approve before it is built: a host check is the evidence only for a measurement the brief records as impossible in the sandbox (an accepted denial), and otherwise is the CM's own verification, recorded but never the receipt.

**Targets.** `pm_lib/campaign.py`, `pm_lib/cli.py`, `references/campaign-mode.md` *Validation* and *Nodes*.

**Accepts.** A host check in the trial produces a hashed log and `context.txt`, and its record names which of the two uses it is.

### 10. The launcher's missing seat lines (T)

**Evidence.** The campaign launcher (`references/campaign-mode.md` *The campaign launcher*) names the child seats; the task-node, plan-review and audit seats were the CM's choice because no line names them (the campaign's D23). The 400-word access paragraph the CM pasted into every child launcher (`fill_launcher.py`, 34 invocations) existed only because the exclusion was not mechanical; the contract already carries exclusions down through the plan's *Explicit Non-Goals* and task prompts and nothing else, and once item 1 lands the paragraph has no reason to exist.

**Change.** Add the task-node, plan-review and acceptance-audit seat lines to the campaign launcher. No generated paragraph, no new file.

**Targets.** `references/campaign-mode.md` *The campaign launcher*.

**Accepts.** The launcher names every seat a campaign uses; the next campaign's dossier records no seat choice the launcher did not name.

### 11. Sandbox reference facts (T)

**Evidence.** Three facts the seats learned the hard way and the reference does not carry: srt injects `GIT_CONFIG_PARAMETERS` into the sandboxed environment (the campaign's own runner refused on it; the skeleton's environment record lists only make, git and Python variables, so nothing in the toolkit changes); `os.sysconf("SC_SEM_NSEMS_MAX")` behind `ProcessPoolExecutor` is denied, so a converter test suite needs a host run; a packed-refs write in a linked worktree is denied with a misleading error in panes. (That a full checkout fails under a deep deny is already at *What it is not*.)

**Change.** Three entries in `references/sandbox.md` *Per-harness facts* and *Troubleshooting*.

**Accepts.** The three facts are in the reference with their symptom and the workaround.

### 12. `answer-child --follow-up`, and `wait-child`'s marker echo (T)

**Evidence.** The CM wanted to send a second message to a question it had already answered (the child had acted on a partial answer); `answer_child` delivers one answer per question. Once, `wait-child` matched a marker string that was the child's own echo of the command that would print it, and returned early.

**Change.** `answer-child --follow-up <text>` delivers a further message to the last answered question; `wait-child` matches a marker only at the start of an output line, never inside a command echo. (A `--hold` is not needed: not calling `answer-child` already holds the child.)

**Targets.** `pm_lib/cli.py`, `pm_lib/campaign.py` (`answer_child`, `wait_child`).

**Accepts.** A unit test for each: a follow-up reaches the child's question channel; an echoed marker does not end a wait.

### 13. Seats, as advice for the next campaign's owner (S)

**Evidence** (the ledger, 54 runs, 100 windows; the PM's 0–2 rating): claude opus-5-5 high as code reviewer 146 commissions, 3 failed, mean 1.88, won 181 of 229 panel pairs, median 245 s; codex gpt-6.1-sol high 93, 14 failed (usage limit), 1.24, won 23 of 157 pairs; opencode deepseek-v4.1-flash max 86, 17 failed, 1.61, median 1,033 s; opencode qwen3.8-flash xhigh 70, 0 failed, 1.46; opencode adacs/qwen 13, 1 failed, 1.00, won 0 of 24 pairs. Drift audit: sonnet-5-5 high 79, 0 failed, 1.82, median 120 s; deepseek 68, 1 failed, 1.91, median 793 s. Developers: opus high on hard slices 11 of 57 accepted without a steer; sonnet medium on easy slices 28 of 32 (1.81). Plan review, surveys and the acceptance audit ran on codex outside the ledger and found the real defects (two runner defects, one plan-wording defect, two scope blockers at the audit): the place a different model earned its spend.

**Advice,** for the next campaign's brief and launcher in its dossier, never for the toolkit's templates (`VISION.md`: every supported tool is eligible for any seat, and enforcement differences are documented without becoming rankings): code panel opus high first seat, deepseek-v4.1-flash max second with one retry then qwen3.8-flash xhigh, adacs/qwen dropped, codex sol only as a third seat on instrument nodes and for the acceptance audit; plan review on a Claude seat other than the plan's author, codex sol for instrument plans and the first round of each physics node; drift audit sonnet high; Developers opus high hard, sonnet high moderate, sonnet medium easy and evidence nodes. The owner's budget note stands: codex is limited and reserved for diversity on the most important tasks.

### 14. Ledger: the failure class (T)

**Change.** The `reason` of a failed review record carries the class item 6 assigns (limit, auth, capacity, timeout, empty) and the leaderboard renders a failures-by-class column. Token spend per commission is recorded where the harness reports it, else left null. Plan-review and task-node seats stay outside the ledger (they have no PM run to sign), and re-rendering the campaign board at every node close is one existing command (`pm ledger render --run-tag`) the launcher tells the CM to run.

**Targets.** `pm_lib/review.py` (the producer), `pm_lib/ledger.py`, `pm_lib/leaderboard.py`, `references/model-ledger.md`.

**Accepts.** The sage26 ledger copy, re-rendered, shows a class for every failed record that has one and `unclassified` for the rest.

### 15. The final evidence bundle is every receipt (C)

**Evidence.** The claims read at the end raised 357 flags and took most of the CM's last day, because the final evidence node's bundle was two receipts while the documents cited every node's measurement, and the documentation nodes wrote statements without a record citation per statement.

**Change.** `references/campaign-mode.md` *Validation*: the final evidence node's bundle is every receipt and every accepted node's verification entry; the dossier template's targets section says, once, that a documentation target's measured statements each name their record as they are written, so the claims read is a lookup.

**Accepts.** The reference and template carry the two sentences; nothing else changes.

### 16. Scope readings (R, one clause)

**Evidence.** The acceptance audit's two blockers were both CM readings of target wording that narrowed what a target reports or claims, recorded as CM decisions; the owner ruled both at sign-off. The contract already sends anything touching a target to the owner (`references/campaign-mode.md`, the stops list in *Purpose and authority*); the CM read "narrowing what a target reports" as not touching it.

**Change.** One clarifying clause on that list: a reading that narrows what a target reports or claims is a change to the target.

**Accepts.** The clause is in `references/campaign-mode.md` *Purpose and authority*; nothing else changes.

### 17. Side records the owner asks for (C)

**Evidence.** The owner asked for `sage16 lesson:` entries during the campaign; none were written, because a side record has no node, no receipt and no table row to fall stale. The review reconstructed them afterwards.

**Change.** `references/dossier-template.md`: a *Side records* section in `work.md` (the CM's file; `brief.md` is the owner's and digest-frozen, so a mid-campaign ask cannot go there), one line per record the owner asks for with where it is written, added when the ask is made; `references/campaign-mode.md` *Acceptance and retrospective* adds the section to what the retrospective records.

**Accepts.** Both references carry the section; a trial retrospective lists each side record with its location or "not written".

### 18. Notification (T, one line)

**Evidence.** The owner's `ping-me` was called 53 times and dropped on the last day for unrelated problems; the owner's global instructions still name it. A wrapper command would not fix a broken notifier.

**Change.** One line in the campaign launcher naming the notification command and the events (node close, owner question, stop), so the CM has no judgement to make about when to call it.

**Accepts.** The line is in `references/campaign-mode.md` *The campaign launcher*.

---

## Declined from the campaign's own suggestions

- A seat probe before review clusters (the shakedown's D3 stands: it duplicates the launch path and cannot predict a capacity failure minutes later; failure classes and a bounded re-commission, item 6, address what actually happened).
- A dossier sidecar and renderer (the shakedown's "trim, don't tool" stands for table state; item 4 keeps only numbering and stamps).
- A `campaign.yaml` holding seats and access rules (the brief's digest is the access rule's only protection; item 10 adds the missing seat lines to the existing launcher instead).
- A `pm.py notify` command (item 18 is one launcher line).
- A sanctioned state-changing task node for one-commit closing work: the owner ruled the one case acceptable after verification, and the dividing line (anything that changes state is a plan node) stays as it is.

## What not to change

- The brief's structure: targets with tolerances, "who supplies the evidence", reserved decision classes, the no-loosening rule. The audit found no loosened target; every reserved decision was the owner's.
- Capture-only, pinned, fail-closed runners written by the CM, not by the measured party; the skeleton stays capture-only (item 7 judges capture, never outcome).
- Evidence at two heads around a documentation-only merge; the CM's own host re-verification before every merge; the acceptance audit on a different model.
- The sandbox and the exclusion list themselves; only their enforcement mechanics move into the toolkit (items 1, 2).
- Serial merges with `--no-ff` and a ratchet at every head.
- `pm_lib`'s stdlib-only, no-orchestrator-import rule (item 3 adds a subprocess, not an import).

---

## Order and acceptance

Do item 4 before item 3 (so review rounds are recorded by the commands that number things), items 1 and 2 together (item 1 removes most of item 2's trigger), item 10 with item 1 (both read the brief's *Exclusions*), and item 14 with item 6 (producer and consumer of the failure class). Items 8 and 9 need the owner's approval of their contract amendments before they are built. Each item becomes a frozen implementation-plan slice when its design choice above is settled; the acceptance lines above are the slice criteria. The whole is accepted in two parts: the PM README's *Verify campaign mode* trial (a fake child, no model) extended with a dossier fixture whose brief names one excluded product path, so the exclusion mechanics, the bookkeeping and the archive step are exercised offline; and one real-harness run of a single node under srt with a sandboxed Claude child, which the trial does not cover and which this work must budget for. Merge `feature/pm-campaign-mode` to `main` first or with this work; the owner's call.
