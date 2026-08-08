#!/usr/bin/env python3
"""Deterministic, differential lint runner for the lint skill.

Three subcommands:

  detect    Report which languages are present in the target set, which tools
            cover them, which are installed, and what coverage is unavailable.
  check     Run the covering linters and report findings. Differential by
            default (`--base <ref>`): only findings absent at the base ref are
            reported as new, so pre-existing debt cannot block a change.
  install   Print (and with --yes, execute) the commands to install missing
            tools. Never invoked implicitly: `check` and `detect` never install.

Design invariants (see SKILL.md):
  * `check` mutates nothing outside a temporary git worktree it removes.
  * A missing tool is reported as `unavailable` coverage, never a silent pass.
  * Absolute mode (`--all`) is for standalone use; differential mode is for
    workflow gates, where pre-existing findings are out of scope.

Exit codes:
  0  pass            no new findings (differential) / no findings (absolute)
  1  findings        new findings present
  2  error           usage, git, or internal failure
  3  coverage        --require-coverage given and some changed language is
                     uncovered, or a differential scope empty because nothing
                     differs from the base ref (nothing was linted)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from typing import Any

EXIT_PASS = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2
EXIT_COVERAGE = 3

# Tools colourise their own diagnostics; strip that before quoting them back.
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# --- Data model --------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    tool: str
    rule: str
    path: str
    line: int
    message: str

    def signature(self) -> tuple[str, str, str, str]:
        """Line-insensitive identity, so unrelated edits that shift lines do not
        read as new findings. The message is kept verbatim: digits in a message
        are semantic (markdownlint's "Expected: 2; Actual: 3"), while positional
        digits live in `line`, which is deliberately excluded. Counts of
        identical signatures are compared, so a second occurrence of the same
        rule in the same file is still new.

        This is a heuristic, not an exact "introduced finding" oracle: a change
        that removes one occurrence and adds an equivalent one elsewhere in the
        same file leaves the count unchanged and is not reported. See README
        "Differential comparison" for the stated limits."""
        return (self.tool, self.rule, self.path, self.message)


@dataclass
class ToolResult:
    name: str
    available: bool
    ran: bool
    findings: list[Finding] = field(default_factory=list)
    error: str | None = None
    skipped_reason: str | None = None


# --- Tool registry -----------------------------------------------------------


@dataclass
class Tool:
    name: str
    language: str
    extensions: tuple[str, ...]
    binary: str
    # argv builder: (binary, [files]) -> argv
    build: Callable[[str, list[str]], list[str]]
    # parser: (stdout, stderr, cwd) -> findings
    parse: Callable[[str, str, str], list[Finding]]
    default: bool = True
    # Exit codes that mean "the tool ran": clean, or ran-and-found-something.
    # Anything else with no parsed findings is a failure, not a pass.
    ok_codes: tuple[int, ...] = (0, 1)
    # Optional extra gate, e.g. clang-tidy needs a compile database.
    precondition: Callable[[str], str | None] | None = None
    # Optional extra argv, e.g. a shipped default config used only when the
    # project has none of its own. Receives the repo root.
    extra_argv: Callable[[str], list[str]] | None = None
    note: str = ""


def _rel(cwd: str, path: str) -> str:
    """Path relative to cwd, symlinks resolved on both sides.

    realpath is required, not abspath: on macOS tempfile hands back
    /var/folders/... while tools report the resolved /private/var/folders/...,
    and relpath between the two yields a ../../.. escape that silently breaks
    base/head signature matching in differential mode.
    """
    try:
        return os.path.relpath(os.path.realpath(os.path.join(cwd, path)),
                               os.path.realpath(cwd))
    except ValueError:
        return path


# -- ruff check -------------------------------------------------------------- #


def _parse_ruff_check(out: str, err: str, cwd: str) -> list[Finding]:
    out = out.strip()
    if not out:
        return []
    try:
        rows = json.loads(out)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ruff check produced unparsable JSON: {out[:200]}") from exc
    findings = []
    for r in rows:
        loc = r.get("location") or {}
        findings.append(
            Finding(
                tool="ruff-check",
                rule=str(r.get("code") or "ruff"),
                path=_rel(cwd, r.get("filename") or ""),
                line=int(loc.get("row") or 0),
                message=str(r.get("message") or "").strip(),
            )
        )
    return findings


# -- ruff format ------------------------------------------------------------- #

_RUFF_FMT = re.compile(r"^Would reformat:\s*(.+)$", re.MULTILINE)


def _parse_ruff_format(out: str, err: str, cwd: str) -> list[Finding]:
    return [
        Finding("ruff-format", "format", _rel(cwd, m.group(1).strip()), 0,
                "file is not formatted")
        for m in _RUFF_FMT.finditer(out + "\n" + err)
    ]


# -- markdownlint ------------------------------------------------------------ #

# markdownlint-cli2 text output, e.g.
#   doc.md:5 error MD058/blanks-around-tables Tables should be surrounded ...
#   doc.md:10:9 error MD056/table-column-count Table column count [Expected: 2 ...]
# The severity word is present in cli2 and absent in some cli1 builds, so it is
# optional here. MD056 is the rule that catches a malformed table row.
_MDL = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+)(?::\d+)?\s+(?:error|warning)?\s*"
    r"(?P<rule>MD\d+)\S*\s+(?P<msg>.*)$",
    re.MULTILINE,
)


def _parse_markdownlint(out: str, err: str, cwd: str) -> list[Finding]:
    findings = []
    for m in _MDL.finditer(out + "\n" + err):
        findings.append(
            Finding("markdownlint", m.group("rule"), _rel(cwd, m.group("path")),
                    int(m.group("line")), m.group("msg").strip())
        )
    return findings


# -- codespell --------------------------------------------------------------- #

_CODESPELL = re.compile(r"^(?P<path>.+?):(?P<line>\d+):\s*(?P<msg>.+)$", re.MULTILINE)


def _parse_codespell(out: str, err: str, cwd: str) -> list[Finding]:
    findings = []
    for m in _CODESPELL.finditer(out):
        findings.append(
            Finding("codespell", "spelling", _rel(cwd, m.group("path")),
                    int(m.group("line")), m.group("msg").strip())
        )
    return findings


# -- shellcheck -------------------------------------------------------------- #


def _parse_shellcheck(out: str, err: str, cwd: str) -> list[Finding]:
    """Parse `--format=json1`.

    JSON rather than the `gcc` format because a shellcheck message may itself
    contain a colon, which a positional `path:line:col:` regex mis-splits.

    Malformed JSON raises rather than returning no findings: shellcheck honours
    a `SHELLCHECK_OPTS` environment variable and its format option wins over
    this argv, so an inherited `SHELLCHECK_OPTS=--format=gcc` would otherwise
    turn every real finding into a silent pass (invariant 3).
    """
    out = out.strip()
    if not out:
        return []
    try:
        payload = json.loads(out)
    except ValueError as e:
        raise RuntimeError(
            f"shellcheck did not emit json1 output ({e}); "
            f"check SHELLCHECK_OPTS in the environment") from e
    if not isinstance(payload, dict) or "comments" not in payload:
        raise RuntimeError("shellcheck json1 output has no 'comments' key")
    findings = []
    for c in payload.get("comments") or []:
        findings.append(
            Finding(
                "shellcheck",
                f"SC{c.get('code')}",
                _rel(cwd, str(c.get("file") or "")),
                int(c.get("line") or 0),
                str(c.get("message") or "").strip(),
            )
        )
    return findings


# -- clang-format ------------------------------------------------------------ #

_CLANG_FMT = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+):(?:\d+):\s*(?:warning|error):\s*(?P<msg>.+?)\s*$", re.MULTILINE
)


def _parse_clang_format(out: str, err: str, cwd: str) -> list[Finding]:
    findings = []
    for m in _CLANG_FMT.finditer(err + "\n" + out):
        findings.append(
            Finding("clang-format", "format", _rel(cwd, m.group("path")),
                    int(m.group("line")), "code is not formatted")
        )
    # Collapse to one finding per file: clang-format emits one per hunk, which
    # is noise for a gate and unstable across unrelated edits.
    seen, uniq = set(), []
    for f in findings:
        if f.path in seen:
            continue
        seen.add(f.path)
        uniq.append(f)
    return uniq


# -- cppcheck ---------------------------------------------------------------- #

_CPPCHECK = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+):(?P<id>[A-Za-z0-9_]+):(?P<msg>.*)$", re.MULTILINE
)


def _parse_cppcheck(out: str, err: str, cwd: str) -> list[Finding]:
    findings = []
    for m in _CPPCHECK.finditer(err + "\n" + out):
        findings.append(
            Finding("cppcheck", m.group("id"), _rel(cwd, m.group("path")),
                    int(m.group("line")), m.group("msg").strip())
        )
    return findings


# -- clang-tidy (opt-in) ----------------------------------------------------- #

_CLANG_TIDY = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+):\d+:\s*(?:warning|error):\s*(?P<msg>.+?)\s*\[(?P<rule>[^\]]+)\]\s*$",
    re.MULTILINE,
)


def _parse_clang_tidy(out: str, err: str, cwd: str) -> list[Finding]:
    return [
        Finding("clang-tidy", m.group("rule"), _rel(cwd, m.group("path")),
                int(m.group("line")), m.group("msg").strip())
        for m in _CLANG_TIDY.finditer(out + "\n" + err)
    ]


def _needs_clang_format_config(cwd: str) -> str | None:
    """Without a .clang-format, clang-format falls back to LLVM style and would
    impose a convention the project never chose. The walk checks the repo root
    plus five ancestors, mirroring clang-format's own upward search -- so a
    stray ~/.clang-format a few directories up will just as silently enable
    format checks here as it would for a bare clang-format invocation. That is
    a known trade-off, kept for parity with the tool's real behavior rather
    than fixed here."""
    here = cwd
    for _ in range(6):
        for name in (".clang-format", "_clang-format"):
            if os.path.exists(os.path.join(here, name)):
                return None
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return "no .clang-format in the project"


def _needs_compile_db(cwd: str) -> str | None:
    for cand in ("compile_commands.json", "build/compile_commands.json"):
        if os.path.exists(os.path.join(cwd, cand)):
            return None
    return "no compile_commands.json found"


# -- gfortran syntax (opt-in) ------------------------------------------------ #

_GFORTRAN = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+):(?:\d+):\s*(?:Warning|Error):\s*(?P<msg>.+?)\s*$", re.MULTILINE
)


def _parse_gfortran(out: str, err: str, cwd: str) -> list[Finding]:
    return [
        Finding("gfortran-syntax", "fortran", _rel(cwd, m.group("path")),
                int(m.group("line")), m.group("msg").strip())
        for m in _GFORTRAN.finditer(err + "\n" + out)
    ]


SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MDL_PROJECT_CONFIGS = (
    ".markdownlint-cli2.jsonc", ".markdownlint-cli2.yaml", ".markdownlint-cli2.yml",
    ".markdownlint-cli2.cjs", ".markdownlint-cli2.mjs", ".markdownlint-cli2.js",
    ".markdownlint.jsonc", ".markdownlint.json",
    ".markdownlint.yaml", ".markdownlint.yml",
    ".markdownlint.cjs", ".markdownlint.mjs",
)


_RUFF_PROJECT_CONFIGS = ("ruff.toml", ".ruff.toml")


def _ruff_config(cwd: str) -> list[str]:
    """Use the project's own ruff config when it has one; otherwise the skill's
    shipped defect-focused default (SKILL.md invariant 4).

    A `pyproject.toml` counts only when it actually carries a `[tool.ruff]`
    table — nearly every Python project has the file, and treating its mere
    presence as configuration would silently disable the default everywhere.
    """
    for name in _RUFF_PROJECT_CONFIGS:
        if os.path.exists(os.path.join(cwd, name)):
            return []
    pyproject = os.path.join(cwd, "pyproject.toml")
    if os.path.exists(pyproject):
        try:
            with open(pyproject, encoding="utf-8") as fh:
                if re.search(r"^\s*\[tool\.ruff", fh.read(), re.MULTILINE):
                    return []
        except OSError:
            pass
    shipped = os.path.join(SKILL_DIR, "config", "ruff.toml")
    return ["--config", shipped] if os.path.exists(shipped) else []


def _markdownlint_config(cwd: str) -> list[str]:
    """Use the project's own markdownlint config when it has one; otherwise the
    skill's shipped defect-focused default (SKILL.md invariant 4)."""
    for name in _MDL_PROJECT_CONFIGS:
        if os.path.exists(os.path.join(cwd, name)):
            return []
    # markdownlint-cli2 also honours a package.json key.
    pkg = os.path.join(cwd, "package.json")
    if os.path.exists(pkg):
        try:
            with open(pkg) as fh:
                if "markdownlint-cli2" in json.load(fh):
                    return []
        except (OSError, ValueError):
            pass
    shipped = os.path.join(SKILL_DIR, "config", "markdownlint.jsonc")
    return ["--config", shipped] if os.path.exists(shipped) else []


PY = (".py", ".pyi")
MD = (".md", ".markdown")
C_EXT = (".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx")
# Extension-based selection only: an extensionless script with a `#!/bin/sh`
# shebang is not picked up. See README "Why shell is extension-only".
SHELL_EXT = (".sh", ".bash")
FORTRAN = (".f90", ".f95", ".f03", ".f08", ".f", ".for", ".ftn")

# Install recipes keyed by BINARY, not by tool: ruff-check and ruff-format ship
# in one executable, so the recipe is declared once for it.
INSTALL_RECIPES: dict[str, dict[str, list[str]]] = {
    "ruff": {"uv": ["uv", "tool", "install", "ruff"],
             "pipx": ["pipx", "install", "ruff"],
             "brew": ["brew", "install", "ruff"],
             "pip": [sys.executable, "-m", "pip", "install", "--user", "ruff"]},
    "codespell": {"uv": ["uv", "tool", "install", "codespell"],
                  "pipx": ["pipx", "install", "codespell"],
                  "brew": ["brew", "install", "codespell"],
                  "pip": [sys.executable, "-m", "pip", "install", "--user", "codespell"]},
    "markdownlint-cli2": {"npm": ["npm", "install", "-g", "markdownlint-cli2"]},
    "shellcheck": {"brew": ["brew", "install", "shellcheck"],
                   "apt": ["sudo", "apt-get", "install", "-y", "shellcheck"]},
    "clang-format": {"brew": ["brew", "install", "clang-format"],
                     "apt": ["sudo", "apt-get", "install", "-y", "clang-format"]},
    "cppcheck": {"brew": ["brew", "install", "cppcheck"],
                 "apt": ["sudo", "apt-get", "install", "-y", "cppcheck"]},
    "clang-tidy": {"brew": ["brew", "install", "llvm"],
                   "apt": ["sudo", "apt-get", "install", "-y", "clang-tidy"]},
    "gfortran": {"brew": ["brew", "install", "gcc"],
                 "apt": ["sudo", "apt-get", "install", "-y", "gfortran"]},
}

TOOLS: list[Tool] = [
    Tool(
        name="ruff-check", language="python", extensions=PY, binary="ruff",
        build=lambda b, f: [b, "check", "--output-format=json", "--force-exclude", "--"] + f,
        parse=_parse_ruff_check, extra_argv=_ruff_config,
        note="Unused names, undefined names, bugbear traps, naive datetimes.",
    ),
    Tool(
        name="ruff-format", language="python", extensions=PY, binary="ruff",
        build=lambda b, f: [b, "format", "--check", "--force-exclude", "--"] + f,
        parse=_parse_ruff_format,
        note="Black-compatible formatting check.",
    ),
    Tool(
        name="markdownlint", language="markdown", extensions=MD, binary="markdownlint-cli2",
        # markdownlint-cli2 appends a project config's "globs" to the CLI file
        # list rather than replacing it, so a config with e.g. "**/*.md" makes
        # both runs lint far beyond the changed files -- and every glob-matched
        # file that exists at head but not in the base checkout (untracked
        # work, runtime artifacts under .git/) reads as introduced by the
        # change. --no-globs suppresses that expansion while still honoring
        # the project's rule config and ignores.
        build=lambda b, f: [b, "--no-globs"] + f,
        parse=_parse_markdownlint, extra_argv=_markdownlint_config,
        note="Catches malformed tables, bad headings, broken structure.",
    ),
    Tool(
        name="codespell", language="any", extensions=(), binary="codespell",
        build=lambda b, f: [b, "--quiet-level=2", "--"] + f,
        parse=_parse_codespell, ok_codes=(0, 65),
        note="Language-agnostic typo check over comments, strings, prose, and"
             " whole-word identifiers (snake_case typos inside a longer"
             " identifier are not tokenized out and can be missed).",
    ),
    Tool(
        name="shellcheck", language="shell", extensions=SHELL_EXT, binary="shellcheck",
        build=lambda b, f: [b, "--severity=info", "--format=json1", "--"] + f,
        parse=_parse_shellcheck,
        # Exit 1 means "ran and found something", so it is accepted only when
        # findings were actually parsed -- see run_tool's `rc not in ok_codes
        # and not findings` branch. Exit 1 with nothing parsed is a suppressed
        # or reformatted run, which must be an error, not a pass.
        ok_codes=(0,),
        note="Unquoted expansions, misused test operators, unreachable code. "
             "Capped at --severity=info: the style tier is presentational "
             "advice, which 'defects, not taste' excludes. A project's own "
             "`.shellcheckrc` still governs which rules apply.",
    ),
    Tool(
        name="clang-format", language="c", extensions=C_EXT, binary="clang-format",
        build=lambda b, f: [b, "--dry-run", "-Werror"] + f,
        parse=_parse_clang_format, precondition=_needs_clang_format_config,
        note="Formatting drift only; requires a .clang-format in the project.",
    ),
    Tool(
        name="cppcheck", language="c", extensions=C_EXT, binary="cppcheck",
        build=lambda b, f: [b, "--enable=warning,portability", "--inline-suppr", "--quiet",
                            "--template={file}:{line}:{id}:{message}"] + f,
        parse=_parse_cppcheck,
        note="Static analysis with no compile database required.",
    ),
    # ---- opt-in tier ------------------------------------------------------- #
    Tool(
        name="clang-tidy", language="c", extensions=C_EXT, binary="clang-tidy",
        build=lambda b, f: [b] + f,
        parse=_parse_clang_tidy, default=False,
        precondition=_needs_compile_db,
        note="Deep static analysis; needs compile_commands.json.",
    ),
    Tool(
        name="gfortran-syntax", language="fortran", extensions=FORTRAN, binary="gfortran",
        build=lambda b, f: [b, "-fsyntax-only", "-Wall", "-Wextra"] + f,
        parse=_parse_gfortran, default=False,
        note="No mature standalone Fortran linter exists; the compiler is the check. "
             "Opt-in because module/include paths are project-specific.",
    ),
]

TOOLS_BY_NAME = {t.name: t for t in TOOLS}


# --- git helpers -------------------------------------------------------------


def run(argv: Sequence[str], cwd: str, timeout: int = 300) -> tuple[int, str, str]:
    try:
        p = subprocess.run(list(argv), cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, check=False)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError as e:
        raise RuntimeError(f"executable not found: {argv[0]}") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"timed out after {timeout}s: {' '.join(argv)}") from e


