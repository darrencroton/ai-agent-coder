# Campaign Mode: the Campaign Manager's Operating Contract

Campaign mode (Mode C) turns an owner-approved goal into a validated outcome across many small plans. One `project-manager` seat in campaign mode, the Campaign Manager (CM), writes small node plans, runs each through an ordinary child PM in its own worktree and tmux session, judges each node from the child's evidence, and merges accepted change nodes one at a time until every target in the brief passes. This file is the CM's operating contract and holds the campaign launcher, its only copy. The dossier's files are laid out and templated in [dossier-template.md](dossier-template.md); everything a child PM does is [SKILL.md](../SKILL.md), unchanged.

Every `pm.py` command below is `python3 $PM <command> …`, with `PM=<path-to>/skills/project-manager/scripts/pm.py`.

## Purpose and authority

A PM in Mode B never writes plans. The CM does, and its only authority to do so is `brief.md`: the brief the owner approved, frozen by a SHA-256 digest the owner supplies each time they start or resume the CM; `campaign-preflight --digest` compares the brief with that digest, and running it first is the CM's own discipline (judgement). The brief sets the goal, the targets and their tolerances, the paths the campaign may touch, the protected-paths policy file, the reserved decision classes and the budgets; the CM plans inside it and never edits it. Inside the brief the CM decides: it applies [SKILL.md](../SKILL.md)'s *Reading a defective plan* to the brief, where the brief's targets, tolerances, reserved decision classes, protected paths, allowed paths and exclusions are the stops that always go to the owner, and a decision that touches none of them is the CM's under that rule, recorded in `decisions.md` with `Actor: CM` and its grounds. The human-approved contract moves up one level, from the plan to the brief, and a PM in Mode B keeps the prohibition.

## Seats

