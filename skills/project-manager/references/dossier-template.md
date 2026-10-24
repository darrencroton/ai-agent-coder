# Campaign Dossier Template

The layout of a campaign dossier and a template for each of its files. This is a template only: the rules that govern every file (who writes it, when, and what the Campaign Manager does with it) are in [campaign-mode.md](campaign-mode.md), and nothing here restates them.

## Layout

```text
<dossier>/                      a git repository of its own, outside the product repository
  brief.md                      the owner's brief
  setup.sh                      the owner's worktree setup script, when brief.md's Worktree setup names one
  work.md                       now, and the node table
  evidence.md                   findings, one entry each
  decisions.md                  decisions, one entry each
  nodes/<node>/plan.md          the committed node plan
  nodes/<node>/policy.json      the committed per-child protected-paths policy file
  nodes/<node>/launcher.md      the filled Mode B launcher the child is launched from
  validation/<node>/            one directory per node: its archived run and, for an evidence node, its receipt
```

## `brief.md`

```md
# Brief: <campaign id>

## Goal
<one paragraph: the outcome the campaign delivers>

## Targets
| Target | Done when | Tolerance | Measured by |
|---|---|---|---|
| T1 | <stated, checkable condition> | <allowed deviation> | <instrument, or the evidence node that measures it> |

## Pinned references
- <reference>: <repository or path> at <SHA>

## Allowed paths
- <product path the campaign may change>
- Node worktree parent: <absolute directory the owner has trusted in each harness>
- Protected-paths policy file: <path in the product repository>, SHA-256 <digest of the file, from shasum -a 256> (see [campaign-mode.md](campaign-mode.md) *Launching and watching a child*)
- Git-ignored output directories: <product directories, or none>

## Reserved decision classes
- <class>: <what the class covers>

## Worktree setup
- <none, or: setup.sh in this dossier, SHA-256 <digest of the file, from shasum -a 256>, run by launch-child --setup in every node worktree before its session starts; what it links or creates, all of it git-ignored and none of it a product change>

## Budgets
- Node budget per target: <N>
- Campaign-wide: <N> nodes; <spend limit>
- Child attempt budget: <N> (the --max-attempts each child PM's init uses)
- max_parallel: 1

## Exclusions
- <path the campaign may not read>
```

## `work.md`

```md
# Work: <campaign id>

## Now
- In flight: <node ids, or none>
- Next action: <one line>
- Blockers: <none, or each with the node it blocks>
- Open questions: <none, or each question to the owner in the shape campaign-mode.md *Questions to the owner* gives>

## Nodes
| Id | Kind | Objective | Target | Dependencies | Surface | Resources | Status | Branch | Worktree | Socket | Session | Base SHA | Head SHA | Policy | Report |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| N1 | change \| evidence \| task | <one line> | T1 | <node ids, or none> | <authorized entries> | <exclusive resources, or none> | <status> | campaign/<id>/N1 | <path> | <socket> | <session> | <SHA> | <SHA> | nodes/N1/policy.json | <child run-report.md, or campaign-reports/N1.md> |
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
- Permission used: <the brief.md section and line that permits it>
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