def git(args: Sequence[str], cwd: str) -> str:
    rc, out, err = run(["git"] + list(args), cwd)
    if rc != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {err.strip() or out.strip()}")
    return out


def repo_root(cwd: str) -> str:
    return git(["rev-parse", "--show-toplevel"], cwd).strip()


def changed_files(root: str, base: str | None) -> list[str]:
    """Repo-relative paths that exist now and differ from the comparison point.

    A gate must see everything about to land, so the working tree is always
    included alongside any committed range: tracked modifications *and*
    untracked files (a file just created but not yet `git add`ed would
    otherwise escape the check entirely).
    """
    names: list[str] = []
    if base:
        names += git(["diff", "--name-only", "--diff-filter=ACMR",
                      f"{base}...HEAD"], root).splitlines()
    names += git(["diff", "--name-only", "--diff-filter=ACMR", "HEAD"],
                 root).splitlines()
    names += git(["ls-files", "--others", "--exclude-standard"],
                 root).splitlines()
    uniq = sorted({n.strip() for n in names if n.strip()})
    return [n for n in uniq if os.path.isfile(os.path.join(root, n))]


def any_change(root: str, base: str) -> bool:
    """True if anything at all differs from `base`, deletions included.

    `changed_files` filters to paths that still exist, so a deletion-only change
    yields an empty scope with nothing to lint. That is a real (empty) answer,
    unlike an empty scope caused by a stale base ref.
    """
    names = git(["diff", "--name-only", f"{base}...HEAD"], root).splitlines()
    names += git(["diff", "--name-only", "HEAD"], root).splitlines()
    names += git(["ls-files", "--others", "--exclude-standard"], root).splitlines()
    return any(n.strip() for n in names)


