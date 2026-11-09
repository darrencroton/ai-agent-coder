"""`pm.py dossier`: the numbers, timestamps and head SHAs of a campaign dossier.

The dossier's Markdown is the Campaign Manager's (CM) own state and stays the
only source: there is no sidecar and no schema, because a second record of the
same facts would be a second source of truth. What a tool can own without one
is the part a hand edit gets wrong: the next `D<n>`/`E<n>` number, the UTC
`At` stamp, and the `Head SHA` cell of a node row. Everything else (a row's
status, the objective, the prose of an entry) is the CM's to write and is never
touched here.

Each command is idempotent so a CM that dies after the write and repeats the
command records nothing twice. The parsing is deliberately plain regexes on
headings and table rows; the layout it reads is the one
`references/dossier-template.md` shows.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import PmError
from . import git_ops
from . import state as state_mod

ACTORS = ("CM", "owner")

# The field labels of an entry, in file order, after the `At` line.
_DECISION_FIELDS = ("Actor", "Decision", "Grounds", "Permission used")
_EVIDENCE_FIELDS = ("Finding", "Source", "SHA read at", "Uncertainty")

_FIELD_LINE_RE = re.compile(r"^- ([^:\n]+): (.*)$")
_HEADING_RE = re.compile(r"^## ", re.MULTILINE)
_NODES_HEADING_RE = re.compile(r"^## Nodes[ \t\r]*$", re.MULTILINE)
_SEPARATOR_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
# A pipe the Markdown has not escaped: the template's `change \| evidence` is one cell.
_PIPE_RE = re.compile(r"(?<!\\)\|")


def add_decision(
    dossier: Path, *, title: str, actor: str, decision: str, grounds: str, permission: str
) -> tuple[int, bool]:
    """Record a `## D<n>: <title>` entry in `decisions.md`; return (n, newly written).

    `actor` is CM or owner because the dossier's approvals name the owner as
    their actor and the brief's permission rules read that field. A repeat of
    an entry already there (same title, same field values; the `At` stamp is
    what differs) writes nothing and returns its number.
    """
    if actor not in ACTORS:
        raise PmError(f"actor must be one of {', '.join(ACTORS)}, not {actor!r}")
    fields = dict(zip(_DECISION_FIELDS, (actor, decision, grounds, permission), strict=True))
    return _add_entry(dossier / "decisions.md", dossier, "D", title, fields)


def add_evidence(
    dossier: Path, *, title: str, finding: str, source: str, sha: str, uncertainty: str
) -> tuple[int, bool]:
    """Record a `## E<n>: <title>` entry in `evidence.md`; return (n, newly written)."""
    fields = dict(zip(_EVIDENCE_FIELDS, (finding, source, sha, uncertainty), strict=True))
    return _add_entry(dossier / "evidence.md", dossier, "E", title, fields)


def _add_entry(path: Path, dossier: Path, letter: str, title: str, fields: dict[str, str]) -> tuple[int, bool]:
    """Append one entry to `path`, or find the identical one already there.

    A value is one line because the entry format is one `- Label: value` line
    per field; a newline would let a value forge a heading or a field.
    """
    if not dossier.is_dir():
        raise PmError(f"dossier directory does not exist: {dossier}")
    for label, value in {"title": title, **fields}.items():
        if not value.strip():
            raise PmError(f"{label} must not be blank")
        if "\n" in value or "\r" in value:
            raise PmError(f"{label} must be a single line")
    title = title.strip()
    fields = {label: value.strip() for label, value in fields.items()}

    text = path.read_bytes().decode("utf-8") if path.exists() else ""
    heading_re = re.compile(rf"^## {letter}(\d+): (.*)$", re.MULTILINE)
    highest = 0
    for match in heading_re.finditer(text):
        number = int(match.group(1))
        highest = max(highest, number)
        if match.group(2).strip() == title and _entry_fields(text, match.end()) == fields:
            return number, False

    number = highest + 1
    entry = f"## {letter}{number}: {title}\n- At: {state_mod.utc_now_iso()}\n"
    entry += "".join(f"- {label}: {value}\n" for label, value in fields.items())
    prefix = text.rstrip("\n") + "\n\n" if text.strip() else ""
    state_mod._atomic_write_bytes(path, (prefix + entry).encode("utf-8"))
    return number, True


def _entry_fields(text: str, start: int) -> dict[str, str]:
    """The `- Label: value` lines of the entry whose heading ends at `start`, `At` excluded."""
    following = _HEADING_RE.search(text, start)
    body = text[start : following.start() if following else len(text)]
    fields: dict[str, str] = {}
    for line in body.splitlines():
        match = _FIELD_LINE_RE.match(line)
        if match and match.group(1) != "At":
            fields[match.group(1)] = match.group(2).strip()
    return fields


def set_head(dossier: Path, *, node: str, head: str) -> bool:
    """Set the `Head SHA` cell of `node`'s row in `work.md`'s `## Nodes` table.

    Returns True when the cell changed and False when it already held `head`.
    The row is found by its `Id` cell, the column by its header, so a CM that
    adds or reorders columns loses nothing. Only that one cell is rewritten:
    every other byte of the file, other cells' padding and line endings
    included, is carried through as it was.
    """
    if not git_ops.is_full_commit_hash(head):
        raise PmError(f"head must be a full 40-hex commit SHA, not {head!r}")
    path = dossier / "work.md"
    if not path.is_file():
        raise PmError(f"no work.md in the dossier: {path}")
    text = path.read_bytes().decode("utf-8")

    heading = _NODES_HEADING_RE.search(text)
    if heading is None:
        raise PmError("work.md has no '## Nodes' heading")
    following = _HEADING_RE.search(text, heading.end())
    section_end = following.start() if following else len(text)

    lines = text[heading.end() : section_end].splitlines(keepends=True)
    offset = heading.end()
    tables = _tables(lines)
    if not tables:
        raise PmError("work.md's '## Nodes' section has no Markdown table")
    with_id = [table for table in tables if "Id" in (cell.strip() for cell in _cells(lines[table[0]]))]
    if not with_id:
        raise PmError("work.md's '## Nodes' table has no 'Id' column")
    first, last = with_id[0]
    header = [cell.strip() for cell in _cells(lines[first])]
    if "Head SHA" not in header:
        raise PmError("work.md's '## Nodes' table has no 'Head SHA' column")
    id_column, head_column = header.index("Id"), header.index("Head SHA")

    matches = []
    for index in range(first + 2, last + 1):
        cells = _cells(lines[index])
        if len(cells) > max(id_column, head_column) and cells[id_column].strip() == node:
            matches.append(index)
    if not matches:
        raise PmError(f"work.md's '## Nodes' table has no row with Id {node!r}")
    if len(matches) > 1:
        raise PmError(f"work.md's '## Nodes' table has {len(matches)} rows with Id {node!r}")

    index = matches[0]
    body = lines[index].rstrip("\r\n")
    ending = lines[index][len(body) :]
    segments = _PIPE_RE.split(body)
    # `| a | b |` splits to ['', ' a ', ' b ', '']: cell k is segment k + 1.
    if segments[head_column + 1].strip() == head:
        return False
    segments[head_column + 1] = f" {head} "
    lines[index] = "|".join(segments) + ending
    new_text = text[:offset] + "".join(lines) + text[section_end:]
    state_mod._atomic_write_bytes(path, new_text.encode("utf-8"))
    return True


def _cells(line: str) -> list[str]:
    """The raw cells of a table line, padding kept and the outer pipes dropped."""
    segments = _PIPE_RE.split(line.rstrip("\r\n"))[1:]
    if segments and not segments[-1].strip():
        segments.pop()
    return segments


def _tables(lines: list[str]) -> list[tuple[int, int]]:
    """(header index, last row index) of each table: a run of `|` lines whose second line is a separator."""
    tables = []
    index = 0
    while index < len(lines):
        if not lines[index].lstrip().startswith("|"):
            index += 1
            continue
        end = index
        while end + 1 < len(lines) and lines[end + 1].lstrip().startswith("|"):
            end += 1
        if end > index and _SEPARATOR_RE.match(lines[index + 1]):
            tables.append((index, end))
        index = end + 1
    return tables
