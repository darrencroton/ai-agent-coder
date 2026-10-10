"""Code metrics for an accepted slice: net line change and Python complexity.

`code_block` measures one accepted slice's commit range (the HEAD recorded at
launch to the accepted HEAD) with the sibling ``code-health`` skill's
``health.py``. That is the toolkit's one deliberate cross-skill import, and it
is path-based (``importlib`` on ``_HEALTH_PATH``) so ``pm_lib`` still imports
nothing outside the standard library. The block is stored on the slice entry
once, at accept; nothing re-reads git for it afterwards.

Metrics are advisory: ``code_block`` never raises. Any failure, including a
missing or unloadable ``health.py``, becomes ``{"error": "<type>: <message>"}``
so a measurement problem can be loud but never blocks an acceptance.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

_HEALTH_PATH = Path(__file__).resolve().parents[3] / "code-health" / "scripts" / "health.py"

# health.py uses `from __future__ import annotations` with @dataclass, which
# looks its own module up in sys.modules while the class body executes, so the
# module must be registered under this name before exec_module runs.
_MODULE_NAME = "_pm_code_health"

_CATEGORIES = ("production", "test", "documentation", "configuration", "data", "other")
_KINDS = ("code", "comment", "blank")

_loaded: tuple[Path, ModuleType] | None = None


def _load_health() -> ModuleType:
    """Load ``health.py`` once per resolved path and reuse it while the path is unchanged."""
    global _loaded
    path = _HEALTH_PATH.resolve()
    if _loaded is not None and _loaded[0] == path:
        return _loaded[1]
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load code-health from {path}")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(_MODULE_NAME)
    sys.modules[_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        # Put back whatever was registered before (a cached good module for
        # another path); only drop the entry when it is still our half-built one.
        if sys.modules.get(_MODULE_NAME) is module:
            if previous is None:
                del sys.modules[_MODULE_NAME]
            else:
                sys.modules[_MODULE_NAME] = previous
        raise
    _loaded = (path, module)
    return module


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=False, check=False)


def _git_ok(repo: Path, *args: str) -> bytes:
    result = _git(repo, *args)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed ({result.returncode}): {stderr}")
    return result.stdout


def _text_at(repo: Path, rev: str | None, path: str) -> str:
    """The file's text at `rev`; empty when no file exists at `path` on that side.

    Absence is established only by a successful `ls-tree` lookup that finds no
    entry, or finds a directory, submodule or symlink where a file would be
    (code-health skips symlinks, so their target text is not file content). A
    failed lookup or an unreadable blob raises, so it can never read as "empty".
    The path is looked up literally from the repository root, never as a
    pathspec, so a name such as ``:foo.py`` is found rather than read as absent.
    """
    if rev is None:
        return ""
    listing = _git_ok(repo, "--literal-pathspecs", "ls-tree", "--full-tree", "-z", rev, "--", path)
    entry = listing.split(b"\0", 1)[0]
    if not entry:
        return ""
    # "<mode> SP <type> SP <oid> TAB <path>"
    meta = entry.split(b"\t", 1)[0].split()
    if len(meta) != 3:
        raise RuntimeError(f"unexpected git ls-tree entry for {rev}:{path}")
    if meta[1] != b"blob" or meta[0] == b"120000":
        return ""
    blob = _git_ok(repo, "cat-file", "blob", meta[2].decode("ascii"))
    return blob.decode("utf-8-sig", errors="replace")


def _changed_paths(repo: Path, base: str, commit: str) -> list[tuple[str, str]]:
    """Touched files as ``(display name, git name)``, sorted by display name.

    The display name is UTF-8 decoded with replacement and is only for
    classification and reports; the git name round-trips arbitrary bytes
    (surrogateescape) so git commands still find a non-UTF-8 file.
    """
    out = _git_ok(repo, "diff", "--no-renames", "-z", "--name-only", f"{base}..{commit}")
    names = [(raw.decode("utf-8", errors="replace"), os.fsdecode(raw)) for raw in out.split(b"\0") if raw]
    return sorted(names)


def _complexity(functions: list[dict]) -> dict[str, int]:
    values = [row["cyclomatic"] for row in functions]
    return {"sum": sum(values), "max": max(values, default=0)}


def _build_block(repo: Path, before_head: str | None, commit: str) -> dict:
    health = _load_health()
    # With no launch-time HEAD the slice built the whole history; the empty
    # tree is the hash-agnostic base, and the "before" side holds no files.
    if before_head is None:
        base = _git_ok(repo, "hash-object", "-t", "tree", "/dev/null").decode("ascii").strip()
        before_rev = None
    else:
        base = before_rev = before_head

    lines = {category: dict.fromkeys(_KINDS, 0) for category in _CATEGORIES}
    sides: dict[str, list] = {"before": [], "after": []}
    for path, git_path in _changed_paths(repo, base, commit):
        language = health.language_for(path)
        category = health.category_for(path, language)[0]
        texts = {
            "before": _text_at(repo, before_rev, git_path),
            "after": _text_at(repo, commit, git_path),
        }
        counts = {side: health.line_counts(text, language) for side, text in texts.items()}
        for kind in _KINDS:
            lines[category][kind] += counts["after"][kind] - counts["before"][kind]
        if language == "Python":
            for side, text in texts.items():
                sides[side].append(
                    health.SourceFile(
                        path=path,
                        language=language,
                        category=category,
                        category_rule="",
                        text=text,
                        counts=counts[side],
                    )
                )

    structure = {side: health.python_structure(files) for side, files in sides.items()}
    errors = [*structure["before"]["parse_errors"], *structure["after"]["parse_errors"]]
    if errors:
        first = errors[0]
        return {
            "lines": lines,
            "complexity": None,
            "complexity_reason": f"{first['path']}:{first['line']}: {first['reason']}",
        }
    return {
        "lines": lines,
        "complexity": {side: _complexity(structure[side]["functions"]) for side in ("before", "after")},
        "complexity_reason": None,
    }


def code_block(repo: Path, before_head: str | None, commit: str) -> dict:
    """The slice's net code change: ``{lines, complexity, complexity_reason}``.

    ``lines`` is the signed after-minus-before change per category (production,
    test, documentation, configuration, data, other) and kind (code, comment,
    blank), counted by code-health's lexical ``line_counts`` heuristic.
    ``complexity`` is the sum and maximum of per-function cyclomatic complexity
    over the touched Python files before and after (Python AST), or ``None``
    with ``complexity_reason`` naming the first file that failed to parse.
    Never raises: any failure returns ``{"error": "<type>: <message>"}``.
    """
    try:
        return _build_block(repo, before_head, commit)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
