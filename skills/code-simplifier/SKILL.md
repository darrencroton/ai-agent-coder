---
name: code-simplifier
description: Simplifies and refines working code for clarity, consistency, and maintainability while preserving exact functionality. Use only when the user explicitly asks for simplification, refactoring, leaner code, or a holistic cleanup pass.
---

# Code Simplifier

Use this skill for a separate improvement pass over code that already works. It is not part of the default scoped-implementation workflow: the user invokes it deliberately, with existing behaviour, tests, or outputs available to compare against. Be ambitious about simplification, but never change what the code does — only how it does it.

## Boundaries

- **Preserve functionality exactly.** Product behaviour, public contracts, data shapes, test meaning, and accepted edge cases are fixed. If a meaningful simplification would require changing any of them, report it as a recommendation instead of making the change.
- **Respect frozen contracts.** After an implementation-plan or scoped-implementation workflow, treat the implemented behaviour and its accepted edge cases as part of the contract. Simplification is never a back door for scope expansion.
- **Use the requested scope.** For a holistic pass, inspect enough surrounding code to simplify the design coherently. For a narrow request, refine only the named files or functions.

## Standards Come From the Project

Discover conventions rather than importing them: read `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING`, linter/formatter configuration, and the surrounding code, then match what the project actually does — naming, module layout, error-handling idiom, comment density, import style, test structure. Do not impose conventions from another ecosystem onto this one.

## What To Improve

- Reduce unnecessary complexity, nesting, and indirection.
- Eliminate redundant code and abstractions that no longer earn their place.
- Improve names where the current ones obscure intent.
- Consolidate related logic that has drifted apart.
- Remove comments that restate the code; keep the ones that carry constraints the code cannot show.
- Prefer explicit, boring constructs over clever or dense ones — clarity beats brevity, and deeply nested conditional expressions usually read worse than a plain conditional.

## What To Avoid

- Over-simplification that hurts debuggability, extension, or reading order.
- Combining unrelated concerns into one function or component to save lines.
- Removing an abstraction that is doing real organizational work.
- Any edit whose only defense is "fewer lines".

## Workflow

1. Identify the target code and the evidence of current behaviour (tests, outputs, usage).
2. Analyze for simplification opportunities within the boundaries above.
3. Apply the project's own standards and conventions.
4. Verify functionality is unchanged: run the relevant tests and checks, or name exactly which checks would prove it when they cannot be run here.
5. Report the significant changes that affect understanding, plus any recommendations that were out of bounds because they would change behaviour.
