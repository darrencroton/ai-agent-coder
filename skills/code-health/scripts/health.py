#!/usr/bin/env python3
"""Portable, evidence-first codebase structure analyser.

This module deliberately contains no quality score.  It measures a small,
auditable set of structural properties, and leaves any judgement about whether
an outlier is appropriate to the caller. The portable floor uses only Python's
standard library and ``git``.

``detect`` records available coverage. ``analyze`` records measured facts; in differential mode
it limits investigation candidates to changed raw values and deltas from a git
base revision.  Python analysis is stdlib-only; Lizard is an optional external
collector for selected non-Python cyclomatic complexity, managed explicitly by
the ``install`` subcommand.  Exit status 0 means analysis completed, not that a
repository is "healthy".
"""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

BUNDLE_VERSION = "1.0"
EXIT_OK = 0
EXIT_ERROR = 2
EXIT_COVERAGE = 3
TOP_PER_FAMILY = 5
DEFAULT_MAX_COMMITS = 200
LIZARD_SUPPORTED = {"JavaScript", "TypeScript", "Java", "Kotlin", "C", "C/C++", "C++", "C#", "Go", "Ruby", "PHP", "Swift", "Rust", "Scala"}
INSTALL_RECIPES = {
    "uv": ["uv", "tool", "install", "lizard"],
    "pipx": ["pipx", "install", "lizard"],
    "brew": ["brew", "install", "lizard"],
    "pip": [sys.executable, "-m", "pip", "install", "--user", "lizard"],
}

# This is intentionally pragmatic rather than a claim to parse every language.
# The map is used for scope and line counting, not as a language standard.
EXTENSIONS = {
    ".py": "Python", ".pyi": "Python", ".js": "JavaScript", ".mjs": "JavaScript",
    ".cjs": "JavaScript", ".jsx": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin", ".c": "C", ".h": "C/C++",
    ".cc": "C++", ".cpp": "C++", ".cxx": "C++", ".hpp": "C++", ".cs": "C#",
    ".go": "Go", ".rs": "Rust", ".rb": "Ruby", ".php": "PHP", ".swift": "Swift",
    ".scala": "Scala", ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell",
    ".fish": "Shell", ".ps1": "PowerShell", ".r": "R", ".R": "R", ".lua": "Lua",
    ".pl": "Perl", ".pm": "Perl", ".ex": "Elixir", ".exs": "Elixir",
    ".fs": "F#", ".fsx": "F#", ".dart": "Dart", ".sql": "SQL",
}
DOC_EXTENSIONS = {".md", ".mdx", ".rst", ".txt", ".adoc"}
CONFIG_EXTENSIONS = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".xml"}
HASH_COMMENT_LANGUAGES = {"Python", "Shell", "Ruby", "Perl", "R", "Lua", "Elixir", "PowerShell"}
SLASH_COMMENT_LANGUAGES = {"JavaScript", "TypeScript", "Java", "Kotlin", "C", "C++", "C#", "Go", "Rust", "Swift", "Scala", "Dart", "PHP"}
METRIC_FAMILIES = ("composition", "duplication", "cyclomatic", "dependencies")


class HealthError(RuntimeError):
    """A user/actionable error, mapped to exit status 2."""


