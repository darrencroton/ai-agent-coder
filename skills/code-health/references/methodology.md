# Measurement Methodology

## Evidence layers

- **Mechanical facts:** file inventory, line classifications, exact normalized repeated windows, Python AST nodes/imports, graph relationships, and Git counts.
- **Heuristic candidates:** a bounded reading list ordered from those facts. Candidate selection is not a verdict.
- **Agent judgment:** whether a measured structure is justified, risky, or worth changing. This never appears in the deterministic JSON bundle.

## Composition

The inventory comes from Git: tracked files plus untracked files not excluded by the repository's ignore rules. Duplicate index-stage rows are collapsed, and unresolved paths are recorded as a coverage limit. Symbolic links are listed as a coverage limit and never followed. Language is inferred from a recorded extension table. Test classification uses recorded path/name rules. Documentation and configuration remain separate from production and test source. V1 has no additional exclude or classification configuration; repository ignore rules are the only exclusion input.

Physical lines are partitioned into blank, comment, and code using the analyzer's recorded conservative lexical method. It recognizes comment-only lines rather than attempting full parsing, so inline and language-specific edge cases remain code. Treat cross-language comment ratios as approximate context, not a comparison target.

File distributions report raw count, minimum, median, upper quantiles, and maximum. A tail value means "inspect this file's role," not "split this file."

## Duplication

The built-in detector compares fixed-length windows of nonblank lines after removing whitespace. It does not strip comments, rename identifiers, or claim semantic clone detection. The evidence bundle records the minimum window and normalization rule.

Differential signatures are count-based. When a changed block duplicates an untouched block, report the relationship and attribute investigation to the changed side. Do not blame the untouched file for the new relationship.

## Function complexity tiers

Python cyclomatic complexity is McCabe-style decision counting over stdlib AST functions. When installed, Lizard adds per-function cyclomatic evidence for its supported non-Python languages. Every function record names its engine; distributions remain within a language and engine coverage tier. It is not cognitive complexity and scores from different algorithms are not interchangeable.

The dependency graph remains Python-only and contains statically resolved internal imports. Dynamic imports, runtime plugin registration, optional imports, path manipulation, and reflection may be absent. `fan_in`, `fan_out`, strongly connected components, and reverse-reachability blast radius describe this graph only; a zero is not proof that a module is unused.

## Differential semantics

Compare the current tracked-plus-untracked tree with the explicit Git base. A regression bucket requires a changed raw value or a new relationship. Rank movement caused by population changes is context only. Renames should map base identities to current paths before signatures are compared.

## Git history context

History is optional because it depends on clone completeness and the selected window. Refuse history evidence on a shallow clone. Record the ref, maximum commit count, and Git invocation. Rename detection is disabled so churn keys remain literal repository paths; pre-rename and post-rename names remain separate facts. Report only revision and churn counts; do not emit author identities. History prioritizes inspection but never enters changed/worsened/resolved structural buckets.

## Interpreting disproportionality

To assess whether complexity seems disproportionate to role:

1. Identify the component's role from callers, public interfaces, tests, and documentation.
2. Compare its raw structure with peers that have the same role and measurement coverage.
3. Check whether complexity is inherent, concentrated at a boundary, generated, or compensating for external constraints.
4. State the plausible maintenance consequence before recommending change.
5. Prefer `no change justified` when evidence does not support intervention.
