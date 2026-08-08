# Measurement Methodology

## Evidence layers

- **Mechanical facts:** file inventory, line classifications, exact normalized repeated windows, Python AST nodes/imports, graph relationships, and Git counts.
- **Heuristic candidates:** a bounded reading list ordered from those facts. Candidate selection is not a verdict.
- **Agent judgment:** whether a measured structure is justified, risky, or worth changing. This never appears in the deterministic JSON bundle.

## Composition

The inventory comes from Git: tracked files plus untracked files not excluded by the repository's ignore rules. Duplicate index-stage rows are collapsed, and unresolved paths are recorded as a coverage limit. Symbolic links are listed as a coverage limit and never followed. Language is inferred from a recorded extension table. Test classification uses recorded path/name rules. Documentation and configuration remain separate from production and test source. V1 has no additional exclude or classification configuration; repository ignore rules are the only exclusion input.

Physical lines are partitioned into blank, comment, and code using the analyzer's recorded conservative lexical method. It recognizes comment-only lines rather than attempting full parsing, so inline and language-specific edge cases remain code. Treat cross-language comment ratios as approximate context, not a comparison target.

A language missing from the extension table produces **no coverage row at all**, so it would read as covered rather than unavailable. Two things close that hole: recognized languages now include Fortran, Make, CMake, Meson and autotools even though none has a complexity or dependency collector, and every remaining unrecognized path is listed in `repository.unrecognised_extensions` and as a coverage limit — present in `detect` as well as `analyze`, but deliberately **not** counted by `--require-coverage`: whether an unclassified file is source is a judgement, and gating on it failed ordinary repositories over a `.babelrc`. Disclosure satisfies the invariant; the gate stays on measurement that actually failed for a language in scope. Data and build products (images, archives, binaries, tabular and scientific data) classify as `data` rather than unmeasured source, so that list stays a signal instead of an inventory dump. Configuration is matched by extension or by named dotfile rather than by a leading dot, because a dotfile holding shell code is unmeasured source and calling it configuration would hide exactly the gap this field exists to expose. Build files without an extension are matched by filename.

Comment detection is per language and deliberately conservative. Fortran covers free-form `!` only, so fixed-form `C` or `*` in column one counts as code (an undercount), while OpenMP and OpenACC directives such as `!$omp` count as comments even though they are semantically executable (an overcount in HPC sources). Make counts `#` only in column one, since a tab-indented `#` is a recipe line the shell interprets.

File distributions report raw count, minimum, median, upper quantiles, and maximum. A tail value means "inspect this file's role," not "split this file."

## Duplication

The built-in detector compares fixed-length windows of nonblank lines after removing whitespace. It does not strip comments, rename identifiers, or claim semantic clone detection. The evidence bundle records the minimum window and normalization rule.

Differential signatures are count-based. When a changed block duplicates an untouched block, report the relationship and attribute investigation to the changed side. Do not blame the untouched file for the new relationship.

## Function complexity tiers

Python cyclomatic complexity is McCabe-style decision counting over stdlib AST functions. When installed, Lizard adds per-function cyclomatic evidence for its supported non-Python languages. Every function record names its engine; distributions remain within a language and engine coverage tier. It is not cognitive complexity and scores from different algorithms are not interchangeable.

## Dependency graphs

Two collectors emit dependency evidence, and both describe the same units: `fan_in`, `fan_out`, strongly connected components, and reverse-reachability blast radius. A zero is not proof that a file is unused, and the two graphs are never merged into one number.

**Python** contains statically resolved internal imports. Dynamic imports, runtime plugin registration, optional imports, path manipulation, and reflection may be absent.

