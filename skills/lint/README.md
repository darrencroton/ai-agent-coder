# Lint — CLI reference and tool matrix

Maintainer/operator reference. The behavioural contract is in
[SKILL.md](SKILL.md); this file covers the CLI surface, the tool table, and
one-off setup.

## Setup

The skill needs Python 3.9+ and `git`. The linters themselves are optional and
per-language: install only what your repositories actually contain. Nothing is
installed implicitly — see "Install safety" below.

```bash
cd <your repo>
python3 ~/.claude/skills/lint/scripts/lint.py detect        # what's missing
python3 ~/.claude/skills/lint/scripts/lint.py install       # dry run
python3 ~/.claude/skills/lint/scripts/lint.py install --yes # execute
```

`install` picks the first recipe matching an available package manager, probed in
this order: `uv`, `pipx`, `brew`, `npm`, `apt`, `pip`. `--all-tools` plans for
every registered tool rather than only those the current change needs — the right
flag for one-time machine setup.

## Commands

### `detect [paths...]`

Reports the tools selected for the scope, how many files each would see, and
which binaries are missing. No linter runs. `--json` for machine output.

### `check [paths...]`

| Flag | Meaning |
|---|---|
| `--base REF` | Differential mode: report only findings absent at `REF`. The normal gate mode. |
| `--all` | Absolute mode: report every finding. With no paths and no `--base`, scope is the whole tracked tree. |
| `--require-coverage` | Exit 3 when a language present in the scope has no tool available. Ranked above findings. |
| `--enable TOOL` | Turn on an opt-in tool (repeatable). |
| `--skip TOOL` | Turn off a tool (repeatable) — the escape hatch when a rule contradicts a project convention. |
| `--json` | Machine-readable report. |
| `--timeout N` | Per-tool timeout, seconds (default 300). |

**Scope**, when no explicit paths are given:

- with `--base`: files changed in `REF...HEAD`, **plus** uncommitted tracked
  modifications, **plus** untracked files. A gate must see everything about to
  land, including a file created but not yet `git add`ed.
- with `--all` and no base: every tracked file.
- otherwise: uncommitted tracked modifications plus untracked files.

There is no `--staged` mode. It would have listed staged paths while linting
worktree content, so a staged defect could pass behind an unstaged fix, and it
had no base ref so it could also block on pre-existing debt. `check --base HEAD`
before staging is the correct pre-commit call and covers the same ground.

Deleted files are never in scope.

### `install [paths...]`

Prints the plan; executes only with `--yes`. `--all-tools` for full setup.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | pass — no new findings (differential) / no findings (absolute) |
| 1 | new findings present |
| 2 | error — bad usage, git failure, or a linter that exited unexpectedly with nothing parseable (at head or at the base ref) |
| 3 | coverage gap: `--require-coverage` was given, or a differential run where nothing differs from the base ref, so nothing was linted (usually a stale ref, e.g. `--base HEAD` after committing). A deletion-only change is an empty scope with a good ref, and still passes. |

Precedence is **error > coverage gap > findings**: an incomplete check is a worse
answer than a complete one that found something. Callers should treat 2 and 3 as
"this check did not happen", never as a pass.

## Tool matrix

Default tier runs whenever a matching file is in scope. Opt-in tier needs
`--enable` because it requires project-specific configuration.

| Tool | Tier | Languages | Binary | Catches |
|---|---|---|---|---|
| `ruff-check` | default | Python | `ruff` | Unused imports/variables, undefined names, unsorted imports, and the rest of the pyflakes/pycodestyle/isort/pyupgrade superset |
| `ruff-format` | default | Python | `ruff` | Formatting drift (Black-compatible) |
| `markdownlint` | default | Markdown | `markdownlint-cli2` | Malformed tables (**MD056** — wrong cell count, where the extra cell is silently dropped when rendered), heading structure, list and spacing errors |
| `codespell` | default | any text | `codespell` | Misspellings in comments, strings, prose, and whole-word identifiers (snake_case parts inside a longer identifier can be missed) |
| `shellcheck` | default | Shell (`.sh`, `.bash`) | `shellcheck` | Unquoted expansions that word-split (**SC2086**), misused `test` operators, unreachable code, `$?` read after the wrong command |
| `clang-format` | default | C/C++ | `clang-format` | Formatting drift; needs a `.clang-format` in the project. One finding per file, not per hunk |
| `cppcheck` | default | C/C++ | `cppcheck` | Static analysis — null dereference, uninitialised use, portability. No compile database needed |
| `git-diff-check` | built-in | any | `git` | Whitespace errors and conflict markers introduced by the change. Inherently differential |
| `clang-tidy` | opt-in | C/C++ | `clang-tidy` | Deep static analysis. Auto-skips unless `compile_commands.json` exists |
| `gfortran-syntax` | opt-in | Fortran | `gfortran` | Syntax and `-Wall -Wextra` warnings. Opt-in because module/include paths are project-specific |

