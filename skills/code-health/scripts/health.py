#!/usr/bin/env python3
"""Portable, evidence-first codebase structure analyser.

This module deliberately contains no quality score.  It measures a small,
auditable set of structural properties, and leaves any judgement about whether
an outlier is appropriate to the caller. The portable floor uses only Python's
standard library and ``git``.

``detect`` records available coverage. ``analyze`` records measured facts; in
differential mode it limits investigation candidates to changed raw values and
deltas from a git base revision.  Python analysis is stdlib-only; Lizard is an
optional external collector for selected non-Python cyclomatic complexity, and
nothing here ever installs it.  Exit status 0 means analysis completed, not
that a repository is "healthy".
"""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
import posixpath
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

BUNDLE_VERSION = "1.2"
EXIT_OK = 0
EXIT_ERROR = 2
EXIT_COVERAGE = 3
TOP_PER_FAMILY = 5
LIZARD_SUPPORTED = {"JavaScript", "TypeScript", "Java", "Kotlin", "C", "C/C++", "C++", "C#", "Go", "Ruby", "PHP", "Swift", "Rust", "Scala"}
LIZARD_INSTALL = "uv tool install lizard  (or pipx install lizard)"

# This is intentionally pragmatic rather than a claim to parse every language.
# The map is used for scope and line counting, not as a language standard.
EXTENSIONS = {
    ".py": "Python", ".pyi": "Python", ".js": "JavaScript", ".mjs": "JavaScript",
    ".cjs": "JavaScript", ".jsx": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin", ".c": "C", ".h": "C/C++",
    ".cc": "C++", ".cpp": "C++", ".cxx": "C++", ".hpp": "C++", ".cs": "C#",
    ".hh": "C++", ".hxx": "C++", ".h++": "C++", ".c++": "C++",
    ".inl": "C++", ".ipp": "C++", ".tcc": "C++",
    ".cu": "CUDA", ".cuh": "CUDA",
    ".go": "Go", ".rs": "Rust", ".rb": "Ruby", ".php": "PHP", ".swift": "Swift",
    ".scala": "Scala", ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell",
    ".fish": "Shell", ".ps1": "PowerShell", ".r": "R", ".R": "R", ".lua": "Lua",
    ".pl": "Perl", ".pm": "Perl", ".ex": "Elixir", ".exs": "Elixir",
    ".fs": "F#", ".fsx": "F#", ".dart": "Dart", ".sql": "SQL",
    ".f90": "Fortran", ".f95": "Fortran", ".f03": "Fortran", ".f08": "Fortran",
    ".f": "Fortran", ".for": "Fortran", ".ftn": "Fortran",
    ".F90": "Fortran", ".F95": "Fortran", ".F03": "Fortran", ".F08": "Fortran",
    ".F": "Fortran", ".FOR": "Fortran", ".FTN": "Fortran",
    ".mk": "Make", ".make": "Make", ".cmake": "CMake", ".m4": "Autoconf",
}
# Build files are matched by name because they carry no extension, or (CMake)
# an extension already owned by documentation.  Recognising them keeps a build
# system visible as source rather than silently 'other'.
FILENAMES = {
    "makefile": "Make", "gnumakefile": "Make", "bsdmakefile": "Make",
    "makefile.am": "Automake", "makefile.in": "Automake",
    "cmakelists.txt": "CMake", "meson.build": "Meson", "configure.ac": "Autoconf",
    ".bashrc": "Shell", ".bash_profile": "Shell", ".zshrc": "Shell", ".zprofile": "Shell",
    ".profile": "Shell", ".bash_aliases": "Shell", ".zshenv": "Shell",
}
DOC_EXTENSIONS = {".md", ".mdx", ".rst", ".txt", ".adoc"}
CONFIG_EXTENSIONS = {".json", ".jsonc", ".json5", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".xml"}
# Extensionless repository metadata; recognised so it is not reported as an
# unmeasured source language.
DOC_FILENAMES = {"license", "licence", "copying", "notice", "authors", "contributors",
                 "codeowners", "readme", "changelog", "version"}
# Data and build products are not unmeasured *source*; classifying them keeps
# `composition.totals_by_category` honest about what the tree actually holds.
DATA_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".webp", ".pdf", ".eps",
    ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".tar", ".jar", ".whl",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp3", ".mp4", ".wav", ".avi", ".mov", ".webm",
    ".csv", ".tsv", ".parquet", ".npy", ".npz", ".h5", ".hdf5", ".fits", ".nc",
    ".dat", ".bin", ".so", ".dylib", ".dll", ".o", ".a", ".exe", ".pyc", ".class",
    ".db", ".sqlite", ".sqlite3", ".pkl", ".pickle", ".lock",
}
# Only the dotfiles the `rc`/`ignore`/`config`/`cfg` suffix rule below misses.
# Named rather than "anything starting with a dot": a dotfile holding shell
# code is unmeasured source, and calling it configuration hides that.
CONFIG_FILENAMES = {
    ".gitattributes", ".gitmodules", ".gitkeep", ".env",
    ".flake8", ".clang-format", ".clang-tidy", ".ds_store",
}
HASH_COMMENT_LANGUAGES = {"Python", "Shell", "Ruby", "Perl", "R", "Lua", "Elixir", "PowerShell",
                          "CMake", "Meson", "Automake", "Autoconf"}
# GNU make treats '#' as a comment only outside a recipe; a tab-indented '#' is
# a shell line.  Counted separately so an indented '#' stays code.
COLUMN_ONE_HASH_LANGUAGES = {"Make"}
# Free-form Fortran only.  Fixed-form 'C'/'*' in column 1 is deliberately not
# claimed here; see references/methodology.md for the resulting undercount.
BANG_COMMENT_LANGUAGES = {"Fortran"}
SLASH_COMMENT_LANGUAGES = {"JavaScript", "TypeScript", "Java", "Kotlin", "C", "C++", "C/C++", "C#", "Go", "Rust", "Swift", "Scala", "Dart", "PHP", "CUDA"}
C_FAMILY = {"C", "C++", "C/C++", "CUDA"}
METRIC_FAMILIES = ("composition", "duplication", "cyclomatic", "dependencies")
# Composition and duplication need only the file's text; the other two need a
# parse, so a parse failure invalidates strictly less than an unreadable file.
LEXICAL_FAMILIES = frozenset({"composition", "duplication"})
AST_FAMILIES = tuple(family for family in METRIC_FAMILIES if family not in LEXICAL_FAMILIES)


class HealthError(RuntimeError):
    """A user/actionable error, mapped to exit status 2."""