def rename_map(root: str, base: str | None) -> dict[str, str]:
    """new path -> old path for files renamed since `base`.

    Without this, renaming a file carries its untouched pre-existing findings
    into the "new" bucket: the base worktree has nothing at the new path, so
    every finding in it looks introduced.
    """
    if not base:
        return {}
    try:
        out = git(["diff", "--name-status", "-M", f"{base}...HEAD"], root)
    except RuntimeError:
        return {}
    mapping: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.rstrip("\n").split("\t")
        if len(parts) == 3 and parts[0].startswith("R"):
            _status, old, new = parts
            mapping[new.strip()] = old.strip()
    return mapping


def whitespace_check(root: str, base: str | None) -> list[Finding]:
    """`git diff --check`: whitespace errors and conflict markers introduced by
    this change. Inherently differential, so it needs no base comparison run.

    Three sources, because no single git invocation covers them all and the
    prescribed pre-commit call passes `--base HEAD`:
      1. the committed range `base...HEAD` (skipped when base resolves to HEAD,
         where it would be empty);
      2. the worktree against HEAD, covering staged and unstaged edits;
      3. untracked files, which no `git diff --check` form reaches, via
         `--no-index` against /dev/null.
    """
    pat = re.compile(r"^(?P<path>.+?):(?P<line>\d+):\s*(?P<msg>.+)$", re.MULTILINE)

    def collect(args: list[str]) -> list[Finding]:
        _rc, out, _err = run(["git", *args], root)
        return [
            Finding("git-diff-check", "whitespace", m.group("path").strip(),
                    int(m.group("line")), m.group("msg").strip())
            for m in pat.finditer(out)
        ]

    findings: list[Finding] = []
    head = ""
    try:
        head = git(["rev-parse", "HEAD"], root).strip()
    except RuntimeError:
        head = ""
    if base:
        try:
            base_sha = git(["rev-parse", base], root).strip()
        except RuntimeError:
            base_sha = base
        if base_sha and base_sha != head:
            findings += collect(["diff", "--check", f"{base}...HEAD"])
    if head:
        findings += collect(["diff", "--check", "HEAD"])
    else:
        findings += collect(["diff", "--check", "--cached"])

    for rel in git(["ls-files", "--others", "--exclude-standard"], root).splitlines():
        rel = rel.strip()
        if not rel or not os.path.isfile(os.path.join(root, rel)):
            continue
        findings += collect(["diff", "--check", "--no-index", "/dev/null", rel])

    seen, uniq = set(), []
    for f in findings:
        key = (f.path, f.line, f.message)
        if key not in seen:
            seen.add(key)
            uniq.append(f)
    return uniq


