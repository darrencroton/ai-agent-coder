#!/usr/bin/env python3
"""Check every composed harness argv against the installed CLIs' own `--help`.

Why this exists
---------------
`test_profiles.py` asserts composed argv as *strings*. That is the right unit
test, but it cannot notice that a flag does not exist. A codex resume shape
frozen from documentation therefore survived five slices of review before
anyone fed it to the real CLI: `codex exec resume` rejects `--sandbox`, `-C`,
and `--add-dir`, so `finalize --steer` against a codex Developer could never
run at all. This script is the check that would have caught it on day one.

It is deliberately **not** a pytest test. The suite guarantees "nothing
skipped" — the last `skipUnless` was removed when the tmux tests went — and a
CLI-dependent test would either reintroduce a skip or make CI non-hermetic.
Running this is an operator/maintainer step, not a CI step.

What it does
------------
For every harness, and for every command PM can compose (developer launch,
reviewer launch, and resume), it extracts the flag-looking tokens from the argv
that `pm_lib.profiles` actually produces and asserts each one appears in the
help text of the exact subcommand that will receive it. A harness that is not
installed is reported and skipped without failing the run.

For the three harnesses PM cannot tell a session id at launch (codex,
opencode, qwen), it additionally checks that the CLI's session store is still
where `slice_ops` looks for it. That is the other shape frozen from
observation rather than contract: an upgrade that relocates the store costs PM
its `finalize --steer` path, and today that is discovered mid-run rather than
here.

Usage
-----
    python3 skills/project-manager/scripts/verify_harness_argv.py
    python3 skills/project-manager/scripts/verify_harness_argv.py --harness codex

Exit status is 0 when every flag of every installed harness was found, 1 when
any flag is not advertised by the receiving command's help, and 2 when a CLI
could not be queried reliably.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from pm_lib import PmError  # noqa: E402
from pm_lib import profiles  # noqa: E402
from pm_lib import slice_ops  # noqa: E402

# Harnesses that cannot be told their session id at launch, so PM must find it
# afterwards by reading the CLI's own session store. claude and copilot are
# absent deliberately: PM sets their id at launch, so they have no store to
# drift. The probe reuses slice_ops' own path helpers rather than restating
# them — a copy here could agree with the docs and disagree with the code.
#
# `glob` and `projections` are what the runtime readers actually consume, kept
# in the shape they consume them: a zero-row projection checks the columns the
# correlation query names, not merely that a table of that name survives.
# `per_repo` marks a store scoped to the working directory rather than the
# user, so absent means "never used here", not "moved", and is never breakage.
_SESSION_STORES: dict[str, dict[str, object]] = {
    "codex": {"kind": "dir", "glob": "**/*.jsonl", "per_repo": False},
    "qwen": {"kind": "dir", "glob": "*.jsonl", "per_repo": True},
    "opencode": {
        "kind": "sqlite",
        "per_repo": False,
        "projections": (
            "SELECT id, directory, time_created FROM session LIMIT 0",
            "SELECT data, session_id, time_created, id FROM part LIMIT 0",
        ),
    },
}

# The help invocation for the subcommand each composed argv actually targets.
# Getting this per-subcommand is the whole point: `codex exec` accepts flags
# that `codex exec resume` rejects, and only a per-subcommand check sees that.
_HELP_COMMANDS: dict[tuple[str, str], list[str]] = {
    ("codex", "launch"): ["codex", "exec", "--help"],
    ("codex", "resume"): ["codex", "exec", "resume", "--help"],
    ("claude", "launch"): ["claude", "--help"],
    ("claude", "resume"): ["claude", "--help"],
    ("copilot", "launch"): ["copilot", "--help"],
    ("copilot", "resume"): ["copilot", "--help"],
    ("opencode", "launch"): ["opencode", "run", "--help"],
    ("opencode", "resume"): ["opencode", "run", "--help"],
    ("qwen", "launch"): ["qwen", "--help"],
    ("qwen", "resume"): ["qwen", "--help"],
}

_REPO = Path("/repo")
_GIT_DIR = _REPO / ".git"


def _flags(argv: list[str]) -> list[str]:
    """The flag-looking tokens of a composed argv, normalized for lookup.

    An equals-style token (`--resume=<id>`, as copilot's resume uses) is
    normalized to its flag half rather than skipped: skipping it would let that
    flag disappear from the CLI while this script still reported success, which
    is worse than not checking at all.

    `-c key="value"` pairs are config overrides whose *key* is validated by the
    harness's config loader, not by its argument parser, so only the `-c` itself
    is checked here — a `-c` value never starts with `-`, so it is excluded
    naturally. PM composes no negative-number arguments, so a prefix test is
    sufficient and honest.
    """
    return sorted({token.split("=", 1)[0] for token in argv if token.startswith("-")})


def _help_text(command: list[str]) -> str:
    # A timeout or non-zero exit is inconclusive, not evidence of a missing flag.
    result = subprocess.run(command, check=False, text=True, capture_output=True, timeout=180)
    result.check_returncode()
    return (result.stdout or "") + (result.stderr or "")


def _mentions(help_text: str, flag: str) -> bool:
    return re.search(rf"(?<![-\w]){re.escape(flag)}(?![-\w])", help_text) is not None


_SESSION_ID = "00000000-0000-0000-0000-000000000000"


def _composed(harness: str) -> list[tuple[str, list[str]]]:
    """Every argv PM can compose for `harness`, labelled by which help applies.

    The optional model and effort flags are composed too, not just the bare
    command: they are exactly the kind of per-harness flag that gets frozen from
    a docs table and never executed (`--variant`, `--effort`,
    `-c model_reasoning_effort`), so omitting them would leave the biggest hole
    this script exists to close. A harness that fails closed on effort (qwen)
    raises here — that is the *correct* behaviour under test elsewhere, not a
    flag defect, so the variant is simply skipped.
    """
    git_dir = _GIT_DIR if harness == "codex" else None
    out: list[tuple[str, list[str]]] = []
    for mode in ("developer", "reviewer"):
        # Separate variants keep qwen's expected effort refusal from hiding its
        # valid model flag. A base or combined variant adds no flag coverage.
        for model, effort in (("a-model", None), (None, "high")):
            try:
                out.append((
                    "launch",
                    profiles.compose_headless_command(
                        harness, "POINTER", mode=mode, repo=_REPO, model=model, effort=effort,
                        session_id=_SESSION_ID, git_access_dir=git_dir,
                    ),
                ))
            except PmError:
                if effort is None:
                    # Nothing but an effort request is allowed to fail closed.
                    raise
                continue
    # Codex is the only harness taking an optional git-access dir; check the
    # resume shape both with and without it, since the override is conditional.
    for access in ({"git_access_dir": git_dir}, {}) if git_dir else ({},):
        out.append((
            "resume",
            profiles.compose_resume_command(
                harness, "CORRECTION", session_id=_SESSION_ID, repo=_REPO, **access
            ),
        ))
    return out


def _store_path(harness: str) -> Path:
    if harness == "codex":
        return slice_ops._codex_sessions_root()
    if harness == "opencode":
        return slice_ops._opencode_session_db()
    return slice_ops._qwen_chats_root(Path.cwd())


def probe_session_store(harness: str) -> tuple[bool, list[str]]:
    """Check that the session store PM correlates against is still usable.

    PM binds a resume to the turn that produced it, and codex/opencode/qwen
    only reveal their id after launch, so an upgrade that moves or reshapes the
    store costs PM its correction path — discovered at `finalize --steer`,
    mid-run, after the Developer has already done the work.

    The guarantee is deliberately narrow, like the flag check above: **the
    store exists and still answers the shape the runtime readers query** — the
    record glob for a directory store, a zero-row run of the real projections
    for opencode. It cannot verify how a *value* inside a record is encoded,
    and that is precisely the gap that bites: the one correlation bug this
    round actually hit was a stored prompt gaining a literal quote layer. A
    pass means "not obviously broken", never "resume works".

    Nothing unexercised is ever reported as verified: an empty store, and a
    `per_repo` store absent because the harness has not run in this directory,
    are both NOT CHECKED rather than ok or broken.
    """
    spec = _SESSION_STORES[harness]
    path = _store_path(harness)
    if not path.exists():
        if spec["per_repo"]:
            return True, [
                f"{harness} store: NOT CHECKED - no store for this working directory ({path})",
                "    Per-repository store; run once from a repo where this harness has worked.",
            ]
        return False, [
            f"{harness} store: MISSING - {path}",
            "    PM cannot correlate a launch here; finalize --steer would fail closed.",
        ]

    if spec["kind"] == "sqlite":
        import sqlite3

        if not path.is_file():
            return False, [f"{harness} store: NOT A DATABASE - {path}"]
        # The same encoded read-only URI the runtime reader builds, so a path
        # this probe accepts is one that reader can also open.
        try:
            connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        except (sqlite3.Error, ValueError) as exc:
            return False, [f"{harness} store: UNREADABLE - {path}: {exc}"]
        try:
            for projection in spec["projections"]:
                connection.execute(projection)
        except sqlite3.Error as exc:
            return False, [
                f"{harness} store: SCHEMA CHANGED - {path}: {exc}",
                "    The correlation query cannot run; finalize --steer would fail closed.",
            ]
        finally:
            connection.close()
        return True, [f"{harness} store: ok ({path}, correlation projections run)"]

    if not path.is_dir():
        return False, [f"{harness} store: NOT A DIRECTORY - {path}"]
    records = sum(1 for _ in path.glob(str(spec["glob"])))
    if not records:
        return True, [f"{harness} store: NOT CHECKED - no {spec['glob']} records under {path}"]
    return True, [f"{harness} store: ok ({path}, {records} record(s) matching {spec['glob']})"]


def verify(harness: str) -> tuple[bool, bool, list[str]]:
    """Returns (ok, checked, lines).

    `ok` is False only on a genuine missing flag. `checked` is False when the
    harness was skipped for not being installed — the caller must not report a
    skipped harness as verified.
    """
    lines: list[str] = []
    executable = profiles.HARNESS_PROFILES[harness]["executable"]
    if shutil.which(executable) is None:
        return True, False, [f"{harness}: SKIPPED - {executable} is not installed"]

    # Keyed by the help *command*, not by kind: harnesses without subcommands
    # (claude, copilot, qwen) map both kinds to the same `--help`, and invoking
    # a slow CLI twice for identical output is pure waste.
    help_cache: dict[tuple[str, ...], str] = {}
    # Check the union once per receiving command. This script verifies flag
    # presence, not argv combinations, so per-variant reports are redundant.
    grouped: dict[tuple[str, tuple[str, ...]], set[str]] = {}
    for kind, argv in _composed(harness):
        help_command = _HELP_COMMANDS[(harness, kind)]
        grouped.setdefault((kind, tuple(help_command)), set()).update(_flags(argv))

    ok = True
    for (kind, help_command), flags in grouped.items():
        if help_command not in help_cache:
            help_cache[help_command] = _help_text(list(help_command))
        missing = [flag for flag in sorted(flags) if not _mentions(help_cache[help_command], flag)]
        if missing:
            ok = False
            lines.append(f"{harness} {kind}: NOT ADVERTISED {', '.join(missing)}")
            lines.append(f"    checked against: {' '.join(help_command)}")
        else:
            lines.append(f"{harness} {kind}: ok ({len(flags)} flags)")

    if harness in _SESSION_STORES:
        store_ok, store_lines = probe_session_store(harness)
        ok = ok and store_ok
        lines.extend(store_lines)
    return ok, True, lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--harness", choices=profiles.SUPPORTED_HARNESSES, help="check only this harness")
    args = parser.parse_args(argv)

    harnesses = [args.harness] if args.harness else list(profiles.SUPPORTED_HARNESSES)
    rejected: list[str] = []
    unverifiable: list[str] = []
    checked: list[str] = []
    skipped: list[str] = []
    stores_unchecked: list[str] = []
    for harness in harnesses:
        try:
            harness_ok, harness_checked, lines = verify(harness)
            if not harness_ok:
                rejected.append(harness)
            (checked if harness_checked else skipped).append(harness)
        except (PmError, subprocess.SubprocessError, OSError) as exc:
            # "Could not ask the CLI" is NOT "the CLI rejects this flag". A
            # verification tool that conflates the two cries wolf, and a tool
            # that cries wolf stops being read.
            unverifiable.append(harness)
            detail = str(exc)
            if isinstance(exc, subprocess.CalledProcessError):
                cli_output = (exc.stderr or exc.stdout or "").strip()
                if cli_output:
                    detail += f": {cli_output}"
            lines = [f"{harness}: COULD NOT VERIFY - {type(exc).__name__}: {detail}"]
        if any("store: NOT CHECKED" in line for line in lines):
            stores_unchecked.append(harness)
        for line in lines:
            print(line)

    print()
    if rejected:
        print(f"RESULT: composed flags not advertised by CLI help, or session store missing: {', '.join(rejected)}")
        return 1
    if unverifiable:
        print(f"RESULT: INCONCLUSIVE - could not query: {', '.join(unverifiable)}. No flag mismatch was found "
              f"in the {len(checked)} harness(es) actually checked; re-run to verify the rest.")
        return 2
    # Never let a skipped harness read as a verified one.
    summary = f"RESULT: every composed flag exists for {len(checked)}/{len(harnesses)} harnesses"
    if skipped:
        summary += f" - NOT CHECKED (not installed): {', '.join(skipped)}"
    print(summary)
    # Reported separately so a store nobody could look at never reads as a
    # verified one, exactly as an uninstalled harness never reads as checked.
    if stores_unchecked:
        print(f"        session store NOT CHECKED (none for this directory): {', '.join(stores_unchecked)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
