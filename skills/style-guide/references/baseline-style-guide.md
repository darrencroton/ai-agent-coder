# Baseline Style Guide

This is the default coding style baseline this skill ships with — one coherent, deliberately-chosen set of conventions, generalised from a real, well-run scientific codebase's own style guide. It serves three purposes: `draft` mode adapts it into a project's own `STYLE-GUIDE.md`; `audit` mode checks code against it directly when no project guide has been drafted yet; `write` mode applies it while writing code under the same condition.

A project's own drafted guide always wins over this baseline for anything it explicitly states. This baseline fills every gap the project hasn't decided for itself, and stands in for a guide entirely until one is drafted.

## Guiding Principle

Style exists to support correctness and reviewability, not to prove compliance with a rulebook. This baseline exists to fill gaps and settle real inconsistency, not to force pointless churn on code that already reads well — which is exactly the trade-off `draft` mode is for: it weighs a project's own established, clear, consistently-applied convention against this baseline and records the outcome, dimension by dimension, in the project's own guide. Once that's resolved (or deliberately left unresolved, in which case this baseline stands as-is), `audit` and `write` apply the result directly, without re-litigating it — see their own mode sections in `SKILL.md`.

## Tool-Owned Style

A configured formatter or linter (clang-format, Black, ruff, prettier, markdownlint, or a language-appropriate equivalent) is authoritative for the mechanics it covers — indentation, line length, import order, whitespace. Cite the config; never restate what it already enforces. Where no tool covers a language in use, this baseline's own defaults apply: a 100-column soft limit and consistent continuation-line indentation.

## Naming

- Functions, local variables, and files: `snake_case` in C, C++, and Python.
- Types (classes, structs): `PascalCase` in Python and C++; in C, prefer a bare `struct Name` over a typedef'd alias for project-owned types, since it keeps the underlying type visible at every use site.
- Constants, macros, and enum values: `UPPER_SNAKE_CASE`.
- Test functions: `test_<behaviour_being_validated>`, named for the behaviour under test, not the implementation.
- A field or identifier that is part of a generated, legacy, or externally-imposed schema keeps its existing name verbatim, even where it breaks every rule above — compatibility beats consistency there.

## Comments

- Good subjects: contracts and invariants a reader cannot infer from the signature; units, coordinate systems, and sign conventions; sentinel and edge-case values; ownership and lifetime rules; why a non-obvious choice was made; why a warning is deliberately non-fatal.
- Poor subjects: restating the next line of code; explaining an obvious assignment; a TODO with no owner or resolution condition; a historical note that belongs in commit history instead.
- Prefer one or two focused lines; move a longer explanation to a function-level comment or a doc.

## Documentation

- Every major document opens with a short purpose statement.
- Prose is not hand-wrapped; a modern renderer soft-wraps a long line automatically, and manual wrapping makes prose harder to edit and review.
- Code and config blocks inside docs stay within a readable width even where prose does not.
- A committed fact must resolve for a fresh clone: never cite a gitignored or machine-local file as the source of a technical claim. Naming a working file (a scratch note, an archived log) as *where work is tracked* is fine; the fact itself must be stated where it is used and evidenced against committed code.
- Keep user-facing docs (README, usage, configuration) and developer-facing docs (contracts, extension points, ownership) separated rather than combined.

## Logging and Errors

Where the project has, or adopts, a logging framework, use this level taxonomy unless the project's own framework already defines an equivalent one:

- **Fatal** — unrecoverable; continuing would corrupt results or hide invalid state.
- **Error** — the current operation aborts; the caller can recover.
- **Warning** — unusual but recoverable; the user should still see it.
- **Info** — a major lifecycle or run milestone.
- **Verbose** — diagnostic detail, shown only in a verbose/debug mode.

Error messages name the concrete file, function, parameter, or path involved, not just "invalid input." Whether an error is *handled* correctly — caught where it should be, a fallback that's actually safe — is a `code-review` question; this baseline only covers what a message and its level look like once the behaviour is already decided.

## Metadata and Configuration

Config and schema files (YAML, JSON, TOML, `.ini`) are structural source code: keep key order consistent with neighbouring entries, provide a `description` wherever the schema allows one, document units for physical quantities, and prefer an explicit empty collection (`[]`, `{}`) to an absent key when the schema expects a list or map.

## Testing

- Test names describe the behaviour under test, not the implementation.
- A skipped test states why.
- Long test output is captured to a file, not left to scroll a terminal or fill an agent's context.
- Whether an assertion is adequate, or a test is missing for genuinely risky behaviour, is a `code-review` call; this baseline only covers naming and output presentation.

## Generated Code

If any part of the codebase is generated (codegen, migrations, compiled schema bindings, autoconf output), state clearly which directories are generated, forbid hand-editing them, and name the regeneration command. Review generated output primarily through its source metadata or generator — but reading the generated diff itself remains a normal part of `code-review`, never something this baseline discourages.

## Repository and Package Boundaries

This baseline has no opinion here — ownership boundaries are inherently project-specific, and substantive architecture policy doesn't belong duplicated into a style guide as a second source of truth. If the project has an existing, authoritative statement of directory or package ownership (an architecture doc, README, or `CONTRIBUTING`), a drafted project guide should point to it briefly, not restate it. If no such document exists, note the gap rather than inventing ownership rules here. Auditing whether the codebase actually honours those boundaries is `code-health`'s and `code-review`'s job, not this skill's.

## Language Notes

### C / C++

Beyond naming and tool-owned formatting above: if a configured formatter owns include ordering (for example clang-format's include-sorting rules), follow its configuration; otherwise preserve the local file's own existing include order rather than reshuffling it unprompted. Use the project's own logging macros rather than raw `printf` in runtime code, except in tests or deliberate CLI output; prefer early returns for invalid or no-op cases over deep nesting.

### Python

Beyond naming and tool-owned formatting: keep CLI parsing in `main()` or script-level orchestration functions, not scattered at module level; prefer `Path` over string paths for repository-relative locations; give a public function a docstring when its behaviour, return value, or error contract is not obvious, using `Args:`/`Returns:` where they clarify the contract.

### Fortran

No mature standalone Fortran linter exists (a compiler's own warnings are the closest substitute); a formatter such as `fprettify` may or may not be part of a given project's setup. Where nothing is tool-owned, this baseline's defaults are: free-form source, a 100-column soft limit, structured block termination (`end do`, `end if`) rather than legacy numeric-label loop constructs, and naming the enclosing construct on a longer `end subroutine <name>` / `end function <name>` / `end module <name>` for readability.

### Markdown

Follow the Documentation section above. Table formatting and other mechanical Markdown concerns are `markdownlint`'s job where the `lint` skill's coverage applies.
