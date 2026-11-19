"""`campaign-preflight`: the mechanical half of a campaign's reconcile.

Everything a Campaign Manager (CM) or an owner can check about a campaign
before any session exists, read from the dossier's `brief.md` and the
product repository, with no run and no token. The CM runs it first at every
reconcile, with the owner's digest; the owner runs it before the first
launch. Each check passes, fails with what to do, or is a note for a human
(harness trust cannot be checked from here). A failure is a question to the
owner, never something the CM fixes in the dossier. The pin lines it reads
are the ones `references/dossier-template.md` shows.

Owner mode (`owner=True`, `--owner`) is the owner's pre-launch reading of the
same checks. What is merely not done yet (the dossier's repository and first
commit, its changed owner files, the integration branch and checkout, the
node worktree parent and the results root, and `gc.packRefs false` when a
sandbox is pinned) is a `todo`, a `Check` that carries the action `--commit`
runs, instead of a failure; the checks that
read the integration head read the pinned base while the branch is absent;
and an uncommitted owner file does not put the owner's files in doubt. Every
other check, and every check without `owner`, is as strict as ever, and the
todos are appended in the order their actions must run, the dossier's commit
last. `read_launcher` reads the campaign launcher the owner fills, so the
owner types no path or harness.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from . import PmError
from . import campaign
from . import git_ops
from . import profiles
from . import sandbox

# A placeholder is any `<…>` outside a code span, or, inside one, an
# UPPER_CASE token or one with whitespace: `campaign/<id>/<node>` is campaign
# vocabulary, `<SPEND_LIMIT>` and `<spend limit>` are blanks the owner left.
ANGLE = re.compile(r"<[^<>\n]+>")
CODE_SPAN = re.compile(r"```.*?```|`[^`\n]*`", re.S)
SHA256 = re.compile(r"SHA-256 `([0-9a-f]{64})`")
BASE = re.compile(r"^\s*-\s+[^\n]*?campaign base: `([^`]+)` at `([0-9a-f]{7,40})`", re.IGNORECASE | re.M)
BACKTICKED = re.compile(r"`([^`]+)`")
# A pin line: `<name>` … SHA-256 `<digest>`, the name being the last code span
# before the digest. A list item opening "Previous" is history, not a pin
# (the rule `_pin_line` applies to the policy pin).
PIN_LINE = re.compile(r"`([^`\n]+)`[^`\n]*?SHA-256 `([0-9a-f]{64})`")
HISTORICAL_ITEM = re.compile(r"^\s*-\s+previous\b", re.I)
# The dossier template's `- Campaign toolkit: <checkout path> at `<commit>``.
TOOLKIT_PIN = re.compile(r"^\s*-\s+campaign toolkit:[^\n]*?`([0-9a-f]{7,40})`", re.I | re.M)
TOOLKIT_LINE = re.compile(r"^\s*-\s+campaign toolkit:", re.I | re.M)
# The owner's files: brief.md always; the others only when present (the
# brief's own sections say whether each is required).
OWNER_FILES = ("brief.md", "setup.sh", sandbox.DOSSIER_POLICY_NAME)


@dataclass
class Check:
    name: str
    passed: bool | None  # None: a note for a human, or a todo (see `action`); neither pass nor fail
    detail: str = ""  # a failure's fix; a todo's description of its action
    action: Callable[[], None] | None = None  # a todo: what `--commit` runs to close the gap


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def placeholders(brief: str) -> list[str]:
    found = set(ANGLE.findall(CODE_SPAN.sub("", brief)))
    for span in CODE_SPAN.findall(brief):
        for token in ANGLE.findall(span):
            inner = token[1:-1]
            if re.fullmatch(r"[A-Z][A-Z0-9_]*", inner) or re.search(r"\s", inner):
                found.add(token)
    return sorted(found)


def _pin_line(text: str, label: str, following: int = 2) -> str:
    """The list item `- <label>:` plus the next few lines (a pin may wrap);
    "" when no such item exists. The label must open the item, so a
    historical `- Previous <label>:` entry is not read as the pin."""
    lines = text.splitlines()
    pattern = re.compile(rf"^\s*-\s+{re.escape(label)}:", re.I)
    for index, line in enumerate(lines):
        if pattern.match(line):
            return "\n".join(lines[index : index + 1 + following])
    return ""


def _section(text: str, heading: str) -> str | None:
    match = re.search(rf"^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S | re.I)
    return match.group(1) if match else None


def _generic_pins(brief: str, dedicated: set[str]) -> list[tuple[str, str]]:
    """(name, SHA-256) for every pin line of the brief that no dedicated check
    reads: every `` `<name>` … SHA-256 `<digest>` `` line except the lines in
    `dedicated` (the policy file's, `setup.sh`'s and `sandbox.json`'s, which
    have their own checks) and a historical (`Previous …`) item."""
    pins: list[tuple[str, str]] = []
    for line in brief.splitlines():
        if line in dedicated or HISTORICAL_ITEM.match(line):
            continue
        match = PIN_LINE.search(line)
        if match:
            pins.append((match.group(1), match.group(2)))
    return pins


def _dedicated_pins(brief: str) -> set[str]:
    """The pin lines the dedicated checks read: the policy file's, and the first pin line of the
    `Worktree setup` and `Sandbox` sections (`setup.sh`'s and `sandbox.json`'s), each identified by
    its line, so the generic pass skips exactly those lines and checks every other pin."""
    lines: set[str] = set()
    policy_line = _pin_line(brief, "Protected-paths policy file", 0)
    if policy_line:
        lines.add(policy_line)
    for heading in ("Worktree setup", "Sandbox"):
        section = _section(brief, heading) or ""
        first = next((line for line in section.splitlines() if SHA256.search(line)), None)
        if first is not None:
            lines.add(first)
    return lines


def _pinned_file_bytes(name: str, dossier: Path, repo: Path, head: str) -> tuple[bytes, str] | None:
    """(content, where it was found) for a pinned file: the dossier first, then
    the integration head; None when neither holds it. A name that leaves the
    dossier is looked for at the head only."""
    candidate = (dossier / name).resolve()
    if candidate.is_relative_to(dossier) and candidate.is_file():
        return candidate.read_bytes(), "the dossier"
    if head:
        blob = git_ops.git_bytes(repo, "cat-file", "blob", f"{head}:{name}")
        if blob is not None:
            return blob, "the integration head"
    return None


def _tracking(dossier: Path, name: str) -> tuple[str, str]:
    """How `name` stands in the dossier's repository: "untracked", "changed"
    (differs from the last commit), "clean", or "error" with git's message.
    The name is a literal path, never a pathspec (`runner[1].sh` is not a glob)."""
    if git_ops.git_result(dossier, "--literal-pathspecs", "ls-files", "--error-unmatch", name)[0] != 0:
        return "untracked", ""
    code, status, err = git_ops.git_result(dossier, "--literal-pathspecs", "status", "--porcelain", "--", name)
    if code != 0:
        return "error", err.strip() or "unknown error"
    return ("changed" if status.strip() else "clean"), ""


def _listed_by_status(dossier: Path, name: str) -> bool:
    """Whether `git status` lists `name` as untracked or changed: an ignored file is not listed, so it is
    never a file a commit could take. A git failure counts as listed, for the commit to report."""
    code, status, _ = git_ops.git_result(dossier, "--literal-pathspecs", "status", "--porcelain", "--", name)
    return code != 0 or bool(status.strip())


def _dossier_pin_files(brief: str, dossier: Path) -> list[str]:
    """The generic pins that resolve to a file inside the dossier: owner files
    beside `brief.md` and `setup.sh`, committed with them."""
    inside = []
    for name, _pinned in _generic_pins(brief, _dedicated_pins(brief)):
        candidate = (dossier / name).resolve()
        if candidate.is_relative_to(dossier) and candidate.is_file():
            inside.append(name)
    return inside


def _first_commit_listing(dossier: Path, *, is_repository: bool) -> tuple[int, list[str], str]:
    """(git's exit code, the sorted file names, git's stderr) for the first
    commit: what `git add -A --dry-run` says it will stage, with the dossier's
    own `.gitignore` applied and nothing staged, plus what an earlier, failed
    first commit already staged (the dry run does not list it). Before `git
    init`, the dry run is against a scratch repository over the dossier's tree."""
    already_staged: set[str] = set()
    if is_repository:
        code, listing, err = git_ops.git_result(dossier, "add", "-A", "--dry-run")
        already_staged = set(git_ops.git_result(dossier, "diff", "--cached", "--name-only")[1].splitlines())
    else:
        with tempfile.TemporaryDirectory(prefix="pm-preflight-") as scratch:
            git_ops.git(Path(scratch), "init", "-q")
            code, listing, err = git_ops.git_result(
                dossier, "--git-dir", str(Path(scratch) / ".git"), "--work-tree", str(dossier), "add", "-A", "--dry-run"
            )
    added = {line[4:].strip("'") for line in listing.splitlines() if line.startswith("add ")}
    return code, sorted(added | already_staged), err


