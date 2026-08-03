---
name: code-review
description: Perform a professional, comprehensive code review of completed work. Use when the user asks to perform a code review. Focus on line-specific findings, risk-based depth, and explicit attention to correctness, safety, tests, and scientific C/C++/Python concerns.
---

# Professional Code Review

Produce a senior-level review that is evidence-based, risk-driven, and difficult to fake with a superficial skim.

## Load Order

- Always read this file.
- Read [references/review-matrix.md](references/review-matrix.md) for the required review dimensions.
- Read [references/scientific-and-language-priorities.md](references/scientific-and-language-priorities.md) whenever the change touches C, C++, Python, numerical code, simulations, data pipelines, benchmarks, or scientific tests.

## Inputs To Identify

From the user message and local context, identify:

- **Review target**: diff, commit, branch, files, directory, or completed task
- **Requirement source**: drift-audit result, plan, spec, ticket, issue, paper, docs, or explicit user criteria
- **Project rules**: `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING`, test/build conventions
- **Evidence available**: git diff, tests, benchmarks, logs, docs, configs, generated outputs

Resolve the target from local context when possible. Ask the user only if multiple plausible review scopes remain.

## Minimum Investigation Standard

Every review must do all of the following:

1. Inspect the actual change set, not just a summary.
2. Read the most relevant surrounding code, not only the modified lines.
3. Follow changed interfaces one hop outward:
   - caller and callee
   - data producer and consumer
   - public API and the primary tests
4. If algorithms, numerics, memory ownership, concurrency, or file formats changed, inspect related tests and invariants before concluding.
5. Review all changed files. Do not stop after the first few files or after style comments.

## Workflow

### 1. Establish scope
- Prefer `git diff`, `git show`, or a user-supplied patch.
- If there is no usable git context, review the supplied files directly and say so.
- Identify high-risk files first: core algorithms, public APIs, memory management, concurrency, I/O, configs, tests, and build files.

### 2. Confirm authorization status
- If a `drift-audit` result is supplied, summarize its verdict and proceed to quality review.
- If the commissioning context states that the authorization check was performed elsewhere — for example a supervising Project Manager that checks the changed surface against the frozen authorization itself — record that as the authorization status and proceed to quality review. Do not re-audit authorization or caveat your conclusions on the absence of a separate report.
- If a frozen contract exists but no `drift-audit` result is supplied and nothing states it was done elsewhere, state that the user should explicitly call the drift-audit skill first. Continue only if the user explicitly asks for combined drift and quality review.
- If a supplied `drift-audit` result is incomplete or contradicted by the diff, report that as an open question instead of silently redoing the audit.

### 3. Establish expected behaviour
- Extract acceptance criteria from the plan/spec/ticket when present.
- If no formal spec exists, infer intended behaviour from tests, docs, code structure, and the user request.
- State important assumptions when they materially affect confidence.
- A frozen contract can be self-contradictory: prose a later revision superseded, a requirement the contract's own conventions forbid satisfying. Report that under `Contract Defects`, addressed to whoever decides acceptance — quote the conflicting clauses, say which reading appears coherent, and review against that reading. Do not prescribe a fix the contract prohibits elsewhere, and do not rate it as a finding against the implementer. Name the conflict; do not settle it.

### 4. Run the review matrix
- Use [references/review-matrix.md](references/review-matrix.md).
- Every review must explicitly consider correctness, failure handling, tests, and maintainability.
- Add the scientific and language-specific checks from [references/scientific-and-language-priorities.md](references/scientific-and-language-priorities.md) when relevant.

### 5. Use supporting validation when it is proportionate
- When cheap and relevant, run targeted tests, builds, linters, or static analysis to validate or falsify review hypotheses.
- Mechanical hygiene is the `lint` skill's job, not yours: unused names, formatting drift, import order, misspellings, malformed Markdown tables. If a lint verdict is supplied, do not re-report what it already found; if one is absent, note that lint coverage was not evidenced rather than substituting a manual scan for it. Spend your attention on what a linter cannot decide — correctness, contracts, edge cases, numerical behaviour.
- For C and C++, treat compiler warnings and sanitizers as supporting evidence, not as substitutes for reasoning.
- For scientific and numerical changes, prefer targeted regression tests, reference-data checks, or benchmark comparisons when available.