# --- Tool selection and execution --------------------------------------------


def have(binary: str) -> bool:
    return shutil.which(binary) is not None


def select_tools(files: list[str], enable: Iterable[str], skip: Iterable[str]) -> list[Tool]:
    enable, skip = set(enable), set(skip)
    exts = {os.path.splitext(f)[1].lower() for f in files}
    chosen = []
    for t in TOOLS:
        if t.name in skip:
            continue
        if not t.default and t.name not in enable:
            continue
        if t.extensions and not (exts & set(t.extensions)):
            continue
        chosen.append(t)
    return chosen


def files_for(tool: Tool, files: list[str]) -> list[str]:
    if not tool.extensions:
        return files
    return [f for f in files if os.path.splitext(f)[1].lower() in tool.extensions]


def run_tool(tool: Tool, files: list[str], cwd: str, timeout: int) -> ToolResult:
    subset = files_for(tool, files)
    if not subset:
        return ToolResult(tool.name, have(tool.binary), False,
                          skipped_reason="no matching files")
    if not have(tool.binary):
        return ToolResult(tool.name, False, False,
                          skipped_reason=f"{tool.binary} not installed")
    if tool.precondition:
        why = tool.precondition(cwd)
        if why:
            return ToolResult(tool.name, True, False, skipped_reason=why)
    binary = shutil.which(tool.binary) or tool.binary
    argv = tool.build(binary, subset)
    if tool.extra_argv:
        # Insert after the binary so flags precede the file list.
        argv = [argv[0]] + tool.extra_argv(cwd) + argv[1:]
    try:
        rc, out, err = run(argv, cwd, timeout=timeout)
        findings = tool.parse(out, err, cwd)
    except RuntimeError as e:
        return ToolResult(tool.name, True, False, error=str(e))
    if rc not in tool.ok_codes and not findings:
        # A tool that exited unexpectedly and produced nothing parseable did
        # not lint anything. Reporting that as "0 findings" would silently
        # disable the gate -- e.g. ruff exits 2 on an invalid config with empty
        # stdout, which previously read as a clean pass.
        detail = ANSI.sub("", (err or out)).strip().splitlines()
        return ToolResult(
            tool.name, True, False,
            error=f"exited {rc} with no parseable findings: "
                  f"{detail[0][:160] if detail else '(no output)'}")
    return ToolResult(tool.name, True, True, findings=findings)