def git_bytes(root: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    """Run git at *root*, converting useful failures into a stable exception."""
    proc = subprocess.run(["git", *args], cwd=root, input=input_bytes, capture_output=True)
    if proc.returncode:
        detail = proc.stderr.decode(errors="replace").strip() or proc.stdout.decode(errors="replace").strip()
        raise HealthError(f"git {' '.join(args)} failed: {detail}")
    return proc.stdout


def git(root: Path, *args: str, input_bytes: bytes | None = None) -> str:
    return git_bytes(root, *args, input_bytes=input_bytes).decode(errors="replace")


def repository_root() -> Path:
    root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
    if not root.is_dir():
        raise HealthError("git did not return a repository root")
    return root


def git_scope_names(root: Path) -> list[str]:
    """Return deduplicated tracked and non-ignored untracked path names."""
    return sorted({name for name in git(root, "ls-files", "-co", "--exclude-standard", "-z").split("\0") if name})


def tracked_and_untracked(root: Path, names: Iterable[str] | None = None) -> list[str]:
    """Return existing tracked and non-ignored untracked files, sorted.

    ``ls-files -co --exclude-standard`` rather than a walk: a walk would
    inspect ignored build output, and tracked-only scope would miss a
    newly-added source file.
    """
    return [name for name in (names or git_scope_names(root))
            if (root / name).is_file() and not (root / name).is_symlink()]


def symlink_paths(root: Path, names: Iterable[str] | None = None) -> list[str]:
    """Return in-scope symbolic links, which are not followed by analysis."""
    return [name for name in (names or git_scope_names(root)) if (root / name).is_symlink()]


def unmerged_paths(root: Path) -> list[str]:
    """Return paths with unresolved index stages."""
    rows = git(root, "ls-files", "--unmerged", "-z").split("\0")
    return sorted({row.split("\t", 1)[1] for row in rows if "\t" in row})


def archive_revision(root: Path, ref: str) -> tempfile.TemporaryDirectory[str]:
    """Materialise base blobs without ``git archive`` attribute exclusions."""
    revision = git(root, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    tmp = tempfile.TemporaryDirectory(prefix="code-health-base-")
    try:
        entries: list[tuple[str, Path]] = []
        for entry in git_bytes(root, "ls-tree", "-r", "-z", revision).split(b"\0"):
            if not entry:
                continue
            metadata, raw_path = entry.split(b"\t", 1)
            mode, kind, object_id = metadata.decode("ascii").split()
            path = raw_path.decode("utf-8", errors="surrogateescape")
            if kind != "blob" or mode == "120000" or not language_for(path):
                continue
            target = Path(tmp.name, path)
            if target.parent == target or not target.is_relative_to(tmp.name):
                raise HealthError("unsafe path in git tree")
            entries.append((object_id, target))
        # One exchange rather than an interleaved write/read protocol.  `scan`
        # holds the same text afterwards, so only the peak roughly doubles, and
        # that buys the removal of a hand-rolled pipe dance.
        blobs = git_bytes(root, "cat-file", "--batch",
                          input_bytes="".join(f"{oid}\n" for oid, _ in entries).encode())
        offset = 0
        for _, target in entries:
            newline = blobs.find(b"\n", offset)
            header = blobs[offset:newline].decode("ascii", errors="replace").split() if newline != -1 else []
            if len(header) != 3 or header[1] != "blob" or not header[2].isdigit():
                raise HealthError("git cat-file returned an invalid blob header")
            start, size = newline + 1, int(header[2])
            offset = start + size + 1
            if offset > len(blobs):
                raise HealthError("git cat-file returned a truncated blob")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blobs[start:start + size])
    except Exception:
        tmp.cleanup()
        raise
    return tmp


def language_for(path: str) -> str | None:
    name = Path(path).name
    return EXTENSIONS.get(Path(path).suffix) or FILENAMES.get(name.lower())


def category_for(path: str, language: str | None) -> tuple[str, str]:
    """Classify files using explicit, intentionally conservative path rules."""
    lower = path.lower().replace("\\", "/")
    parts = lower.split("/")
    name = parts[-1]
    if language:
        test = (
            any(part in {"test", "tests", "__tests__", "spec", "specs"} for part in parts[:-1])
            or name.startswith(("test_", "test-"))
            or name.endswith(("_test" + Path(path).suffix.lower(), "-test" + Path(path).suffix.lower()))
            or ".test." in name or ".spec." in name
        )
        return ("test" if test else "production", "explicit_path_or_filename_rule")
    suffix = Path(path).suffix.lower()
    if suffix in CONFIG_EXTENSIONS or (name.startswith(".") and (
            name in CONFIG_FILENAMES or name.endswith(("rc", "ignore", "config", "cfg")))):
        return "configuration", "extension_or_dotfile_rule"
    # Metadata names only when extensionless or already a doc extension, so a
    # `version.awk` stays an unrecognised source file rather than a document.
    if suffix in DOC_EXTENSIONS or lower.startswith("docs/") or (not suffix and name in DOC_FILENAMES):
        return "documentation", "extension_or_docs_directory_rule"
    if suffix in DATA_EXTENSIONS:
        return "data", "data_or_build_product_rule"
    return "other", "unrecognised_extension_rule"


def unrecognised_extensions(paths: Iterable[str]) -> dict[str, int]:
    """Unrecognised suffixes (or bare names) and how many files carry each.

    A language absent from the extension table produces no coverage row, so
    this is the only evidence those files exist.  Grouped rather than listed:
    on real repositories the list was hundreds of data tables.
    """
    counts = Counter(Path(path).suffix.lower() or Path(path).name.lower() for path in paths
                     if not language_for(path) and category_for(path, None)[0] == "other")
    return {key: counts[key] for key in sorted(counts)}


def line_counts(text: str, language: str | None) -> dict[str, int | str]:
    """A lexical (not parser-accurate) physical/code/comment/blank breakdown."""
    lines = text.splitlines()
    blank = 0
    comment = 0
    in_block = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            blank += 1
            continue
        if language in HASH_COMMENT_LANGUAGES and stripped.startswith("#"):
            comment += 1
        elif language in COLUMN_ONE_HASH_LANGUAGES and line.startswith("#"):
            comment += 1
        elif language in BANG_COMMENT_LANGUAGES and stripped.startswith("!"):
            comment += 1
        elif language in SLASH_COMMENT_LANGUAGES:
            if in_block or stripped.startswith("/*"):
                comment += 1
                in_block = "*/" not in stripped
            elif stripped.startswith("//"):
                comment += 1
    return {
        "physical": len(lines), "code": len(lines) - blank - comment,
        "comment": comment, "blank": blank, "method": "lexical_heuristic",
    }


@dataclass(frozen=True)
class SourceFile:
    path: str
    language: str
    category: str
    category_rule: str
    text: str
    counts: dict[str, int | str]


def scan(root: Path, paths: Iterable[str] | None = None) -> tuple[list[SourceFile], list[dict[str, str]]]:
    """Read supported source files; unreadable/decode failures are recorded."""
    source: list[SourceFile] = []
    unreadable: list[dict[str, str]] = []
    chosen = sorted(paths if paths is not None else _files_under(root))
    for path in chosen:
        language = language_for(path)
        if not language:
            continue
        try:
            text = (root / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            unreadable.append({"path": path, "reason": f"unreadable_utf8: {exc.__class__.__name__}"})
            continue
        category, rule = category_for(path, language)
        source.append(SourceFile(path, language, category, rule, text, line_counts(text, language)))
    return source, unreadable


def _files_under(root: Path) -> list[str]:
    # Archive snapshots do not contain ignored files, and a recursive walk here
    # keeps the base independent of the caller's index/worktree state.
    return sorted(str(path.relative_to(root)).replace(os.sep, "/") for path in root.rglob("*") if path.is_file())


def composition(root: Path, paths: Iterable[str], source: list[SourceFile]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Source plus separately-classified documentation, configuration and data.

    Source rows reuse the single ``scan`` read, so nothing is opened twice.
    """
    rows = {file.path: {"path": file.path, "language": file.language, "category": file.category,
                        "category_rule": file.category_rule, **file.counts} for file in source}
    unreadable: list[dict[str, str]] = []
    for path in paths:
        if language_for(path):
            continue  # `scan` already read it, or already recorded why it could not.
        try:
            text = (root / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            unreadable.append({"path": path, "reason": f"unreadable_utf8: {exc.__class__.__name__}"})
            continue
        category, rule = category_for(path, None)
        rows[path] = {"path": path, "language": None, "category": category, "category_rule": rule,
                      **line_counts(text, None)}
    all_rows = [rows[path] for path in sorted(rows)]
    totals: dict[str, dict[str, int]] = defaultdict(lambda: {"files": 0, "physical": 0, "code": 0, "comment": 0, "blank": 0})
    distributions: dict[str, list[int]] = defaultdict(list)
    for row in all_rows:
        total = totals[row["category"]]
        total["files"] += 1
        for key in ("physical", "code", "comment", "blank"):
            total[key] += int(row[key])
        distributions[row["category"]].append(int(row["code"]))
    return {
        "line_count_method": "lexical_heuristic",
        "files": all_rows,
        "totals_by_category": {key: totals[key] for key in sorted(totals)},
        "code_line_size_distribution_by_category": {
            key: size_distribution(values) for key, values in sorted(distributions.items())
        },
    }, unreadable


def size_distribution(values: list[int]) -> dict[str, int]:
    values = sorted(values)
    if not values:
        return {"count": 0, "min": 0, "p50": 0, "p90": 0, "max": 0}
    def pct(percent: int) -> int:
        return values[(len(values) - 1) * percent // 100]
    return {"count": len(values), "min": values[0], "p50": pct(50), "p90": pct(90), "max": values[-1]}


def code_windows(source: list[SourceFile], width: int) -> dict[str, list[tuple[int, str]]]:
    """Per file, the signature of every window of *width* nonblank lines.

    Blank lines are separators, not part of an exact window; physical start
    lines are kept for investigation.
    """
    windows: dict[str, list[tuple[int, str]]] = {}
    for file in source:
        lines = [(number, "".join(line.split()))
                 for number, line in enumerate(file.text.splitlines(), 1) if line.strip()]
        windows[file.path] = [
            (lines[start][0],
             hashlib.sha256("\n".join(text for _, text in lines[start:start + width]).encode()).hexdigest())
            for start in range(len(lines) - width + 1)
        ]
    return windows


def duplicate_blocks(windows: dict[str, list[tuple[int, str]]],
                     width: int) -> tuple[dict[str, int], dict[str, dict[str, Any]]]:
    """Occurrence counts per signature, plus one record per duplicated run.

    A repeated 30-line block is one finding, not the 25 overlapping windows a
    sliding scan sees.  A window continues a run only when its whole occurrence
    set follows one shared predecessor of the same count, so a more-repeated
    tail keeps its own record.  The fixed-width signature stays the comparison
    identity, and counts cover every signature because the other side of a
    comparison needs them even where it holds no duplicate.
    """
    positions: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for path, rows in windows.items():
        for index, (_, signature) in enumerate(rows):
            positions[signature].append((path, index))
    counts = {signature: len(group) for signature, group in positions.items()}
    repeated = {signature for signature, count in counts.items() if count > 1}

    def predecessor(signature: str) -> str | None:
        group = positions[signature]
        if any(index == 0 for _, index in group):
            return None
        earlier = {windows[path][index - 1][1] for path, index in group}
        found = earlier.pop() if len(earlier) == 1 else None
        return found if found != signature and counts.get(found) == len(group) else None

    continuation: dict[str, str] = {}
    for signature in repeated:
        found = predecessor(signature)
        if found is not None:
            continuation[found] = signature
    blocks: dict[str, dict[str, Any]] = {}
    for start in repeated - set(continuation.values()):
        run, tail = [start], start
        while tail in continuation:
            tail = continuation[tail]
            run.append(tail)
        # Every member of the run is recorded, not just its start: the same
        # signature can start a run on one side of a comparison and continue a
        # longer one on the other, and dropping the continuation's occurrences
        # would leave that side's delta with nowhere to point.  Each member's
        # length is measured from its OWN occurrences, so the row's line count
        # and its cited lines describe the same block.
        for offset, signature in enumerate(run):
            blocks[signature] = {
                "signature": signature, "line_count": width + len(run) - 1 - offset,
                "count": counts[signature], "starts_run": offset == 0,
                "occurrences": sorted(({"path": path, "start_line": windows[path][index][0]}
                                       for path, index in positions[signature]),
                                      key=lambda row: (row["path"], row["start_line"])),
            }
    return counts, blocks


def duplication_facts(source: list[SourceFile], width: int, base_source: list[SourceFile] | None,
                      changed: Iterable[str]) -> dict[str, Any]:
    """One row per duplicated block on either side, with both raw counts.

    A block the change resolved keeps its row so the delta stays honest; it has
    no current block, so its occurrence lists are empty and the counts say why.
    """
    current_counts, current = duplicate_blocks(code_windows(source, width), width)
    base_counts, base = duplicate_blocks(code_windows(base_source or [], width), width)
    changed = set(changed)
    rows = []
    for signature in sorted(set(current) | set(base)):
        block, was = current.get(signature), base.get(signature)
        # One row per run, anchored on whichever side actually holds the run.
        if not (block or {}).get("starts_run") and not (was or {}).get("starts_run"):
            continue
        occurrences = block["occurrences"] if block else []
        head, old = current_counts.get(signature, 0), base_counts.get(signature, 0)
        rows.append({
            "signature": signature,
            # Each side's own run length. Borrowing the other's would claim a
            # block that side does not have; carrying both is what makes a
            # clone that grew without repeating more times comparable at all.
            "line_count": (block or was)["line_count"],
            "base_line_count": was["line_count"] if was else 0,
            "current_count": head, "base_count": old, "count_delta": head - old,
            "changed_side_occurrences": [o for o in occurrences if o["path"] in changed],
            "current_occurrences": occurrences,
        })
    return {"normalization": "whitespace_only", "minimum_window_lines": width, "signatures": rows}


def cyclomatic_for(node: ast.AST) -> int:
    value = 1
    decision_nodes = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.IfExp, ast.ExceptHandler, ast.comprehension)
    # A nested definition is independently reported.  Counting its decisions
    # in the enclosing function would inflate the outer raw value.
    todo = list(ast.iter_child_nodes(node))
    while todo:
        child = todo.pop()
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(child, decision_nodes):
            value += 1
        elif isinstance(child, ast.BoolOp):
            value += max(0, len(child.values) - 1)
        elif isinstance(child, ast.Match):
            value += max(0, len(child.cases) - 1)
        todo.extend(ast.iter_child_nodes(child))
    return value


def module_name(path: str) -> str:
    result = str(Path(path).with_suffix("")).replace("/", ".")
    return result[:-9] if result.endswith(".__init__") else result


def python_structure(source: list[SourceFile]) -> dict[str, Any]:
    """Parse Python function complexity and direct internal import edges.

    Import edges are deliberately raw syntactic evidence: conditional imports,
    importlib calls and packaging import roots are not inferred.
    """
    python = [f for f in source if f.language == "Python"]
    trees: dict[str, ast.AST] = {}
    errors: list[dict[str, Any]] = []
    functions: list[dict[str, Any]] = []
    for file in python:
        try:
            tree = ast.parse(file.text, filename=file.path)
        except SyntaxError as exc:
            errors.append({"path": file.path, "line": exc.lineno or 0, "reason": exc.msg})
            continue
        trees[file.path] = tree
        def collect(node: ast.AST, parents: list[str], source_path: str) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    qualified = ".".join([*parents, child.name])
                    functions.append({"path": source_path, "name": qualified, "line": child.lineno,
                                      "cyclomatic": cyclomatic_for(child)})
                    collect(child, [*parents, child.name], source_path)
                elif isinstance(child, ast.ClassDef):
                    collect(child, [*parents, child.name], source_path)
                else:
                    collect(child, parents, source_path)
        collect(tree, [], file.path)
    functions.sort(key=lambda row: (-row["cyclomatic"], row["path"], row["line"], row["name"]))
    modules: dict[str, str] = {}
    for path in trees:
        name = module_name(path)
        if name not in modules or (modules[name].endswith(".pyi") and path.endswith(".py")):
            modules[name] = path
    edges: set[tuple[str, str]] = set()
    for path, tree in trees.items():
        current = module_name(path)
        package = current.rsplit(".", 1)[0] if "." in current else ""
        for node in ast.walk(tree):
            candidates: list[str] = []
            if isinstance(node, ast.Import):
                candidates = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                stem = node.module or ""
                if node.level:
                    base = package.split(".") if package else []
                    up = max(0, len(base) - node.level + 1)
                    stem = ".".join(base[:up] + ([stem] if stem else []))
                candidates = [stem]
            for candidate in candidates:
                # Longest module prefix wins (``pkg.a.b`` before ``pkg.a``).
                matches = [name for name in modules if candidate == name or candidate.startswith(name + ".")]
                # Repositories often put import roots below a tooling/skills
                # directory.  Use a suffix only when it is unique; ambiguity
                # is deliberately left as no edge rather than guessed.
                if not matches:
                    suffix_matches = [name for name in modules if name.endswith("." + candidate)]
                    matches = suffix_matches if len(suffix_matches) == 1 else []
                if matches:
                    target = modules[max(matches, key=len)]
                    if target != path:
                        edges.add((path, target))
    return {
        "parse_errors": errors,
        "functions": functions,
        "function_cyclomatic_distribution": size_distribution([r["cyclomatic"] for r in functions]),
        "imports": graph_facts(sorted(trees), edges),
    }


def reverse_reachability(components: list[list[str]], edges: set[tuple[str, str]]) -> dict[str, int]:
    """How many other files transitively reach each node.

    The SCC condensation is a DAG and Tarjan emits it in reverse topological
    order, so one backwards pass answers every node.  The accumulator is a
    bitmask over files, not a set of paths: a set costs O(V^2) *objects* on a
    wide graph (measured 624 MB at 4,000 nodes).
    """
    component_of: dict[str, int] = {}
    own: list[int] = []
    for index, members in enumerate(components):
        mask = 0
        for path in members:
            mask |= 1 << len(component_of)
            component_of[path] = index
        own.append(mask)
    parents: dict[int, set[int]] = defaultdict(set)
    for src, dst in edges:
        if component_of[src] != component_of[dst]:
            parents[component_of[dst]].add(component_of[src])
    reaching = [0] * len(components)
    for index in reversed(range(len(components))):
        for parent in parents[index]:
            reaching[index] |= own[parent] | reaching[parent]
    return {path: (reaching[index] | own[index]).bit_count() - 1 for path, index in component_of.items()}


def graph_facts(nodes: Iterable[str], edges: set[tuple[str, str]]) -> dict[str, Any]:
    """Edges, cycles, and per-node fan-in/fan-out/blast radius.

    Shared by every dependency collector, so Python imports and C includes are
    described in identical units.
    """
    inbound: Counter[str] = Counter(dst for _, dst in edges)
    outbound: Counter[str] = Counter(src for src, _ in edges)
    # One pass; per-node edge scans are quadratic on dense C graphs.
    adjacency: dict[str, list[str]] = defaultdict(list)
    for src, dst in edges:
        adjacency[src].append(dst)
    graph = {path: sorted(adjacency.get(path, ())) for path in sorted(nodes)}
    components = strongly_connected_components(graph)
    reaching = reverse_reachability(components, edges)
    return {"edges": [{"from": src, "to": dst} for src, dst in sorted(edges)],
            "nodes": [{"path": path, "fan_in": inbound[path], "fan_out": outbound[path],
                       "reverse_reachability": reaching[path]} for path in graph],
            "cycles": sorted(component for component in components if len(component) > 1)}


# Quoted includes only.  ``#include <...>`` names a system or external header
# by contract, so excluding it keeps the graph internal without a header search
# path.  Continuation lines and macro-computed includes are not handled.
# A leading BOM is tolerated so an MSVC-authored first line is not skipped.
INCLUDE_PATTERN = re.compile(r'^﻿?[ \t]*#[ \t]*include[ \t]*"([^"\n]+)"', re.MULTILINE)
# C++ d-char: anything but whitespace, parentheses and backslash.  A quote is
# legal, so `R""(...)""` is a raw string.
RAW_STRING_OPEN = re.compile(r'R"([^()\s\\]{0,16})\(')
RAW_STRING_PREFIXES = ("", "L", "u", "U", "u8")
IDENTIFIER_CHAR = re.compile(r"[A-Za-z0-9_]")
NEXT_INTERESTING = re.compile(r"""[/"']|R"|\n""")


def blank_span(text: str) -> str:
    return "".join(" " if char != "\n" else "\n" for char in text)


def mask_non_code(text: str, language: str | None = None) -> str:
    """Blank comments and raw strings, preserving offsets and line structure.

    Ordinary string and character literals are consumed but kept verbatim: an
    include's own quoted target is a string, so blanking strings would erase
    the very thing being measured, while skipping them stops a literal such as
    ``"/*"`` or ``"dir//b.h"`` from being mistaken for a comment.
    """
    # `.h` is C/C++ ambiguous, so raw strings stay enabled there; only a
    # definitely-C translation unit disables them.
    raw_strings_possible = language in {"C++", "C/C++", "CUDA", None}
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        match = NEXT_INTERESTING.search(text, i)
        if not match:
            out.append(text[i:])
            break
        if match.start() > i:
            out.append(text[i:match.start()])
            i = match.start()
        char = text[i]
        pair = text[i:i + 2]
        if pair == "/*":
            end = text.find("*/", i + 2)
            end = n if end == -1 else end + 2
            out.append(blank_span(text[i:end]))
            i = end
        elif pair == "//":
            end = text.find("\n", i)
            while end != -1 and text[end - 1:end] == "\\":
                end = text.find("\n", end + 1)
            end = n if end == -1 else end
            # blank_span, not spaces: a spliced comment spans newlines, and
            # eating them would shift every line number below it.
            out.append(blank_span(text[i:end]))
            i = end
        elif char == "R" and raw_strings_possible and self_delimited_raw(text, i):
            raw = RAW_STRING_OPEN.match(text, i)
            terminator = ")" + raw.group(1) + '"'
            end = text.find(terminator, raw.end())
            if end == -1:
                # Never blank to end of file on an unterminated opener: the
                # loss would be silent and unbounded.
                out.append(char)
                i += 1
            else:
                out.append(blank_span(text[i:end + len(terminator)]))
                i = end + len(terminator)
        elif char in "\"'":
            j = i + 1
            while j < n and text[j] != char and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            if j < n and text[j] == char:
                out.append(text[i:j + 1])
                i = j + 1
            else:
                # Unterminated: emit the quote alone so the rest of the line is
                # still lexed and a following comment is still masked.
                out.append(char)
                i += 1
        else:
            out.append(char)
            i += 1
    return "".join(out)


def self_delimited_raw(text: str, index: int) -> bool:
    """True when ``R"`` at `index` opens a raw string rather than ending a token.

    ``TAG_ERROR"("`` is string concatenation after an identifier, not a raw
    string; only a standard encoding prefix may precede the ``R``.
    """
    if not RAW_STRING_OPEN.match(text, index):
        return False
    start = index
    while start > 0 and IDENTIFIER_CHAR.match(text[start - 1]):
        start -= 1
    return text[start:index] in RAW_STRING_PREFIXES


def c_include_structure(source: list[SourceFile], inventory: Iterable[str] | None = None) -> dict[str, Any]:
    """Direct internal ``#include "..."`` edges between C-family files.

    Raw syntactic evidence, deliberately not a preprocessor: an include guarded
    by ``#ifdef`` still counts, and a header reached only through a macro is
    invisible.  Resolution tries the including file's own directory first, then
    a repository-relative path, then a unique basename; ambiguity yields no edge
    rather than a guess, matching the Python collector's rule.
    """
    files = [item for item in source if item.language in C_FAMILY]
    paths = {item.path for item in files}
    # Every in-scope path, so a present-but-unmeasured header (.inc, generated
    # body) is not reported as absent from the repository.
    known = (set(inventory) if inventory is not None else {item.path for item in source}) | paths
    by_basename: dict[str, list[str]] = defaultdict(list)
    for path in sorted(known):
        by_basename[posix_name(path)].append(path)
    edges: set[tuple[str, str]] = set()
    provenance: dict[tuple[str, str], str] = {}
    unresolved: dict[tuple[str, str], dict[str, Any]] = {}
    def record(path: str, target: str, line: int, reason: str) -> None:
        unresolved.setdefault((path, target), {"path": path, "include": target, "line": line, "reason": reason})
    for item in sorted(files, key=lambda row: row.path):
        directory = PurePosixPath(item.path).parent
        masked = mask_non_code(item.text, item.language)
        for match in INCLUDE_PATTERN.finditer(masked):
            target = match.group(1)
            line = masked.count("\n", 0, match.start()) + 1
            # Canonicalise separators before normpath: POSIX normpath does not
            # treat '\' as a separator, so the order decides the result.
            canonical = posixpath.normpath(target.replace("\\", "/"))
            if posixpath.isabs(canonical) or re.match(r"^[A-Za-z]:", canonical):
                record(item.path, target, line, "outside_repository")
                continue
            # Resolve against the including directory before judging escape:
            # `../include/b.h` from `src/` is ordinary and stays inside.
            local = posixpath.normpath(str(directory / canonical))
            if local in {".", ".."} or local.startswith("../") or canonical in {".", ".."}:
                record(item.path, target, line, "outside_repository")
                continue
            if local in paths:
                resolved, via = local, "exact_relative"
            elif canonical in paths:
                resolved, via = canonical, "repository_relative"
            elif local in known or canonical in known:
                # The exact target exists but is not a measured C-family file
                # (unreadable, symlink, or another language).  Naming it beats
                # guessing a same-named file elsewhere.
                record(item.path, target, line, "in_repository_unmeasured_language")
                continue
            else:
                # Uniqueness is decided across the whole repository, not just
                # measured files: an unmeasured same-named file makes the
                # basename ambiguous, so guessing past it would be wrong.
                matches = by_basename.get(posix_name(canonical), [])
                if len(matches) != 1:
                    record(item.path, target, line,
                           "ambiguous_basename" if matches else "not_in_repository")
                    continue
                if matches[0] not in paths:
                    record(item.path, target, line, "unique_unmeasured_basename")
                    continue
                resolved, via = matches[0], "unique_basename"
            if resolved != item.path:
                edges.add((item.path, resolved))
                provenance[(item.path, resolved)] = via
            else:
                record(item.path, target, line, "self_include")
    facts = graph_facts(sorted(paths), edges)
    for edge in facts["edges"]:
        edge["via"] = provenance[(edge["from"], edge["to"])]
    return {"unresolved_includes": [unresolved[key] for key in sorted(unresolved)],
            # A `unique_basename` edge can wire an unrelated copy of a file into
            # the graph.  Counted rather than listed: a repository built with
            # `-I` paths resolves almost every edge that way, and the list then
            # buried the one number a reader needs.
            "resolution_counts": dict(sorted(Counter(provenance.values()).items())),
            **facts}


def posix_name(path: str) -> str:
    return PurePosixPath(path.replace("\\", "/")).name


@dataclass(frozen=True)
class LizardResult:
    """Result of optional Lizard collection; errors are coverage evidence."""
    installed: bool
    version: str | None
    functions: list[dict[str, Any]]
    error: str | None = None


def lizard_status() -> LizardResult:
    binary = shutil.which("lizard")
    if not binary:
        return LizardResult(False, None, [], "lizard not installed")
    proc = subprocess.run([binary, "--version"], capture_output=True, text=True)
    if proc.returncode:
        return LizardResult(True, None, [], "lizard --version failed")
    version = (proc.stdout or proc.stderr).strip().splitlines()
    return LizardResult(True, version[0] if version else "unknown", [])


LIZARD_CSV_COLUMNS = ("nloc", "ccn", "token", "param", "length", "location",
                      "file", "function", "long_name", "start", "end")


def parse_lizard_csv(output: str) -> list[dict[str, Any]]:
    """Parse Lizard's headerless 11-column ``--csv`` rows.

    Pinned to the one shape Lizard emits.  Anything else raises, which becomes
    a recorded collector error and unavailable coverage — never a silent zero.
    """
    parsed: list[dict[str, Any]] = []
    for values in csv.reader(output.splitlines()):
        try:
            row = dict(zip(LIZARD_CSV_COLUMNS, values, strict=True))
            function = {"path": row["file"].replace("\\", "/"), "name": row["function"],
                        "line": int(row["start"]), "cyclomatic": int(row["ccn"]),
                        "collector": "lizard"}
            if not function["path"] or not function["name"] or function["line"] < 1:
                raise ValueError("empty filename or function, or non-positive start line")
        except ValueError as exc:
            raise HealthError(f"unparsable lizard CSV row: {values}") from exc
        parsed.append(function)
    return sorted(parsed, key=lambda r: (-r["cyclomatic"], r["path"], r["line"], r["name"]))


def lizard_complexity(root: Path, source: list[SourceFile], status: LizardResult | None = None) -> LizardResult:
    status = status or lizard_status()
    eligible = [item.path for item in source if item.language in LIZARD_SUPPORTED]
    if not eligible or not status.installed:
        return status
    binary = shutil.which("lizard")
    assert binary  # status's installed state was derived from this probe.
    functions: list[dict[str, Any]] = []
    for start in range(0, len(eligible), 200):
        # `--` so a path that looks like an option cannot turn into one.
        proc = subprocess.run([binary, "--csv", "--", *eligible[start:start + 200]], cwd=root, capture_output=True, text=True)
        if proc.returncode:
            return LizardResult(True, status.version, [], f"lizard failed: {(proc.stderr or proc.stdout).strip()}")
        try:
            functions.extend(parse_lizard_csv(proc.stdout))
        except HealthError as exc:
            return LizardResult(True, status.version, [], str(exc))
    functions.sort(key=lambda row: (-row["cyclomatic"], row["path"], row["line"], row["name"]))
    return LizardResult(True, status.version, functions)


def strongly_connected_components(graph: dict[str, list[str]]) -> list[list[str]]:
    """Tarjan's SCC algorithm, in reverse topological order of the condensation.

    Iterative: a deep C header chain would overflow a recursive walk, and that
    surfaces as an execution error rather than as evidence.  Members are sorted
    and the walk is driven from sorted keys, so the order is stable.
    """
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    result: list[list[str]] = []

    def enter(node: str) -> tuple[str, Any]:
        index[node] = low[node] = len(index)
        stack.append(node)
        on_stack.add(node)
        return node, iter(graph[node])

    for start in sorted(graph):
        if start in index:
            continue
        work = [enter(start)]
        while work:
            node, children = work[-1]
            target = next(children, None)
            if target is None:
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[node])
                if low[node] == index[node]:
                    component: list[str] = []
                    while not component or component[-1] != node:
                        component.append(stack.pop())
                        on_stack.remove(component[-1])
                    result.append(sorted(component))
            elif target not in index:
                work.append(enter(target))
            elif target in on_stack:
                low[node] = min(low[node], index[target])
    return result


@dataclass(frozen=True)
class Baseline:
    """What the base revision could not supply, and what that invalidates.

    Derived once so the coverage matrix, the disclosed limits and candidate
    suppression cannot drift.  ``scope`` is the current tree's languages, which
    stops a base-only failure inventing rows for a language now absent.
    """
    unreadable: tuple[dict[str, str], ...] = ()
    unparsable: tuple[dict[str, Any], ...] = ()
    scope: frozenset[str] = frozenset()
    changed_base_paths: frozenset[str] = frozenset()

    @property
    def unreadable_paths(self) -> set[str]:
        """Base paths with no text at all, so no lexical measure is comparable."""
        return {row["path"] for row in self.unreadable}

    @property
    def paths(self) -> set[str]:
        """Base paths with no comparable AST; a parse failure still has text."""
        return self.unreadable_paths | {row["path"] for row in self.unparsable}

    @property
    def voided(self) -> dict[tuple[str, str], str]:
        """(language, metric family) -> reason the baseline cannot support it.

        Unreadable voids every family for that language; a parse failure voids
        only the AST families, since the text itself read fine.
        """
        result: dict[tuple[str, str], str] = {}
        for rows, families, reason in (
            (self.unparsable, AST_FAMILIES, "base source in this language failed to parse"),
            (self.unreadable, METRIC_FAMILIES, "unreadable base source in this language"),
        ):
            for row in rows:
                language = language_for(row["path"])
                if language in self.scope:
                    result.update({(language, family): reason for family in families})
        # One include graph spans the C family, so an unreadable base member
        # voids the dependency row for every C-family language in scope — even
        # when the failing file's own extension is no longer present.
        if {language_for(row["path"]) for row in self.unreadable} & C_FAMILY:
            result.update({(language, "dependencies"): "incomplete base C-family include graph"
                           for language in self.scope & C_FAMILY})
        # Duplication signatures span every language at once, so missing base
        # text understates a base count anywhere and could fabricate a rise in
        # a language the failing file has nothing to do with.
        if self.unreliable_duplication:
            result.update({(language, "duplication"): "unreadable base source; duplication spans all languages"
                           for language in self.scope})
        return result

    @property
    def unreliable_graphs(self) -> set[str]:
        """Dependency graphs whose baseline is too incomplete to compare."""
        languages = {language_for(path) for path in self.paths}
        return ({"python"} if "Python" in languages else set()) | (
            {"c_includes"} if languages & C_FAMILY else set())

    @property
    def unreliable_duplication(self) -> bool:
        """True when the base pool is missing text the current pool still has.

        A parse failure does not count: that file's text was read and still
        contributed its windows.  Nor does a file that never changed — it is
        unreadable on both sides, so neither count can be understated alone.
        Both sides of this comparison are base-path identities, so a renamed
        file is matched under the name the base knew it by.
        """
        return bool(self.unreadable_paths & self.changed_base_paths)


@dataclass(frozen=True)
class Differential:
    """Base-side context for differential attribution; inert in absolute mode."""
    active: bool = False
    changed: frozenset[str] = frozenset()
    renames: dict[str, str] = field(default_factory=dict)
    source: tuple[SourceFile, ...] = ()
    structure: dict[str, Any] = field(default_factory=dict)
    includes: dict[str, Any] = field(default_factory=dict)
    lizard: LizardResult = field(default_factory=lambda: LizardResult(False, None, []))
    baseline: Baseline = Baseline()

    def unchanged(self, path: str, unusable: set[str]) -> bool:
        """True when a differential run must skip *path* for this metric family.

        *unusable* is the base paths that family cannot compare against, which
        is narrower for a lexical measure than for one needing a parse.
        """
        return self.active and (path not in self.changed
                                or self.renames.get(path, path) in unusable)

    def base_path(self, path: str) -> str:
        return self.renames.get(path, path)


def coverage_cell(language: str, family: str, unreadable_languages: set[str | None],
                  python_parse_gap: bool, c_family_gap: bool, lizard: LizardResult) -> tuple[str, str]:
    """Measurement status for one language x metric-family cell."""
    if family in LEXICAL_FAMILIES and language not in unreadable_languages:
        return "covered", "stdlib lexical analysis"
    if family in LEXICAL_FAMILIES:
        return "unavailable", "unreadable source in this language"
    if language == "Python":
        if python_parse_gap or language in unreadable_languages:
            return "unavailable", "Python AST parse/read gap"
        return "covered", "Python stdlib AST"
    if family == "dependencies" and language in C_FAMILY:
        # One graph spans the family, so any unreadable member voids it.
        if c_family_gap:
            return "unavailable", "unreadable source in the C-family include graph"
        return "covered", "stdlib quoted-#include scan (no preprocessor evaluation)"
    if family == "cyclomatic":
        if language in unreadable_languages:
            return "unavailable", "unreadable source in this language"
        if language in LIZARD_SUPPORTED:
            if lizard.installed and not lizard.error:
                return "covered", f"Lizard {lizard.version or 'unknown'}"
            return "unavailable", lizard.error or "lizard not installed"
    return "unavailable", "no portable parser for this language"


def scope_languages(source: list[SourceFile], unreadable: list[dict[str, str]]) -> frozenset[str]:
    """Current-tree languages, including ones whose files failed to read.

    A language with no coverage row would read as covered.
    """
    return frozenset({item.language for item in source}
                     | {language_for(row["path"]) for row in unreadable if language_for(row["path"])})


def coverage_matrix(source: list[SourceFile], unreadable: list[dict[str, str]], structure: dict[str, Any],
                    lizard: LizardResult, baseline: Baseline | None = None) -> list[dict[str, str]]:
    """Per-language, per-family status.  Never 'covered' by omission."""
    baseline = baseline or Baseline()
    unreadable_languages = {language_for(row["path"]) for row in unreadable}
    voided = baseline.voided
    python_parse_gap = bool(structure["parse_errors"])
    c_family_gap = bool(unreadable_languages & C_FAMILY)
    matrix = []
    for language in sorted(scope_languages(source, unreadable)):
        for family in METRIC_FAMILIES:
            status, reason = coverage_cell(language, family, unreadable_languages,
                                           python_parse_gap, c_family_gap, lizard)
            # A clean current measurement is still not a clean comparison when
            # the base could not supply the same language.
            if status == "covered" and (language, family) in voided:
                status, reason = "unavailable", voided[(language, family)]
            matrix.append({"language": language, "metric_family": family, "status": status, "reason": reason})
    return matrix


def coverage_limits(conflicts: list[str], symlinks: list[str], baseline: Baseline) -> list[dict[str, Any]]:
    """Disclosed reasons a measurement is incomplete, in reading order."""
    kinds = (
        ("unmerged_index", conflicts),
        ("symbolic_links_not_followed", symlinks),
        ("unreadable_base_files", sorted(row["path"] for row in baseline.unreadable)),
        ("base_parse_errors", sorted(row["path"] for row in baseline.unparsable)),
    )
    return [{"kind": kind, "paths": paths} for kind, paths in kinds if paths]


def changed_paths(root: Path, base: str, current_paths: Iterable[str] | None = None) -> tuple[set[str], dict[str, str]]:
    """Changed current paths plus current->base mapping for Git-detected renames."""
    tokens = git(root, "diff", "--name-status", "-M", "-z", base, "--").split("\0")
    changed: set[str] = set()
    renames: dict[str, str] = {}
    i = 0
    while i < len(tokens) and tokens[i]:
        code, i = tokens[i][:1], i + 1
        # R and C carry two paths; the second is the current one.
        span = 2 if code in {"R", "C"} else 1
        if i + span > len(tokens):
            break
        paths, i = tokens[i:i + span], i + span
        changed.add(paths[-1])
        if code == "R":
            renames[paths[-1]] = paths[0]
    # Non-ignored untracked paths are additions, absent from git diff.
    tracked = set(git(root, "ls-files", "-z").split("\0"))
    changed.update(path for path in (current_paths or tracked_and_untracked(root)) if path not in tracked)
    return changed, renames


def cyclomatic_candidates(structure: dict[str, Any], lizard_functions: list[dict[str, Any]],
                          diff: Differential) -> list[tuple[tuple[int, ...], dict[str, Any]]]:
    # Lines shift under unrelated edits.  Python qualified names (and Lizard's
    # long names) are more stable identities for a differential raw delta.
    old_python = {(row["path"], row["name"]): row["cyclomatic"] for row in diff.structure.get("functions", [])}
    old_lizard = {(row["path"], row["name"]): row["cyclomatic"] for row in diff.lizard.functions}
    rows = []
    for row in [*structure["functions"], *lizard_functions]:
        if diff.unchanged(row["path"], diff.baseline.paths):
            continue
        previous = old_lizard if row.get("collector") == "lizard" else old_python
        delta = row["cyclomatic"] - previous.get((diff.base_path(row["path"]), row["name"]), 0)
        if diff.active and delta == 0:
            continue
        rows.append(((row["cyclomatic"], delta), {
            "kind": "cyclomatic", "path": row["path"], "line": row["line"],
            "raw_value": row["cyclomatic"], "delta_from_base": delta if diff.active else None,
            "collector": row.get("collector", "python_ast"),
            "note": "Measured complexity; investigate context before recommending change."}))
    return rows


def file_size_candidates(source: list[SourceFile], diff: Differential) -> list[tuple[tuple[int, ...], dict[str, Any]]]:
    old_files = {item.path: int(item.counts.get("code", 0)) for item in diff.source}
    rows = []
    for item in source:
        if diff.unchanged(item.path, diff.baseline.unreadable_paths):
            continue
        value = int(item.counts.get("code", 0))
        delta = value - old_files.get(diff.base_path(item.path), 0)
        if diff.active and delta == 0:
            continue
        rows.append(((value, delta), {
            "kind": "file_size", "path": item.path, "line": 1, "raw_value": value,
            "delta_from_base": delta if diff.active else None,
            "note": "Raw code-line count; inspect the file's role before considering decomposition."}))
    return rows


def duplication_candidates(duplication: dict[str, Any], diff: Differential) -> list[tuple[tuple[int, ...], dict[str, Any]]]:
    # An incomplete base pool cannot establish that duplication rose, exactly as
    # an incomplete baseline cannot establish that a cycle is new.
    if diff.active and diff.baseline.unreliable_duplication:
        return []
    rows = []
    for row in duplication["signatures"]:
        occurrences = row["changed_side_occurrences"] if diff.active else row["current_occurrences"]
        grew = row["count_delta"] > 0 or row["line_count"] > row["base_line_count"]
        if occurrences and (grew or not diff.active):
            first = occurrences[0]
            # Extent first: a block repeated twice over 326 lines duplicates far
            # more code than a 6-line window repeated 33 times, and the second
            # kind crowded the first out entirely on both reference repositories.
            rows.append(((row["line_count"], row["current_count"], row["count_delta"]), {
                "kind": "duplication", "path": first["path"], "line": first["start_line"],
                "raw_value": row["current_count"], "delta_from_base": row["count_delta"] if diff.active else None,
                "line_count": row["line_count"], "base_line_count": row["base_line_count"],
                "note": "Exact whitespace-normalised repeated block; investigate whether shared structure is intentional."}))
    return rows


def cycle_candidates(structure: dict[str, Any], includes: dict[str, Any],
                     diff: Differential) -> list[tuple[tuple[int, ...], dict[str, Any]]]:
    inverse_renames = {old: new for new, old in diff.renames.items()}
    graphs = [
        ("python", structure["imports"], diff.structure.get("imports", {}),
         "Static Python import cycle; inspect runtime boundaries and intentionality."),
        ("c_includes", includes, diff.includes,
         "Static C-family include cycle from quoted includes; inspect header guards and intentionality."),
    ]
    rows = []
    for name, current_graph, base_graph, note in graphs:
        # An incomplete baseline cannot establish that a cycle is new.
        if diff.active and name in diff.baseline.unreliable_graphs:
            continue
        rows.extend(((row["raw_value"],), row)
                    for row in new_cycles(current_graph, base_graph, inverse_renames, note, diff.active))
    return rows


def candidates(source: list[SourceFile], structure: dict[str, Any], includes: dict[str, Any],
               lizard_functions: list[dict[str, Any]], duplication: dict[str, Any],
               diff: Differential) -> list[dict[str, Any]]:
    """Build a bounded per-family reading list from raw facts.

    Families are ranked independently so unrelated units are never collapsed
    into a disguised repository score.
    """
    result: list[dict[str, Any]] = []
    for family in (cycle_candidates(structure, includes, diff),
                   cyclomatic_candidates(structure, lizard_functions, diff),
                   file_size_candidates(source, diff),
                   duplication_candidates(duplication, diff)):
        ranked = sorted(family, key=lambda pair: (tuple(-value for value in pair[0]),
                                                  pair[1]["path"], pair[1]["line"]))
        result.extend(row for _, row in ranked[:TOP_PER_FAMILY])
    return result


def new_cycles(current_graph: dict[str, Any], base_graph: dict[str, Any], inverse_renames: dict[str, str],
               note: str, differential: bool) -> list[dict[str, Any]]:
    """Cycles worth reading, with pre-existing ones filtered out.

    A cycle is pre-existing only when its members were already mutually
    connected in one base cycle *and* every edge between them already existed:
    shrinking a cycle creates nothing, rewiring one into a smaller cycle does.
    """
    old_components = [
        {inverse_renames.get(path, path) for path in component}
        for component in base_graph.get("cycles", [])
    ]
    old_edges = {
        (inverse_renames.get(edge["from"], edge["from"]), inverse_renames.get(edge["to"], edge["to"]))
        for edge in base_graph.get("edges", [])
    }
    rows = []
    for component in sorted(tuple(component) for component in current_graph.get("cycles", [])):
        members = set(component)
        inner_edges = {(edge["from"], edge["to"]) for edge in current_graph.get("edges", [])
                       if edge["from"] in members and edge["to"] in members}
        pre_existing_members = any(members <= old for old in old_components)
        if differential and pre_existing_members and inner_edges <= old_edges:
            continue
        row = {"kind": "dependency_cycle", "path": component[0], "line": 1,
               "raw_value": len(component), "delta_from_base": 1 if differential else None,
               "members": list(component), "note": note}
        if differential and pre_existing_members:
            # These files were already mutually connected; what changed is the
            # edge, so say so rather than implying the whole cycle is new.
            row["new_edges"] = [{"from": src, "to": dst} for src, dst in sorted(inner_edges - old_edges)]
            row["note"] = f"{note} These members were already mutually connected; the listed edges are new."
        rows.append(row)
    return rows


def output_text(bundle: dict[str, Any]) -> str:
    coverage = bundle["coverage"]
    covered = sum(row["status"] == "covered" for row in coverage)
    lizard = bundle["collectors"]["lizard"]
    lines = [f"Code health evidence bundle {bundle['bundle_version']}",
             f"Coverage: {covered} covered, {len(coverage) - covered} unavailable",
             f"Collector: Lizard {lizard['version'] if lizard['installed'] else 'MISSING (optional)'}"]
    if not lizard["installed"]:
        # Named, never executed: installing changes the machine, so it stays a
        # human decision and this tool has no code path that performs one.
        lines.append(f"Optional collector, install by hand to widen coverage: {LIZARD_INSTALL}")
    unrecognised = bundle["repository"]["unrecognised_extensions"]
    if unrecognised:
        lines.append("Unrecognised extensions (no coverage row): "
                     + ", ".join(f"{key} x{count}" for key, count in unrecognised.items()))
    for limit in bundle["repository"]["coverage_limits"]:
        lines.append(f"Coverage limit: {limit['kind']} ({', '.join(limit['paths'])})")
    for row in coverage:
        lines.append(f"  {row['language']} {row['metric_family']}: {row['status']} ({row['reason']})")
    if "candidates" in bundle:
        lines.append(f"Investigation candidates: {len(bundle['candidates'])} (not health verdicts)")
    return "\n".join(lines)


def base_facts(root: Path, base: str, current_paths: list[str], lizard: LizardResult,
               scope: frozenset[str]) -> Differential:
    """Measure the base revision and record what it could not supply."""
    changed, renames = changed_paths(root, base, current_paths)
    # Names from the tree itself: archiving drops blobs analysis cannot read,
    # so a base-only unmeasured file would otherwise vanish from resolution.
    names = [name for name in git(root, "ls-tree", "-r", "--name-only", "-z", base).split("\0") if name]
    with archive_revision(root, base) as archive:
        snapshot = Path(archive)
        source, unreadable = scan(snapshot)
        structure = python_structure(source)
        includes = c_include_structure(source, names)
        collected = lizard_complexity(snapshot, source, lizard)
    return Differential(
        active=True, changed=frozenset(changed), renames=renames, source=tuple(source),
        structure=structure, includes=includes, lizard=collected,
        baseline=Baseline(unreadable=tuple(unreadable), unparsable=tuple(structure["parse_errors"]),
                          scope=scope,
                          changed_base_paths=frozenset(renames.get(path, path) for path in changed)))


def required_languages(source: list[SourceFile], unreadable: list[dict[str, str]],
                       diff: Differential) -> set[str]:
    """Languages whose coverage the caller actually asked about.

    An unreadable recognised file still makes its language required, and so
    does a base file the comparison needed but could not read or parse.  The
    scope filter stops a base-only failure demanding absent coverage.
    """
    current = [(item.path, item.language) for item in source]
    current += [(row["path"], language_for(row["path"])) for row in unreadable]
    base = [(path, language_for(path)) for path in sorted(diff.baseline.paths)]
    # Each list is filtered by its own identity: a renamed file appears under
    # its current name in one and the name the base knew it by in the other.
    return ({language for path, language in current
             if language in diff.baseline.scope and not (diff.active and path not in diff.changed)}
            | {language for path, language in base
               if language in diff.baseline.scope
               and not (diff.active and path not in diff.baseline.changed_base_paths)})


def build_bundle(args: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    root = repository_root()
    scope_names = git_scope_names(root)
    current_paths = tracked_and_untracked(root, scope_names)
    conflicts = unmerged_paths(root)
    source, unreadable = scan(root, current_paths)
    structure = python_structure(source)
    languages = scope_languages(source, unreadable)
    analyzing = args.command == "analyze"
    # Only `analyze` consumes the include graph; `detect` is the cheap coverage
    # probe and must not pay for a full scan it discards.  Full scope names, not
    # the followable subset: a symlinked or unreadable header must be named as
    # the exact target, never guessed past.
    includes = c_include_structure(source, scope_names) if analyzing else {}
    probe = lizard_status()
    lizard = lizard_complexity(root, source, probe) if analyzing else probe
    diff = (base_facts(root, args.base, current_paths, probe, languages)
            if analyzing and args.base else Differential(baseline=Baseline(scope=languages)))
    coverage = coverage_matrix(source, unreadable, structure, lizard, diff.baseline)
    invocation: dict[str, Any] = {
        "command": args.command,
        "mode": "detect" if not analyzing else ("differential" if args.base else "absolute"),
        "duplicate_lines": getattr(args, "duplicate_lines", None),
    }
    if diff.active:
        invocation.update({
            "base": args.base,
            "base_revision": git(root, "rev-parse", "--verify", f"{args.base}^{{commit}}").strip(),
            "changed_paths": sorted(diff.changed),
            "rename_map": {key: diff.renames[key] for key in sorted(diff.renames)},
        })
    bundle: dict[str, Any] = {
        "bundle_version": BUNDLE_VERSION,
        "invocation": invocation,
        "repository": {"scope": "tracked_plus_untracked_nonignored", "vendor_excludes": [],
                       "coverage_limits": coverage_limits(conflicts, symlink_paths(root, scope_names), diff.baseline),
                       "unrecognised_extensions": unrecognised_extensions(current_paths),
                       "current_revision": git(root, "rev-parse", "HEAD").strip()},
        "collectors": {"lizard": {"installed": lizard.installed, "version": lizard.version, "error": lizard.error,
                                  "supported_languages": sorted(LIZARD_SUPPORTED)}},
        "coverage": coverage,
        "facts": {"unreadable_files": unreadable},
    }
    if analyzing:
        duplication = duplication_facts(source, args.duplicate_lines, list(diff.source), diff.changed)
        composition_facts, composition_unreadable = composition(root, current_paths, source)
        merged = {row["path"]: row for row in [*unreadable, *composition_unreadable]}
        bundle["facts"]["unreadable_files"] = [merged[path] for path in sorted(merged)]
        bundle["facts"].update({"composition": composition_facts, "duplication": duplication, "python": structure,
                                "c_includes": includes,
                                "base_unreadable_files": list(diff.baseline.unreadable),
                                "lizard": {"functions": lizard.functions, "error": lizard.error}})
        bundle["candidate_selection"] = {
            "limit_per_family": TOP_PER_FAMILY,
            "ordering": "raw values and raw deltas descending within each metric family; "
                        "duplication orders by block extent before occurrence count, so a bounded "
                        "list can omit a large count rise behind longer blocks",
            "cross_family_score": False,
            "verdict": "none",
        }
        bundle["candidates"] = candidates(source, structure, includes, lizard.functions, duplication, diff)
    # Unrecognised files are disclosed, not gated: whether an unclassified file
    # is source is a judgement, and blocking on it made ordinary repositories
    # fail over a `.babelrc`.  The gate stays on measurement that actually
    # failed for a language in scope.
    required = required_languages(source, unreadable, diff)
    gap = any(row["language"] in required and row["status"] == "unavailable" for row in coverage)
    blocking_conflicts = set(conflicts) & diff.changed if diff.active else set(conflicts)
    empty_differential = analyzing and args.base is not None and not diff.changed
    return bundle, bool(empty_differential or (args.require_coverage and (gap or blocking_conflicts)))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    def common(target: argparse.ArgumentParser) -> None:
        target.add_argument("--json", action="store_true", help="emit deterministic JSON")
        target.add_argument("--require-coverage", action="store_true", help="exit 3 for unavailable requested coverage")
    detect = sub.add_parser("detect", help="report metric coverage without analysis")
    common(detect)
    analyze = sub.add_parser("analyze", help="measure structural evidence")
    common(analyze)
    modes = analyze.add_mutually_exclusive_group(required=True)
    modes.add_argument("--base", metavar="REF", help="compare current scope with a git revision")
    modes.add_argument("--all", action="store_true", help="analyse the current complete scope")
    analyze.add_argument("--duplicate-lines", type=int, default=6, help="minimum duplicate window length (default: %(default)s)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if getattr(args, "duplicate_lines", 6) < 1:
        parser().error("--duplicate-lines must be positive")
    try:
        bundle, coverage_exit = build_bundle(args)
    except HealthError as exc:
        print(f"code-health: error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # Never turn a tool failure into an apparent pass.
        print(f"code-health: internal error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(json.dumps(bundle, sort_keys=True, separators=(",", ":")) if args.json else output_text(bundle))
    return EXIT_COVERAGE if coverage_exit else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