### 6. Separate findings from uncertainty
- Report confirmed defects and clear risks as findings.
- Put ambiguous concerns under `Open Questions / Assumptions`, not as hard findings.
- Do not report untouched pre-existing issues unless the current change interacts with them.
- When a plan-based workflow asks for residual or post-plan considerations, keep them separate from findings and the verdict. This category is only for genuinely pre-existing observations that do not interact with the change, unrelated out-of-scope opportunities, or inconsequential/speculative notes worth later consideration. A material defect introduced by the change remains a finding even when fixing it would require files outside the frozen contract; do not launder it into a post-plan consideration.

### 7. Validate review depth
Before finishing, ask:

- Did I check or request the `drift-audit` result when a frozen contract exists?
- Did I inspect all changed files?
- Did I inspect the relevant tests?
- Did I follow risky interfaces beyond the diff?
- Did I assess numerical or scientific implications if present?
- Am I reporting the highest-impact issues first?

## Severity

- `P0 Critical`: definite correctness, safety, security, data-loss, or reproducibility failure; or a stated requirement is unmet and its absence causes a failure of that severity. Rate an unmet requirement by what its absence actually causes, not by the fact that a document stated it.
- `P1 High`: strong evidence of a bug or major risk that should block completion until fixed.
- `P2 Medium`: important weakness in tests, validation, maintainability, portability, or performance that should be addressed soon.
- `P3 Low`: minor clarity, documentation, or style issue. Mention only if it adds signal.

`P0` and `P1` are gated on reachability: rate a finding there only when you can state how the defect is reached — a CLI invocation, a configuration legal under documented ranges, a plausible input file, a caller inside the reviewed system, or, for surface documented as public, any value inside its documented domain. Hand-constructing values at a helper's signature that no caller and no documented contract can produce is a demonstration of the defect, not a reachability path; where a plan declares an input domain (see the `implementation-plan` skill), behaviour outside it is likewise unreachable. Such findings cap at `P2` and state why. Reachability gates severity and nothing else: never stop examining edge cases, never suppress a finding, and when reachability is genuinely uncertain say so and rate `P2` rather than guessing in either direction.

Default output should focus on `P0` to `P2`. Include `P3` only when the review is otherwise clean or the note is unusually useful.

## What Good Findings Look Like

Each finding must include:

- `file:line`
- severity
- concise title
- why it matters in runtime, scientific, or maintenance terms
- evidence path: violated requirement, failing scenario, risky control flow, missing validation, or contract break — for a `P0` or `P1`, this must include the reachability path required above, and a finding capped at `P2` for unreachability must say what it would take to reach it
- concrete fix direction or verification step

Bad finding: vague, speculative, or style-only without impact.

Good finding: specific, reproducible, and tied to real engineering risk.

## Required Output

When a `drift-audit` result exists, or the commissioning context states that authorization was checked elsewhere, start with `Authorization Status` recording which of the two it was, then list quality findings ordered by severity. When no frozen contract exists, start with findings as usual.

```md
## Authorization Status
- Drift audit verdict, or the check the commissioning context performed:
- Notes:

## Findings

1. [P1] `path/to/file:123` Title
   Why this is a problem, what behaviour or requirement it breaks, and the fix direction.

## Contract Defects
- Conflicting clauses and the reading that appears coherent. `- none` when the contract is self-consistent or absent.

## Open Questions / Assumptions
- Only include items that materially affect confidence.

## Coverage Summary
- Scope reviewed: ...
- Requirements checked against: ...
- Dimensions checked: correctness, edge cases, tests, ...
- Validation run / not run: ...

## Verdict
- `FAIL` if any `P0` or unresolved `P1`
- `PASS WITH RISKS` if only `P2` or `P3` issues remain, or validation gaps remain
- `PASS` only when no material issues were found
```

If there are no findings, say so explicitly and still list residual risks or unrun validation.

## Review Priorities

Prioritize in this order unless the task clearly demands otherwise:

1. Correctness and unmet requirements
2. Memory, resource, and lifetime safety
3. Numerical or scientific validity and reproducibility
4. Concurrency and shared-state hazards
5. Error handling and recovery paths
6. Test adequacy and regression protection
7. Performance and scalability when the change touches hot paths or large data
8. Maintainability, portability, and documentation

## Avoid Shallow Reviews

- Do not rely on a single score threshold to suppress medium-risk issues.
- Do not conclude "looks good" after scanning only the diff header or happy-path tests.
- Do not treat passing tests as proof of correctness.
- Do not over-index on style when deeper risks exist.
- Do not skip scientific review dimensions just because the code compiles.

## Notes

- Plan-based review is a special case, not the whole skill.
- When the user asks for a "review", default to a review mindset even if no plan file exists.
- Reviews are primarily about findings. Summaries are secondary.