def lint_tree(tools: list[Tool], files: list[str], cwd: str, timeout: int) -> list[ToolResult]:
    return [run_tool(t, files, cwd, timeout) for t in tools]


# --- Coverage ----------------------------------------------------------------


def coverage(files: list[str], results: list[ToolResult]) -> dict[str, str]:
    """language -> covered | unavailable | none.

    `unavailable` is the honest answer when a language is present but no tool
    covering it ran. It must never be reported as a pass.
    """
    exts = {os.path.splitext(f)[1].lower() for f in files}
    langs = set()
    for t in TOOLS:
        if not t.extensions:
            # An extension-less tool (e.g. codespell) applies to every change,
            # so its language is always "present". Without this, a change of
            # only unrecognised extensions reported empty coverage and passed
            # with nothing actually checked -- the silent pass invariant 2 bans.
            langs.add(t.language)
        elif exts & set(t.extensions):
            langs.add(t.language)
    ran = {r.name for r in results if r.ran}
    out = {}
    for lang in sorted(langs):
        covering = [t.name for t in TOOLS if t.language == lang]
        out[lang] = "covered" if (set(covering) & ran) else "unavailable"
    return out


# --- Differential comparison -------------------------------------------------


def diff_findings(head: list[Finding], base: list[Finding]) -> list[Finding]:
    """Findings whose signature occurs more often at head than at base."""
    base_counts = Counter(f.signature() for f in base)
    new: list[Finding] = []
    seen: Counter = Counter()
    for f in head:
        sig = f.signature()
        seen[sig] += 1
        if seen[sig] > base_counts.get(sig, 0):
            new.append(f)
    return new


