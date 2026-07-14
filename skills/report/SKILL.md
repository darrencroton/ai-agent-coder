---
name: report
description: Create a concise evidence-backed written synthesis when the user explicitly asks for a report, assessment, investigation summary, comparison memo, status note, or final summary. Do not use for implementation planning, scoped implementation receipts, drift audits, code reviews, handoffs, PM run summaries, or commit messages.

---

# Report

Use this skill only when the useful deliverable is a human-readable synthesis of evidence and judgment. Keep it lean. The report should clarify what was found, what it means, and what should happen next without replacing the narrower workflow skills.

## Relationship To Other Skills

Prefer the more specific skill whenever one applies:

- Use `implementation-plan` for plan-first work, frozen acceptance slices, authorized surfaces, validation plans, and next-chat prompts.
- Use `scoped-implementation` for implementation receipts after a frozen slice is executed.
- Use `drift-audit` for authorization gates.
- Use `code-review` for quality findings and review verdicts.
- Use `handoff` when the goal is to preserve continuation state for another chat or agent.
- Use `project-manager` summaries and artifacts for PM run status.
- Use `commit` for commit messages and staging discipline.

`report` may summarize outputs from those skills after they have done their jobs, but it must not redefine their contracts or invent replacement sections.

## Workflow

1. State the report question or decision in one sentence.
2. Gather enough primary evidence to support the claims.
3. Decide the lightest useful structure for the user's request.
4. Write the synthesis with clear separation between facts, interpretation, recommendations, and unknowns.
5. End with the concrete next action, or state that none is needed.

## Default Shape

Use only the sections that add value. Rename or omit sections for short status notes.

```md
## Summary

## Evidence

## Findings

## Recommendation / Next Action

## Risks / Unknowns
```

For comparisons, include the decision criteria before the recommendation. For final summaries, include validation performed and residual risks. For status notes, a short `Current State`, `Blockers`, and `Next Action` format is enough.

## Evidence Standard

- Reference the files inspected.
- Name commands, traces, tests, logs, or documents used.
- Separate confirmed facts from assumptions.
- State what was not checked when that affects confidence.
- Keep conclusions proportional to the evidence available.
- When synthesizing outputs from multiple Reviewers or contributors, define the judgment criteria before making evaluative claims.
- When reporting an audit chain, identify whether each drift audit and code review was performed by a Reviewer or by Developer self-audit; include the Reviewer tool/label or self-audit fallback context when available.

## Output Rules

- Prefer short sections and high-signal bullets over long prose.
- Use explicit labels such as `Confirmed`, `Assumption`, `Unverified`, and `Blocked` when helpful.
- Make scope boundaries explicit when they affect interpretation.
- Make next actions concrete and ordered.
- Keep file-output reports in `docs/` only when the user asks for a document or file output.
- Do not add a completion-status section unless it clarifies why the requested report is done.

If the report becomes an implementation plan, handoff, drift audit, or code review while writing it, stop and switch to the appropriate skill.