def _overlaps(first: Path, second: Path) -> bool:
    """Whether either path is, or lies inside, the other, as real paths."""
    one, two = Path(os.path.realpath(first)), Path(os.path.realpath(second))
    return one.is_relative_to(two) or two.is_relative_to(one)


# The actions a `todo` carries: each runs one step of `--commit` and raises
# PmError, carrying git's own message, when it fails.
def _init_dossier(dossier: Path) -> None:
    git_ops.git(dossier, "init", "-q")


def _create_branch(repo: Path, branch: str, base: str) -> None:
    git_ops.git(repo, "branch", branch, base)


def _add_checkout(repo: Path, checkout: Path, branch: str) -> None:
    git_ops.git(repo, "worktree", "add", "-q", str(checkout), branch)


def _make_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PmError(f"could not create {path}: {exc}") from exc


def _disable_ref_packing(repo: Path) -> None:
    git_ops.git(repo, "config", "gc.packRefs", "false")


def _commit_owner_files(dossier: Path, files: list[str] | None) -> None:
    """The dossier's owner commit. `files` None is the first commit, which
    takes everything the `.gitignore` leaves (`git add -A`); afterwards only
    the named owner files are staged and committed, by literal path, so nothing
    else the CM has staged or modified is swept in, and a file staged then
    restored is no commit. The identity is git's own."""
    if files is None:
        git_ops.git(dossier, "add", "-A")
        git_ops.git(dossier, "commit", "-q", "-m", f"Brief: {dossier.name}")
        return
    git_ops.git(dossier, "--literal-pathspecs", "add", "--", *files)
    if git_ops.git_result(dossier, "--literal-pathspecs", "diff", "--cached", "--quiet", "--", *files)[0] != 0:
        git_ops.git(
            dossier, "--literal-pathspecs", "commit", "-q", "-m", f"Brief: update {', '.join(files)}", "--", *files
        )