@contextmanager
def base_worktree(root: str, base: str):
    """Yield a detached worktree at `base`, removed on exit."""
    path = tempfile.mkdtemp(prefix="lint-base-")
    git(["worktree", "add", "--detach", "--quiet", path, base], root)
    try:
        yield path
    finally:
        for args in (["worktree", "remove", "--force", path], ["worktree", "prune"]):
            try:
                run(["git", *args], root)
            except RuntimeError:
                pass
        shutil.rmtree(path, ignore_errors=True)


# --- Reporting ---------------------------------------------------------------


def report(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2))
        return

    mode = payload["mode"]
    print(f"lint: {mode} mode, {payload['file_count']} file(s) in scope")

    for name, state in sorted(payload["coverage"].items()):
        mark = "ok" if state == "covered" else "UNAVAILABLE"
        print(f"  coverage {name:<10} {mark}")

    for r in payload["tools"]:
        if r["ran"]:
            print(f"  ran      {r['name']:<16} {r['findings']} finding(s)")
        elif r.get("error"):
            print(f"  ERROR    {r['name']:<16} {r['error']}")
        else:
            print(f"  skipped  {r['name']:<16} {r.get('skipped_reason')}")

    findings = payload["new_findings"]
    if findings:
        label = "NEW findings" if mode == "differential" else "findings"
        print(f"\n{label} ({len(findings)}):")
        for f in findings:
            loc = f"{f['path']}:{f['line']}" if f["line"] else f["path"]
            print(f"  {loc}: [{f['tool']}/{f['rule']}] {f['message']}")
    else:
        print("\nno new findings" if mode == "differential" else "\nno findings")

    for msg in payload.get("errors") or []:
        if msg.startswith("(at base ref)"):
            # Head-side errors already printed as tool rows above.
            print(f"  ERROR    {msg}")

    print(f"\nverdict: {payload['verdict']}")
    if payload.get("reason"):
        print(f"reason: {payload['reason']}")
    if payload["uncovered"]:
        print(f"uncovered languages: {', '.join(payload['uncovered'])} "
              "-- treat as N/A, not as a pass")
    print_missing_hint(payload.get("missing_binaries") or [])


# --- Subcommands -------------------------------------------------------------


def package_managers() -> list[str]:
    order = ["uv", "pipx", "brew", "npm", "apt-get", "pip"]
    found = []
    for m in order:
        if m == "pip":
            found.append("pip")
        elif have(m):
            found.append("apt" if m == "apt-get" else m)
    return found


def print_missing_hint(missing: list[str]) -> None:
    """The single place that says what a missing linter means and what fixes it.

    `check` never installs (invariant 1), so the only correct response to a
    missing tool is to tell the human what is absent and what would install it.
    Shared by `detect` and `check` so the wording cannot drift apart.
    """
    if not missing:
        return
    # Not "this language went unchecked": another tool may cover it, and
    # `coverage` owns that answer.
    print(f"\nmissing linters: {', '.join(missing)} — those checks did not run")
    print("  install (human, one-off):  lint.py install        # dry run, prints commands")
    print("                             lint.py install --yes  # execute them")
    # Only mention uv where uv would actually supply something missing.
    uv_would_help = any("uv" in INSTALL_RECIPES.get(b, {}) for b in missing)
    if uv_would_help and "uv" not in package_managers():
        print("  uv is the simplest way to get the Python linters and is not installed here:")
        print("    brew install uv   (or see https://docs.astral.sh/uv/)")