| Seat | Skill | Authority | Never |
|---|---|---|---|
| Owner | a human | approves `brief.md` and supplies its digest; approves reserved decision classes; answers questions to the owner and every human gate; signs off acceptance | — |
| Campaign Manager (CM) | `project-manager`, Mode C | writes node plans and per-child policy files within the brief; launches, watches, answers, judges and stops child PMs; merges accepted change nodes; keeps the dossier | implements; supervises a plan it wrote; changes `brief.md`; answers a human gate; loosens a target or tolerance; pushes, deploys or causes external side effects (its merges to the integration branch are local; a push to a remote is the owner's) |
| Child PM | `project-manager`, Mode B, unchanged | supervises one node plan exactly as [SKILL.md](../SKILL.md) says, launched from the Mode B launcher with its campaign-child paragraph | writes plans or adds nodes, which holds depth at two by the child's contract alone (a prompt-level rule: `launch-child` needs no token, so nothing mechanical stops a child launching one) |
| Developers and reviewers | unchanged | as in Mode B, launched and commissioned by the child PM | as in Mode B |
| Read-only delegate | `orchestrator` | reads, investigates and reports for a task node | edits, builds, tests or commits |

## The dossier

The dossier is the CM's whole state: a git repository of its own, outside the product repository, laid out as [dossier-template.md](dossier-template.md) shows. `brief.md` is the owner's and the CM only reads it. `work.md`, `evidence.md`, `decisions.md` and `validation/` are the CM's; an owner approval in `decisions.md` names the owner as its actor. Node plans and per-child protected-paths policy files live in the dossier, under `nodes/<node>/` beside the node's filled launcher, and are committed there before the node launches. The CM has no HANDOFF.md of its own: `work.md` *Now* is its handoff, kept current at every step, so a CM that dies mid-loop loses nothing a restart needs, provided `work.md` was kept current (the CM's own discipline, judgement); where it was not, reconcile recovers a node from `git worktree list` and each worktree's `.pm/campaign/child.json` manifest.

Two goalpost rules hold the brief still:

1. **Decisions use permissions, never create them.** Every entry in `decisions.md` names the permission it uses: a `brief.md` permission, an owner ruling already recorded in `decisions.md` (`Actor: owner`, `D<n>`), or, for a decision the CM takes under *Reading a defective plan*, that rule and the brief clause it reads; a decision none of these covers is a question to the owner.
2. **`brief.md` is protected by its owner-supplied digest.** `campaign-preflight --digest <the owner's digest>` compares it with the file; any difference stops the CM before anything launches, and is a question to the owner. Running the command is the CM's own discipline (judgement); nothing launches it for the CM.

Reconcile is how the CM starts, and restart is reconcile: there is no other recovery. Its first step is `python3 $PM campaign-preflight --dossier <dossier> --repo <product repo> --integration-branch <branch> --worktree <the integration checkout: the launcher's product repo path when that is the checkout> --digest <the owner's digest> --harness <the child PM's harness and each child seat's>`, the mechanical half: it checks `brief.md` against the digest, the dossier a repository of its own with `brief.md` and `setup.sh` committed and unchanged (the CM's own files may be mid-edit) and no placeholders left, the integration branch built on the pinned base with no tracked HANDOFF.md at its head, the product's committed policy file, the dossier's `setup.sh` and, when the brief's `## Sandbox` section pins one, the dossier's `sandbox.json` against the SHA-256 digests `brief.md` pins (and runs `setup.sh` in a throwaway detached worktree of the integration head with the code `launch-child --setup` uses, only when every integrity check above it passed), the node worktree parent, and the tools on the machine; a failure is a question to the owner, never a fix in the dossier. The rest of reconcile's inputs are `work.md`; `git worktree list` in the product repository; each in-flight node worktree's token-free `pm status` (run from inside the worktree with no `PM_RUN_TOKEN` in the environment, so its output is unverified) and its `wait-child --worktree <worktree>` output; and every child's `run-report.md`. It ends in four confirmations:

1. Every target has work in flight, a passing receipt at the integration head, or an open gap recorded in `work.md`.
2. Every node traces to a target.
3. No failure has silently disappeared: every failed receipt, stopped child and open finding is still on the record.
4. Budgets hold, per target and campaign-wide.

A confirmation that fails is fixed in `work.md` before anything launches, or becomes a question to the owner when fixing it needs one. A policy file or `setup.sh` that no longer matches its pin stops the CM the way a digest mismatch does, and is a question to the owner.

## Nodes

| Kind | Produces | Runs as | Surface | Merged |
|---|---|---|---|---|
| Change | a product change toward a target | a child PM on a node plan | product paths, disjoint from every protected path | yes, once judged and accepted |
| Evidence | a measurement of a target at a recorded product SHA, as a report and its receipt | a child PM on a node plan | its report path, `campaign-reports/<node>.md` | no: the branch is kept and the receipt copied into `validation/<node>/` |
| Task | a reading: an investigation, a plan review, an audit | the read-only delegate through `orchestrator` | none: no worktree and no branch | no: findings go into `evidence.md` |

The dividing line: anything that executes, builds, tests, generates outputs or changes state needs a plan node, run by a child PM under the floor; reading is a task node. The one exception is the owner's worktree setup script (*Launching and watching a child*), which creates git-ignored scaffolding and nothing else. A delegate's `files` must sit beneath the delegate's repository, so the dossier excerpts a task node needs go inline in its prompt, except that an audit task node may take the dossier itself as the delegate's repository, since the whole dossier is its material and inlining it is not faithful. Plan review is a read-only task node whose prompt embeds `implementation-plan`, plus `check-plan --plan <dossier>/nodes/<node>/plan.md --repo <product repo> --policy <dossier>/nodes/<node>/policy.json`; a node launches only when both are clean (the CM's own discipline, judgement: `launch-child` checks neither).

Node plan conventions:

- A reserved node's plan says `Approval needed before implementation: no` and names the `decisions.md` id that approved it, because the owner's approval is recorded in the dossier before launch and a child PM has nobody to ask.
- Every authorized entry stays disjoint from every protected path; `check-plan --policy` refuses an overlap.
- An evidence node's surface is its report path, `campaign-reports/<node>.md`, and nothing else.
- An evidence node's measurements are made by a script committed in the dossier under `nodes/<node>/` and pinned by SHA-256 in the plan, a pin the Developer checks before running it and the receipt's *Command* cites; a verdict-producing check inside it is an instrument under *Trust before use* (*Validation*), a capture-only script is not.
- An evidence node's plan says that a steer after review edits the report only, from the files the script already wrote; a correction that would re-measure is a new node or a stop.
- The brief's *Exclusions* are copied, as written, into every node plan's *Explicit Non-Goals*, the section that reaches the child's Developers and reviewers through their slice contract, and into every task-node prompt the CM writes; nothing else carries an exclusion down.
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
- **Cleanup** of a finished or dead child, in this order: run `stop-child --worktree <worktree> --reason "<why>"`, which captures the pane to `.pm/campaign/pane.txt` (for a dead pane, the placeholder `capture_text` returns), prints the reviewer process groups the child's `run.json` files record, and kills the child's tmux server, but refuses and exits 2 when that server does not carry the `PM_CHILD_WORKTREE` mark `launch-child` set for this worktree (a reused socket now hosting another child), or hosts a session that is not `campaign-*` or `pm-*` (an operator's own session), each of which is a question for the owner and never a reason to kill by hand, and exits 2 when sessions remain after the kill; check by hand for those reviewer process groups, which sit outside the child's tmux server; copy HANDOFF.md, `.pm/campaign/`, `.pm/runs/<id>/` and the authoritative state root `<worktree-git-dir>/pm/<run-id>/` (`run.json`, `events.jsonl`, the assessments; find the git dir with `git -C <worktree> rev-parse --absolute-git-dir`) into `validation/<node>/`, together with every artifact a receipt hashes into `validation/<node>/artifacts/`, because `git worktree remove` deletes git-ignored files; then capture the worktree's uncommitted work completely, because a child that died mid-slice can leave a Developer's uncommitted edits: write `git -C <worktree> diff --cached --binary` to `validation/<node>/staged.patch` first, before anything is staged, so a partly staged file keeps its staged version; then write `git -C <worktree> status --porcelain` to `status.txt`; then run `git -C <worktree> add -A` and write `git -C <worktree> diff --cached --binary HEAD` to `diff.patch`, which holds the staged, unstaged and new non-ignored files, binary-safe (git-ignored paths such as `.pm/` stay out). `add -A` records a nested repository (`?? dir/` in `status.txt`, a directory with its own `.git`) as a bare gitlink with none of its content, so copy it by hand and keep the worktree until it is copied. Then `git worktree remove <worktree>`, keeping the branch, and `git worktree prune` in the product repository, because a product gate that cached a worktree inside the node worktree leaves a stale entry that reconcile's `git worktree list` would otherwise show. `git worktree remove` refuses a worktree with modified or untracked files and `--force` discards them, so `--force` is used only after `staged.patch`, `status.txt` and `diff.patch` are all written and any nested repository is copied, and only when `diff.patch` is non-empty whenever `status.txt` lists any change; otherwise stop and keep the worktree. The removal deletes the worktree's git dir and with it the state root, which is why the copy comes first. Everything after `stop-child` here is the CM's own procedure (judgement), not toolkit-enforced. An unscoped `stop --scavenge` or a bare `tmux kill-server` is never used.
- **Reserved approvals** move up to `decisions.md`, recorded before launch.
- **Budgets** hold per target and campaign-wide, as `brief.md` sets them; two failed attempts at one target is a question for the owner.
- **Child output** stays out of git's way, in git-ignored output directories or the slice artifact directory. A brief may name the git-ignored output directories a product uses; the toolkit never reads them.

## Launching and watching a child

Fill the Mode B launcher from `implementation-plan`'s SKILL.md → "Next Chat Prompt Format", including its campaign-child paragraph (the launcher's only copy; this file is the contract that paragraph serves), with the node plan's dossier path, the node worktree as its repo, the child's seats from the campaign launcher, the run tag `campaign/<id>`, the node branch, the node's policy file and the child attempt budget from `brief.md` as `--max-attempts`; write it to `nodes/<node>/launcher.md` and commit it. `launch-child` writes `/HANDOFF.md` into the product repository's git exclude file (persisting after the campaign), verifies it with `git check-ignore`, and refuses a launch whose base commit tracks HANDOFF.md or whose ignore rules override the exclusion (mechanical), so the child PM's HANDOFF.md cannot fail floor fact 7; the CM does nothing for it. When the brief has a *Worktree setup* section, the owner's script is committed in the dossier as `setup.sh`, pinned in the brief by SHA-256 like the policy file, and passed as `--setup`: `launch-child` runs it inside the new worktree before the child's session starts, in its own process group and without `PM_RUN_TOKEN`, keeps its output in `.pm/campaign/setup.log`, and fails the launch on a non-zero exit, a ten-minute hang (everything still in the script's process group is killed; a process that left the group is named as possibly still running) or a worktree left dirty outside `.pm/`, so the child PM and every Developer start in a worktree whose git-ignored scaffolding (data links, a virtualenv link, agent wiring) is already in place. The script is owner code: `campaign-preflight` checks it against its pin at every reconcile and runs it in a throwaway worktree, so it must create only git-ignored files, wait for everything it starts, and take no exclusive resource (a port, a lock) that a running child could hold; the CM never edits it, and a setup failure is a question to the owner, not a fix in the dossier. Then launch:

```sh
python3 $PM launch-child --repo <product repo> --base <integration head> --branch campaign/<id>/<node> \
  --worktree <trusted parent>/<node> --prompt-file <dossier>/nodes/<node>/launcher.md \
  --harness <child PM harness> --model <full model id> --effort <effort> \
  [--setup <dossier>/setup.sh] [--sandbox <dossier>/sandbox.json]
python3 $PM wait-child --worktree <trusted parent>/<node> --wait <node wait length>
```

Issue that `wait-child` as one long background command and let it return (never poll). It returns on a signal, or when the wait runs out. The signals:

- **A question** (`cm-question.md`, printed after `question present: yes` between `--- question ---` and `--- end question ---` lines, each line prefixed with `|` and a space, capped at 40 lines and 4096 bytes with a truncation marker line): decide it; record it in `decisions.md` (goalpost rule 1); answer with `answer-child --worktree <worktree> --text "<answer>"`; and wait again. A question that needs the owner goes to the owner first.
- **A dialog marker**: read it as *Reading the pane* in [SKILL.md](../SKILL.md#reading-the-pane) says. A dialog the child is waiting on goes to a human; ordinary output that merely contains a marker string does not, and a child PM's pane routinely shows PM vocabulary and Developer pane tails, so the CM reads the pane and waits again.
- **The result** (`.pm/campaign/child-result.json`, printed with its `status`): judge the node. The `child run …` lines are a tokenless cross-check, never a fact.
- **Session death** (`session running: False` with no result): the node ends as a stopped child does.

A wait that elapses with no signal is information, as `observe`'s is: wait longer, or decide. A human may `tmux -L <socket> attach -t <child session>` at any time to watch the child and its Developers, which share the child's private server; `launch-child` prints that line beside the session and socket, and a bare `attach` picks the most recently used session, usually a Developer. A sandboxed child is watched only through the `watch:` line as printed, never a plain attach, for the reason [sandbox.md](sandbox.md) gives.

Node worktrees are created under one parent directory the owner has trusted through each harness's own mechanism, because an interactive harness may show a directory-trust dialog for a new path and `sessions.wait_until_ready` fails closed on it, so `launch-child` fails; a trust prompt at launch is a question for the owner, never something the CM acknowledges. Never reuse an explicit `--socket` across children; omit `--socket` to get the unique default. `stop-child` refuses a server carrying another worktree's `PM_CHILD_WORKTREE` mark, so a re-run against an old manifest cannot kill a later child on a reused socket: the advice is about clarity, not safety (an explicit `--socket` longer than 64 characters or equal to `default`, the operator's own server, is refused).

Each child gets one protected-paths policy file, `nodes/<node>/policy.json`, recorded in `work.md`. Each node policy holds every entry of the product's committed policy file plus that file's own path, minus only the instrument an instrument-class node builds: the toolkit reads one policy per run and never the product's, and self-protection covers only a policy file inside the worktree, while the child's policy lives in the dossier. That every node policy is built this way is the CM's own discipline (judgement), checked when judging the node. A plain entry naming a directory (`instruments`, no trailing `/`, no wildcard) protects that path and everything below it, at plan time, at `grant` and at floor fact 5 alike; a trailing-slash entry with a wildcard is refused at load. A new instrument reaches the product policy, and the brief's pinned SHA, only through the owner, by a question. The policy actually used is readable in the child's `run.json` at `<worktree-git-dir>/pm/<run-id>/run.json` (`policy.protected_paths` and `policy.protected_paths_source`; see [run-state.md](run-state.md)); compare it with `work.md` when judging the node.

### Sandbox

When the brief's `## Sandbox` section pins the dossier's `sandbox.json`, every child runs inside an OS sandbox the owner's policy defines. What the boundary is, what enforces what, its limits and its troubleshooting are in [sandbox.md](sandbox.md); the CM's part is only this:

- **The pin.** `campaign-preflight` checks `sandbox.json` against the brief's pin at every reconcile (tracked and unchanged, SHA-256 match, a valid policy, `srt` on `PATH`), and a failure is a question to the owner, like any pin. The CM never edits `sandbox.json`; the owner changes it and its pin together.
- **`--sandbox` on every launch.** Pass `--sandbox <dossier>/sandbox.json` to every `launch-child` while the brief pins it. `launch-child` refuses, before anything is created, a launch without it or with a file whose SHA-256 differs from the pin (mechanical, a toolkit check; the brief is the launcher's dossier root's `brief.md`). A brief whose section is `none`, or that has none, launches unsandboxed. A `WARNING:` line the launch prints for the policy names an option the owner enabled in the pinned file: the owner's choice, not the CM's to act on.
- **The `watch:` line, as printed.** For a sandboxed child `launch-child` prints `sandbox: control dir <path>` and `watch: srt -s <control dir>/attach-policy.json tmux -L <socket> attach -t <session>`; record it in `work.md` beside the socket, and give a human that command, never a plain `tmux attach`.
- **`stop-child`'s removals.** For a sandboxed child, `stop-child` also waits for the server's `srt` to exit, removes the child's sandbox temp dir and its control directory, and prints `removed sandbox temp dir <path>` and `removed sandbox control dir <path>` (or that each was already gone). It exits 2 when that teardown is skipped, fails or leaves anything behind, which, like its other refusals, is a question for the owner, never a reason to clean up by hand. `launch-child --setup` still runs on the host, before the sandbox exists.
- **No submodules.** The toolkit refuses a repository with a gitlink, and the CM's own `git status` and `git diff` in a node worktree are not covered by that refusal: see the submodules bullet in [sandbox.md](sandbox.md#what-it-is-not-limitations-and-residuals).

## Validation

- **Trust before use.** An instrument counts only after it passes a known-good control and fails a known-bad one, and its verdict is always pass, fail or error.
- **Independence.** Instruments are built in owner-approved nodes and then protected, in two layers: the child's floor is mechanical (fact 5 refuses acceptance of a slice that changes a path in its node policy, over Git-visible files only), while which instruments each node policy lists, and what a merge or an inherited commit brings in, are the CM's judgement.
- **Measurement ownership.** The CM measures through evidence nodes, run by a child PM at a recorded SHA. An implementing Developer's claim of success points at evidence and is never the evidence.
- **The ratchet.** A target that passed must keep passing: cheap checks re-run at every new integration head, expensive ones at milestones, and acceptance needs a final evidence node at the final head. Every receipt names the measured product SHA separately from the commit that adds the report.

A receipt records: the measured product SHA, the instrument version, the configuration, the command, the outcome (pass, fail or error), and the artifact hashes. The artifacts it hashes are copied into `validation/<node>/artifacts/` at cleanup, because `git worktree remove` deletes the git-ignored output they sit in; a receipt whose artifact was not copied states that the artifact was not retained, so its hash is the only record.

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

With `max_parallel: 1` in `brief.md`, nodes run serially. Going parallel is launching a second child before waiting on the first, under disjoint surfaces, with exclusive resources respected and merges still one at a time; the per-child tmux sockets and per-run ledgers already hold.

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
Sandbox policy: <dossier>/sandbox.json, or none

Use the project-manager skill in campaign mode (Mode C). You are the Campaign Manager. Read this toolkit's skills/project-manager/references/campaign-mode.md in full, then the dossier.

Reconcile first: run `campaign-preflight` with the digest above and every seat's harness, read work.md, and make the four confirmations. Then run the loop.

Commits: standing authorisation for Developers to commit each slice on their node branch, for the Campaign Manager to commit its own files in the dossier, and for its local `git merge --no-ff` of accepted change nodes into the integration branch.

Never implement. Never supervise a plan you wrote. Never change brief.md. Never answer a human gate. Never loosen a target or tolerance. Never push, deploy or cause external side effects.
```
