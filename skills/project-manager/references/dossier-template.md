# Campaign Dossier Template

The layout of a campaign dossier and a template for each of its files. This is a template only: the rules that govern every file (who writes it, when, and what the Campaign Manager does with it) are in [campaign-mode.md](campaign-mode.md), and nothing here restates them. Keep the backticked pin lines in the shape shown (the campaign base, the campaign toolkit's commit, the node worktree parent, and each pinned file with its SHA-256 digest: the policy file, `setup.sh`, `sandbox.json` and every owner script): `campaign-preflight` reads them.

## Layout

```text
<dossier>/                      a git repository of its own, outside the product repository
  brief.md                      the owner's brief
  setup.sh                      the owner's worktree setup script, when brief.md's Worktree setup names one
  sandbox.json                  the owner's srt base policy, when brief.md's Sandbox section names one
  work.md                       now, and the node table
  evidence.md                   findings, one entry each
  decisions.md                  decisions, one entry each
  nodes/<node>/plan.md          the committed node plan
  nodes/<node>/policy.json      the committed per-child protected-paths policy file
  nodes/<node>/launcher.md      the filled Mode B launcher the child is launched from
  validation/<node>/            one directory per node: its archived run and, for an evidence node, its receipt
  retrospective.md              the campaign's retrospective, written at acceptance
```

## `brief.md`

```md
# Brief: <campaign id>

## Goal
<one paragraph: the outcome the campaign delivers>

## Targets
| Target | Done when | Tolerance | Measured by | Who supplies the evidence |
|---|---|---|---|---|
| T1 | <stated, checkable condition> | <allowed deviation> | <instrument, or the evidence node that measures it> | <the CM alone \| the owner's eyes \| the owner's hands> |

## Pinned references
- Campaign base: `<integration branch's starting branch>` at `<SHA>`
- Campaign toolkit: <checkout path> at `<commit>`
- <reference>: <repository or path> at <SHA>
- <owner script>: `<file>` in this dossier, SHA-256 `<digest of the file, from shasum -a 256>`, copied byte for byte into each node that runs it
- Previous <what it pinned>: `<file>`, SHA-256 `<superseded digest>` (optional history of a re-pin; `campaign-preflight` checks every other pin line and skips an item that opens with `Previous`)

## Allowed paths
- <product path the campaign may change>
- Instrument paths: <product paths that become protected once their controls pass; none>
- Node worktree parent: `<absolute directory the owner has trusted in each harness>`
- Protected-paths policy file: `<path in the product repository>`, SHA-256 `<digest of the file, from shasum -a 256>` (see [campaign-mode.md](campaign-mode.md) *Launching and watching a child*)
- Git-ignored output directories: <product directories, or none>

## Reserved decision classes
- <class>: <what the class covers>

## Worktree setup
- <none, or: `setup.sh` in this dossier, SHA-256 `<digest of the file, from shasum -a 256>`, run by launch-child --setup in every node worktree before its session starts; what it links or creates, all of it git-ignored and none of it a product change>

## Sandbox
- <none, or: `sandbox.json` in this dossier, SHA-256 `<digest of the file, from shasum -a 256>`, the srt base policy every child runs under (see [sandbox.md](sandbox.md)); the section is either that pin line or `- none` alone>

## Budgets
- Node budget per target: <N>; task nodes <count | do not count>; a ratchet target's budget is its planned integration heads plus one
- Campaign-wide: <N> nodes; <spend limit>
- Child attempt budget: <N> (the --max-attempts each child PM's init uses)
- max_parallel: 1

## Exclusions
- <path no seat may read>
- CM only: <paths the Campaign Manager alone may read, or none; every other seat is excluded from them>
```

## `work.md`

```md
# Work: <campaign id>

## Now
- In flight: <node ids, or none>
- Next action: <one line>
- Blockers: <none, or each with the node it blocks>
- Open questions: <none, or each question to the owner in the shape campaign-mode.md *Questions to the owner* gives>
- Watching: <for each node in flight, the `watch:` line launch-child printed, or none>

## Nodes
| Id | Kind | Objective | Target | Dependencies | Surface | Resources | Status | Branch | Base SHA | Head SHA | Policy | Report |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| N1 | change \| evidence \| task | <one line> | T1 | <node ids, or none> | <authorized entries> | <exclusive resources, or none> | <status> | campaign/<id>/N1 | <SHA> | <SHA> | nodes/N1/policy.json | <child run-report.md, or campaign-reports/N1.md> |
```

## `evidence.md`

```md
## E<n>: <short title>
- Finding: <what was found>
- Source: <file:line>
- SHA read at: <SHA>
- Uncertainty: <what could make this wrong>
```

## `decisions.md`

```md
## D<n>: <short title>
- Actor: <CM | owner>
- Decision: <what was decided>
- Grounds: <the evidence it rests on>
- Permission used: <the brief.md section and line that permits it; the owner ruling already recorded here (D<n>); or, for a CM decision, *Reading a defective plan* and the brief clause read>
```

An approval of rules quotes them. An approval never summarises the rule it approves, because a later reader, an auditor included, takes the summary for the rule:

```md
## D<n>: <approval of nodes/<node>/plan.md>
- Actor: owner
- Decision: approved nodes/<node>/plan.md at SHA-256 `<digest of the plan>`, whose rules read: "<the rule, in the plan's own words>"
- Grounds: <the evidence it rests on>
- Permission used: <the brief.md reserved class that makes this approval the owner's>
```

## `validation/`

```text
validation/<node>/
  HANDOFF.md                    the child PM's handoff
  campaign/                     the worktree's .pm/campaign/ (launcher copy, manifest, questions and answers, pane captures, child-result.json)
  runs/<run-id>/                the worktree's .pm/runs/<run-id>/
  state/<run-id>/               the state root <worktree-git-dir>/pm/<run-id>/ (run.json, events.jsonl, the assessments)
  staged.patch                  the worktree's staged changes (see campaign-mode.md *Cleanup* for the capture order)
  status.txt                    the worktree's uncommitted-file list
  diff.patch                    the worktree's uncommitted work, binary-safe
  artifacts/                    the files an evidence node's receipt hashes
  receipt.md                    an evidence node's receipt
```

The receipt shape:

```md
# Receipt: <node> for <target>
- Measured product SHA: <SHA>
- Instrument version: <version or SHA>
- Configuration: <configuration>
- Command: <command>
- Outcome: pass | fail | error
- Artifact hashes: <artifact>: <sha256> (a copy is in artifacts/), or <artifact>: not retained
- Report: campaign-reports/<node>.md at <the commit that adds it>
```

## Receipt criteria

The acceptance-criteria lines an evidence slice uses with [evidence-runner.sh](evidence-runner.sh), which the CM copies to `nodes/<node>/run.sh` and adapts:

```md
- [ ] The Developer checked the runner's SHA-256 against the plan's pin before running it, and recorded the check.
- [ ] The runner ran once, from the node worktree root, and exited 0; `shasum -a 256 -c manifest.sha256` in the output directory reports every listed file OK, N of N.
- [ ] The report quotes each step's `.status` verbatim and the tail of each named log verbatim.
- [ ] The measured SHA in `context.txt` is distinct from the commit that adds the report.
- [ ] A runner exit of 2 is recorded as such, the measurement is not used, and the run stops.
```