def cmd_detect(args) -> int:
    root = repo_root(args.repo)
    files = resolve_files(args, root)
    tools = select_tools(files, args.enable, args.skip)
    rows = []
    for t in tools:
        subset = files_for(t, files)
        rows.append({
            "name": t.name, "language": t.language, "binary": t.binary,
            "installed": have(t.binary), "files": len(subset),
            "default": t.default, "note": t.note,
        })
    missing = sorted({t.binary for t in tools if not have(t.binary)})
    payload = {
        "repo": root, "file_count": len(files), "tools": rows,
        "missing_binaries": missing,
        "package_managers": package_managers(),
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"lint detect: {len(files)} file(s) in scope under {root}")
        if not files:
            # "0 files, all tools installed" reads as a green light while almost
            # nothing was inspected. Say what scoping actually happened.
            print("  (scope defaults to CHANGED files, and none are changed — "
                  "pass paths or --base <ref> to inspect coverage)")
        for r in rows:
            state = "installed" if r["installed"] else "MISSING"
            tier = "" if r["default"] else " (opt-in)"
            print(f"  {r['name']:<16} {r['language']:<9} {r['files']:>4} file(s)  {state}{tier}")
        if missing:
            print_missing_hint(missing)
        else:
            print("\nall selected tools installed")
    return EXIT_PASS


def cmd_install(args) -> int:
    root = repo_root(args.repo)
    files = resolve_files(args, root) if not args.all_tools else []
    tools = TOOLS if args.all_tools else select_tools(files, args.enable, args.skip)
    managers = package_managers()
    plans: list[tuple[str, list[str]]] = []
    unresolved: list[str] = []
    for binary in sorted({t.binary for t in tools if not have(t.binary)}):
        recipes = INSTALL_RECIPES.get(binary, {})
        chosen = next((recipes[m] for m in managers if m in recipes), None)
        if chosen is None:
            unresolved.append(
                f"{binary} (no recipe for available managers: {', '.join(managers) or 'none'})")
        else:
            plans.append((binary, chosen))

    if not plans and not unresolved:
        print("nothing to install: every selected tool is already available")
        return EXIT_PASS

    print("lint install plan:")
    for binary, cmd in plans:
        print(f"  {binary:<20} {' '.join(cmd)}")
    for u in unresolved:
        print(f"  UNRESOLVED           {u}")

    if not args.yes:
        print("\ndry run -- nothing executed. Re-run with --yes to install.")
        print("NOTE: installing tools mutates this machine's environment. Under "
              "Mode B a supervised Developer must NOT run this; report the "
              "missing tool instead.")
        return EXIT_PASS

    failed = []
    for binary, cmd in plans:
        print(f"\n$ {' '.join(cmd)}")
        try:
            rc, out, err = run(cmd, root, timeout=args.timeout)
        except RuntimeError as e:
            print(f"  failed: {e}")
            failed.append(binary)
            continue
        sys.stdout.write(out)
        sys.stderr.write(err)
        if rc != 0:
            failed.append(binary)
    if failed:
        print(f"\nfailed to install: {', '.join(failed)}")
        return EXIT_ERROR
    print("\ninstall complete")
    return EXIT_PASS


def resolve_files(args, root: str) -> list[str]:
    if args.paths:
        out = []
        for p in args.paths:
            ap = os.path.abspath(p)
            if os.path.isdir(ap):
                for dirpath, dirnames, filenames in os.walk(ap):
                    dirnames[:] = [d for d in dirnames if d not in
                                   {".git", "__pycache__", "node_modules", "venv", ".venv"}]
                    for fn in filenames:
                        out.append(_rel(root, os.path.join(dirpath, fn)))
            elif os.path.isfile(ap):
                out.append(_rel(root, ap))
            else:
                # Silently dropping a path the caller named (a typo, a stale or
                # deleted file) lints less than asked and still reports a pass.
                raise RuntimeError(f"path not found: {p}")
        return sorted(set(out))
    if getattr(args, "all", False) and not args.base:
        # Absolute mode over no explicit paths: the whole tracked tree.
        return [n for n in git(["ls-files"], root).splitlines()
                if n.strip() and os.path.isfile(os.path.join(root, n))]
    return changed_files(root, args.base)


