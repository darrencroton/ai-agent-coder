"""`pm.py dossier`: the numbers, timestamps and head SHAs of a campaign dossier.

The dossier's Markdown is the Campaign Manager's (CM) own state and stays the
only source: there is no sidecar and no schema, because a second record of the
same facts would be a second source of truth. What a tool can own without one
is the part a hand edit gets wrong: the next `D<n>`/`E<n>` number, the UTC
`At` stamp, and the `Head SHA` cell of a node row. Everything else (a row's
status, the objective, the prose of an entry) is the CM's to write and is never
touched here. `counts` only reads, and prints the numbers a findings report
quotes about the campaign's own record.

Each command is idempotent so a CM that dies after the write and repeats the
command records nothing twice. The parsing is deliberately plain regexes on
headings and table rows; the layout it reads is the one
`references/dossier-template.md` shows.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
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
# The template's three node kinds, and the one open or archived CM question file name.
_KINDS = ("change", "evidence", "task")
_QUESTION_RE = re.compile(r"cm-question(-\d+)?\.md")


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


def counts(dossier: Path, *, repo: Path | None = None, diff: str | None = None) -> list[str]:
    """The campaign's countable record as fixed, sorted output lines.

    Reads only what the toolkit writes or defines: `work.md`'s Nodes table,
    each `nodes/<node>/reviews/round-<N>/status.json`, each archived child run
    `validation/<node>/state/<run>/` (`run.json` and `events.jsonl`: the
    controller's own state, not its Markdown report), the open
    `validation/<node>/campaign/cm-question.md` and archived
    `cm-question-<N>.md` and, with `repo` and `diff` (a `BASE..HEAD` range of
    two revisions), `git diff --numstat` of the range. A review round with no
    `status.json` (a `review-plan` that failed or was killed) and a run with
    no timestamped event are counted and shown `untimed`, and left out of the
    time totals. Anything else unreadable or malformed raises PmError naming
    the file: a count is never a silent zero. Diff files with line counts are
    the text files; a binary file has none and is counted on its own line.
    """
    lines: list[str] = []

    kinds: dict[str, int] = {}
    for kind in _node_kinds(dossier / "work.md"):
        kinds[kind] = kinds.get(kind, 0) + 1
    lines.append(f"nodes total {sum(kinds.values())}")
    lines.extend(f"nodes kind {kind} {kinds[kind]}" for kind in sorted(kinds))

    rounds = total = untimed_total = 0
    for reviews in sorted((dossier / "nodes").glob("*/reviews")):
        round_dirs = [path for path in reviews.glob("round-*") if path.is_dir()]
        timed = [seconds for path in round_dirs if (seconds := _round_seconds(path / "status.json")) is not None]
        untimed = len(round_dirs) - len(timed)
        rounds, total, untimed_total = rounds + len(round_dirs), total + sum(timed), untimed_total + untimed
        lines.append(f"review rounds {reviews.parent.name} {len(round_dirs)} {_duration(sum(timed))} untimed {untimed}")
    lines.append(f"review rounds total {rounds} {_duration(total)} untimed {untimed_total}")

    by_status: dict[str, int] = {}
    runs = steers = total = untimed_total = 0
    for run_dir in sorted((dossier / "validation").glob("*/state/*")):
        if run_dir.is_symlink() or not run_dir.is_dir():
            continue
        status, stop, seconds, run_steers = _run_state(run_dir)
        by_status[status] = by_status.get(status, 0) + 1
        runs, steers, total = runs + 1, steers + run_steers, total + (seconds or 0)
        untimed_total += seconds is None
        lines.append(
            f"run {run_dir.parts[-3]} {run_dir.name} status {status} steers/relaunches {run_steers} "
            f"{'untimed' if seconds is None else _duration(seconds)} stop {stop}"
        )
    lines.append(f"runs total {runs} steers/relaunches {steers} {_duration(total)} untimed {untimed_total}")
    lines.extend(f"runs status {status} {by_status[status]}" for status in sorted(by_status))

    questions: dict[str, int] = {}
    for path in (dossier / "validation").glob("*/campaign/cm-question*.md"):
        if _QUESTION_RE.fullmatch(path.name) and path.is_file():
            questions[path.parts[-3]] = questions.get(path.parts[-3], 0) + 1
    lines.extend(f"cm questions {node} {questions[node]}" for node in sorted(questions))
    lines.append(f"cm questions total {sum(questions.values())}")

    if repo is not None and diff is not None:
        lines.extend(_diff_counts(repo, diff))
    return lines


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PmError(f"cannot read {path}: {exc}") from exc


def _duration(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    return f"seconds {seconds} = {hours}h {remainder // 60}m {remainder % 60}s"


def _node_kinds(path: Path) -> list[str]:
    """The Kind cell of every row of `work.md`'s Nodes table, which needs Id and Kind columns.

    A Kind outside the template's three and an Id that two rows share are
    refused: the counts would otherwise quote a typo or a repeated node.
    """
    text = _read(path)
    heading = _NODES_HEADING_RE.search(text)
    if heading is None:
        raise PmError(f"{path} has no '## Nodes' heading")
    following = _HEADING_RE.search(text, heading.end())
    lines = text[heading.end() : following.start() if following else len(text)].splitlines()
    tables = [table for table in _tables(lines) if {"Id", "Kind"} <= {cell.strip() for cell in _cells(lines[table[0]])}]
    if len(tables) != 1:
        raise PmError(f"{path}: the '## Nodes' section needs exactly one table with Id and Kind columns")
    first, last = tables[0]
    header = [cell.strip() for cell in _cells(lines[first])]
    id_column, kind_column = header.index("Id"), header.index("Kind")
    kinds, ids = [], set()
    for line in lines[first + 2 : last + 1]:
        cells = _cells(line)
        if len(cells) <= max(id_column, kind_column) or not cells[id_column].strip() or not cells[kind_column].strip():
            raise PmError(f"{path}: a Nodes row has no Id or Kind: {line.strip()!r}")
        node, kind = cells[id_column].strip(), cells[kind_column].strip()
        if kind not in _KINDS:
            raise PmError(f"{path}: node {node!r} has Kind {kind!r}, not one of {', '.join(_KINDS)}")
        if node in ids:
            raise PmError(f"{path}: two Nodes rows have the Id {node!r}")
        ids.add(node)
        kinds.append(kind)
    return kinds


def _utc(path: Path, stamp: object) -> datetime:
    try:
        return datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise PmError(f"{path}: {stamp!r} is not a YYYY-MM-DDTHH:MM:SSZ timestamp") from None


def _round_seconds(path: Path) -> int | None:
    """Seconds between `started_at` and `finished_at` in a review round's `status.json`, or None when it has none.

    A round directory without the file, or a file whose `finished_at` is null, is what a
    `review-plan` that failed or was interrupted leaves (`plan_review` treats it as a round);
    a file that cannot be read, or a timestamp that is not one, is refused.
    """
    if not path.exists():
        return None
    try:
        status = json.loads(_read(path))
    except ValueError as exc:
        raise PmError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(status, dict):
        raise PmError(f"{path} is not a JSON object")
    if status.get("finished_at") is None:
        return None
    seconds = int((_utc(path, status.get("finished_at")) - _utc(path, status.get("started_at"))).total_seconds())
    if seconds < 0:
        raise PmError(f"{path}: finished_at is before started_at")
    return seconds


def _run_state(run_dir: Path) -> tuple[str, str, int | None, int]:
    """(status, stop reason, seconds or None, steers/relaunches) of an archived child run.

    Read from the controller's `run.json` and `events.jsonl`: the same figures
    `render_run_report` prints, without the Markdown table, whose cells a `|`
    in a slice title or decision shifts. Seconds span the first to the last
    parseable event timestamp, rounded as `state.run_elapsed` does, and are
    None when no event carries one.
    """
    path = run_dir / "run.json"
    try:
        data = json.loads(_read(path))
    except ValueError as exc:
        raise PmError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or "status" not in data or not isinstance(data.get("slices"), list):
        raise PmError(f"{path} is not a JSON object with a status and a slices list")
    attempts = [entry.get("attempts", 0) if isinstance(entry, dict) else None for entry in data["slices"]]
    if not all(isinstance(count, int) and not isinstance(count, bool) for count in attempts):
        raise PmError(f"{path}: a slice has no integer attempts")
    try:
        events = state_mod.read_events(run_dir)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise PmError(f"cannot read {run_dir / 'events.jsonl'}: {exc}") from exc
    stamps = [stamp for event in events if (stamp := state_mod.parse_event_ts(event.get("ts"))) is not None]
    seconds = int(round((max(stamps) - min(stamps)).total_seconds())) if stamps else None
    return str(data["status"]), str(data.get("stop_reason")), seconds, sum(attempts)


def _diff_counts(repo: Path, diff: str) -> list[str]:
    """Totals and per-extension counts of `git diff --numstat` of a `BASE..HEAD` range, a rename shown as a delete and an add.

    The range is two revisions, each resolved to a commit before git sees it, so a single
    revision (which would count the working tree) or an option-shaped value is refused.
    """
    base, dots, head = diff.partition("..")
    if not dots or not base or not head or base.startswith("-") or head.startswith("-"):
        raise PmError(f"--diff must be BASE..HEAD, two revisions that do not start with '-', not {diff!r}")
    ids = []
    for endpoint in (base, head):
        try:
            ids.append(git_ops.git(repo, "rev-parse", "--verify", "--end-of-options", f"{endpoint}^{{commit}}"))
        except PmError as exc:
            raise PmError(f"--diff endpoint {endpoint!r} is not a commit in {repo}: {exc}") from exc
    raw = git_ops.git_bytes(repo, "diff", "--numstat", "-z", "--no-renames", *ids)
    if raw is None:
        raise PmError(f"git diff --numstat {ids[0]} {ids[1]} failed in {repo}")
    files = added = deleted = binary = 0
    by_extension: dict[str, list[int]] = {}
    for record in raw.decode("utf-8", errors="surrogateescape").split("\0")[:-1]:
        plus, minus, name = (record.split("\t", 2) + ["", ""])[:3]
        if plus == minus == "-":
            binary += 1
            continue
        if not (plus.isdigit() and minus.isdigit() and name):
            raise PmError(f"git diff --numstat {diff} gave an unreadable record: {record!r}")
        row = by_extension.setdefault(Path(name).suffix or "(none)", [0, 0, 0])
        row[0] += 1
        row[1] += int(plus)
        row[2] += int(minus)
        files, added, deleted = files + 1, added + int(plus), deleted + int(minus)
    return [
        f"diff files {files} added {added} deleted {deleted}",
        *(
            f"diff extension {ext} files {row[0]} added {row[1]} deleted {row[2]}"
            for ext, row in sorted(by_extension.items())
        ),
        f"diff binary files {binary}",
    ]
