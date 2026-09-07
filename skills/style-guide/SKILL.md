---
name: style-guide
description: Draft a project-specific coding style guide from a default baseline, audit written code against that guide (or the baseline directly when none exists), or write/edit code following it — covering naming, comments, documentation, logging, metadata, tests, and generated code across C/C++, Python, Fortran, and Markdown. Use when the user asks to create or update a style guide, check code for style consistency, or write/implement code following their style preferences.
---

# Style Guide

Three modes, sharing one standard:

- **`draft`** — adapt [references/baseline-style-guide.md](references/baseline-style-guide.md) into this project's own `STYLE-GUIDE.md`.
- **`audit`** — check written code against the project's drafted guide, or the baseline directly if none has been drafted.
- **`write`** — write or edit code following the project's drafted guide, or the baseline directly if none has been drafted.

A project's own drafted guide always wins over the baseline for anything it explicitly states. The baseline fills every gap the project hasn't decided for itself, and — before any guide is drafted — stands in for one entirely: `audit` and `write` always have a concrete standard to work from, even in a brand-new repository.

A drafted guide lives at the repository root as `STYLE-GUIDE.md` by default. If `draft` wrote it somewhere else, or the project already points to one from `AGENTS.md`/`CLAUDE.md`/`CONTRIBUTING`, `audit` and `write` use that location — check for an explicit pointer before assuming no guide exists and falling back to the baseline.

## Boundaries

This skill stays in exactly one lane: naming, comments, documentation, logging/error presentation, metadata, tests, and generated-code handling.

- Mechanical formatting is the `lint` skill's job, and the project's own formatter's job before that. Never restate a rule a linter or formatter config already enforces.
- Quantitative structure — complexity, duplication, dependency shape — is `code-health`'s job.
- Correctness, safety, and behavioural risk are `code-review`'s job.
- A holistic pass that actually changes existing, working code is `code-simplifier`'s job; `audit` only reports, and `write` only applies the standard to code already being written for another reason — neither mode goes looking for unrelated code to change.
- A written evidence synthesis for a stakeholder is the `report` skill's job. `draft` mode's output is a project artifact developers work from, not a report about one.

A finding from `audit` is real evidence — it never causes an automatic, non-waivable failure on its own, but it is not noise either: weigh it in a recorded human or PM judgement, and a plan may require an `audit` run as acceptance evidence for a style-sensitive slice (see Integration below).

## Mode: `draft`