def git(root: Path, *args: str, input_bytes: bytes | None = None) -> str:
    """Run git at *root*, converting useful failures into a stable exception."""
    proc = subprocess.run(["git", *args], cwd=root, input=input_bytes, capture_output=True)
    if proc.returncode:
        detail = proc.stderr.decode(errors="replace").strip() or proc.stdout.decode(errors="replace").strip()
        raise HealthError(f"git {' '.join(args)} failed: {detail}")
    return proc.stdout.decode(errors="replace")


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

    ``git ls-files -co --exclude-standard`` is important: a directory walk
    would accidentally inspect ignored build output and dependencies, while
    tracked-only scope would miss a newly-added source file in a change.
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
        tree = subprocess.run(
            ["git", "ls-tree", "-r", "-z", revision], cwd=root, capture_output=True, check=False,
        )
        if tree.returncode:
            raise HealthError(tree.stderr.decode(errors="replace").strip() or "git ls-tree failed")
        entries = []
        for entry in tree.stdout.split(b"\0"):
            if not entry:
                continue
            metadata, raw_path = entry.split(b"\t", 1)
            mode, kind, object_id = metadata.decode("ascii").split()
            if kind != "blob" or mode == "120000":
                continue
            path = raw_path.decode("utf-8", errors="surrogateescape")
            if not language_for(path):
                continue
            target = Path(tmp.name, path)
            if target.parent == target or not target.is_relative_to(tmp.name):
                raise HealthError("unsafe path in git tree")
            entries.append((object_id, target))
        process = subprocess.Popen(
            ["git", "cat-file", "--batch"], cwd=root, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert process.stdin and process.stdout
        try:
            for object_id, target in entries:
                process.stdin.write(f"{object_id}\n".encode())
                process.stdin.flush()
                header = process.stdout.readline().decode("ascii", errors="replace").split()
                if len(header) != 3 or header[1] != "blob" or not header[2].isdigit():
                    raise HealthError("git cat-file returned an invalid blob header")
                blob = process.stdout.read(int(header[2]))
                if len(blob) != int(header[2]) or process.stdout.read(1) != b"\n":
                    raise HealthError("git cat-file returned a truncated blob")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(blob)
            process.stdin.close()
            if process.wait() != 0:
                raise HealthError(process.stderr.read().decode(errors="replace").strip() or "git cat-file failed")
        except Exception:
            process.kill()
            process.wait()
            raise
        finally:
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe and not pipe.closed:
                    pipe.close()
    except Exception:
        tmp.cleanup()
        raise
    return tmp


def language_for(path: str) -> str | None:
    return EXTENSIONS.get(Path(path).suffix)


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
    if suffix in DOC_EXTENSIONS or lower.startswith("docs/"):
        return "documentation", "extension_or_docs_directory_rule"
    if suffix in CONFIG_EXTENSIONS or name.startswith("."):
        return "configuration", "extension_or_dotfile_rule"
    return "other", "unrecognised_extension_rule"


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


def composition(root: Path, paths: Iterable[str] | None = None) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Composition includes source plus separately-classified docs/config files."""
    all_rows: list[dict[str, Any]] = []
    unreadable: list[dict[str, str]] = []
    # Current working-tree composition must have the same git-defined scope as
    # analysis.  The archive has no .git directory, but test repositories do.
    for path in sorted(paths if paths is not None else _files_under(root)):
        language = language_for(path)
        category, rule = category_for(path, language)
        try:
            text = (root / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            unreadable.append({"path": path, "reason": f"unreadable_utf8: {exc.__class__.__name__}"})
            continue
        row = {"path": path, "language": language, "category": category, "category_rule": rule, **line_counts(text, language)}
        all_rows.append(row)
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


def normalized_windows(source: list[SourceFile], width: int) -> dict[str, dict[str, Any]]:
    """Return exact whitespace-normalised windows and their occurrence counts."""
    found: dict[str, dict[str, Any]] = {}
    for file in source:
        # Blank lines are formatting separators, not a meaningful part of an
        # exact code window.  Keep physical start lines for investigation.
        lines = [(number, line) for number, line in enumerate(file.text.splitlines(), 1) if line.strip()]
        for start in range(0, max(0, len(lines) - width + 1)):
            normalized = "\n".join("".join(line.split()) for _, line in lines[start:start + width])
            if not normalized.strip():
                continue
            signature = hashlib.sha256(normalized.encode()).hexdigest()
            item = found.setdefault(signature, {"signature": signature, "line_count": width, "occurrences": []})
            item["occurrences"].append({"path": file.path, "start_line": lines[start][0]})
    for item in found.values():
        item["occurrences"].sort(key=lambda x: (x["path"], x["start_line"]))
        item["count"] = len(item["occurrences"])
    return found


def duplication_facts(source: list[SourceFile], width: int, base_source: list[SourceFile] | None, changed: set[str]) -> dict[str, Any]:
    current = normalized_windows(source, width)
    base = normalized_windows(base_source or [], width)
    rows = []
    for signature in sorted(set(current) | set(base)):
        head = current.get(signature, {"count": 0, "occurrences": []})
        old = base.get(signature, {"count": 0, "occurrences": []})
        # A signature that occurs only once on both sides is not duplication,
        # even when it is new. Keep resolved duplicates (base >= 2) as well as
        # current duplicates so differential counts remain honest.
        if max(head["count"], old["count"]) < 2:
            continue
        delta = head["count"] - old["count"]
        changed_occurrences = [o for o in head["occurrences"] if o["path"] in changed]
        rows.append({
            "signature": signature, "line_count": width, "current_count": head["count"],
            "base_count": old["count"], "count_delta": delta,
            "changed_side_occurrences": changed_occurrences,
            "current_occurrences": head["occurrences"],
        })
    return {"normalization": "whitespace_only", "window_lines": width, "signatures": rows}


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
    inbound: Counter[str] = Counter(dst for _, dst in edges)
    outbound: Counter[str] = Counter(src for src, _ in edges)
    graph = {path: sorted(dst for src, dst in edges if src == path) for path in sorted(trees)}
    sccs = strongly_connected_components(graph)
    cycles = [component for component in sccs if len(component) > 1]
    reverse = {path: [] for path in graph}
    for src, destinations in graph.items():
        for dst in destinations:
            reverse[dst].append(src)
    blast = []
    for path in sorted(graph):
        seen = {path}
        queue: deque[str] = deque(reverse[path])
        while queue:
            item = queue.popleft()
            if item not in seen:
                seen.add(item)
                queue.extend(reverse[item])
        blast.append({"path": path, "reverse_reachability": len(seen) - 1})
    blast.sort(key=lambda row: (-row["reverse_reachability"], row["path"]))
    return {
        "parse_errors": errors,
        "functions": functions,
        "function_cyclomatic_distribution": size_distribution([r["cyclomatic"] for r in functions]),
        "imports": {"edges": [{"from": src, "to": dst} for src, dst in sorted(edges)],
                    "fan_in": [{"path": p, "value": inbound[p]} for p in sorted(graph)],
                    "fan_out": [{"path": p, "value": outbound[p]} for p in sorted(graph)],
                    "cycles": cycles, "reverse_reachability": blast},
    }


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


def parse_lizard_csv(output: str) -> list[dict[str, Any]]:
    """Parse Lizard's headerless 11-column CSV and headered variants."""
    raw_rows = list(csv.reader(output.splitlines()))
    parsed: list[dict[str, Any]] = []
    if not raw_rows:
        return parsed
    headered = any(value.strip().lower() == "ccn" for value in raw_rows[0])
    if headered:
        header = [value.strip().lower() for value in raw_rows.pop(0)]
        rows = [dict(zip(header, values, strict=False)) for values in raw_rows]
    else:
        columns = ("nloc", "ccn", "token", "param", "length", "location",
                   "file", "function", "long_name", "start", "end")
        rows = [dict(zip(columns, values, strict=False)) for values in raw_rows]
    for row in rows:
        try:
            complexity = int(row.get("ccn") or row.get("cyclomatic_complexity") or "")
            name = row.get("function") or row.get("long_name") or row.get("name") or ""
            path = row.get("filename") or row.get("file") or ""
            line = int(row.get("start") or row.get("start_line") or "0")
            location = row.get("location", "")
            # Traditional lizard CSV has location: ``function@3-8@src/a.js``.
            if location and (not name or not path):
                pieces = location.rsplit("@", 2)
                if len(pieces) == 3:
                    name = name or pieces[0]
                    path = path or pieces[2]
                    if not line:
                        line = int(pieces[1].split("-", 1)[0])
            if not name or not path or line < 1:
                raise ValueError("missing function, filename, or start")
        except (TypeError, ValueError) as exc:
            raise HealthError(f"unparsable lizard CSV row: {row}") from exc
        parsed.append({"path": path.replace("\\", "/"), "name": name, "line": line,
                       "cyclomatic": complexity, "collector": "lizard"})
    return sorted(parsed, key=lambda r: (-r["cyclomatic"], r["path"], r["line"], r["name"]))


def lizard_complexity(root: Path, source: list[SourceFile], status: LizardResult | None = None) -> LizardResult:
    status = status or lizard_status()
    eligible = [item.path for item in source if item.language in LIZARD_SUPPORTED]
    if not eligible:
        return status
    if not status.installed:
        return status
    binary = shutil.which("lizard")
    assert binary  # status's installed state was derived from this probe.
    functions: list[dict[str, Any]] = []
    for start in range(0, len(eligible), 200):
        proc = subprocess.run([binary, "--csv", *eligible[start:start + 200]], cwd=root, capture_output=True, text=True)
        if proc.returncode:
            return LizardResult(True, status.version, [], f"lizard failed: {(proc.stderr or proc.stdout).strip()}")
        try:
            functions.extend(parse_lizard_csv(proc.stdout))
        except HealthError as exc:
            return LizardResult(True, status.version, [], str(exc))
    functions.sort(key=lambda row: (-row["cyclomatic"], row["path"], row["line"], row["name"]))
    return LizardResult(True, status.version, functions)


def strongly_connected_components(graph: dict[str, list[str]]) -> list[list[str]]:
    """Tarjan's SCC algorithm, sorted so JSON is stable across Python runs."""
    index = 0
    indices: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    result: list[list[str]] = []
    def visit(node: str) -> None:
        nonlocal index
        indices[node] = low[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in graph[node]:
            if target not in indices:
                visit(target)
                low[node] = min(low[node], low[target])
            elif target in on_stack:
                low[node] = min(low[node], indices[target])
        if low[node] == indices[node]:
            component = []
            while True:
                target = stack.pop()
                on_stack.remove(target)
                component.append(target)
                if target == node:
                    break
            result.append(sorted(component))
    for node in sorted(graph):
        if node not in indices:
            visit(node)
    return sorted(result)


def coverage_matrix(source: list[SourceFile], unreadable: list[dict[str, str]], structure: dict[str, Any], lizard: LizardResult) -> list[dict[str, str]]:
    languages = sorted({item.language for item in source} | {
        language_for(row["path"]) for row in unreadable if language_for(row["path"])
    })
    python_errors = structure["parse_errors"]
    unreadable_languages = {language_for(row["path"]) for row in unreadable}
    matrix = []
    for language in languages:
        for family in METRIC_FAMILIES:
            if family in {"composition", "duplication"} and language not in unreadable_languages:
                status, reason = "covered", "stdlib lexical analysis"
            elif family in {"composition", "duplication"}:
                status, reason = "unavailable", "unreadable source in this language"
            elif language == "Python" and not python_errors and language not in unreadable_languages:
                status, reason = "covered", "Python stdlib AST"
            elif language == "Python":
                status, reason = "unavailable", "Python AST parse/read gap"
            elif family == "cyclomatic" and language in LIZARD_SUPPORTED and lizard.installed and not lizard.error:
                status, reason = "covered", f"Lizard {lizard.version or 'unknown'}"
            elif family == "cyclomatic" and language in LIZARD_SUPPORTED:
                status, reason = "unavailable", lizard.error or "lizard not installed"
            else:
                status, reason = "unavailable", "no portable parser for this language"
            matrix.append({"language": language, "metric_family": family, "status": status, "reason": reason})
    return matrix


def changed_paths(root: Path, base: str, current_paths: Iterable[str] | None = None) -> tuple[set[str], dict[str, str]]:
    """Changed current paths plus current->base mapping for Git-detected renames."""
    output = git(root, "diff", "--name-status", "-M", "-z", base, "--")
    tokens = output.split("\0")
    changed: set[str] = set()
    renames: dict[str, str] = {}
    i = 0
    while i < len(tokens) and tokens[i]:
        status = tokens[i]
        i += 1
        code = status[:1]
        if code in {"R", "C"}:
            if i + 1 >= len(tokens):
                break
            old, new = tokens[i], tokens[i + 1]
            i += 2
            changed.add(new)
            if code == "R":
                renames[new] = old
        else:
            if i >= len(tokens):
                break
            path = tokens[i]
            i += 1
            changed.add(path)
    # Non-ignored untracked paths are additions, absent from git diff.
    tracked = set(git(root, "ls-files", "-z").split("\0"))
    changed.update(path for path in (current_paths or tracked_and_untracked(root)) if path not in tracked)
    return changed, renames


def candidates(
    source: list[SourceFile],
    base_source: list[SourceFile] | None,
    structure: dict[str, Any],
    lizard_functions: list[dict[str, Any]],
    duplication: dict[str, Any],
    changed: set[str],
    base_structure: dict[str, Any] | None,
    base_lizard_functions: list[dict[str, Any]],
    rename_map: dict[str, str],
    differential: bool,
) -> list[dict[str, Any]]:
    """Build a bounded per-family reading list from raw facts.

    Families are ranked independently so unrelated units are never collapsed
    into a disguised repository score.
    """
    # Lines shift under unrelated edits.  Python qualified names (and Lizard's
    # long names) are more stable identities for a differential raw delta.
    old_complexity = {(row["path"], row["name"]): row["cyclomatic"] for row in (base_structure or {}).get("functions", [])}
    old_lizard = {(row["path"], row["name"]): row["cyclomatic"] for row in base_lizard_functions}
    by_family: dict[str, list[tuple[tuple[int, ...], dict[str, Any]]]] = defaultdict(list)
    for row in [*structure["functions"], *lizard_functions]:
        if differential and row["path"] not in changed:
            continue
        old_path = rename_map.get(row["path"], row["path"])
        old_values = old_lizard if row.get("collector") == "lizard" else old_complexity
        old = old_values.get((old_path, row["name"]), 0)
        delta = row["cyclomatic"] - old
        if differential and delta == 0:
            continue
        item = {"kind": "cyclomatic", "path": row["path"], "line": row["line"], "raw_value": row["cyclomatic"], "delta_from_base": delta if differential else None, "collector": row.get("collector", "python_ast"), "note": "Measured complexity; investigate context before recommending change."}
        by_family["cyclomatic"].append(((row["cyclomatic"], delta), item))
    for row in duplication["signatures"]:
        occurrences = row["changed_side_occurrences"] if differential else row["current_occurrences"]
        if (row["count_delta"] > 0 or not differential) and occurrences:
            first = occurrences[0]
            item = {"kind": "duplication", "path": first["path"], "line": first["start_line"], "raw_value": row["current_count"], "delta_from_base": row["count_delta"] if differential else None, "window_lines": row["line_count"], "note": "Exact whitespace-normalised repeated window; investigate whether shared structure is intentional."}
            by_family["duplication"].append(((row["current_count"], row["line_count"], row["count_delta"]), item))

    old_files = {item.path: int(item.counts.get("code", 0)) for item in base_source or []}
    for item in source:
        if differential and item.path not in changed:
            continue
        value = int(item.counts.get("code", 0))
        old = old_files.get(rename_map.get(item.path, item.path), 0)
        delta = value - old
        if differential and delta == 0:
            continue
        row = {"kind": "file_size", "path": item.path, "line": 1, "raw_value": value,
               "delta_from_base": delta if differential else None,
               "note": "Raw code-line count; inspect the file's role before considering decomposition."}
        by_family["file_size"].append(((value, delta), row))

    current_cycles = {tuple(component) for component in structure["imports"]["cycles"]}
    inverse_renames = {old: new for new, old in rename_map.items()}
    old_cycles = {
        tuple(sorted(inverse_renames.get(path, path) for path in component))
        for component in (base_structure or {}).get("imports", {}).get("cycles", [])
    }
    for component in sorted(current_cycles):
        if differential and (component in old_cycles or not changed.intersection(component)):
            continue
        row = {"kind": "dependency_cycle", "path": component[0], "line": 1,
               "raw_value": len(component), "delta_from_base": 1 if differential else None,
               "members": list(component),
               "note": "Static Python import cycle; inspect runtime boundaries and intentionality."}
        by_family["dependency_cycle"].append(((len(component),), row))

    result: list[dict[str, Any]] = []
    for family in ("dependency_cycle", "cyclomatic", "file_size", "duplication"):
        ranked = sorted(
            by_family[family],
            key=lambda pair: (tuple(-value for value in pair[0]), pair[1]["path"], pair[1]["line"]),
        )
        result.extend(row for _, row in ranked[:TOP_PER_FAMILY])
    return result


def history_facts(root: Path, maximum: int) -> dict[str, Any]:
    if git(root, "rev-parse", "--is-shallow-repository").strip() == "true":
        return {"status": "unavailable", "reason": "shallow_repository"}
    invocation = ["git", "log", f"--max-count={maximum}", "--format=%H", "--numstat", "--no-renames"]
    records = git(root, *invocation[1:]).splitlines()
    churn: dict[str, dict[str, int]] = defaultdict(lambda: {"revisions": 0, "additions": 0, "deletions": 0})
    commits = 0
    for line in records:
        if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", line):
            commits += 1
            continue
        fields = line.split("\t", 2)
        if len(fields) == 3:
            added, deleted, path = fields
            # Binary numstat uses '-', which cannot honestly be represented as
            # a line count; omit its line deltas while retaining its revision.
            entry = churn[path]
            entry["revisions"] += 1
            if added.isdigit():
                entry["additions"] += int(added)
            if deleted.isdigit():
                entry["deletions"] += int(deleted)
    return {"status": "covered", "revision": git(root, "rev-parse", "HEAD").strip(), "max_commits": maximum,
            "git_invocation": invocation,
            "commits_considered": commits, "churn": [{"path": p, **churn[p]} for p in sorted(churn)]}


def output_text(bundle: dict[str, Any]) -> str:
    coverage = bundle["coverage"]
    covered = sum(row["status"] == "covered" for row in coverage)
    unavailable = len(coverage) - covered
    lines = [f"Code health evidence bundle {bundle['bundle_version']}", f"Coverage: {covered} covered, {unavailable} unavailable"]
    lizard = bundle["collectors"]["lizard"]
    collector_state = lizard["version"] if lizard["installed"] else "MISSING (optional)"
    lines.append(f"Collector: Lizard {collector_state}")
    if not lizard["installed"]:
        lines.append(f"Install plan: {shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} install")
    for limit in bundle["repository"]["coverage_limits"]:
        lines.append(f"Coverage limit: {limit['kind']} ({', '.join(limit['paths'])})")
    for row in coverage:
        lines.append(f"  {row['language']} {row['metric_family']}: {row['status']} ({row['reason']})")
    if "candidates" in bundle:
        lines.append(f"Investigation candidates: {len(bundle['candidates'])} (not health verdicts)")
    return "\n".join(lines)


def package_managers() -> list[str]:
    """Installation preference matches the lint skill's portable policy."""
    return [manager for manager in ("uv", "pipx", "brew") if shutil.which(manager)] + ["pip"]


def install_lizard(args: argparse.Namespace) -> int:
    """Print the one optional collector install plan; mutate only with --yes."""
    status = lizard_status()
    if status.installed:
        print(f"nothing to install: lizard is already available ({status.version or 'version unknown'})")
        return EXIT_OK
    manager = package_managers()[0]
    command = INSTALL_RECIPES[manager]
    print("code-health install plan:")
    print(f"  lizard  {' '.join(command)}")
    if not args.yes:
        print("\ndry run -- nothing executed. Installation changes the machine; a human may re-run with --yes.")
        return EXIT_OK
    proc = subprocess.run(command)
    if proc.returncode:
        print("code-health: lizard installation failed", file=sys.stderr)
        return EXIT_ERROR
    print("install complete")
    return EXIT_OK


def build_bundle(args: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    root = repository_root()
    scope_names = git_scope_names(root)
    current_paths = tracked_and_untracked(root, scope_names)
    conflicts = unmerged_paths(root)
    symlinks = symlink_paths(root, scope_names)
    source, unreadable = scan(root, current_paths)
    structure = python_structure(source)
    initial_lizard = lizard_status()
    lizard = lizard_complexity(root, source, initial_lizard) if args.command == "analyze" else initial_lizard
    mode = "detect" if args.command == "detect" else ("differential" if args.base else "absolute")
    invocation = {"command": args.command, "mode": mode, "duplicate_lines": getattr(args, "duplicate_lines", None)}
    changed: set[str] = set()
    base_source: list[SourceFile] | None = None
    base_structure: dict[str, Any] | None = None
    base_lizard_functions: list[dict[str, Any]] = []
    rename_map: dict[str, str] = {}
    if args.command == "analyze" and args.base:
        changed, rename_map = changed_paths(root, args.base, current_paths)
        invocation["base"] = args.base
        invocation["base_revision"] = git(root, "rev-parse", "--verify", f"{args.base}^{{commit}}").strip()
        invocation["changed_paths"] = sorted(changed)
        invocation["rename_map"] = {key: rename_map[key] for key in sorted(rename_map)}
        with archive_revision(root, args.base) as archive:
            base_source, _ = scan(Path(archive))
            base_structure = python_structure(base_source)
            base_lizard_functions = lizard_complexity(Path(archive), base_source, initial_lizard).functions
    coverage = coverage_matrix(source, unreadable, structure, lizard)
    bundle: dict[str, Any] = {
        "bundle_version": BUNDLE_VERSION,
        "invocation": invocation,
        "repository": {"scope": "tracked_plus_untracked_nonignored", "vendor_excludes": [],
                       "coverage_limits": ([{"kind": "unmerged_index", "paths": conflicts}] if conflicts else [])
                       + ([{"kind": "symbolic_links_not_followed", "paths": symlinks}] if symlinks else []),
                       "current_revision": git(root, "rev-parse", "HEAD").strip()},
        "collectors": {"lizard": {"installed": lizard.installed, "version": lizard.version, "error": lizard.error,
                                    "supported_languages": sorted(LIZARD_SUPPORTED)}},
        "coverage": coverage,
        "facts": {"unreadable_files": unreadable},
    }
    empty_differential = args.command == "analyze" and args.base is not None and not changed
    history_gap = False
    if args.command == "analyze":
        dup = duplication_facts(source, args.duplicate_lines, base_source, changed)
        composition_facts, composition_unreadable = composition(root, current_paths)
        all_unreadable = {row["path"]: row for row in [*unreadable, *composition_unreadable]}
        bundle["facts"]["unreadable_files"] = [all_unreadable[path] for path in sorted(all_unreadable)]
        bundle["facts"].update({"composition": composition_facts, "duplication": dup, "python": structure,
                                "lizard": {"functions": lizard.functions, "error": lizard.error}})
        if args.history:
            bundle["facts"]["history"] = history_facts(root, args.max_commits)
            history_gap = bundle["facts"]["history"]["status"] == "unavailable"
        bundle["candidate_selection"] = {
            "limit_per_family": TOP_PER_FAMILY,
            "ordering": "raw values and raw deltas descending within each metric family",
            "cross_family_score": False,
            "verdict": "none",
        }
        bundle["candidates"] = candidates(source, base_source, structure, lizard.functions, dup, changed, base_structure, base_lizard_functions, rename_map, bool(args.base))
    required_languages = {f.language for f in source if (not args.base or f.path in changed)} if args.command == "analyze" else {f.language for f in source}
    gap = any(row["language"] in required_languages and row["status"] == "unavailable" for row in coverage)
    blocking_conflicts = set(conflicts) & changed if args.command == "analyze" and args.base else set(conflicts)
    return bundle, bool(empty_differential or (args.require_coverage and (gap or history_gap or blocking_conflicts)))


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
    analyze.add_argument("--history", action="store_true", help="add repository-context churn evidence")
    analyze.add_argument("--max-commits", type=int, default=DEFAULT_MAX_COMMITS, help="history commit cap (default: %(default)s)")
    analyze.add_argument("--duplicate-lines", type=int, default=6, help="exact duplicate window length (default: %(default)s)")
    install = sub.add_parser("install", help="print (or with --yes, execute) the Lizard install command")
    install.add_argument("--yes", action="store_true", help="actually execute the install command")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "install":
        return install_lizard(args)
    if getattr(args, "duplicate_lines", 6) < 1 or getattr(args, "max_commits", 1) < 1:
        parser().error("--duplicate-lines and --max-commits must be positive")
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
