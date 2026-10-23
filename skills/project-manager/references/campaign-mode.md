# Campaign Mode: the Campaign Manager's Operating Contract

Campaign mode turns an owner-approved goal into a validated outcome across many small plans. One `project-manager` seat in campaign mode, the Campaign Manager (CM), writes small node plans, runs each through an ordinary child PM in its own worktree and tmux session, judges each node from the child's evidence, and merges accepted change nodes one at a time until every target in the brief passes. This file is the CM's operating contract and holds the campaign launcher, its only copy. The dossier's files are laid out and templated in [dossier-template.md](dossier-template.md); everything a child PM does is [SKILL.md](../SKILL.md), unchanged.

Every `pm.py` command below is `python3 $PM <command> …`, with `PM=<path-to>/skills/project-manager/scripts/pm.py`, as the [README](../README.md#verify-your-setup-no-real-model-1-minute) sets it.

## Purpose and authority

A PM in plan mode never writes plans. The CM does, and its only authority to do so is `brief.md`: the brief the owner approved, frozen by a SHA-256 digest the owner supplies each time they start or resume the CM. The brief sets the goal, the targets and their tolerances, the paths the campaign may touch, the protected-paths policy file, the reserved decision classes and the budgets; the CM plans inside it and never edits it. This is a deliberate revision of the PM role, recorded in [VISION.md](../../../docs/VISION.md) *The Roles*: the human-approved contract moves up one level, from the plan to the brief, and a PM in plan mode keeps the prohibition.

## Seats

| Seat | Skill | Authority | Never |
|---|---|---|---|
| Owner | a human | approves `brief.md` and supplies its digest; approves reserved decision classes; answers questions to the owner and every human gate; signs off acceptance | — |
| Campaign Manager (CM) | `project-manager`, campaign mode | writes node plans and per-child policy files within the brief; launches, watches, answers, judges and stops child PMs; merges accepted change nodes; keeps the dossier | implements; supervises a plan it wrote; changes `brief.md`; answers a human gate; loosens a target or tolerance |
| Child PM | `project-manager`, plan mode, unchanged | supervises one node plan exactly as [SKILL.md](../SKILL.md) says, launched from the Mode B launcher with its campaign-child paragraph | writes plans or adds nodes, so depth is fixed at two |
| Developers and reviewers | unchanged | as in plan mode, launched and commissioned by the child PM | as in plan mode |
| Read-only delegate | `orchestrator` | reads, investigates and reports for a task node | edits, builds, tests or commits |

## The dossier

The dossier is the CM's whole state: a git repository of its own, outside the product repository, laid out as [dossier-template.md](dossier-template.md) shows. `brief.md` is the owner's and the CM only reads it. `work.md`, `evidence.md`, `decisions.md` and `validation/` are the CM's; an owner approval in `decisions.md` names the owner as its actor. Node plans and per-child protected-paths policy files live in the dossier, under `nodes/<node>/` beside the node's filled launcher, and are committed there before the node launches. The CM has no HANDOFF.md of its own: `work.md` *Now* is its handoff, kept current at every step, so a CM that dies mid-loop loses nothing a restart needs.

Two goalpost rules hold the brief still:

1. **Decisions use permissions, never create them.** Every entry in `decisions.md` names the `brief.md` permission it uses; a decision no permission covers is a question to the owner.
2. **`brief.md` is protected by its owner-supplied digest.** Check it with `shasum -a 256 brief.md` in the dossier and compare the hex digest with the one the owner supplied; any difference stops the CM before anything launches, and is a question to the owner.

Reconcile is how the CM starts, and restart is reconcile: there is no other recovery. Its inputs are `brief.md` checked against the digest; `work.md`; `git worktree list` in the product repository; each in-flight node worktree's token-free `pm status` (run from inside the worktree with no `PM_RUN_TOKEN` in the environment, so its output is unverified) and its `wait-child --worktree <worktree>` output; and every child's `run-report.md`. It ends in four confirmations:

1. Every target has work in flight, a passing receipt at the integration head, or an open gap recorded in `work.md`.
2. Every node traces to a target.
3. No failure has silently disappeared: every failed receipt, stopped child and open finding is still on the record.
4. Budgets hold, per target and campaign-wide.

A confirmation that fails is fixed in `work.md` before anything launches, or becomes a question to the owner when fixing it needs one.

## Nodes

| Kind | Produces | Runs as | Surface | Merged |
|---|---|---|---|---|
| Change | a product change toward a target | a child PM on a node plan | product paths, disjoint from every protected path | yes, once judged and accepted |
| Evidence | a measurement of a target at a recorded product SHA, as a report and its receipt | a child PM on a node plan | its report path, `campaign-reports/<node>.md` | no: the branch is kept and the receipt copied into `validation/<node>/` |
| Task | a reading: an investigation, a plan review, an audit | the read-only delegate through `orchestrator` | none: no worktree and no branch | no: findings go into `evidence.md` |

The dividing line: anything that executes, builds, tests, generates outputs or changes state needs a plan node, run by a child PM under the floor; reading is a task node. A delegate's `files` must sit beneath the delegate's repository, so the dossier excerpts a task node needs go inline in its prompt. Plan review is a read-only task node whose prompt embeds `implementation-plan`, plus `check-plan --plan <dossier>/nodes/<node>/plan.md --repo <product repo> --policy <dossier>/nodes/<node>/policy.json`; a node launches only when both are clean.

Node plan conventions:

- A reserved node's plan says `Approval needed before implementation: no` and names the `decisions.md` id that approved it, because the owner's approval is recorded in the dossier before launch and a child PM has nobody to ask.
- Every authorized entry stays disjoint from every protected path; `check-plan --policy` refuses an overlap.
- An evidence node's surface is its report path, `campaign-reports/<node>.md`, and nothing else.
- The child's run tag is `campaign/<id>` and its node branch is `campaign/<id>/<node>`, where `<id>` is the campaign id the brief names.

## The loop and its rules

1. Reconcile (*The dossier*).
2. Choose the next node from `work.md`: a target with an open gap, its dependencies met and its budget holding.
3. Write the node plan with `implementation-plan` at `nodes/<node>/plan.md` and the child's policy file at `nodes/<node>/policy.json`, and add the node's row to `work.md` *Nodes*.
4. Review the plan with a plan-review task node and `check-plan --policy` (*Nodes*), and revise it until both are clean.
5. For a reserved class, record the owner's approval in `decisions.md` before launch; without it, ask the owner and do not launch.
6. Fill the launcher at `nodes/<node>/launcher.md`, commit the plan, policy and launcher in the dossier, record base SHA, branch, worktree and policy in `work.md`, then `launch-child` (*Launching and watching a child*) and record the session and socket it prints.
7. Wait with `wait-child`, answering each question with `answer-child`, until `.pm/campaign/child-result.json` appears or the child's session dies.
8. Judge the node once, and record the judgement in `work.md` and the findings in `evidence.md`.
9. For an accepted change node, merge it with `git merge --no-ff` (*Merges*); for an evidence node, copy its receipt into `validation/<node>/receipt.md`.
10. Clean up the child: `stop-child`, the reviewer check, the copy into `validation/<node>/`, then `git worktree remove` (*Cleanup*).
11. Ratchet: after a merge, run the cheap checks as an evidence node at the new integration head, through steps 3–10 (*Validation*).
12. Update `work.md` *Now* and return to step 2. When every target has a passing receipt at the integration head, go to *Acceptance and retrospective*.

A task node takes steps 2, 8 and 12 only: its prompt goes to the read-only delegate through `orchestrator`, and its findings go into `evidence.md`.

The rules:

- **Judge once.** Judge a node from the child's `run-report.md`, its assessments, its Developer judgments, its recorded delegated plan decisions, the residuals in its HANDOFF.md, and the whole node diff, which is always `git diff <integration head>...<branch tip>`; a wrong delegated decision is corrected through a later node, never by editing the child's run. A follow-up node starts from the integration head by default; when it starts from a stopped node's branch head, the inherited unaccepted commits are part of the diff the CM judges and reviews before any merge. The CM commissions its own review only when: (a) the node is a reserved class (one `drift-audit` of the node diff against the brief's protections); (b) the child's report carries tolerances or plan defects the CM cannot verify itself; (c) the child scored its Developer 0, or stopped, where the review diagnoses the stop for re-planning and never readies the node for a merge; (d) the question is new: an interaction between nodes, or an instrument's validity.
- **Merges.** Record the merge in `work.md` before it happens, and merge one node at a time with `git merge --no-ff campaign/<id>/<node>` in the product repository's checkout of the integration branch. Merge only a node whose child run is `complete` and whose branch tip is its last accepted slice commit, checked by comparing `git rev-parse campaign/<id>/<node>` with the commit the child's last accepted assessment names (a stopped or still-`active` child's tip may carry unaccepted work). A follow-up node starts from a stopped node's branch head only when `git diff --name-only --no-renames <integration head>...<stopped head>` touches no protected path, because no floor ever re-checks inherited commits; otherwise it starts from the integration head.
- **Conflicts.** On a merge conflict, `git merge --abort`, then rerun the node from the new integration head under a new node id and branch; never rebase an accepted branch.
- **Stopped children** end their node and are never resumed; the CM plans a follow-up node from the branch head when useful, under the merge rule above.
- **Cleanup** of a finished or dead child, in this order: run `stop-child --worktree <worktree> --reason "<why>"`, which captures the pane to `.pm/campaign/pane.txt` (for a dead pane, the placeholder `capture_to` writes), prints the reviewer process groups the child's `run.json` files record, and kills the child's tmux server; check by hand for those reviewer process groups, which sit outside the child's tmux server; copy HANDOFF.md, `.pm/campaign/`, `.pm/runs/<id>/` and the authoritative state root `<worktree-git-dir>/pm/<run-id>/` (`run.json`, `events.jsonl`, the assessments; find the git dir with `git -C <worktree> rev-parse --absolute-git-dir`) into `validation/<node>/`, and record `git -C <worktree> status --porcelain` and `git -C <worktree> diff` there too, because a child that died mid-slice can leave a Developer's uncommitted edits; then `git worktree remove <worktree>`, keeping the branch. `git worktree remove` refuses a worktree with modified or untracked files and `--force` discards them, so `--force` is used only after that capture. The removal deletes the worktree's git dir and with it the state root, which is why the copy comes first. An unscoped `stop --scavenge` or a bare `tmux kill-server` is never used.
- **Reserved approvals** move up to `decisions.md`, recorded before launch.
- **Budgets** hold per target and campaign-wide, as `brief.md` sets them; two failed attempts at one target is a question for the owner.
- **Child output** stays out of git's way, in git-ignored output directories or the slice artifact directory. A brief may name the git-ignored output directories a product uses; the toolkit never reads them.

## Launching and watching a child

Fill the Mode B launcher from `implementation-plan`'s SKILL.md → "Next Chat Prompt Format", including its campaign-child paragraph, with the node plan's dossier path, the node worktree as its repo, the child's seats from the campaign launcher, the run tag `campaign/<id>`, the node branch, the node's policy file and the child attempt budget from `brief.md` as `--max-attempts`; write it to `nodes/<node>/launcher.md` and commit it. Then launch:

```sh
python3 $PM launch-child --repo <product repo> --base <integration head> --branch campaign/<id>/<node> \
  --worktree <trusted parent>/<node> --prompt-file <dossier>/nodes/<node>/launcher.md \
  --harness <child PM harness> --model <full model id> --effort <effort>
python3 $PM wait-child --worktree <trusted parent>/<node> --wait <node wait length>
```

Issue that `wait-child` as one long background command and let it return (never poll). It returns on one of three signals:

- **A question** (`cm-question.md`, printed after `question present: yes`): decide it; record it in `decisions.md` when the answer uses a `brief.md` permission; answer with `answer-child --worktree <worktree> --text "<answer>"`; and wait again. A question that needs the owner goes to the owner first.
- **A dialog marker**: read it as *Reading the pane* in [SKILL.md](../SKILL.md#reading-the-pane) says. A dialog the child is waiting on goes to a human; ordinary output that merely contains a marker string does not, and a child PM's pane routinely shows PM vocabulary and Developer pane tails, so the CM reads the pane and waits again.
- **The result** (`.pm/campaign/child-result.json`, printed with its `status`): judge the node. The `child run …` lines are a tokenless cross-check, never a fact.

A wait that elapses with no signal is information, as `observe`'s is: wait longer, or decide. A human may `tmux -L <socket> attach` at any time to watch the child and its Developers, which share the child's private server.

Node worktrees are created under one parent directory the owner has trusted through each harness's own mechanism, because an interactive harness may show a directory-trust dialog for a new path and `sessions.wait_until_ready` fails closed on it, so `launch-child` fails; a trust prompt at launch is a question for the owner, never something the CM acknowledges. Never reuse an explicit `--socket` across children; omit `--socket` to get the unique default, because a re-run of `stop-child` against an old manifest would kill a later child on a reused socket.

Each child gets one protected-paths policy file, `nodes/<node>/policy.json`, recorded in `work.md`. Every policy handed to a child lists the product repository's committed policy file path explicitly, because self-protection covers only a policy file inside the worktree, and the child's policy lives in the dossier. Write a protected directory with a trailing slash (`instruments/`): a slash-less entry naming a directory blocks overlapping plan surfaces but does not protect the files below it at `grant` or floor fact 5. An instrument-class node is launched with a policy that omits the instrument it builds. The policy actually used is readable in the child's `run.json` at `<worktree-git-dir>/pm/<run-id>/run.json` (`policy.protected_paths` and `policy.protected_paths_source`; see [run-state.md](run-state.md)); compare it with `work.md` when judging the node.

## Validation

- **Trust before use.** An instrument counts only after it passes a known-good control and fails a known-bad one, and its verdict is always pass, fail or error.
- **Independence.** Instruments are built in owner-approved nodes and then protected, so no later node can change the instrument a target is measured by.
- **Measurement ownership.** The CM measures through evidence nodes, run by a child PM at a recorded SHA. An implementing Developer's claim of success points at evidence and is never the evidence.
- **The ratchet.** A target that passed must keep passing: cheap checks re-run at every new integration head, expensive ones at milestones, and acceptance needs a final evidence node at the final head. Every receipt names the measured product SHA separately from the commit that adds the report.

A receipt records: the measured product SHA, the instrument version, the configuration, the command, the outcome (pass, fail or error), and the artifact hashes.

## Questions to the owner

A question to the owner is recorded in `work.md` *Now* with the nodes it blocks, the evidence, the options, a recommendation, and what continues meanwhile. Work the question does not block goes on.

## Acceptance and retrospective

A campaign is accepted on a final evidence node at the final integration head, an independent audit run as a read-only task node on a model different from the CM's, and the owner's sign-off. The retrospective records:

- owner attention: when and for how long the owner was needed;
- the questions asked, and whether each was necessary;
- child stop causes;
- plan versions per node, and nodes per target;
- spend and wall time.

## Worktrees and parallelism

Run serially first, with `max_parallel: 1` in `brief.md`. Going parallel is launching a second child before waiting on the first, under disjoint surfaces, with exclusive resources respected and merges still one at a time; the per-child tmux sockets and per-run ledgers already hold.

## The campaign launcher

Paste this into a fresh PM-capable session to start or resume a CM; it is the launcher's only copy.

```md
Dossier: <absolute path to the dossier repository>
Brief digest: <SHA-256 of brief.md, supplied by the owner>
Product repo: <absolute path>   Integration branch: <branch>
Protected-paths policy file: <path of the product's committed policy file, as brief.md pins it>
Child PM: <harness> <full model id> <effort>
Child seats: Developer <harness> <full model id> <effort>; drift auditor <harness> <full model id> <effort>; code reviewers <harness> <full model id> <effort>; …
Node wait length: <seconds for each wait-child --wait>

Use the project-manager skill in campaign mode. You are the Campaign Manager. Read this toolkit's skills/project-manager/references/campaign-mode.md in full, then the dossier.

Reconcile first: check brief.md against the digest above with `shasum -a 256 brief.md`, read work.md, and make the four confirmations. Then run the loop.

Never implement. Never supervise a plan you wrote. Never change brief.md. Never answer a human gate. Never loosen a target or tolerance.
```