**Why no `gcc -Wall` entry for C:** compiling a translation unit generically
needs the project's include paths and defines, which cannot be inferred. `cppcheck`
gives most of the value with no build system, and `clang-tidy` gives the rest once
a compile database exists.

**Why no Fortran linter:** no mature standalone one exists. `gfortran
-fsyntax-only` is the pragmatic check, kept opt-in because a file using a module
will not compile standalone without the right `-I`/`-J` paths. Differential mode
largely absorbs the resulting noise (the same errors appear at base and cancel),
but a brand-new file can still report spuriously — hence opt-in.

**Why shell is extension-only:** selection is driven by file extension, so a
script named `deploy` with a `#!/bin/bash` shebang and no suffix is **not**
linted, and passing it explicitly does not help — `select_tools` and `files_for`
both filter on `os.path.splitext`, so the file is dropped either way. Sniffing
shebangs would mean reading the content of every changed file at both the head
and the base ref. Until that changes, an extensionless script needs `shellcheck`
run against it directly, and the shell language will not appear in the coverage
report at all.

**Why shellcheck runs at `--severity=info`:** the `style` tier is presentational
advice (backticks versus `$(...)`), which "defects, not taste" excludes. `info`
is kept because SC2086 — an unquoted expansion that word-splits on a path
containing a space — lives there and is a real defect. The cap is unconditional
because `severity` is not a valid `.shellcheckrc` directive (verified against
0.11.0), so it cannot conflict with a project's own configuration. A project's
`.shellcheckrc` selects *rules* (`disable=SC2086`), and shellcheck honours it
regardless of this flag, including rc files found in a script's own directory
rather than the repository root.

**Why shellcheck alone uses `ok_codes=(0,)`:** shellcheck honours a
`SHELLCHECK_OPTS` environment variable whose `--format` beats the one this skill
passes. An inherited `SHELLCHECK_OPTS=--format=quiet` prints nothing and exits 1
— which under the usual `(0, 1)` would have read as a clean pass. With `(0,)`,
exit 1 is accepted only when findings were actually parsed, and non-JSON output
raises instead of parsing to zero findings.

**Why no complexity metric:** complexity is measured and interpreted by the
[`code-health`](../code-health/) skill. It is a judgement signal rather than a
mechanically decidable defect, so it does not belong in lint's gate-shaped
finding model.

## Configuration

Each linter reads the target project's own config first — `pyproject.toml`/`.ruff.toml`
for ruff, `.clang-format` for clang-format, `.markdownlint*`/`.markdownlint-cli2*`
for markdownlint. That is the intended way to encode a project's conventions.

Only markdownlint has a shipped fallback, `config/markdownlint.jsonc`, used when
the project supplies none. It keeps rules on by default and turns off the ones
that encode a house style rather than a defect: line length (MD013 — many
projects deliberately do not hard-wrap prose), list/fence/emphasis marker styles,
table pipe spacing, and duplicate headings (MD024 — implementation plans repeat
`### Validation Plan` once per slice by design). The load-bearing defect rules
stay on, MD056 table-column-count among them.

Two facts bound the risk of a future markdownlint release adding a noisy rule:
findings are differential, so only what this change introduces is reported, and
lint is not a floor fact, so `--skip markdownlint` is always available.

## Differential comparison

Findings are compared by **signature**, not by line:

```text
(tool, rule, path, message)
```

Counts of identical signatures are compared, so:

- an unrelated edit that shifts line numbers is **not** new;
- a **second** occurrence of the same rule in the same file **is** new;
- any finding in a file that did not exist at the base ref is new;
- a file **renamed** since the base ref is linted at its old path on the base
  side and its findings remapped, so a rename does not fabricate findings.

Message text is compared verbatim. Digits in a message are semantic
(markdownlint's `Expected: 2; Actual: 3`); positional digits live in the `line`
field, which the signature excludes.

**Stated limits — this is a heuristic, not an oracle.** Removing one finding and
adding an equivalent one elsewhere in the same file nets to zero and is not
reported. Formatters emit one finding per file, so a file already unformatted at
base cannot show a second formatting regression. Use `--all` when you need the
complete picture rather than the delta.

The base side is linted in a detached `git worktree` created under the system temp
directory and removed afterwards. The working tree is never checked out, stashed,
or otherwise disturbed.

## Install safety

`check` and `detect` never install. This is deliberate and load-bearing: under
Mode B the supervised Developer prompt forbids installing or changing
dependencies, so a lint step that could install would hand an unattended agent a
way around that rule. A supervised Developer that finds a tool missing must
report the gap; the human installs it once.

`install` without `--yes` prints the exact commands and exits without running
them.

## Tests

```bash
python3 -m unittest discover -s skills/lint/tests -p 'test_*.py'
```

No linter binary required — each parser is tested against output
captured from the real tool. The suite pins the bugs found during
implementation: symlink resolution on both sides of `_rel` (the macOS
`/var` → `/private/var` case that silently broke every signature match),
count-based rather than set-based signature comparison, untracked files being in
scope, absolute mode scoping to the tracked tree, and the optional severity token
in `markdownlint-cli2` output.
