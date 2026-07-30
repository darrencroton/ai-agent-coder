---
name: drift-audit
description: Audit an implementation diff against a frozen contract before normal code review. Use when the user explicitly asks for drift audit, authorization audit, scope audit, or to check whether a completed implementation stayed inside an implementation-plan or scoped-implementation slice.
---

# Drift Audit

Use this skill to answer one question: was the implementation authorized?

This is not a general code review. Do not spend the review budget judging whether the code is elegant, optimal, or fully maintainable except where that proves scope drift. Quality review belongs to `code-review` after this gate passes.

## Required Inputs

Identify or reconstruct these inputs before auditing:

- frozen contract or slice receipt
- allowed files/functions/components
- explicit non-goals
- expected tests or validation
- actual diff, commit, branch, or changed files
- relevant tests touched or expected

If no frozen contract exists, stop after drafting a candidate contract from the user request and mark the audit `BLOCKED: no frozen contract`. Do not pretend an inferred contract has the same authority as an approved one.

## Workflow

1. Read the frozen contract and list the authorized surface.
2. Inspect the actual change set with `git diff`, `git show`, or the supplied patch.
3. Compare actual changed files/functions/components against the authorized surface.
4. Identify behaviour added, behaviour removed, hidden rewrites, missing tests, and new coupling.
5. Check whether explicit non-goals were preserved.
6. Return an authorization verdict before any quality judgement.

## What Counts As Drift

Report drift when the implementation includes:

- files, functions, components, schemas, routes, or configs outside the authorized surface
- behaviour not listed in the acceptance criteria
- removed or weakened edge cases
- broad rewrites hidden inside a narrow task
- new coupling to auth, billing, persistence, global state, routing, shared types, or API contracts
- test changes that broaden, weaken, or silently redefine the requested behaviour
- missing tests that were required by the frozen contract

Do not report drift for incidental formatting or import ordering unless it changes behaviour, coupling, public surface, or reviewability.

## Contract Defects Are Not Drift

Before recording a requirement as unsatisfied, check whether the contract can be satisfied at all. Plans get revised, and prose a later decision superseded often survives in an earlier section; a requirement may also be forbidden by the contract's own stated conventions, leaving no implementation that honours it without violating something else.

Report that under `Contract Defects`: quote the clauses that conflict and say which reading appears coherent, then audit the change against that reading. Do not prescribe a fix the contract prohibits elsewhere, and do not fail the implementer for declining to write one. Whoever decides acceptance resolves which reading binds — your job is to surface the conflict, not settle it.

## Verdicts

- `PASS`: no material drift found.
- `PASS WITH RISKS`: no clear unauthorized behaviour, but the contract or evidence is incomplete enough that a human should review before accepting.
- `FAIL`: unapproved scope expansion, behaviour removal, hidden rewrite, risky coupling, or required tests missing.
- `BLOCKED`: no frozen contract, unusable diff, or missing evidence prevents an audit.

A contract defect alone does not decide the verdict: report it, then rate the change on the coherent reading.

## Output

Use this format:

```md
## Authorization Gate
- Intended slice:
- Authorized surface:
- Actual changed surface:
- Verdict: PASS / PASS WITH RISKS / FAIL / BLOCKED

## Drift Findings
1. [P1] `path/to/file:123` Title
   Why this is outside the frozen contract, what behaviour or review boundary it changes, and the fix direction.

## Behaviour Added
- ...

## Behaviour Removed
- ...

## Missing Tests
- ...

## New Coupling
- ...

## Contract Defects
- ...

## Non-Goals Check
- Preserved:
- Violated:

## Next Action
- ...
```

If there are no findings, say `- none` in the relevant sections.