def campaign_preflight(
    *,
    dossier: Path,
    repo: Path,
    integration_branch: str,
    worktree: Path | None = None,
    digest: str | None = None,
    harnesses: tuple[str, ...] = (),
    owner: bool = False,
) -> list[Check]:
    checks: list[Check] = []

    def ok(name: str) -> None:
        checks.append(Check(name, True))

    def fail(name: str, detail: str) -> None:
        checks.append(Check(name, False, detail))

    def note(name: str) -> None:
        checks.append(Check(name, None))

    def todo(name: str, detail: str, action: Callable[[], None]) -> Check:
        """A gap `--commit` closes (owner mode only); not appended, so the caller places it."""
        return Check(name, None, detail, action)

    def pin_fix(actual: str, cm_text: str) -> str:
        """What to do about a pin mismatch: the owner's action, or the CM's question to the owner."""
        return f"update the pin in brief.md to `{actual}`, or restore the pinned file" if owner else cm_text

    def directory(label: str, path: Path) -> None:
        name = f"{label} {path} exists and is writable"
        if path.is_dir() and os.access(path, os.W_OK):
            ok(name)
        elif owner and not os.path.lexists(path):
            checks.append(todo(name, f"mkdir -p {path}", partial(_make_directory, path)))
        else:
            fail(name, f"mkdir -p {path}")

    dossier = dossier.expanduser().resolve()
    brief_path = dossier / "brief.md"
    if not brief_path.is_file():
        raise PmError(f"no brief.md in {dossier}")
    brief = brief_path.read_text(encoding="utf-8", errors="replace")
    integrity = True  # false once the owner's files or the digest are in doubt

    # The dossier: its own git repository, with the owner's files committed
    # and unchanged. The CM's files (work.md, evidence.md, …) may be dirty.
    # In owner mode a repository not made yet, a first commit not made yet and
    # owner files not yet committed are todos, and the last two do not put the
    # owner's files in doubt: nothing has been committed to be in doubt about.
    code, top, _ = git_ops.git_result(dossier, "rev-parse", "--show-toplevel")
    is_repository = code == 0 and Path(top.strip()).resolve() == dossier
    first_commit = False
    commit_todo: Check | None = None  # appended last: its action runs last
    if is_repository:
        ok("dossier is its own git repository")
        first_commit = owner and git_ops.git_head(dossier) is None
    elif owner and code != 0:
        checks.append(
            todo("dossier is its own git repository", f"git -C {dossier} init", partial(_init_dossier, dossier))
        )
        first_commit = True
    else:
        fail(
            "dossier is its own git repository",
            f"git -C {dossier} init (the dossier is a repository of its own, outside the product)",
        )
        integrity = False
    if first_commit:
        code, staged, err = _first_commit_listing(dossier, is_repository=is_repository)
        if code != 0:
            fail("the dossier's first commit can be staged", f"git add -A --dry-run failed: {err.strip()}")
        else:
            more = ", …" if len(staged) > 10 else ""
            commit_todo = todo(
                "dossier has a first commit",
                f"git add -A, then commit as `Brief: {dossier.name}`: {len(staged)} file(s): "
                f"{', '.join(staged[:10])}{more}",
                partial(_commit_owner_files, dossier, None),
            )
    elif is_repository:
        unsettled: list[str] = []
        for name in OWNER_FILES:
            if name != "brief.md" and not (dossier / name).exists():
                continue
            state, git_error = _tracking(dossier, name)
            if state == "untracked" and owner:
                unsettled.append(name)
            elif state == "untracked":
                fail(f"{name} is tracked in the dossier", f"git -C {dossier} add {name} and commit")
                integrity = False
            elif state == "error":
                fail(f"{name} is committed and unchanged", f"git status failed in the dossier: {git_error}")
                integrity = False
            elif state == "changed" and owner:
                unsettled.append(name)
            elif state == "changed":
                fail(
                    f"{name} is committed and unchanged",
                    f"{name} differs from the dossier's last commit; commit it, then take the digest again",
                )
                integrity = False
            else:
                ok(f"{name} is committed and unchanged")
        if owner:
            unsettled += [
                name
                for name in _dossier_pin_files(brief, dossier)
                if name not in unsettled and _listed_by_status(dossier, name)
            ]
            if unsettled:
                commit_todo = todo(
                    "dossier's owner files are committed and unchanged",
                    f"commit {', '.join(unsettled)} (staged and committed by path; nothing else is swept in)",
                    partial(_commit_owner_files, dossier, unsettled),
                )

    missing = placeholders(brief)
    if missing:
        fail("brief.md has no placeholders", "fill " + ", ".join(missing) + " in brief.md")
    else:
        ok("brief.md has no placeholders")

    actual_digest = sha256_of(brief_path)
    if digest is None:
        note(f"brief digest {actual_digest}: no owner digest given to compare (the CM always passes one)")
    elif digest.lower() == actual_digest:
        ok("brief.md matches the owner's digest")
    else:
        fail("brief.md matches the owner's digest", f"the brief's digest is {actual_digest}; stop and ask the owner")
        integrity = False

    # The product repository and its integration branch.
    try:
        repo = git_ops.resolve_repo(repo)
    except PmError as exc:
        fail("product repository", str(exc))
        return checks
    if dossier == repo or dossier.is_relative_to(repo):
        fail(
            "dossier is outside the product repository",
            "the dossier is a repository of its own; move it out of the product tree",
        )
        integrity = False
    else:
        ok("dossier is outside the product repository")
    code, head, _ = git_ops.git_result(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{integration_branch}")
    head = head.strip() if code == 0 else ""
    base_match = BASE.search(brief)
    base = ""
    if base_match:
        code, resolved, _ = git_ops.git_result(
            repo, "rev-parse", "--verify", "--quiet", f"{base_match.group(2)}^{{commit}}"
        )
        base = resolved.strip() if code == 0 else ""
    # The tree the head-reading checks below read: the head, or in owner mode
    # the base the absent branch will be created at.
    tree = head or (base if owner else "")
    # A name git would take as an option, or refuse, is no branch to schedule.
    name_code, _, name_error = (
        git_ops.git_result(repo, "check-ref-format", "--branch", integration_branch)
        if not head and owner
        else (0, "", "")
    )
    if name_code != 0:
        fail(
            f"integration branch {integration_branch} is a valid branch name",
            name_error.strip() or "git refuses it",
        )
    elif not head and owner and base:
        checks.append(
            todo(
                f"integration branch {integration_branch} exists in {repo}",
                f"git -C {repo} branch {integration_branch} {base[:12]} (the campaign base)",
                partial(_create_branch, repo, integration_branch, base),
            )
        )
    elif not head:
        fail(
            f"integration branch {integration_branch} exists in {repo}",
            "create it at the campaign base: git worktree add -b <branch> <checkout> <base>",
        )
    else:
        ok(f"integration branch {integration_branch} at {head[:12]}")
    if tree:
        if git_ops.git_result(repo, "cat-file", "-e", f"{tree}:HANDOFF.md")[0] == 0:
            fail(
                "integration head does not track HANDOFF.md",
                "launch-child refuses a base that tracks HANDOFF.md; untrack it on the integration branch",
            )
        else:
            ok("integration head does not track HANDOFF.md")

    if not base_match:
        fail("brief pins a campaign base", "add `- Campaign base: `<branch>` at `<SHA>`` under Pinned references")
    elif not base:
        fail(
            f"campaign base {base_match.group(2)} resolves", "the pinned base is not a commit in the product repository"
        )
    else:
        ok(f"campaign base {base[:12]} resolves")
        if head:
            if git_ops.git_result(repo, "merge-base", "--is-ancestor", base, head)[0] == 0:
                ok("integration head descends from the base")
            else:
                fail(
                    "integration head descends from the base",
                    f"{integration_branch} is not built on {base[:12]}; re-pin the base or reset the branch",
                )

    if worktree is not None:
        wt = worktree.expanduser()
        if owner and tree and not os.path.lexists(wt):
            # `git -C <repo> worktree add` reads a relative path against the repository, not this shell.
            wt = wt.absolute()
            checks.append(
                todo(
                    f"integration checkout {wt} exists",
                    f"git -C {repo} worktree add {wt} {integration_branch}",
                    partial(_add_checkout, repo, wt, integration_branch),
                )
            )
        elif not wt.is_dir():
            fail(f"integration checkout {wt} exists", f"git -C {repo} worktree add -b {integration_branch} {wt} <base>")
        else:
            common = git_ops.git_result(wt, "rev-parse", "--path-format=absolute", "--git-common-dir")[1].strip()
            repo_common = git_ops.git_result(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")[1].strip()
            if not common or Path(common).resolve() != Path(repo_common).resolve():
                fail(f"integration checkout {wt} belongs to {repo}", "it is a different repository or clone")
            else:
                on = git_ops.current_branch(wt)
                wt_head = git_ops.git_head(wt)
                if on == integration_branch and wt_head == head:
                    ok(f"integration checkout {wt} is on {integration_branch} at its head")
                else:
                    fail(
                        f"integration checkout {wt} is on {integration_branch} at its head",
                        f"it is on {on or 'a detached head'} at {(wt_head or '')[:12]}",
                    )
                dirty = git_ops.meaningful_status_lines(git_ops.git_status_text(wt))
                ok("integration checkout is clean") if not dirty else fail(
                    "integration checkout is clean", "; ".join(dirty[:5])
                )

    # The protected-paths policy file, as committed at the integration head (the base, in owner mode, until
    # the branch exists).
    policy_text = _pin_line(brief, "Protected-paths policy file")
    policy_path = BACKTICKED.search(policy_text.split("SHA-256")[0]) if policy_text else None
    policy_sha = SHA256.search(policy_text) if policy_text else None
    if not policy_path or not policy_sha:
        fail("brief pins the policy file", "write `- Protected-paths policy file: `<path>`, SHA-256 `<digest>``")
    elif tree:
        blob = git_ops.git_bytes(repo, "cat-file", "blob", f"{tree}:{policy_path.group(1)}")
        if blob is None:
            fail(
                f"policy file {policy_path.group(1)} is committed at the integration head",
                "commit it in the product repository",
            )
        elif hashlib.sha256(blob).hexdigest() == policy_sha.group(1):
            ok("policy file at the integration head matches its pin")
        else:
            fail(
                "policy file at the integration head matches its pin",
                pin_fix(
                    hashlib.sha256(blob).hexdigest(),
                    "the committed file differs from the brief's SHA-256; a changed policy is a question to the owner",
                ),
            )

    # The brief's product exclusions, read once: the setup trial applies them
    # as a launch does, and their own check is reported further down.
    exclusions: list[str] = []
    exclusions_error: str | None = None
    try:
        exclusions = sandbox.brief_product_exclusions(brief)
    except PmError as exc:
        exclusions_error = str(exc)

    # The owner's worktree setup script, if the brief names one.
    setup_section = _section(brief, "Worktree setup")
    if setup_section is None:
        fail(
            "brief has a Worktree setup section",
            "add `## Worktree setup` naming `setup.sh` with its SHA-256, or `none`",
        )
    elif "setup.sh" in setup_section:
        setup_path = dossier / "setup.sh"
        setup_pin = SHA256.search(setup_section)
        if not setup_path.is_file():
            fail("setup.sh is in the dossier", "the brief names it; commit it beside brief.md")
        elif not setup_pin:
            fail("setup.sh is pinned", "add its SHA-256 to the Worktree setup section")
        elif sha256_of(setup_path) != setup_pin.group(1):
            fail(
                "setup.sh matches its pin",
                f"its SHA-256 is {sha256_of(setup_path)}; "
                + pin_fix(sha256_of(setup_path), "a changed script is a question to the owner"),
            )
            integrity = False
        elif not setup_path.read_text(encoding="utf-8", errors="replace").strip():
            fail("setup.sh is not blank", "launch-child refuses a blank script")
        else:
            ok("setup.sh matches its pin")
            if not tree:
                fail("setup.sh trial", "no integration head to run it against")
            elif not integrity:
                fail(
                    "setup.sh trial",
                    "skipped: the brief or the owner's files failed their checks above, and an unverified script is never run",
                )
            elif exclusions_error is not None:
                fail(
                    "setup.sh trial",
                    "skipped: the brief's Exclusions cannot be read (see below), so the trial could not apply them "
                    "as a launch would",
                )
            else:
                checks.append(_trial_setup(repo, tree, setup_path, exclusions))
    elif "none" not in setup_section.lower():
        fail("Worktree setup names setup.sh or none", "the section is present but names neither")
    else:
        ok("Worktree setup: none")

    # The owner's sandbox policy, if the brief pins one. A pin is checked
    # against the file, and the file against what launch-child will refuse.
    sandbox_pinned = False
    try:
        sandbox_pin = sandbox.brief_sandbox_pin(brief)
    except PmError as exc:
        fail("brief's Sandbox section names sandbox.json or none", str(exc))
    else:
        if sandbox_pin is None:
            ok("sandbox: none")
        else:
            sandbox_pinned = True
            policy_file = dossier / sandbox.DOSSIER_POLICY_NAME
            read_error: str | None = None
            try:
                policy_sha256 = sha256_of(policy_file) if policy_file.is_file() else None
            except OSError as exc:
                policy_sha256, read_error = None, str(exc)
            if read_error is not None:
                fail(
                    "sandbox.json is readable",
                    f"it could not be read ({read_error}); make it readable to this user "
                    f"(chmod u+r {policy_file}), then run campaign-preflight again",
                )
                integrity = False
            elif policy_sha256 is None:
                fail("sandbox.json is in the dossier", "the brief's Sandbox section pins it; commit it beside brief.md")
            elif policy_sha256 != sandbox_pin:
                fail(
                    "sandbox.json matches its pin",
                    f"its SHA-256 is {policy_sha256}; "
                    + pin_fix(policy_sha256, "a changed policy is a question to the owner"),
                )
                integrity = False
            else:
                ok("sandbox.json matches its pin")
                try:
                    policy, warnings, _sha256 = sandbox.load_pinned_policy(policy_file, sandbox_pin)
                except PmError as exc:
                    fail(
                        "sandbox.json is a valid sandbox policy",
                        f"{exc}; the owner corrects the policy, commits it and re-pins its SHA-256",
                    )
                else:
                    ok("sandbox.json is a valid sandbox policy")
                    for warning in warnings:
                        note(warning)
                    # The rest of what launch-child refuses before creating
                    # anything; an absent srt is the `srt on PATH` check's.
                    if shutil.which("srt") is not None:
                        try:
                            sandbox.check_binaries(policy)
                        except PmError as exc:
                            fail("sandbox.json passes launch-child's srt checks", f"{exc}; launch-child refuses it")
                        else:
                            ok("sandbox.json passes launch-child's srt checks")
                    if platform.system() != "Darwin":
                        fail(
                            "sandboxing is supported on this host",
                            "sandboxing is implemented for macOS only; launch-child refuses a sandboxed launch here",
                        )

    # The product paths launch-child removes from every node worktree. One
    # absent from the head is probably a typo, but an excluded path may
    # legitimately not exist yet, so it is a note for a human.
    if exclusions_error is not None:
        fail(
            "brief's Exclusions product: lines name product paths",
            f"{exclusions_error}; launch-child refuses this brief",
        )
    else:
        ok(f"exclusions: {len(exclusions)} product path(s)" if exclusions else "exclusions: none")
        for path in exclusions:
            # The sandbox deny is a glob at any depth, so a short or common
            # name hides every same-named path inside the sandbox too.
            note(f"exclusions: a sandboxed child denies /**/{path}, every path of that name at any depth")
            if tree and git_ops.git_result(repo, "cat-file", "-e", f"{tree}:{path}")[0] != 0:
                note(
                    f"exclusions: product path {path} is not in the integration head's tree; a typo, or a path"
                    " that does not exist yet"
                )
    # The output directories stop-child --archive copies before it removes a
    # worktree: a line that does not parse would otherwise be found at the
    # first archive, after a node has run.
    try:
        output_dirs = sandbox.brief_output_directories(brief)
    except PmError as exc:
        fail("brief's Git-ignored output directories line names product directories", str(exc))
    else:
        if output_dirs is None:
            fail(
                "brief names its git-ignored output directories",
                "add `- Git-ignored output directories: <directories>` or `none` under Allowed paths; without it the"
                " archive copies nothing before a node worktree is removed",
            )
        else:
            ok(f"output directories: {', '.join(output_dirs)}" if output_dirs else "output directories: none")

    # Every other file the brief pins by SHA-256, in the dossier or at the head.
    for name, pinned in _generic_pins(brief, _dedicated_pins(brief)):
        found = _pinned_file_bytes(name, dossier, repo, tree)
        if found is None:
            fail(
                f"pinned file {name} is in the dossier or at the integration head",
                "the brief pins it by SHA-256 but it is in neither; commit it, or correct the brief",
            )
        elif hashlib.sha256(found[0]).hexdigest() != pinned:
            actual = hashlib.sha256(found[0]).hexdigest()
            fail(
                f"pinned file {name} matches its pin",
                f"its SHA-256 in {found[1]} is {actual}; "
                + pin_fix(actual, "a changed pinned file is a question to the owner"),
            )
        else:
            ok(f"pinned file {name} matches its pin (in {found[1]})")

    # The toolkit checkout, when the brief pins one.
    toolkit_pin = TOOLKIT_PIN.search(brief)
    if not toolkit_pin and not TOOLKIT_LINE.search(brief):
        note("brief does not pin the campaign toolkit: its checkout is unpinned, so a changed toolkit goes unseen")
    elif not toolkit_pin or len(toolkit_pin.group(1)) != 40:
        fail(
            "brief pins the campaign toolkit by its full commit",
            f"the Campaign toolkit line carries `{toolkit_pin.group(1) if toolkit_pin else 'no readable commit'}`; "
            "write the full 40-hex commit in backticks",
        )
    else:
        toolkit_head = git_ops.git_head(sandbox.toolkit_root())
        if toolkit_head == toolkit_pin.group(1):
            ok(f"toolkit checkout HEAD is its pinned commit {toolkit_head[:12]}")
        else:
            repin = (
                f"re-pin the Campaign toolkit line to `{toolkit_head}` after reading what changed, "
                "or check out the pinned commit"
                if owner and toolkit_head
                else "check out the pinned commit, or ask the owner to re-pin"
            )
            fail(
                "toolkit checkout is at its pinned commit",
                f"the toolkit is at {(toolkit_head or 'no readable commit')[:12]}, the brief pins "
                f"{toolkit_pin.group(1)[:12]}; {repin}",
            )

    # The node worktree parent, and the results root `stop-child --archive` writes each node's outputs to.
    parent_text = _pin_line(brief, "Node worktree parent", 0)
    parent = BACKTICKED.search(parent_text) if parent_text else None
    parent_dir: Path | None = None
    if not parent:
        fail("brief names the node worktree parent", "write `- Node worktree parent: `<absolute dir>``")
    else:
        parent_dir = Path(parent.group(1)).expanduser()
        if not parent_dir.is_absolute():
            fail(f"node worktree parent {parent_dir} is absolute", "name the directory by its absolute path")
        else:
            directory("node worktree parent", parent_dir)
        note(
            f"trust: a harness that asks per linked worktree (Claude Code does) shows a trust dialog for each new node"
            f" worktree under {parent_dir}; the CM asks the owner once, at its first reconcile, for permission to add"
            " those entries (references/sandbox.md Troubleshooting)"
        )

    results_text = _pin_line(brief, "Results root", 0)
    results = BACKTICKED.search(results_text) if results_text else None
    if not results:
        fail(
            "brief names the results root",
            "add `- Results root: `<absolute directory>`` under Allowed paths; stop-child --archive writes each "
            "node's outputs there",
        )
    else:
        results_dir = Path(results.group(1)).expanduser()
        if not results_dir.is_absolute():
            fail(f"results root {results_dir} is absolute", "name the directory by its absolute path")
        else:
            others = {"the dossier": dossier}
            if parent_dir is not None and parent_dir.is_absolute():
                others["the node worktree parent"] = parent_dir
            if worktree is not None:
                others["the integration checkout"] = worktree.expanduser()
            overlapped = [what for what, other in others.items() if _overlaps(results_dir, other)]
            if overlapped:
                fail(
                    f"results root {results_dir} is separate from the campaign's own directories",
                    f"it overlaps {', '.join(overlapped)}; name a directory outside all of them",
                )
            else:
                ok(f"results root {results_dir} is separate from the campaign's own directories")
            directory("results root", results_dir)

    if os.environ.get("VIRTUAL_ENV"):
        note(
            f"VIRTUAL_ENV={os.environ['VIRTUAL_ENV']} is set and every seat inherits it; "
            "start the CM from a shell without the product's virtualenv"
        )

    # Tools on this machine.
    ok("tmux on PATH") if shutil.which("tmux") else fail("tmux on PATH", "install tmux")
    if sandbox_pinned:
        try:
            ok(f"srt {sandbox.srt_version()} on PATH")
        except PmError as exc:
            fail("srt on PATH", f"{exc}; the brief pins a sandbox, and launch-child refuses it without srt")
        # A sandboxed child in a linked worktree may write only the literal files of its own
        # branch ref. A ref pack (host-side `git gc`) prunes the empty `refs/heads/<…>/` directory
        # that ref needs, and the child's next commit then fails; the repository's config is
        # shared by every worktree, so one setting covers the campaign. It does not stop an explicit
        # `git pack-refs` or a `git maintenance` pack-refs task.
        name = f"gc.packRefs is false in {repo}"
        action = f"git -C {repo} config gc.packRefs false"
        if git_ops.git_result(repo, "config", "--type=bool", "--get", "gc.packRefs")[1].strip() == "false":
            ok(name)
        elif owner:
            checks.append(
                todo(
                    name,
                    f"{action} (a ref pack removes the directory a sandboxed child's branch ref needs)",
                    partial(_disable_ref_packing, repo),
                )
            )
        else:
            fail(name, f"{action}; a ref pack removes the directory a sandboxed child's branch ref needs")
    for harness in harnesses:
        ok(f"harness {harness} on PATH") if shutil.which(harness) else fail(
            f"harness {harness} on PATH", "install it or change the seats"
        )
    if commit_todo is not None:
        checks.append(commit_todo)
    return checks


def _trial_setup(repo: Path, head: str, setup_path: Path, exclusions: list[str]) -> Check:
    """Run the setup script in a throwaway detached worktree of the integration
    head with the code `launch-child --setup` uses, then remove the worktree:
    the brief's product `exclusions` are removed by the same sparse checkout
    first, and an excluded path the script recreates fails the trial as it
    would the launch. The log is read before removal, so a failure carries
    its tail."""
    name = "setup.sh runs in a fresh worktree and leaves it clean"
    temp = Path(tempfile.mkdtemp(prefix="pm-preflight-"))
    trial = temp / "worktree"
    log = temp / "setup.log"
    left_behind = "(trial worktree removed)"
    try:
        code, _, err = git_ops.git_result(repo, "worktree", "add", "--detach", "-q", str(trial), head)
        if code != 0:
            return Check(name, False, f"could not add a trial worktree: {err.strip()}")
        try:
            campaign.apply_exclusions(trial, exclusions, left_behind=left_behind)
            campaign.run_setup_script(setup_path, trial, log=log, left_behind=left_behind)
            campaign.refuse_excluded_paths(trial, exclusions, left_behind=left_behind)
            result = Check(name, True)
        except PmError as exc:
            tail = " | ".join(campaign.log_tail(log, 10)) or "(no output)"
            result = Check(name, False, f"{exc}; log tail: {tail}")
    finally:
        # Only the trial is touched: a repo-wide `git worktree prune` would
        # also drop the registration of any node worktree whose volume is
        # not mounted at this moment.
        git_ops.git_result(repo, "worktree", "remove", "--force", str(trial))
        shutil.rmtree(temp, ignore_errors=True)
        if temp.exists() or str(trial) in git_ops.git_result(repo, "worktree", "list", "--porcelain")[1]:
            result = Check(
                name,
                False,
                f"the trial worktree {trial} could not be removed; remove it by hand (git worktree remove --force {trial})",
            )
    return result


# The campaign launcher (`references/campaign-mode.md`, *The campaign launcher*): `Key: value` lines.
LAUNCHER_PATHS = ("Dossier", "Product repo", "Created from")
LAUNCHER_SEATS = ("Child PM", "Child seats", "Task-node delegate", "Plan reviewer", "Acceptance auditor")
# A harness named on a seat line is a standalone word: `claude-sonnet-5-5` and `opencode-go/x` are model ids.
HARNESS_WORD = re.compile(rf"(?<![\w/-])(?:{'|'.join(map(re.escape, profiles.SUPPORTED_HARNESSES))})(?![\w/-])")


@dataclass(frozen=True)
class PreflightInputs:
    """Where the campaign's files are and which harnesses its seats use: the explicit options, or what
    `read_launcher` takes from the owner's launcher file in their place."""

    dossier: Path
    repo: Path  # the product repository to read: Product repo when it exists, else Created from
    worktree: Path | None  # the integration checkout: Product repo
    integration_branch: str
    harnesses: tuple[str, ...]


def _launcher_line(key: str) -> re.Pattern[str]:
    """The launcher's `<key>:` line: group 1 the key and its separator, group 2 the value."""
    return re.compile(rf"^([ \t]*{re.escape(key)}:[ \t]*)([^\r\n]*)", re.M)


def read_launcher(path: Path) -> PreflightInputs:
    """Read the paths, integration branch and seat harnesses from the launcher
    file `path` (the filled template, fence or no fence). A missing or
    repeated line, a value still holding `<…>`, a path that is not absolute,
    a seat line naming no harness, or a file inside the dossier (the CM writes
    the dossier, and the owner's digest must not sit where it writes) is a
    PmError naming it. The `Brief digest:` line must appear exactly once but
    may be blank or a placeholder: `--commit` fills it in."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PmError(f"cannot read the launcher {path}: {exc}") from exc

    def value(key: str, *, filled: bool = True) -> str:
        found = _launcher_line(key).findall(text)
        if len(found) != 1:
            raise PmError(f"the launcher {path} must have exactly one `{key}:` line, it has {len(found)}")
        result = found[0][1].strip()
        if filled and (not result or ANGLE.search(result)):
            raise PmError(f"the launcher {path}: fill in `{key}:` (it holds {result or 'nothing'!r})")
        return result

    value("Brief digest", filled=False)
    paths: dict[str, Path] = {}
    for key in LAUNCHER_PATHS:
        paths[key] = Path(value(key)).expanduser()
        if not paths[key].is_absolute():
            raise PmError(f"the launcher {path}: `{key}:` must be an absolute path, it is {paths[key]}")
    branch = value("Integration branch")
    harnesses: list[str] = []
    for key in LAUNCHER_SEATS:
        named = HARNESS_WORD.findall(value(key))
        if not named:
            supported = ", ".join(profiles.SUPPORTED_HARNESSES)
            raise PmError(f"the launcher {path}: `{key}:` names none of the supported harnesses ({supported})")
        harnesses += dict.fromkeys(name for name in named if name not in harnesses)
    dossier = paths["Dossier"]
    if Path(os.path.realpath(path)).is_relative_to(Path(os.path.realpath(dossier))):
        raise PmError(f"the launcher {path} lies inside the dossier {dossier}; keep it beside the dossier")
    product = paths["Product repo"]
    return PreflightInputs(
        dossier=dossier,
        repo=product if product.is_dir() else paths["Created from"],
        worktree=product,
        integration_branch=branch,
        harnesses=tuple(harnesses),
    )


def write_launcher_digest(path: Path, digest: str) -> None:
    """Replace the value on the launcher's one `Brief digest:` line and nothing else, byte for byte."""
    text = path.read_bytes().decode("utf-8")
    updated, count = _launcher_line("Brief digest").subn(lambda match: match.group(1) + digest, text)
    if count != 1:
        raise PmError(f"the launcher {path} must have exactly one `Brief digest:` line, it has {count}")
    path.write_bytes(updated.encode("utf-8"))