**C family** (every C, C++ and CUDA extension in the analyzer's table — read the coverage matrix rather than a list copied here) contains internal `#include "..."` edges found by a lexical scan. It is deliberately not a preprocessor:

- Only quoted includes are edges. `#include <...>` names a system or external header by contract and is excluded, so no header search path is required.
- Comments and raw string literals are blanked before scanning, so a commented-out include is not an edge. Ordinary string and character literals are skipped rather than blanked — an include's own target is a string, and blanking would erase what is being measured — so a literal such as `"/*"` or `"dir//b.h"` cannot be mistaken for a comment. Raw strings are disabled for `.c` translation units (C has none) and enabled elsewhere, including the C/C++-ambiguous `.h`. An identifier ending in `R` before `"(` is string concatenation rather than a raw-string opener, and an unterminated opener is not treated as a raw string at all, so a lexing mistake can never silently blank the rest of a file. A leading byte-order mark does not hide the first include.
- The masker is lexical, not a compiler. On input that does not compile — an unterminated block comment, a stray apostrophe, a line-splice inside a literal — masking can be wrong in either direction. An unterminated quote is bounded to its line so it cannot swallow the includes below it.
- An include inside `#if`/`#ifdef` still counts. The graph records what the source text says, not what a given build configuration compiles.
- A path is resolved against the including file's own directory, then repository-relative, then a unique basename. Separators are canonicalized before normalization, so a Windows-style path resolves the same way on either host.
- Escape is judged *after* resolving against the including directory, so `../include/b.h` from `src/` is ordinary and stays inside. Only a target that is absolute, drive-lettered, or still outside the root once resolved is recorded `outside_repository`, and it never reaches the basename fallback: an explicit path out of the tree is external coupling, not an invitation to guess a same-named file inside it.
- An exact target that exists in the repository but is not a measured C-family file — unreadable, a symlink, or another language — is named as such rather than resolved to a same-named file elsewhere. The basename fallback is a last resort, never a way to override a path that was written explicitly.
- Basename uniqueness is decided across the whole repository, not just measured files: an unmeasured same-named file makes the basename ambiguous rather than invisible to the guess. Every unresolved include is recorded with a line number and a reason: `ambiguous_basename` (more than one candidate anywhere in the repository), `in_repository_unmeasured_language` (the exact target exists but is not measured — unreadable, a symlink, or another language), `unique_unmeasured_basename` (no exact target; the one basename match is unmeasured), `outside_repository`, `not_in_repository`, or `self_include`.
- Both sides of a differential use the same inventory and therefore the same resolution rules, so a base-side guess cannot manufacture a base relationship that hides a real new one.
- Macro-computed includes and continuation lines are invisible. A unique basename can still resolve to the wrong file — a vendored copy, a build artifact, or a target that really comes from an `-I` path outside the repository. Every edge therefore records how it was resolved in `via` (`exact_relative`, `repository_relative`, `unique_basename`), and the guessed ones are listed together in `basename_resolved_edges` so a reader can discount them without re-deriving the graph.

One graph spans the whole C family, so a single unreadable member makes the dependency row `unavailable` for every C-family language rather than only its own.

An include cycle is normal in C where header guards make it harmless; it is a prompt to check that the guards exist and that the coupling is intended, never a defect on its own.

## Differential semantics

Compare the current tracked-plus-untracked tree with the explicit Git base. A regression bucket requires a changed raw value or a new relationship. A dependency cycle counts as new unless its members were already mutually connected in a single base cycle *and* every edge between them already existed. Shrinking a cycle therefore reports nothing, while rewiring one into a smaller cycle with a new edge reports it, and a cycle absent from every base cycle is new even if no member file changed.

A file the base could not supply makes the baseline incomplete, whether it was unreadable or failed to parse: the affected coverage rows are `unavailable` rather than clean, the paths are listed in `facts.base_unreadable_files` or the `base_parse_errors` coverage limit so the cause is traceable, and any delta or cycle candidate that only its absence would produce is suppressed rather than reported as a change. When a reported cycle's members were already mutually connected at the base, the row carries `new_edges` and says so, rather than implying the whole cycle is new. Rank movement caused by population changes is context only. Renames should map base identities to current paths before signatures are compared.

## Git history context

History is optional because it depends on clone completeness and the selected window. Refuse history evidence on a shallow clone. Record the ref, maximum commit count, and Git invocation. Rename detection is disabled so churn keys remain literal repository paths; pre-rename and post-rename names remain separate facts. Report only revision and churn counts; do not emit author identities. History prioritizes inspection but never enters changed/worsened/resolved structural buckets.

## Interpreting disproportionality

To assess whether complexity seems disproportionate to role:

1. Identify the component's role from callers, public interfaces, tests, and documentation.
2. Compare its raw structure with peers that have the same role and measurement coverage.
3. Check whether complexity is inherent, concentrated at a boundary, generated, or compensating for external constraints.
4. State the plausible maintenance consequence before recommending change.
5. Prefer `no change justified` when evidence does not support intervention.