1. **Start from the baseline.** Read [references/baseline-style-guide.md](references/baseline-style-guide.md) — every dimension in it is the default unless this project overrides it.
2. **Fold in what the project already governs.** A formatter/linter config stays authoritative for its own mechanics (cite it, don't restate it). Any `AGENTS.md`/`CLAUDE.md`/`CONTRIBUTING` style content, or an existing style document, wins over the baseline for whatever it explicitly states.
3. **Apply any additional instructions given at draft time** (for example, "we prefer camelCase for our Python test helpers") as explicit overrides, and record them as such.
4. **Check the project's actual dominant convention against the baseline, dimension by dimension.** Where it already matches, the baseline stands unchanged — say so briefly. Where it substantially and consistently differs — the project's real, dominant pattern, not a handful of stray files — stop and raise it with the user rather than silently overriding the baseline or silently forcing the baseline over strong existing practice. Record how it's resolved, and why.
5. **Write the document** to the path the user names, or propose a top-level `STYLE-GUIDE.md` if unspecified. Its own prose stays un-hard-wrapped, per the baseline's own documentation section. If it's written anywhere other than the default root path, also record a one-line pointer to it in `AGENTS.md`/`CLAUDE.md` (creating a short one if neither exists) — otherwise a later `audit` or `write` session has no way to discover it and silently falls back to the baseline.
6. **Report** a short summary: which dimensions are baseline-as-is, which are project overrides and why, and where the file was written.

## Mode: `audit`

1. **Load the standard.** The project's own drafted guide if one exists; [references/baseline-style-guide.md](references/baseline-style-guide.md) directly otherwise. State explicitly which one was used.
2. **Scope.** Tracked modifications and additions plus new untracked files against a base ref — the same set `lint`'s differential mode uses, not `git diff --name-only <base>` alone, which misses untracked files and lists paths already deleted. Whole-repository scope only for an explicit standalone audit.
3. **Check each file against the standard**, dimension by dimension: naming, comments, documentation, logging/error presentation, metadata, test naming/presentation, generated-code handling. A violation of the loaded standard is a finding — the standard is authoritative whether it came from the project's own guide or the baseline.
4. **State why each finding matters** — clarity, reviewability, maintenance, or the specific rule it breaks — and skip anything with no discernible effect. Against a base ref, note whether it's newly introduced or pre-existing at that ref.
5. **Never mutate files.** `audit` only reports; hand findings to a human, a recorded judgement, or a separately invoked `code-simplifier` pass, never act on them here — not even a fix that looks obviously right.
6. **Report** each finding as `file:line`, the rule it breaks, why it matters, introduced/pre-existing, and a concrete fix direction, plus a verdict: which standard was used, files in scope, and the finding count.

## Mode: `write`

1. **Load the standard** — the project's drafted guide if one exists, the baseline otherwise — before writing or editing any code in the task.
2. **Apply it as you write.** Naming, comment discipline, logging presentation, documentation shape, and the rest of the loaded standard's dimensions apply to every file touched, exactly as loaded — negotiating the standard against local convention is `draft` mode's job, already resolved (or deliberately left unresolved, in which case the baseline stands) by the time `write` runs.
3. **An explicit instruction for the current task wins.** If the task's own instructions conflict with the standard, follow the task and say so briefly — this mode shapes default choices, it never overrides something the user actually asked for.

No separate output for this mode — the result is the code itself, written under the loaded standard.

## Output

`draft` mode ends with:

```md
## Style Guide Draft

### Baseline-As-Is
- dimension: unchanged from the baseline

### Project Overrides
- dimension: what changed, and why (existing convention, explicit instruction, or discussion outcome)

### Open Discussion Points
- dimension: what differs and needs a decision, if any were deferred

### Written To
- path
```

`audit` mode ends with:

```md
## Style Audit

### Standard Used
- Project-owned guide at `path`, or the baseline (state which)

### Scope
- Base ref / whole-repository, and file count

### Findings
1. `file:line` — dimension — introduced / pre-existing
   Rule it breaks, why it matters, and a concrete fix direction.

### Verdict
- Dimensions checked, findings count, coverage caveats
```

If there are no findings, say so explicitly rather than omitting the section.

## Integration

- **Before implementation** (`scoped-implementation`, or any first-draft coding): when this skill is available in the session, run `write` mode — load the applicable standard and apply it while coding, alongside the frozen contract `scoped-implementation`'s own preconditions already gather. This is where style guidance is worth the most and costs the least: it shapes code as it's written instead of flagging it afterward. When this skill isn't available, `scoped-implementation` remains fully self-contained and falls back to the project's own documented conventions and dominant local pattern, exactly like any other skill that isn't in play.
- **`code-simplifier`** follows this skill's baseline as its fallback only when `style-guide` is also available in the current session and the project has no drafted guide of its own. When `style-guide` is not available, `code-simplifier` remains fully self-contained: it preserves the narrowest coherent local convention it can find and reports the ambiguity, rather than depending on a file it may not have.
- **`code-review`** may still note an incidental style issue at `P3`; that is unaffected. `audit` mode is for when the user wants a deliberate, complete style pass, not an incidental one folded into a correctness review.
- **Not a mechanical gate, but not weightless either.** Unlike `lint` before `commit`, `audit` is never required before any other skill runs by default, and a finding never triggers an automatic or non-waivable failure — style is judgement, one layer softer than lint's deterministic findings, and this repository's principle of mechanising the floor and judging the rest keeps judgement out of hard gates. It can still be required as explicit acceptance evidence: a plan that names a style-sensitive slice (a broad rename, a logging-presentation change, a generated-code convention change, a documentation reorganisation, or a full baseline-alignment migration on an existing codebase) may require an `audit` run as validation, the same way any plan may name any validation step under "contracts before code" — its findings still receive accountable judgement, never automatic failure.

## Notes

- No scripts. Every dimension here requires reading and judgement, not a deterministic tool; that is precisely the gap this skill fills next to `lint`'s mechanical floor.
- Standalone use (Rung 0): all three modes work with no plan, no PM, and no other skill in play. `draft` and `write` need only a codebase (or none, for a genuinely new one) to read; `audit` needs only a base ref or a file scope.
- A full migration of an existing, inconsistent codebase to the baseline is a broad, cross-cutting change: run `draft` (or confirm the project's existing guide) first, `audit --all` to see the whole gap, then route the actual fixes through `implementation-plan` → `scoped-implementation` slices rather than one unbounded pass — the same reason any other broad change gets sliced in this repository.