def cmd_check(args) -> int:
    root = repo_root(args.repo)
    try:
        files = resolve_files(args, root)
    except RuntimeError as e:
        print(f"lint: {e}")
        return EXIT_ERROR
    differential = bool(args.base) and not args.all

    if not files:
        # An empty differential scope is a coverage gap when nothing differs from
        # the base at all, because then nothing was linted and the usual cause is
        # a stale ref (`--base HEAD` after committing) -- a check that did not
        # happen, never a pass. If the ref is good and the change simply has no
        # lintable file (deletions only), that is a real answer: pass.
        gap = differential and not any_change(root, args.base)
        if gap:
            reason = (f"nothing differs between {args.base} and the working tree"
                      " -- check the base ref")
        elif differential:
            reason = "changes exist but none is a lintable file (e.g. deletions only)"
        else:
            reason = "no files in scope"
        payload = {"mode": "differential" if differential else "absolute",
                   "repo": root, "file_count": 0, "coverage": {}, "tools": [],
                   "new_findings": [], "uncovered": [], "missing_binaries": [],
                   "verdict": "coverage-gap" if gap else "pass", "reason": reason}
        report(payload, args.json)
        return EXIT_COVERAGE if gap else EXIT_PASS

    tools = select_tools(files, args.enable, args.skip)
    head_results = lint_tree(tools, files, root, args.timeout)
    head_findings = [f for r in head_results for f in r.findings]
    head_findings += whitespace_check(root, args.base if differential else None)

    base_errors: list[str] = []
    if differential:
        renames = rename_map(root, args.base)
        with base_worktree(root, args.base) as wt:
            present = []
            base_to_head: dict[str, str] = {}
            for f in files:
                old = renames.get(f, f)
                if os.path.isfile(os.path.join(wt, old)):
                    present.append(old)
                    base_to_head[old] = f
            base_results = lint_tree(tools, present, wt, args.timeout) if present else []
            base_findings = [
                # Re-point a renamed file's base findings at its head path, so
                # the signature comparison lines up.
                replace(f, path=base_to_head.get(f.path, f.path))
                for r in base_results for f in r.findings
            ]
            # A tool that ran at head but failed at base would make every
            # pre-existing finding look new. That is worse than no gate.
            base_errors = [f"{r.name}: {r.error}" for r in base_results if r.error]
        new = diff_findings(head_findings, base_findings)
    else:
        new = head_findings

    cov = coverage(files, head_results)
    uncovered = [k for k, v in cov.items() if v != "covered"]
    errored = ([f"{r.name}: {r.error}" for r in head_results if r.error]
               + [f"(at base ref) {m}" for m in base_errors])

    if errored:
        verdict = "error"
    elif uncovered and args.require_coverage:
        # Ranked above findings deliberately: an incomplete check is a worse
        # answer than a complete one that found something.
        verdict = "coverage-gap"
    elif new:
        verdict = "findings"
    else:
        verdict = "pass"

    payload = {
        "mode": "differential" if differential else "absolute",
        "repo": root,
        "base": args.base if differential else None,
        "file_count": len(files),
        "files": files,
        "coverage": cov,
        "uncovered": uncovered,
        "missing_binaries": sorted({t.binary for t in tools if not have(t.binary)}),
        "errors": errored,
        # Per-tool finding COUNT, not the findings themselves: the full list is
        # reported once under new_findings, so carrying it twice would be noise.
        "tools": [dict(asdict(r), findings=len(r.findings)) for r in head_results],
        "new_findings": [asdict(f) for f in new],
        "verdict": verdict,
    }

    report(payload, args.json)

    if verdict == "error":
        return EXIT_ERROR
    if verdict == "findings":
        return EXIT_FINDINGS
    if verdict == "coverage-gap":
        return EXIT_COVERAGE
    return EXIT_PASS


# --- CLI ---------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lint.py",
        description="Differential lint runner: report only findings this change introduced.",
    )
    p.add_argument("--repo", default=".", help="repository path (default: cwd)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("paths", nargs="*", help="explicit files/dirs (default: changed files)")
        sp.add_argument("--enable", action="append", default=[],
                        metavar="TOOL", help="enable an opt-in tool (repeatable)")
        sp.add_argument("--skip", action="append", default=[],
                        metavar="TOOL", help="skip a tool (repeatable)")
        sp.add_argument("--json", action="store_true", help="machine-readable output")
        sp.add_argument("--timeout", type=int, default=300, help="per-tool timeout seconds")

    d = sub.add_parser("detect", help="report tool coverage and what is missing")
    common(d)
    d.add_argument("--base", default=None, help="comparison ref (affects file scope only)")
    d.set_defaults(func=cmd_detect)

    c = sub.add_parser("check", help="run linters and report findings")
    common(c)
    c.add_argument("--base", default=None, metavar="REF",
                   help="base ref for differential mode (e.g. the slice's before_head)")
    c.add_argument("--all", action="store_true",
                   help="absolute mode: report every finding, not only new ones")
    c.add_argument("--require-coverage", action="store_true",
                   help="exit 3 when a present language has no tool available")
    c.set_defaults(func=cmd_check)

    i = sub.add_parser("install", help="print (or with --yes, run) install commands")
    common(i)
    i.add_argument("--base", default=None, help=argparse.SUPPRESS)
    i.add_argument("--yes", action="store_true", help="actually execute the commands")
    i.add_argument("--all-tools", action="store_true",
                   help="plan for every registered tool, not just those the scope needs")
    i.set_defaults(func=cmd_install)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for name in list(args.enable) + list(args.skip):
        if name not in TOOLS_BY_NAME:
            print(f"unknown tool: {name}\nknown: {', '.join(sorted(TOOLS_BY_NAME))}",
                  file=sys.stderr)
            return EXIT_ERROR
    try:
        return args.func(args)
    except RuntimeError as e:
        print(f"lint: {e}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
