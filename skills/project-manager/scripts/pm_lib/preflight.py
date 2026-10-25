"""`campaign-preflight`: the mechanical half of a campaign's reconcile.

Everything a Campaign Manager (CM) or an owner can check about a campaign
before any session exists, read from the dossier's `brief.md` and the
product repository, with no run and no token. The CM runs it first at every
reconcile, with the owner's digest; the owner runs it before the first
launch. Each check passes, fails with what to do, or is a note for a human
(harness trust cannot be checked from here). A failure is a question to the
owner, never something the CM fixes in the dossier. The pin lines it reads
are the ones `references/dossier-template.md` shows.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import PmError
from . import campaign
from . import git_ops
from . import sandbox

# A placeholder is any `<…>` outside a code span, or, inside one, an
# UPPER_CASE token or one with whitespace: `campaign/<id>/<node>` is campaign
# vocabulary, `<SPEND_LIMIT>` and `<spend limit>` are blanks the owner left.
ANGLE = re.compile(r"<[^<>\n]+>")
CODE_SPAN = re.compile(r"```.*?```|`[^`\n]*`", re.S)
SHA256 = re.compile(r"SHA-256 `([0-9a-f]{64})`")
BASE = re.compile(r"^\s*-\s+[^\n]*?campaign base: `([^`]+)` at `([0-9a-f]{7,40})`", re.IGNORECASE | re.M)
BACKTICKED = re.compile(r"`([^`]+)`")
# The owner's files: brief.md always; the others only when present (the
# brief's own sections say whether each is required).
OWNER_FILES = ("brief.md", "setup.sh", sandbox.DOSSIER_POLICY_NAME)


@dataclass
class Check:
    name: str
    passed: bool | None  # None: a note for a human, neither pass nor fail
    detail: str = ""


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


def campaign_preflight(
    *,
    dossier: Path,
    repo: Path,
    integration_branch: str,
    worktree: Path | None = None,
    digest: str | None = None,
    harnesses: tuple[str, ...] = (),
) -> list[Check]:
    checks: list[Check] = []

    def ok(name: str) -> None:
        checks.append(Check(name, True))

    def fail(name: str, detail: str) -> None:
        checks.append(Check(name, False, detail))

    def note(name: str) -> None:
        checks.append(Check(name, None))

    dossier = dossier.expanduser().resolve()
    brief_path = dossier / "brief.md"
    if not brief_path.is_file():
        raise PmError(f"no brief.md in {dossier}")
    brief = brief_path.read_text(encoding="utf-8", errors="replace")
    integrity = True  # false once the owner's files or the digest are in doubt

    # The dossier: its own git repository, with the owner's files committed
    # and unchanged. The CM's files (work.md, evidence.md, …) may be dirty.
    code, top, _ = git_ops.git_result(dossier, "rev-parse", "--show-toplevel")
    if code != 0 or Path(top.strip()).resolve() != dossier:
        fail(
            "dossier is its own git repository",
            f"git -C {dossier} init (the dossier is a repository of its own, outside the product)",
        )
        integrity = False
    else:
        ok("dossier is its own git repository")
        for name in OWNER_FILES:
            if name != "brief.md" and not (dossier / name).exists():
                continue
            if git_ops.git_result(dossier, "ls-files", "--error-unmatch", name)[0] != 0:
                fail(f"{name} is tracked in the dossier", f"git -C {dossier} add {name} and commit")
                integrity = False
            else:
                code, status, err = git_ops.git_result(dossier, "status", "--porcelain", "--", name)
                if code != 0:
                    fail(
                        f"{name} is committed and unchanged",
                        f"git status failed in the dossier: {err.strip() or 'unknown error'}",
                    )
                    integrity = False
                elif status.strip():
                    fail(
                        f"{name} is committed and unchanged",
                        f"{name} differs from the dossier's last commit; commit it, then take the digest again",
                    )
                    integrity = False
                else:
                    ok(f"{name} is committed and unchanged")

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
    if not head:
        fail(
            f"integration branch {integration_branch} exists in {repo}",
            "create it at the campaign base: git worktree add -b <branch> <checkout> <base>",
        )
    else:
        ok(f"integration branch {integration_branch} at {head[:12]}")
        if git_ops.git_result(repo, "cat-file", "-e", f"{head}:HANDOFF.md")[0] == 0:
            fail(
                "integration head does not track HANDOFF.md",
                "launch-child refuses a base that tracks HANDOFF.md; untrack it on the integration branch",
            )
        else:
            ok("integration head does not track HANDOFF.md")

    base_match = BASE.search(brief)
    if not base_match:
        fail("brief pins a campaign base", "add `- Campaign base: `<branch>` at `<SHA>`` under Pinned references")
    else:
        base_ref = base_match.group(2)
        code, base, _ = git_ops.git_result(repo, "rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}")
        base = base.strip() if code == 0 else ""
        if not base:
            fail(f"campaign base {base_ref} resolves", "the pinned base is not a commit in the product repository")
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
        if not wt.is_dir():
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

    # The protected-paths policy file, as committed at the integration head.
    policy_text = _pin_line(brief, "Protected-paths policy file")
    policy_path = BACKTICKED.search(policy_text.split("SHA-256")[0]) if policy_text else None
    policy_sha = SHA256.search(policy_text) if policy_text else None
    if not policy_path or not policy_sha:
        fail("brief pins the policy file", "write `- Protected-paths policy file: `<path>`, SHA-256 `<digest>``")
    elif head:
        blob = git_ops.git_bytes(repo, "cat-file", "blob", f"{head}:{policy_path.group(1)}")
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
                "the committed file differs from the brief's SHA-256; a changed policy is a question to the owner",
            )

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
                f"its SHA-256 is {sha256_of(setup_path)}; a changed script is a question to the owner",
            )
            integrity = False
        elif not setup_path.read_text(encoding="utf-8", errors="replace").strip():
            fail("setup.sh is not blank", "launch-child refuses a blank script")
        else:
            ok("setup.sh matches its pin")
            if not head:
                fail("setup.sh trial", "no integration head to run it against")
            elif not integrity:
                fail(
                    "setup.sh trial",
                    "skipped: the brief or the owner's files failed their checks above, and an unverified script is never run",
                )
            else:
                checks.append(_trial_setup(repo, head, setup_path))
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
                    f"its SHA-256 is {policy_sha256}; a changed policy is a question to the owner",
                )
                integrity = False
            else:
                ok("sandbox.json matches its pin")
                try:
                    _policy, warnings = sandbox.load_base_policy(policy_file)
                except PmError as exc:
                    fail(
                        "sandbox.json is a valid sandbox policy",
                        f"{exc}; the owner corrects the policy, commits it and re-pins its SHA-256",
                    )
                else:
                    ok("sandbox.json is a valid sandbox policy")
                    for warning in warnings:
                        note(warning)

    # The node worktree parent.
    parent_text = _pin_line(brief, "Node worktree parent", 0)
    parent = BACKTICKED.search(parent_text) if parent_text else None
    if not parent:
        fail("brief names the node worktree parent", "write `- Node worktree parent: `<absolute dir>``")
    else:
        parent_dir = Path(parent.group(1)).expanduser()
        if not parent_dir.is_absolute():
            fail(f"node worktree parent {parent_dir} is absolute", "name the directory by its absolute path")
        elif parent_dir.is_dir() and os.access(parent_dir, os.W_OK):
            ok(f"node worktree parent {parent_dir} exists and is writable")
        else:
            fail(f"node worktree parent {parent_dir} exists and is writable", f"mkdir -p {parent_dir}")
        note(
            f"trust: every child harness must open a new worktree under {parent_dir} without a trust dialog; the owner checks this once by hand"
        )

    # Tools on this machine.
    ok("tmux on PATH") if shutil.which("tmux") else fail("tmux on PATH", "install tmux")
    if sandbox_pinned:
        try:
            ok(f"srt {sandbox.srt_version()} on PATH")
        except PmError as exc:
            fail("srt on PATH", f"{exc}; the brief pins a sandbox, and launch-child refuses it without srt")
    for harness in harnesses:
        ok(f"harness {harness} on PATH") if shutil.which(harness) else fail(
            f"harness {harness} on PATH", "install it or change the seats"
        )
    return checks


def _trial_setup(repo: Path, head: str, setup_path: Path) -> Check:
    """Run the setup script in a throwaway detached worktree of the integration
    head with the code `launch-child --setup` uses, then remove the worktree.
    The log is read before removal, so a failure carries its tail."""
    name = "setup.sh runs in a fresh worktree and leaves it clean"
    temp = Path(tempfile.mkdtemp(prefix="pm-preflight-"))
    trial = temp / "worktree"
    log = temp / "setup.log"
    try:
        code, _, err = git_ops.git_result(repo, "worktree", "add", "--detach", "-q", str(trial), head)
        if code != 0:
            return Check(name, False, f"could not add a trial worktree: {err.strip()}")
        try:
            campaign.run_setup_script(setup_path, trial, log=log, left_behind="(trial worktree removed)")
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
