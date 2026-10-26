"""The OS sandbox around a run's tmux server: `srt` policies and the server's life.

A sandboxed run's private tmux server runs in the foreground under Anthropic's
`sandbox-runtime` (`srt`), so every pane process it creates, and every
descendant of those, inherits the Seatbelt restrictions of the policy `srt`
was started with. This module is the toolkit's one seam onto that mechanism:

- the operator's base policy, loaded and refused when it defeats the boundary
  (`load_base_policy`; `load_pinned_policy` also returns the SHA-256 of the
  very bytes it parsed);
- the policies the toolkit derives from it for a worktree and branch: the
  server's effective policy, a Mode B reviewer's, and the operator's attach
  client's (`derive_policy`, `derive_reviewer_policy`, `derive_attach_policy`),
  the first two carrying the denies that keep a pane from redirecting the
  host's next git command (the repository's config and hooks, and the
  pointer files `commondir`, `gitdir` and `config.worktree`, denied in every
  checkout because git honours them in a plain one too), plus the refusal of
  a write grant reaching `srt`'s own installation or the launchers the host
  runs it through (`check_binaries`);
- the host-only control directory and its record (`control_dir`,
  `write_record`, `read_record`), and the per-run temp directory
  (`make_tmpdir`, `remove_tmpdir`);
- the server itself: started under `srt`, verified by two probes, stopped
  (`start_server`, `verify_server`, `stop_server`), and a one-shot command
  wrapped the same way (`wrap_command`); the one teardown both modes share
  (`tear_down`), whose structured `TeardownOutcome` they decide from;
- a campaign brief's sandbox pin, which `launch-child` and
  `campaign-preflight` both read (`find_brief`, `brief_sandbox_pin`);
- a Mode B run's server: the socket every command of a sandboxed run must
  be pointed at (`require_run_socket`), the three policies its control
  directory holds (`derive_run_policies`), the server `start-slice` ensures,
  rebuilding what `init` created when a `stop` removed it
  (`ensure_run_server`), the run end's teardown, retryable while anything
  is left (`teardown_run`, `sandbox_remains`, `sandbox_left`), a tokenless
  scavenge's (`scavenge_run`), and the state-free kill a scavenge falls
  back on (`kill_run_server`).

Paths in a policy are judged as `srt` resolves them: `~` and `~/` expanded,
symbolic links followed. A pattern entry (a string holding `*`, `?`, `[` or
`]`) is judged by the directory its fixed prefix names, as though it granted
that whole directory, which fails toward refusing. An entry written as
`{"path": ..., "literal": true}` is never a pattern, so every filesystem entry
the toolkit derives itself is written that way: a worktree under `work[1]`
must not also grant `work1`. Base-policy entries stay as the operator wrote
them.

A base policy is refused for a grant that is the filesystem root or the home
directory (equality only: every grant lies inside `/`, and grants inside the
home directory are normal), or that is, contains or lies inside the toolkit
checkout or the tmux socket directory; for a `network.allowUnixSockets` entry
that is, contains or lies inside the tmux socket directory (`srt` renders each
entry as a `subpath` connect rule, so it would let a pane drive the operator's
own unsandboxed tmux server); and, at `check_binaries`, for a write grant that
is or contains `srt`'s package, the real or the `PATH`-spelled `srt` and
`node`.

A grant is compared with a protected path by file identity (`st_dev`,
`st_ino`), not by spelling: on a case-insensitive volume `/users/me` is
`/Users/me`, and `srt` enforces it as such. A path that does not exist yet is
judged by its deepest existing ancestor plus the components below it, those
compared case-folded and NFC-normalized: the directory may exist, under
either spelling, by the time `srt` resolves the grant.

The socket path is granted under its real directory: `srt` resolves a path
only when it exists, the socket does not exist when `srt` starts, and
Seatbelt compares canonical paths, so the `/tmp/...` spelling of a socket
under `/private/tmp` would match nothing and the server could not create it.

Session creation and every other tmux call stay in `sessions`; the one tmux
contact here is the server itself (run under `srt`) and the
`display-message` that reads its pid. Nothing in this module scrubs or records
the inherited environment beyond removing `PM_RUN_TOKEN`. macOS only.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

from . import PmError
from . import git_ops
from . import ledger
from . import sessions
from . import state as state_mod

SRT_MISSING = "srt is required for a sandboxed run; install @anthropic-ai/sandbox-runtime"
SSL_CERT_FILE = Path("/etc/ssl/cert.pem")
CONTROL_ROOT_NAME = "pm-sandbox"
RECORD_NAME = "record.json"
TMPDIR_PREFIX = "pm-sandbox-"
PROBE_DIR_NAME = ".pm"
PROBE_FILE_NAME = "sandbox-probe"
BRIEF_NAME = "brief.md"
# The base policy's file name in a campaign dossier, beside `brief.md`.
DOSSIER_POLICY_NAME = "sandbox.json"
# A Mode B run's socket is this prefix and the run id; its control directory
# holds these files beside the record.
RUN_SOCKET_PREFIX = "pm-run-"
POLICY_NAME = "policy.json"
REVIEWER_POLICY_NAME = "reviewer-policy.json"
ATTACH_POLICY_NAME = "attach-policy.json"
SRT_LOG_NAME = "srt.log"

START_DEADLINE_SECONDS = 10.0
PROBE_TIMEOUT_SECONDS = 10.0
SRT_EXIT_WAIT_SECONDS = 5.0
POLL_SECONDS = 0.1
# A C `pid_t` is a signed 32-bit integer on macOS (and Linux).
_PID_MAX = 2**31 - 1

# `srt` grants every sandbox `/tmp/claude` (its shared TMPDIR) without being
# asked; a literal deny removes the grant, under both spellings because the
# directory may not exist yet when `srt` resolves the entry.
_SHARED_TMP_DENIES = ("/tmp/claude", "/private/tmp/claude")
# `srt`'s other implicit write grants, denied for the write-less attach client.
_IMPLICIT_GRANT_DENIES = ("~/.npm/_logs", "~/.claude/debug")
_PATTERN_SPLIT = re.compile(r"[*?\[\]]")
# A NUL, a control character or a lone surrogate: none names a usable path,
# and each breaks a later step (realpath, the OS call, a printed message).
_BAD_PATH_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")
# A brief's `## Sandbox` section and its pin, in the shapes
# `campaign-preflight` reads every other dossier pin in (`preflight._section`,
# `preflight.SHA256`); kept here because `preflight` imports `campaign`,
# which imports this module.
_SANDBOX_SECTION = re.compile(r"^##\s+Sandbox\s*$(.*?)(?=^##\s|\Z)", re.M | re.S | re.I)
_PIN_SHA256 = re.compile(r"SHA-256 `([0-9a-f]{64})`")
# `none` as the whole section, give or take one list marker, whitespace and case.
_NONE_ONLY = re.compile(r"\s*(?:(?:[-*+]|\d+[.)])\s+)?none\s*", re.I)

# `srt` processes this process started, by pid. The server outlives
# `start_server`, so its `Popen` is kept here rather than dropped while still
# running; `stop_server` reaps it through this when the same process stops it.
_STARTED: dict[int, subprocess.Popen] = {}


# --- Path entries --------------------------------------------------------------


def toolkit_root() -> Path:
    """The real path of the checkout holding `skills/project-manager`."""
    return Path(__file__).resolve().parents[4]


def _entry_text(entry: Any) -> tuple[str, bool]:
    """(path text, is literal) for a policy path entry: a string, or the
    `{"path": ..., "literal": true}` object `srt` accepts in its four
    filesystem lists. ValueError, its text completing "entry <entry> ...",
    for anything else and for a path holding a NUL, control or
    lone-surrogate character."""
    if isinstance(entry, str):
        text, literal = entry, False
    elif (
        isinstance(entry, dict)
        and set(entry) == {"path", "literal"}
        and entry["literal"] is True
        and isinstance(entry["path"], str)
    ):
        text, literal = entry["path"], True
    else:
        raise ValueError("is neither a path nor a {path, literal: true} object")
    if _BAD_PATH_CHARS.search(text):
        raise ValueError("holds a NUL, control or lone-surrogate character")
    return text, literal


def _expand_tilde(text: str) -> str:
    """`~` and `~/...` expanded as `srt` expands them; `~user` is not."""
    if text == "~":
        return str(Path.home())
    if text.startswith("~/"):
        return str(Path.home()) + text[1:]
    return text


def _is_absolute_entry(text: str) -> bool:
    """Whether `srt` resolves `text` without the working directory: it
    starts with `/`, or is `~` or `~/...` (a `~user` spelling is relative to
    `srt`, which expands only the caller's own `~`)."""
    return text.startswith("/") or text == "~" or text.startswith("~/")


def _resolve_entry(entry: Any) -> Path:
    """The real path a write entry reaches. A pattern is reduced to the
    directory of its fixed prefix (see the module docstring)."""
    text, literal = _entry_text(entry)
    # srt collapses repeated slashes; POSIX realpath keeps a leading `//`.
    expanded = re.sub(r"/+", "/", _expand_tilde(text))
    if not literal and _PATTERN_SPLIT.search(expanded):
        prefix = _PATTERN_SPLIT.split(expanded, maxsplit=1)[0]
        expanded = (prefix[:-1] if prefix.endswith("/") else os.path.dirname(prefix)) or "/"
    return Path(os.path.realpath(expanded))


def _write_grant(entry: Any) -> tuple[str, Path]:
    """(path text, resolved path) of a write entry. ValueError, its text
    completing "entry <entry> ...", when the entry is malformed or cannot be
    resolved."""
    text, _literal_entry = _entry_text(entry)
    try:
        return text, _resolve_entry(entry)
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot be resolved ({exc})") from None


def _fold(component: str) -> str:
    """`component` case-folded and NFC-normalized, so spellings a
    case-insensitive volume treats as one name compare equal."""
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", component).casefold())


def _anchor(path: Path) -> tuple[tuple[int, int] | None, tuple[str, ...]]:
    """`path`'s identity: (`st_dev`, `st_ino`) of its deepest existing
    ancestor (itself, when it exists) and the components below that, folded
    (see `_fold`). Two spellings of one file have the same identity; two
    paths that do not exist yet compare equal whenever a case-insensitive
    volume could make them one, which errs toward refusing."""
    for candidate in (path, *path.parents):
        try:
            status = os.stat(candidate)
        except OSError:
            continue
        return (status.st_dev, status.st_ino), tuple(_fold(part) for part in path.parts[len(candidate.parts) :])
    return None, tuple(_fold(part) for part in path.parts)


def _same(a: Path, b: Path) -> bool:
    """Whether two absolute paths name the same file (see `_anchor`)."""
    return a == b or _anchor(a) == _anchor(b)


def _relation(grant: Path, target: Path) -> str | None:
    """How a resolved write grant stands to `target`, by identity (see
    `_anchor`): "is", "is an ancestor of", "is inside", or None when the
    two are unrelated."""
    grant_identity, target_identity = _anchor(grant), _anchor(target)
    if grant == target or grant_identity == target_identity:
        return "is"
    if any(_anchor(parent) == grant_identity for parent in target.parents):
        return "is an ancestor of"
    if any(_anchor(parent) == target_identity for parent in grant.parents):
        return "is inside"
    return None


def _literal(path: Path | str) -> dict[str, Any]:
    return {"path": str(path), "literal": True}


def _real(path: Path | str) -> Path:
    return Path(os.path.realpath(path))


def _socket_grant(socket: str) -> str:
    """`sessions.socket_path(socket)` under its real directory (see the
    module docstring for why the unresolved spelling cannot be used)."""
    path = sessions.socket_path(socket)
    return str(_real(path.parent) / path.name)


def _socket_dir() -> Path:
    """The real tmux socket directory: the negative probe's ground."""
    return _real(sessions.socket_path("x").parent)


# The lists `srt` reads from a policy's two sections; each must be a list when present.
_POLICY_LISTS = (
    ("network", ("allowedDomains", "deniedDomains", "allowUnixSockets")),
    ("filesystem", ("denyRead", "allowRead", "allowWrite", "denyWrite")),
)


def _list_fault(policy: Any) -> str | None:
    """Why `policy`, its `network` or `filesystem` section, or one of
    `_POLICY_LISTS` has the wrong shape, or None; an absent section or list
    passes (the derivations add it)."""
    if not isinstance(policy, dict):
        return "the policy is not an object"
    for section_name, keys in _POLICY_LISTS:
        section = policy.get(section_name, {})
        if not isinstance(section, dict):
            return f"{section_name} is not an object"
        for key in keys:
            if key in section and not isinstance(section[key], list):
                return f"{section_name}.{key} is not a list"
    return None


def _check_lists(policy: Any) -> None:
    """PmError naming the fault `_list_fault` finds in `policy`, if any."""
    fault = _list_fault(policy)
    if fault:
        raise PmError(f"sandbox policy: {fault}")


def _write_entries(policy: dict[str, Any]) -> list[Any]:
    """`policy`'s `filesystem.allowWrite` entries; PmError from
    `_check_lists` first, so a malformed policy is refused here as everywhere."""
    _check_lists(policy)
    return policy.get("filesystem", {}).get("allowWrite", [])


# --- The base policy -------------------------------------------------------------


def load_base_policy(path: Path | str) -> tuple[dict[str, Any], list[str]]:
    """Load the operator's base policy; return `(policy, warnings)`.

    PmError, naming the file and the reason, for: an unreadable or non-JSON
    file, or a path that cannot name one; a top level that is not an object
    holding `network` and `filesystem` objects; `network.allowAllUnixSockets`,
    `allowAppleEvents` or `filesystem.disabled` set (each defeats the
    boundary outright); a network or filesystem list `srt` reads
    (`allowedDomains`, `deniedDomains`, `allowUnixSockets`, `denyRead`,
    `allowRead`, `allowWrite`, `denyWrite`) present but not a list; an
    `allowWrite` entry that is not a path entry, holds a NUL, control or
    lone-surrogate character, is relative, is the same file as `/` or the
    home directory (equality only: any grant is inside `/`, and grants inside
    the home directory are normal), or is, contains or lies inside
    `toolkit_root()` or the tmux socket directory, however spelled; a
    `network.allowUnixSockets` entry that is malformed or relative in the same
    ways, or is, contains or lies inside the tmux socket directory (a pane
    could then drive the operator's own unsandboxed tmux server).

    `warnings` holds one line for `enableWeakerNetworkIsolation` and one for
    `network.allowLocalBinding`, when set, each naming what the option opens;
    the caller prints them. The policy is returned as parsed: deriving from
    it never mutates it, and `srt` applies its own schema check on top.
    """
    policy, warnings, _sha256 = load_pinned_policy(path)
    return policy, warnings


def _policy_refusal(path: Path) -> Callable[[str], PmError]:
    """A builder of the PmError `sandbox policy <path>: <reason>`, the path
    quoted when it holds a character that would garble the message."""
    label = repr(str(path)) if _BAD_PATH_CHARS.search(str(path)) else str(path)
    return lambda reason: PmError(f"sandbox policy {label}: {reason}")


def load_pinned_policy(path: Path | str, expected_sha256: str | None = None) -> tuple[dict[str, Any], list[str], str]:
    """Load the operator's base policy as `load_base_policy` does and return
    `(policy, warnings, sha256)`, the digest being of the very bytes parsed:
    the file is read once, so no swap between a hash and a parse can put a
    policy under the server other than the one hashed. With
    `expected_sha256`, PmError before any parse when the digest differs
    (the file no longer has the SHA-256 the run recorded). PmError for every
    `load_base_policy` refusal.
    """
    path = Path(path)
    refuse = _policy_refusal(path)
    # ValueError covers a path no file can have: one with an embedded NUL,
    # or one that cannot be encoded (UnicodeError is a ValueError).
    try:
        data = path.read_bytes()
    except (OSError, ValueError) as exc:
        raise refuse(f"unreadable ({exc})") from exc
    sha256 = hashlib.sha256(data).hexdigest()
    if expected_sha256 is not None and sha256 != expected_sha256:
        raise PmError(
            f"sandbox policy {path} no longer has the SHA-256 this run recorded ({expected_sha256}); refusing"
            " to start the server under a policy the run was not initialised with"
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise refuse(f"unreadable ({exc})") from exc
    policy, warnings = _validate_base_policy(text, path)
    return policy, warnings, sha256


def _validate_base_policy(text: str, path: Path) -> tuple[dict[str, Any], list[str]]:
    """`(policy, warnings)` from the base policy `text` read from `path`;
    PmError for every refusal `load_base_policy` names but an unreadable file."""
    refuse = _policy_refusal(path)
    try:
        policy = json.loads(text)
    except json.JSONDecodeError as exc:
        raise refuse(f"not valid JSON ({exc})") from exc
    if not isinstance(policy, dict):
        raise refuse("the top level is not a JSON object")
    for key in ("network", "filesystem"):
        if not isinstance(policy.get(key), dict):
            raise refuse(f"it has no {key!r} object")
    network, filesystem = policy["network"], policy["filesystem"]

    if network.get("allowAllUnixSockets"):
        raise refuse("network.allowAllUnixSockets is set: every Unix socket on the host would be reachable from inside")
    if policy.get("allowAppleEvents"):
        raise refuse("allowAppleEvents is set: Apple Events let a sandboxed process drive applications on the host")
    if filesystem.get("disabled"):
        raise refuse("filesystem.disabled is set: it turns filesystem confinement off")
    fault = _list_fault(policy)
    if fault:
        raise refuse(fault)

    def resolve_entry(entry: Any, key: str) -> tuple[str, Path]:
        """(path text, resolved path) of a `key` entry; PmError when it is
        malformed, relative or unresolvable."""
        try:
            text_entry, _literal_entry = _entry_text(entry)
            if not _is_absolute_entry(text_entry):
                raise refuse(
                    f"{key} entry {text_entry!r} is relative: it would resolve against the server's working directory"
                )
            return _write_grant(entry)
        except ValueError as exc:
            raise refuse(f"{key} entry {entry!r} {exc}") from None

    # Equality only for `/` and the home directory; a relation for the rest.
    exact = (
        (Path("/"), "the filesystem root"),
        (_real(Path.home()), "the home directory"),
    )
    related = (
        (toolkit_root(), "the toolkit checkout"),
        (_socket_dir(), "the tmux socket directory"),
    )
    for entry in filesystem.get("allowWrite", []):
        text_entry, resolved = resolve_entry(entry, "allowWrite")
        for target, name in exact:
            if _same(resolved, target):
                raise refuse(f"allowWrite entry {text_entry!r} resolves to {name} ({target})")
        for target, name in related:
            relation = _relation(resolved, target)
            if relation:
                verb = "resolves to" if relation == "is" else relation
                raise refuse(f"allowWrite entry {text_entry!r} {verb} {name} ({target})")
    socket_dir = _socket_dir()
    for entry in network.get("allowUnixSockets", []):
        text_entry, resolved = resolve_entry(entry, "network.allowUnixSockets")
        relation = _relation(resolved, socket_dir)
        if relation:
            raise refuse(
                f"network.allowUnixSockets entry {text_entry!r} {relation} the tmux socket directory"
                f" ({socket_dir}): a sandboxed pane could connect to the operator's own tmux server"
            )

    warnings: list[str] = []
    if policy.get("enableWeakerNetworkIsolation"):
        warnings.append(
            f"sandbox policy {path}: enableWeakerNetworkIsolation is set: it opens the macOS trustd service"
            " to the sandbox, which srt documents as a data-exfiltration path"
        )
    if network.get("allowLocalBinding"):
        warnings.append(
            f"sandbox policy {path}: network.allowLocalBinding is set: it opens loopback and this host's own"
            " interface addresses to the sandbox, outside the egress proxy"
        )
    return policy, warnings


# --- Derived policies ------------------------------------------------------------


def _git_dirs(worktree: Path) -> tuple[Path, Path]:
    """(the worktree's own git dir, the common git dir), both real paths."""
    return git_ops.worktree_git_dir(worktree), _common_dir_of(worktree)


def _common_dir_of(worktree: Path) -> Path:
    """The real path of `worktree`'s common git dir."""
    return Path(git_ops.git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()


def _refuse_repository_grants(base: dict[str, Any], worktree: Path, common: Path) -> None:
    """PmError when a base write grant is, contains or lies inside the
    worktree or the common git dir: the toolkit grants exactly what a run
    needs there, and such an entry would reach the reviewer policy too.
    Judged by identity (see `_relation`)."""
    for entry in _write_entries(base):
        try:
            text, resolved = _write_grant(entry)
        except ValueError as exc:
            raise PmError(f"sandbox policy: allowWrite entry {entry!r} {exc}") from None
        for target, label in ((worktree, "the worktree"), (common, "the common git dir")):
            relation = _relation(resolved, target)
            if relation:
                raise PmError(
                    f"sandbox policy allowWrite entry {text!r} {relation} {label} ({target}): the toolkit grants"
                    " exactly what a run needs inside the repository; remove the entry"
                )


def _with_lists(base: dict[str, Any]) -> dict[str, Any]:
    """A deep copy of `base` whose `network.allowedDomains`,
    `network.deniedDomains` and four filesystem lists exist (`srt` requires
    the first four); PmError from `_check_lists` first."""
    _check_lists(base)
    policy = copy.deepcopy(base)
    network = policy.setdefault("network", {})
    filesystem = policy.setdefault("filesystem", {})
    for key in ("allowedDomains", "deniedDomains"):
        network.setdefault(key, [])
    for key in ("denyRead", "allowRead", "allowWrite", "denyWrite"):
        filesystem.setdefault(key, [])
    return policy


def _repository_denies(worktree: Path, git_dir: Path, common: Path, control_root: Path) -> list[dict[str, Any]]:
    """The `denyWrite` entries every server and reviewer policy carries,
    whatever runs inside: the repository's config and hooks, the control
    root, `srt`'s shared temp directory and, in every checkout, the pointer
    files `<git dir>/commondir`, `gitdir` and `config.worktree`: git honours
    them in a plain repository too, where it never writes them, and a pane
    that wrote one could redirect the host's next git command to a
    repository of its own. In a plain checkout (the git dir is the common
    dir) the git dir's `worktrees` directory is denied as well, holding the
    other linked worktrees' pointer files (`git worktree add` is unavailable
    inside); `modules` is left writable, which the host-side refusal of a
    gitlink covers. Where `.git` is a file (a linked worktree) the `.git`
    file and the git dir's `hooks` are denied too."""
    denies = [
        _literal(common / "config"),
        _literal(common / "hooks"),
        _literal(control_root),
        *(_literal(path) for path in _SHARED_TMP_DENIES),
        _literal(git_dir / "commondir"),
        _literal(git_dir / "gitdir"),
        _literal(git_dir / "config.worktree"),
    ]
    if git_dir == common:
        denies.append(_literal(git_dir / "worktrees"))
    if (worktree / ".git").is_file():
        denies += [_literal(worktree / ".git"), _literal(git_dir / "hooks")]
    return denies


def _repository_reads(worktree: Path, common: Path, extra_read: Iterable[Path | str]) -> list[dict[str, Any]]:
    return [_literal(worktree), _literal(common), _literal(toolkit_root()), *(_literal(path) for path in extra_read)]


def derive_policy(
    base: dict[str, Any],
    *,
    worktree: Path,
    branch: str,
    socket: str,
    tmpdir: Path,
    control_root: Path,
    pm_inside: bool,
    extra_read: Iterable[Path | str] = (),
) -> dict[str, Any]:
    """The server's effective policy: `base` plus the toolkit's grants and
    denies for `worktree` on `branch`. `base` is not mutated.

    Writes gain the worktree and its git dir; for a linked worktree also
    `<common>/objects` and literal entries for this branch's ref, its reflog,
    their locks and `packed-refs.lock`, and nothing else under the common
    dir; then the tmux socket, `tmpdir` and, when the PM itself runs inside
    (`pm_inside`, Mode C), the ledger directory. `denyWrite`, which `srt`
    ranks above every grant, gains `_repository_denies` and, when the PM runs
    outside (Mode B), the state root `<git dir>/pm`. Reads gain the
    worktree, the common dir, the toolkit checkout and `extra_read`; the
    socket becomes reachable and `allowPty` is set. Every filesystem entry
    added here is a literal entry (see the module docstring).

    PmError when a base write grant reaches into the repository (see
    `_refuse_repository_grants`).
    """
    worktree = _real(worktree)
    git_dir, common = _git_dirs(worktree)
    _refuse_repository_grants(base, worktree, common)
    socket_grant = _socket_grant(socket)

    policy = _with_lists(base)
    filesystem = policy["filesystem"]
    allow_write = filesystem["allowWrite"]
    allow_write += [_literal(worktree), _literal(git_dir)]
    if git_dir != common:
        allow_write += [
            _literal(common / "objects"),
            _literal(common / "refs" / "heads" / branch),
            _literal(common / "refs" / "heads" / f"{branch}.lock"),
            _literal(common / "logs" / "refs" / "heads" / branch),
            _literal(common / "logs" / "refs" / "heads" / f"{branch}.lock"),
            _literal(common / "packed-refs.lock"),
        ]
    allow_write += [_literal(socket_grant), _literal(_real(tmpdir))]
    if pm_inside:
        allow_write.append(_literal(_real(ledger.ledger_dir())))

    filesystem["denyWrite"] += _repository_denies(worktree, git_dir, common, _real(control_root))
    if not pm_inside:
        filesystem["denyWrite"].append(_literal(git_dir / "pm"))
    filesystem["allowRead"] += _repository_reads(worktree, common, extra_read)
    policy["network"].setdefault("allowUnixSockets", []).append(socket_grant)
    policy["allowPty"] = True
    return policy


def derive_reviewer_policy(
    base: dict[str, Any],
    *,
    worktree: Path,
    tmpdir: Path,
    control_root: Path,
    extra_read: Iterable[Path | str] = (),
) -> dict[str, Any]:
    """A Mode B reviewer's policy: `base` plus the effective policy's reads
    and repository denies and a write grant on `tmpdir` only, so the
    reviewer cannot write the repository; no tmux socket is added, so it
    cannot reach the Developer's server. Same base refusal as
    `derive_policy`; `base` is not mutated."""
    worktree = _real(worktree)
    git_dir, common = _git_dirs(worktree)
    _refuse_repository_grants(base, worktree, common)

    policy = _with_lists(base)
    filesystem = policy["filesystem"]
    filesystem["allowWrite"].append(_literal(_real(tmpdir)))
    filesystem["denyWrite"] += _repository_denies(worktree, git_dir, common, _real(control_root))
    filesystem["allowRead"] += _repository_reads(worktree, common, extra_read)
    policy["allowPty"] = True
    return policy


def derive_attach_policy(base: dict[str, Any], *, socket: str, control_root: Path) -> dict[str, Any]:
    """The operator's attach client's policy: `base`'s network (the socket
    added) and read lists, no write grant at all, and `base`'s `denyWrite`
    plus the control root and `srt`'s implicit grants. A pane can make an
    attached client run a command (`detach-client -E`); under this policy
    that command can write nothing. `base` is not mutated; PmError from
    `_check_lists`."""
    _check_lists(base)
    network = copy.deepcopy(base.get("network", {}))
    for key in ("allowedDomains", "deniedDomains"):
        network.setdefault(key, [])
    network.setdefault("allowUnixSockets", []).append(_socket_grant(socket))
    filesystem = base.get("filesystem", {})
    deny_write = copy.deepcopy(filesystem.get("denyWrite", []))
    deny_write += [_literal(_real(control_root)), *(_literal(path) for path in _SHARED_TMP_DENIES)]
    deny_write += [_literal(path) for path in _IMPLICIT_GRANT_DENIES]
    return {
        "network": network,
        "filesystem": {
            "denyRead": copy.deepcopy(filesystem.get("denyRead", [])),
            "allowRead": copy.deepcopy(filesystem.get("allowRead", [])),
            "allowWrite": [],
            "denyWrite": deny_write,
        },
        "allowPty": True,
    }


# --- srt and node ------------------------------------------------------------------


def srt_package_root() -> Path:
    """The directory holding `package.json` above the real path of the `srt`
    entry point: `srt`'s installed package."""
    srt = shutil.which("srt")
    if srt is None:
        raise PmError(SRT_MISSING)
    entry = _real(srt)
    for candidate in entry.parents:
        if (candidate / "package.json").is_file():
            return candidate
    raise PmError(f"could not find the package directory of srt ({entry}): no package.json above it")


def srt_version() -> str:
    """`srt --version`'s output, stripped."""
    if shutil.which("srt") is None:
        raise PmError(SRT_MISSING)
    try:
        result = subprocess.run(["srt", "--version"], check=False, text=True, capture_output=True)
    except OSError as exc:
        raise PmError(f"could not run srt --version: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise PmError(f"srt --version failed: {detail or f'exit {result.returncode}'}")
    return result.stdout.strip()


def check_binaries(policy: dict[str, Any]) -> None:
    """PmError when `srt` is absent, or when any write grant of `policy` is,
    contains or lies inside `srt`'s package directory, or is or contains the
    real `node` that runs it, or the `srt` and `node` paths as `PATH` spells
    them (a launcher symlink: a grant on its directory, such as
    `/opt/homebrew/bin`, would let a sandbox replace the `srt` the host runs
    next): `srt` documents a writable installation as a way to run code on
    the host. Judged by identity (see `_relation`)."""
    package = srt_package_root()
    node = shutil.which("node")
    if node is None:
        raise PmError("node is required to run srt but is not on PATH")
    node_real = _real(node)
    launchers = (
        (Path(os.path.abspath(shutil.which("srt") or "srt")), "the srt launcher"),
        (Path(os.path.abspath(node)), "the node launcher"),
    )
    for entry in _write_entries(policy):
        try:
            text, resolved = _write_grant(entry)
        except ValueError as exc:
            raise PmError(f"sandbox policy: allowWrite entry {entry!r} {exc}") from None
        relation = _relation(resolved, package)
        if relation:
            raise PmError(
                f"sandbox write grant {text!r} {relation} srt's installation ({package}): a sandbox that can"
                " write srt can run code on the host"
            )
        relation = _relation(resolved, node_real)
        if relation in ("is", "is an ancestor of"):
            raise PmError(
                f"sandbox write grant {text!r} {relation} the node that runs srt ({node_real}): a sandbox that"
                " can write it can run code on the host"
            )
        for launcher, name in launchers:
            relation = _relation(resolved, launcher)
            if relation in ("is", "is an ancestor of"):
                raise PmError(
                    f"sandbox write grant {text!r} {relation} {name} ({launcher}): a sandbox that can replace it"
                    " can run code on the host"
                )


def inside_sandbox() -> bool:
    """Whether this process runs inside an `srt` sandbox."""
    return os.environ.get("SANDBOX_RUNTIME") == "1"


def wrap_command(policy_path: Path | str, argv: list[str], *, tmpdir: Path | str) -> list[str]:
    """`argv` run under `srt` with `policy_path`, with `TMPDIR` set to
    `tmpdir` and, when the file exists, `SSL_CERT_FILE` pointed at the system
    bundle (some harnesses verify TLS through `trustd`, which Seatbelt
    blocks)."""
    certs = [f"SSL_CERT_FILE={SSL_CERT_FILE}"] if SSL_CERT_FILE.exists() else []
    return ["srt", "-s", str(policy_path), "env", f"TMPDIR={tmpdir}", *certs, *argv]


# --- Control directory, record, temp directory ----------------------------------


def _check_socket_name(socket: str) -> None:
    """PmError unless `socket` can name one path component: not empty, `.`
    or `..`, and free of `/` and of NUL, control and lone-surrogate
    characters."""
    if (
        not isinstance(socket, str)
        or not socket
        or "/" in socket
        or socket in {".", ".."}
        or _BAD_PATH_CHARS.search(socket)
    ):
        raise PmError(f"invalid tmux socket name for a sandbox: {socket!r}")


def control_root(common_dir: Path | str) -> Path:
    """`<common dir>/pm-sandbox`: the root every derived policy denies."""
    return Path(common_dir) / CONTROL_ROOT_NAME


def control_dir(common_dir: Path | str, socket: str) -> Path:
    """`<common dir>/pm-sandbox/<socket>`: one server's host-only files."""
    _check_socket_name(socket)
    return control_root(common_dir) / socket


def write_record(control: Path | str, **fields: Any) -> Path:
    """Write `fields` as `<control>/record.json` (atomically); return its path.

    The record names the worktree, branch, socket, temp dir, base policy and
    its SHA-256, the `srt` version, the `srt` and server pids and the start
    time; `control`, the real path of `control` itself, is added here, so
    `stop_server` finds the directory without the worktree. Nothing inside
    the sandbox can write beneath the control root, so a record found there
    was written by the host.
    """
    control = Path(control)
    control.mkdir(parents=True, exist_ok=True)
    record = {**fields, "control": str(_real(control))}
    target = control / RECORD_NAME
    staging = control / f"{RECORD_NAME}.tmp"
    staging.write_text(json.dumps(record, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(staging, target)
    return target


def read_record(control: Path | str) -> dict[str, Any]:
    """The JSON object in `<control>/record.json`; PmError when the file is
    missing, unreadable, not JSON or not an object."""
    path = Path(control) / RECORD_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise PmError(f"sandbox record is missing: {path}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise PmError(f"sandbox record is unreadable: {path} ({exc})") from exc
    try:
        record = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PmError(f"sandbox record is malformed: {path} ({exc})") from exc
    if not isinstance(record, dict):
        raise PmError(f"sandbox record is malformed: {path} (not a JSON object)")
    return record


def make_tmpdir(socket: str) -> Path:
    """Create `pm-sandbox-<socket>` under `tempfile.gettempdir()` (mode 0700)
    and return its real path; PmError when it already exists."""
    _check_socket_name(socket)
    path = Path(tempfile.gettempdir()) / f"{TMPDIR_PREFIX}{socket}"
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        raise PmError(f"sandbox temp dir already exists: {path}; another run may own it") from None
    except OSError as exc:
        raise PmError(f"could not create sandbox temp dir {path}: {exc}") from exc
    return _real(path)


def _check_tmpdir(path: Path) -> None:
    """PmError unless `path` is a `pm-sandbox-*` name directly under
    `tempfile.gettempdir()` that is absent or a directory, not a symbolic link."""
    if not path.name.startswith(TMPDIR_PREFIX) or _real(path.parent) != _real(tempfile.gettempdir()):
        raise PmError(
            f"refusing to remove {path}: not a {TMPDIR_PREFIX}* directory directly under {tempfile.gettempdir()}"
        )
    if os.path.lexists(path) and (path.is_symlink() or not path.is_dir()):
        raise PmError(f"refusing to remove {path}: not a directory")


def remove_tmpdir(path: Path | str) -> bool:
    """Remove a sandbox temp dir recursively; True when removed, False when
    already gone. PmError, removing nothing, unless its name starts with
    `pm-sandbox-` and its parent is `tempfile.gettempdir()`, or when it is
    not a directory."""
    path = Path(path)
    _check_tmpdir(path)
    if not os.path.lexists(path):
        return False
    try:
        shutil.rmtree(path)
    except OSError as exc:
        raise PmError(f"could not remove sandbox temp dir {path}: {exc}") from exc
    return True


# --- The server -------------------------------------------------------------------


def _server_pid(socket: str, timeout: float | None = None) -> int:
    """The pid of the server on `socket`; PmError when tmux cannot say, or does not within `timeout` s."""
    argv = ["tmux", "-L", socket, "display-message", "-p", "#{pid}"]
    try:
        result = subprocess.run(argv, check=False, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise PmError(
            f"could not read the server pid on tmux socket {socket!r}: no reply within {timeout:g} s"
        ) from None
    except OSError as exc:
        raise PmError(f"could not read the server pid on tmux socket {socket!r}: {exc}") from exc
    reply = result.stdout.strip()
    if result.returncode != 0 or not reply.isdigit():
        detail = (result.stderr or result.stdout or "").strip()
        raise PmError(f"could not read the server pid on tmux socket {socket!r}: {detail or 'no pid'}")
    return int(reply)


def _log_tail(log_path: Path, lines: int = 3) -> str:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(no log)"
    tail = [line for line in text.splitlines() if line.strip()][-lines:]
    return " | ".join(tail) if tail else "(empty log)"


def _kill_group(process: subprocess.Popen) -> None:
    """SIGKILL the process group `process` leads (`srt` and the server it
    wraps share it), then reap `process`."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=SRT_EXIT_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def start_server(*, socket: str, policy_path: Path, worktree: Path, tmpdir: Path, log_path: Path) -> tuple[int, int]:
    """Start `srt -s <policy> env TMPDIR=<tmpdir> [SSL_CERT_FILE=...] tmux -L <socket> -D`
    and return `(srt_pid, server_pid)` once the server answers.

    `-D` keeps tmux in the foreground, so `srt` stays its parent and `srt`'s
    egress proxy lives exactly as long as the server. The process runs with
    cwd `worktree` (`srt`'s own denies key off it), stdin from /dev/null,
    stdout and stderr appended to `log_path`, in a session and process group
    of its own, and with `PM_RUN_TOKEN` removed from its environment (tmux
    seeds the server's global environment from it).

    PmError, before anything starts, for an invalid socket name, off macOS
    or when a server already answers on `socket`. The server must answer
    within 10 s (polled every 0.1 s, each probe bounded by the time left, and
    an answer after the deadline refused); when `srt` exits first, the
    deadline passes or the probe cannot look, the process group is killed
    and the PmError quotes the log's last three lines. An exception during
    the wait, KeyboardInterrupt included, kills the group too and
    propagates. No retry and no fallback to an unsandboxed server.
    """
    _check_socket_name(socket)
    if platform.system() != "Darwin":
        raise PmError("sandboxing is implemented for macOS only")
    # This host-side probe is also what creates the tmux socket directory
    # (mode 0700) when it is absent; the sandboxed server cannot create it,
    # so the probe must run before srt starts.
    if sessions.server_running(socket, timeout=START_DEADLINE_SECONDS):
        raise PmError(f"a tmux server already answers on socket {socket!r}; refusing to start a sandboxed one")
    if shutil.which("srt") is None:
        raise PmError(SRT_MISSING)
    argv = wrap_command(policy_path, ["tmux", "-L", socket, "-D"], tmpdir=tmpdir)
    env = {name: value for name, value in os.environ.items() if name != "PM_RUN_TOKEN"}
    log_path = Path(log_path)
    try:
        with log_path.open("ab") as log:
            process = subprocess.Popen(
                argv,
                cwd=str(worktree),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                env=env,
            )
    except OSError as exc:
        raise PmError(f"could not start srt for socket {socket!r}: {exc}") from exc

    deadline = time.monotonic() + START_DEADLINE_SECONDS
    late = f"the server did not answer within {START_DEADLINE_SECONDS:g} s"
    # Any exit from the wait but success kills the group, an interrupt
    # included: the pid is recorded nowhere until the server answers.
    try:
        while True:
            returncode = process.poll()
            if returncode is not None:
                failure = f"srt exited with status {returncode} before the server answered"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = late
                break
            try:
                answered = sessions.server_running(socket, timeout=remaining)
                if answered:
                    server_pid = _server_pid(socket, timeout=max(deadline - time.monotonic(), POLL_SECONDS))
            except PmError as exc:
                # A probe bounded by the time left fails as the deadline passes.
                failure = f"{late} (last probe: {exc})" if time.monotonic() >= deadline else str(exc)
                break
            if time.monotonic() >= deadline:
                failure = late
                break
            if answered:
                _STARTED[process.pid] = process
                return process.pid, server_pid
            time.sleep(POLL_SECONDS)
    except BaseException:
        _kill_group(process)
        raise
    _kill_group(process)
    raise PmError(
        f"sandboxed tmux server on socket {socket!r} did not start: {failure}; {log_path} ends: {_log_tail(log_path)}"
    )


def verify_server(*, socket: str, worktree: Path) -> None:
    """PmError unless the server on `socket` is really sandboxed, proven
    from the host before any session starts:

    1. its global environment carries `SANDBOX_RUNTIME=1`;
    2. the negative probe: a `run-shell` touch of `<socket dir>/<socket>.probe`
       must fail and leave no file (it succeeds on any unsandboxed server).
       When it does not, or does not return in time, the file is removed if
       present, the server is killed, and the error names the probe; a
       failure of either cleanup step is appended to that error, never
       raised in its place;
    3. the positive probe: a `run-shell` touch of `<worktree>/.pm/sandbox-probe`
       must succeed and leave the file, which is then removed, so a policy
       that does not grant the worktree is caught here. The host creates
       `<worktree>/.pm` first when it is absent (and removes it again).

    Each of the three probes must return within 10 s, or verification
    fails with PmError, so a wedged server cannot hang it. PmError at once
    for an invalid socket name.
    """
    _check_socket_name(socket)
    value = sessions.server_environment(socket, "SANDBOX_RUNTIME", timeout=PROBE_TIMEOUT_SECONDS)
    if value != "1":
        raise PmError(
            f"the server on socket {socket!r} does not carry SANDBOX_RUNTIME=1 (found {value!r}): it is not running"
            " under srt"
        )

    outside = sessions.socket_path(socket).parent / f"{socket}.probe"
    try:
        outside.unlink(missing_ok=True)
    except OSError as exc:
        raise PmError(f"could not clear the negative probe file {outside}: {exc}") from exc

    def clean_up() -> str:
        """Remove the probe file if present and kill the server; the outcome,
        for the error the caller raises."""
        cleanup: list[str] = []
        try:
            outside.unlink(missing_ok=True)
        except OSError as exc:
            cleanup.append(f"removing {outside} failed too: {exc}")
        try:
            sessions.kill_server(socket)
        except PmError as exc:
            cleanup.append(f"killing the server failed too: {exc}")
        else:
            cleanup.append("the server was killed")
        return "; ".join(cleanup)

    try:
        returncode, output = sessions.run_shell(
            socket, f"touch {shlex.quote(str(outside))}", timeout=PROBE_TIMEOUT_SECONDS
        )
    except PmError as exc:
        raise PmError(f"negative probe could not run: {exc}; {clean_up()}") from exc
    if returncode == 0 or os.path.lexists(outside):
        raise PmError(
            f"negative probe failed: the server on socket {socket!r} could create {outside}, outside any sandbox"
            f" grant, so it is not confined; {clean_up()}"
        )

    probe_dir = Path(worktree) / PROBE_DIR_NAME
    created = not probe_dir.exists()
    inside = probe_dir / PROBE_FILE_NAME
    try:
        probe_dir.mkdir(exist_ok=True)
        inside.unlink(missing_ok=True)
        returncode, output = sessions.run_shell(
            socket, f"touch {shlex.quote(str(inside))}", timeout=PROBE_TIMEOUT_SECONDS
        )
        found = inside.is_file()
        inside.unlink(missing_ok=True)
    except OSError as exc:
        raise PmError(f"positive probe could not run: {exc}") from exc
    finally:
        if created:
            try:
                probe_dir.rmdir()
            except OSError:
                pass
    if returncode != 0 or not found:
        detail = output.strip() or f"exit {returncode}"
        raise PmError(
            f"positive probe failed: the server on socket {socket!r} could not create {inside} ({detail}); the"
            " policy does not grant the worktree"
        )


def _pid_alive(pid: int) -> bool:
    """Whether `pid` still runs. A server this process started is asked
    through its `Popen` (which also reaps it); any other pid through signal 0."""
    process = _STARTED.get(pid)
    if process is not None:
        if process.poll() is None:
            return True
        del _STARTED[pid]
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _record_srt_pid(record: dict[str, Any]) -> int | None:
    """The record's `srt_pid` when it is a pid that fits a `pid_t`, else None."""
    srt_pid = record.get("srt_pid")
    return srt_pid if type(srt_pid) is int and 0 < srt_pid <= _PID_MAX else None


def _kill_and_wait(socket: str, srt_pid: int | None) -> tuple[list[str], bool]:
    """`sessions.kill_server(socket)`, then up to 5 s for `srt_pid` to exit;
    return (the sessions still listed, whether `srt_pid` still runs). While
    sessions are listed the server is up, so `srt` is asked once, not
    waited for. PmError from the kill."""
    remaining = sessions.kill_server(socket)
    if srt_pid is None:
        return remaining, False
    if not remaining:
        deadline = time.monotonic() + SRT_EXIT_WAIT_SECONDS
        while _pid_alive(srt_pid) and time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)
    return remaining, _pid_alive(srt_pid)


def _still_up(socket: str, remaining: list[str], srt_pid: int | None, srt_alive: bool) -> list[str]:
    """One line each for the sessions still listed on `socket` and an `srt` still running."""
    lines: list[str] = []
    if remaining:
        lines.append(f"sessions still listed on socket {socket}: {', '.join(remaining)}")
    if srt_alive:
        lines.append(f"srt still running: {srt_pid}")
    return lines


def _foreign_tmpdir(record: dict[str, Any], socket: str) -> Path | None:
    """The record's temp dir when it is a well-formed `pm-sandbox-<socket>`
    path under a temp root other than this process's (a `TMPDIR` other than
    the one the record was written under), else None: `_check_tmpdir` would
    refuse it, and a temp dir this process cannot vouch for is no reason to
    leave the server running."""
    text = record.get("tmpdir")
    if not isinstance(text, str) or _BAD_PATH_CHARS.search(text):
        return None
    tmpdir = Path(text)
    if not tmpdir.is_absolute() or tmpdir.name != f"{TMPDIR_PREFIX}{socket}":
        return None
    return tmpdir if _real(tmpdir.parent) != _real(tempfile.gettempdir()) else None


def _teardown_targets(socket: str, record: dict[str, Any]) -> tuple[int | None, Path, Path, Path | None]:
    """(`srt` pid or None, temp dir to remove, control dir, foreign temp dir
    or None) from `record`, each checked so that `stop_server` refuses before
    it kills or removes anything: the pid fits a `pid_t`, and the temp dir
    passes `_check_tmpdir`. For a record whose temp dir lies under another
    temp root (`_foreign_tmpdir`), the temp dir to remove is this process's
    own name for it (`run_tmpdir`), which is the socket's and passes the
    check, and the record's is returned as the foreign one."""

    def malformed(reason: str) -> PmError:
        return PmError(f"the sandbox record for socket {socket!r} is malformed: {reason}")

    def path_field(name: str) -> str | None:
        """The record's `name` path; None when the record has no such key,
        PmError when the value is null or not a path."""
        if name not in record:
            return None
        value = record[name]
        if value is None:
            raise malformed(f"{name} is null")
        if not isinstance(value, str) or not value or _BAD_PATH_CHARS.search(value):
            raise malformed(f"{name} is not a path: {value!r}")
        return value

    if not isinstance(record, dict):
        raise malformed("it is not an object")
    if "srt_pid" not in record:
        raise malformed("srt_pid is missing")
    srt_pid = record["srt_pid"]
    if srt_pid is not None and _record_srt_pid(record) is None:
        raise malformed(f"srt_pid is not a pid or null: {srt_pid!r}")
    tmpdir_text = path_field("tmpdir")
    if tmpdir_text is None:
        raise malformed("tmpdir is missing")
    foreign = _foreign_tmpdir(record, socket)
    tmpdir = run_tmpdir(socket) if foreign is not None else Path(tmpdir_text)
    _check_tmpdir(tmpdir)

    control_text = path_field("control")
    if control_text is not None:
        control = Path(control_text)
        if not control.is_absolute() or control.name != socket or control.parent.name != CONTROL_ROOT_NAME:
            raise PmError(
                f"refusing to remove sandbox control dir {control}: the record for socket {socket!r} must name an"
                f" absolute <common dir>/{CONTROL_ROOT_NAME}/{socket}"
            )
        return srt_pid, tmpdir, control, foreign
    worktree = path_field("worktree")
    if worktree is None:
        raise malformed("it names neither control nor worktree")
    return srt_pid, tmpdir, control_dir(_common_dir_of(Path(worktree)), socket), foreign


@dataclass
class TeardownOutcome:
    """What `stop_server` found and did, as facts: its callers decide from
    these fields, never from `notes`, which word them one line each for
    printing.

    `tmpdir_state` is "removed", "gone" (already) or "foreign" (the record's
    temp dir lies under another temp root and was left in place; `tmpdir`
    names it then). `control_state` is "removed", "gone" (already),
    "refused" (not a directory, left in place) or "kept" (the server is not
    gone, so the control dir and its record stay for a retry).
    """

    socket: str
    # The sessions `sessions.kill_server` reported still listed.
    remaining: list[str]
    srt_pid: int | None
    # The recorded `srt` still runs after the wait.
    srt_alive: bool
    tmpdir: Path
    tmpdir_state: Literal["removed", "gone", "foreign"]
    control: Path
    control_state: Literal["removed", "gone", "refused", "kept"]
    notes: list[str]

    @property
    def left(self) -> list[str]:
        """What makes the teardown incomplete, one line each: sessions still
        listed, an `srt` still running, the control dir still there. A
        foreign temp dir is not among them: each mode weighs it itself."""
        left = _still_up(self.socket, self.remaining, self.srt_pid, self.srt_alive)
        if self.control_state in ("refused", "kept"):
            left.append(f"sandbox control dir {self.control} is still there")
        return left


def stop_server(*, socket: str, record: dict[str, Any]) -> TeardownOutcome:
    """Tear down the server `record` describes; return what was found and
    done (`TeardownOutcome`).

    `sessions.kill_server(socket)` ends the server (and with it `srt` and
    its proxy); `remaining` is what it reports still listed. Then: up to 5 s
    for `record["srt_pid"]` to exit (not waited for while sessions are
    listed; `srt still running: <pid>` when it does not); the record's temp
    dir removed through `remove_tmpdir`; the control directory removed. That
    directory is `record["control"]`, which must be an absolute
    `.../pm-sandbox/<socket>`; only a record without it falls back to
    deriving it from the record's worktree's common git dir.

    The control directory, and the record in it, is kept while any session
    is listed or `srt` still runs, so a retry can still find the server and
    the pid; it is removed only once the server is gone. A record whose temp
    dir lies under a temp root other than this process's (`_foreign_tmpdir`)
    still has its server killed and its control dir removed; that temp dir
    is left in place, and a note names it and the `TMPDIR` difference (or
    says it is already gone). `notes` holds one line per step's outcome,
    removals, already-gone and kept directories and a control path refused
    as not a directory included, for the caller to print.

    PmError, before anything is killed or removed, for an invalid socket
    name or a missing or malformed `srt_pid` (one that does not fit a
    `pid_t` included), `tmpdir` (a null one, and an existing one that is a
    symbolic link or not a directory, included), `control` or (in the
    fallback) `worktree`.
    """
    _check_socket_name(socket)
    srt_pid, local, control, foreign = _teardown_targets(socket, record)
    remaining, srt_alive = _kill_and_wait(socket, srt_pid)
    notes: list[str] = []
    if srt_alive:
        notes.append(f"srt still running: {srt_pid}")

    removed = remove_tmpdir(local)
    if removed:
        notes.append(f"removed sandbox temp dir {local}")
    tmpdir, tmpdir_state = local, "removed" if removed else "gone"
    if foreign is not None:
        tmpdir, tmpdir_state = foreign, "foreign" if os.path.lexists(foreign) else "gone"
    if tmpdir_state == "foreign":
        notes.append(
            f"left sandbox temp dir {foreign} in place: it is not under this process's temp root"
            f" {tempfile.gettempdir()}, so TMPDIR differs from the one the run's sandbox was set up under;"
            " remove it by hand, or repeat this under that TMPDIR"
        )
    elif tmpdir_state == "gone":
        notes.append(f"sandbox temp dir already gone: {tmpdir}")

    if not os.path.lexists(control):
        control_state = "gone"
        notes.append(f"sandbox control dir already gone: {control}")
    elif control.is_symlink() or not control.is_dir():
        control_state = "refused"
        notes.append(f"refused to remove sandbox control dir {control}: not a directory; it was left in place")
    elif remaining or srt_alive:
        control_state = "kept"
        notes.append(f"kept sandbox control dir {control} and its record for a retry: the server is not gone")
    else:
        try:
            shutil.rmtree(control)
        except OSError as exc:
            raise PmError(f"could not remove sandbox control dir {control}: {exc}") from exc
        control_state = "removed"
        notes.append(f"removed sandbox control dir {control}")
    return TeardownOutcome(
        socket=socket,
        remaining=remaining,
        srt_pid=srt_pid,
        srt_alive=srt_alive,
        tmpdir=tmpdir,
        tmpdir_state=tmpdir_state,
        control=control,
        control_state=control_state,
        notes=notes,
    )


def tear_down(*, socket: str, control: Path, record: dict[str, Any] | None) -> TeardownOutcome:
    """The teardown both modes share, a Mode B run end's and `stop-child`'s:
    `stop_server` with `record`, its control dir taken as `control`, which
    the caller derives (never a path the record names). With no record (a
    teardown that removed it, or a server whose record was never written),
    the sandbox is torn down from what `init` or `launch-child` created: the
    socket's server, if one answers, the temp dir `make_tmpdir` names
    (`run_tmpdir`) and `control`. The control root `<common git
    dir>/pm-sandbox` is removed too once nothing else is left in it.

    PmError as `stop_server` raises it, and, with no record, when no temp
    directory is usable to name the temp dir by.
    """
    if record is None:
        try:
            tmpdir = run_tmpdir(socket)
        except OSError as exc:
            raise PmError(f"the sandbox temp dir could not be looked for: no temp directory is usable ({exc})") from exc
        record = {"srt_pid": None, "tmpdir": str(tmpdir)}
    outcome = stop_server(socket=socket, record={**record, "control": str(control)})
    # `rmdir` refuses a non-empty root, so another server's directory is never touched.
    try:
        control.parent.rmdir()
    except OSError:
        pass
    return outcome


# --- A Mode B run's server -------------------------------------------------------


def run_sandbox(state: dict[str, Any], repo: Path) -> tuple[dict[str, Any], str, Path]:
    """(the run's `sandbox` block, its socket, its control dir) from MAC-checked
    run state. PmError when the block is malformed, or when its control dir
    is not `<repo's common git dir>/pm-sandbox/<socket>`: the location every
    derived policy denies, which is what makes its files the host's. A
    control dir holding a NUL, control or surrogate character, or one whose
    real path cannot be resolved, is a malformed block too."""
    block = state.get("sandbox")
    socket = block.get("socket") if isinstance(block, dict) else None
    control_text = block.get("control_dir") if isinstance(block, dict) else None
    if (
        not isinstance(socket, str)
        or not isinstance(control_text, str)
        or not control_text
        or _BAD_PATH_CHARS.search(control_text)
    ):
        raise PmError(f"this run's sandbox record in run state is malformed: {block!r}")
    expected = control_dir(_common_dir_of(repo), socket)
    control = Path(control_text)
    try:
        same = _same(_real(control), _real(expected))
    except (OSError, ValueError) as exc:
        raise PmError(
            f"this run's sandbox record in run state is malformed: {block!r} (its control dir cannot be resolved:"
            f" {exc})"
        ) from exc
    if not same:
        raise PmError(
            f"this run's sandbox control dir {control} is not {expected}, the only place a host-written one can be"
        )
    return block, socket, control


def records_sandbox(state: dict[str, Any]) -> bool:
    """Whether run state records a sandbox: any `sandbox` value but an absent
    key or null, so an empty or malformed block counts and is refused by
    whatever reads it, never taken for an unsandboxed run."""
    return state.get("sandbox") is not None


def require_run_socket(state: dict[str, Any]) -> None:
    """PmError when the run records a sandbox and `PM_TMUX_SOCKET` (as
    `sessions.tmux_argv` reads it) is unset or names another server; the
    message gives the export line. Every tmux call of a run goes through
    `PM_TMUX_SOCKET`, so without this a sandboxed run's command could reach
    the operator's default, unsandboxed server. Nothing for an unsandboxed
    run (no `sandbox` key, or null). Any other recorded `sandbox` value that
    is not an object naming a socket, an empty one included, is refused with
    PmError too: a malformed record fails closed."""
    if not records_sandbox(state):
        return
    block = state["sandbox"]
    socket = block.get("socket") if isinstance(block, dict) else None
    if not isinstance(socket, str) or not socket:
        raise PmError(f"this run's sandbox record in run state names no socket: {block!r}")
    current = os.environ.get("PM_TMUX_SOCKET", "").strip()
    if current == socket:
        return
    found = f"PM_TMUX_SOCKET is {current!r}" if current else "PM_TMUX_SOCKET is unset"
    raise PmError(
        f"this run's Developer server is sandboxed on tmux socket {socket!r}, but {found}; run "
        f"`export PM_TMUX_SOCKET={socket}` and retry"
    )


def run_tmpdir(socket: str) -> Path:
    """`pm-sandbox-<socket>` under the real path of `tempfile.gettempdir()`:
    where `make_tmpdir` creates a server's temp dir, spelled as it returns
    it (the directory need not exist). OSError when no temp directory is
    usable."""
    _check_socket_name(socket)
    return _real(tempfile.gettempdir()) / f"{TMPDIR_PREFIX}{socket}"


def derive_run_policies(
    base: dict[str, Any], *, repo: Path, branch: str, socket: str, tmpdir: Path, control_root: Path, plan_dir: Path
) -> dict[str, dict[str, Any]]:
    """A Mode B run's three policies, by the file name its control directory
    holds each under: the server's (`derive_policy`, the PM outside, the
    plan's directory readable), the reviewer's and the attach client's.
    PmError from the derivations, and from `check_binaries` on the server's
    and the reviewer's policies."""
    policies = {
        POLICY_NAME: derive_policy(
            base,
            worktree=repo,
            branch=branch,
            socket=socket,
            tmpdir=tmpdir,
            control_root=control_root,
            pm_inside=False,
            extra_read=[plan_dir],
        ),
        REVIEWER_POLICY_NAME: derive_reviewer_policy(
            base, worktree=repo, tmpdir=tmpdir, control_root=control_root, extra_read=[plan_dir]
        ),
        ATTACH_POLICY_NAME: derive_attach_policy(base, socket=socket, control_root=control_root),
    }
    check_binaries(policies[POLICY_NAME])
    check_binaries(policies[REVIEWER_POLICY_NAME])
    return policies


def write_control_file(control: Path, name: str, content: dict[str, Any]) -> Path:
    """Write `content` as JSON to `<control>/<name>` (atomically: a staging
    file, then `os.replace`); return its path. PmError naming the path on
    any OSError."""
    target = control / name
    staging = control / f"{name}.tmp"
    try:
        staging.write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")
        os.replace(staging, target)
    except OSError as exc:
        raise PmError(f"could not write sandbox control file {target}: {exc}") from exc
    return target


def _refuse_foreign_server(socket: str, record: dict[str, Any] | None, record_path: Path) -> None:
    """PmError unless the server answering on `socket` is provably the one
    `record` names: `SANDBOX_RUNTIME=1` in its global environment and the
    record's `server_pid`. A server with no record to compare against
    (`record` None) is refused too: its ownership cannot be established."""
    stop_hint = f"stop it with `tmux -L {socket} kill-server` once you know whose it is"
    if record is None:
        raise PmError(
            f"a tmux server answers on this run's socket {socket!r}, but the run's sandbox record {record_path}"
            f" is gone, so its ownership cannot be established; refusing to start a session there ({stop_hint})"
        )
    value = sessions.server_environment(socket, "SANDBOX_RUNTIME")
    if value != "1":
        raise PmError(
            f"a tmux server answers on this run's socket {socket!r} without SANDBOX_RUNTIME=1 (found {value!r}):"
            f" it is not this run's sandboxed server; refusing to start a session there ({stop_hint})"
        )
    pid = _server_pid(socket, timeout=PROBE_TIMEOUT_SECONDS)
    if record.get("server_pid") != pid:
        raise PmError(
            f"the sandboxed tmux server on socket {socket!r} has pid {pid}, but this run's record names"
            f" {record.get('server_pid')!r}: it is not the server this run started; refusing to start a session there"
        )


def _recorded_tmpdir(record: dict[str, Any], socket: str, record_path: Path) -> Path:
    """The record's temp dir, recreated when it has gone (a reboot can clear
    it). PmError when the record names none, when it does not lie under this
    process's temp root (a `TMPDIR` other than the one the record was
    written under), or when `_check_tmpdir` refuses it."""
    text = record.get("tmpdir")
    if not isinstance(text, str) or not text:
        raise PmError(f"the sandbox record {record_path} names no temp dir")
    tmpdir = Path(text)
    root = tempfile.gettempdir()
    if _real(tmpdir.parent) != _real(root):
        raise PmError(
            f"this run's sandbox temp dir {tmpdir} is not under this process's temp root {root}: TMPDIR differs"
            " from the one the run's sandbox was set up under; retry with that TMPDIR"
        )
    _check_tmpdir(tmpdir)
    if not os.path.lexists(tmpdir) and make_tmpdir(socket) != _real(tmpdir):
        raise PmError(f"the sandbox temp dir was recreated somewhere other than the record's {tmpdir}")
    return tmpdir


def ensure_run_server(state: dict[str, Any], *, repo: Path) -> bool:
    """Make sure the sandboxed server of the Mode B run `state` describes is
    up on its socket before `start-slice` creates a session there; return
    True when this call started it, False when it was already running.

    A running server is accepted only when the run's record exists, the
    server's global environment carries `SANDBOX_RUNTIME=1` and its pid is
    the record's `server_pid`: anything else on the socket is refused, never
    joined or killed. With no server, a record whose `srt_pid` still runs
    (a teardown keeps the record while its `srt` survives) is refused with
    PmError naming the pid: a second server is never started beside an
    orphaned `srt` whose panes may still run, and nothing here kills it.
    Otherwise the base policy file is re-read once (`load_pinned_policy`)
    and refused unless its SHA-256 is still the one `init` recorded, and the
    three policies are re-derived from it (`derive_run_policies`).

    When the control directory or its record is absent (a `stop` removed
    them, and `start-slice` resumes the run), what `init` created is rebuilt
    from the MAC-checked `sandbox` block and that policy: the control
    directory at the recorded path, `policy.json`, `reviewer-policy.json`,
    `attach-policy.json`, the temp dir `make_tmpdir` names (created unless a
    partial teardown left it) and a record without pids. Otherwise the three
    policy files are rewritten, which deliberately repairs a missing
    `reviewer-policy.json` or `attach-policy.json` (a Mode B review would
    otherwise fail closed on it), and the record's temp dir is recreated
    when it has gone. Either way the server is then started and verified and
    the record updated with its pids.

    On any failure after `start_server` returned, the server is killed and
    `srt` waited for, and the PmError carries that outcome: "the server was
    killed" only when the kill left no session listed and `srt` has exited;
    otherwise the sessions still listed and/or "srt still running: <pid>";
    or the kill's own failure. An interrupt propagates as itself, with the
    outcome as a note. The
    control and temp directories are the run's and stay, and the record
    keeps its last pids, so the next `start-slice` starts afresh. PmError
    for every refusal, a temp dir outside this process's temp root and a
    control file that cannot be written among them.
    """
    block, socket, control = run_sandbox(state, repo)
    record_path = control / RECORD_NAME
    record: dict[str, Any] | None = None
    if os.path.lexists(record_path):
        record = read_record(control)
        if record.get("socket") != socket:
            raise PmError(f"the sandbox record {record_path} names socket {record.get('socket')!r}, not {socket!r}")

    if sessions.server_running(socket, timeout=START_DEADLINE_SECONDS):
        _refuse_foreign_server(socket, record, record_path)
        return False
    srt_pid = _record_srt_pid(record) if record is not None else None
    if srt_pid is not None and _pid_alive(srt_pid):
        raise PmError(
            f"no server answers on this run's socket {socket!r}, but the run's previous srt (pid {srt_pid}, from"
            f" {record_path}) still runs, and sessions it started may still be running under its proxy; end it"
            f" (`kill {srt_pid}`, or `stop` again once it has exited) before start-slice can start a fresh server"
        )

    policy_text = block.get("policy")
    recorded_sha256 = block.get("policy_sha256")
    if not isinstance(policy_text, str) or not isinstance(recorded_sha256, str):
        raise PmError(f"this run's sandbox record in run state is malformed: {block!r}")
    base, _warnings, _sha256 = load_pinned_policy(Path(policy_text), recorded_sha256)

    tmpdir = _recorded_tmpdir(record, socket, record_path) if record is not None else run_tmpdir(socket)
    policies = derive_run_policies(
        base,
        repo=repo,
        branch=state["branch"],
        socket=socket,
        tmpdir=tmpdir,
        control_root=control.parent,
        plan_dir=Path(state["plan"]["path"]).parent,
    )
    version = srt_version()
    if record is None:
        try:
            control.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PmError(f"could not recreate sandbox control dir {control}: {exc}") from exc
        if not os.path.lexists(tmpdir):
            tmpdir = make_tmpdir(socket)
        elif tmpdir.is_symlink() or not tmpdir.is_dir():
            raise PmError(f"sandbox temp dir {tmpdir} exists but is not a directory; refusing to use it")
        else:
            # A partial teardown left it; the server gets the real path the record stores.
            tmpdir = _real(tmpdir)
        for name, content in policies.items():
            write_control_file(control, name, content)
        record = {
            "worktree": str(repo),
            "branch": state["branch"],
            "socket": socket,
            "tmpdir": str(tmpdir),
            "policy": policy_text,
            "policy_sha256": recorded_sha256,
            "srt_version": version,
            "srt_pid": None,
            "server_pid": None,
            "started_at": None,
        }
        try:
            write_record(control, **record)
        except OSError as exc:
            raise PmError(f"could not write sandbox record {record_path}: {exc}") from exc
    else:
        for name, content in policies.items():
            write_control_file(control, name, content)

    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    srt_pid, server_pid = start_server(
        socket=socket,
        policy_path=control / POLICY_NAME,
        worktree=repo,
        tmpdir=tmpdir,
        log_path=control / SRT_LOG_NAME,
    )
    try:
        verify_server(socket=socket, worktree=repo)
        fields = {key: value for key, value in record.items() if key != "control"}
        fields.update(srt_pid=srt_pid, server_pid=server_pid, started_at=started_at, srt_version=version)
        write_record(control, **fields)
    except BaseException as exc:
        # Cleanup must never replace the failure it is cleaning up after.
        try:
            remaining, srt_alive = _kill_and_wait(socket, srt_pid)
            left = "; ".join(_still_up(socket, remaining, srt_pid, srt_alive)) or "the server was killed"
        except BaseException as cleanup_exc:
            left = f"killing the server failed too ({cleanup_exc}): check tmux -L {socket}"
        outcome = f"sandboxed server on socket {socket!r} did not come up verified; {left}"
        if not isinstance(exc, Exception):
            # An interrupt propagates as itself, carrying the outcome.
            exc.add_note(outcome)
            raise
        raise PmError(f"{exc}; {outcome}") from exc
    return True


def _remains(socket: str, control: Path, record: dict[str, Any] | None) -> list[str]:
    """What is still there of the sandbox on `socket`, one line each: a
    server answering on the socket (or one that cannot be checked), the
    control dir, the temp dir `make_tmpdir` names and, from `record` when
    there is one, a temp dir it names under another temp root and an `srt`
    still running."""
    left: list[str] = []
    try:
        if sessions.server_running(socket, timeout=START_DEADLINE_SECONDS):
            left.append(f"a server still answers on socket {socket}")
    except PmError as exc:
        left.append(f"the server on socket {socket} cannot be checked ({exc})")
    paths = [control, run_tmpdir(socket)]
    srt_pid = None
    if record is not None:
        foreign = _foreign_tmpdir(record, socket)
        if foreign is not None:
            paths.append(foreign)
        srt_pid = _record_srt_pid(record)
    left += [f"{path} remains" for path in paths if os.path.lexists(path)]
    if srt_pid is not None and _pid_alive(srt_pid):
        left.append(f"srt still running: {srt_pid}")
    return left


def _readable_record(control: Path) -> dict[str, Any] | None:
    """The record in `control`, or None when it is missing or unreadable."""
    try:
        return read_record(control)
    except PmError:
        return None


def run_record(state: dict[str, Any], *, repo: Path) -> dict[str, Any] | None:
    """The record in the control dir of the sandbox run state records, or
    None when the run records none, its block is malformed (`run_sandbox`)
    or the record is missing or unreadable."""
    if not records_sandbox(state):
        return None
    try:
        _block, _socket, control = run_sandbox(state, repo)
    except PmError:
        return None
    return _readable_record(control)


def sandbox_remains(state: dict[str, Any], *, repo: Path) -> list[str]:
    """What is still there of the sandbox run state records, one line each
    (`_remains`): a server answering on its socket, its control dir, the
    temp dir `make_tmpdir` names, and, when its record is readable, a temp
    dir the record names under another temp root and a surviving `srt`.
    Empty for an unsandboxed run; PmError from `run_sandbox` for a
    malformed block."""
    if not records_sandbox(state):
        return []
    _block, socket, control = run_sandbox(state, repo)
    return _remains(socket, control, _readable_record(control))


def _stop_unrecorded(state: dict[str, Any], run_dir: Path, socket: str) -> bool:
    """Whether a complete run's event log holds no `sandbox-stopped` event
    for `socket` since its last `complete` event: the run end's teardown
    went through, but the event recording it was lost (an append that
    failed). False for a run that is not complete. PmError naming the log
    when it cannot be read or holds a line that is not JSON (a truncated
    append, say)."""
    if state.get("status") != "complete":
        return False
    try:
        events = state_mod.read_events(run_dir)
    except (OSError, ValueError) as exc:
        raise PmError(
            f"the event log {run_dir / 'events.jsonl'} is unreadable or corrupt ({exc}), so whether this run's"
            " sandbox-stopped event was recorded cannot be told"
        ) from exc
    for event in reversed(events):
        data = event.get("data")
        if event.get("kind") == "sandbox-stopped" and isinstance(data, dict) and data.get("socket") == socket:
            return False
        if event.get("kind") == "complete":
            return True
    return True


def sandbox_left(state: dict[str, Any], *, repo: Path, run_dir: Path | None = None) -> bool:
    """Whether the run records a sandbox of which anything is still there
    (`sandbox_remains`), so a run end whose teardown failed can retry it.
    With `run_dir`, a complete run whose `sandbox-stopped` event was lost
    (`_stop_unrecorded`) counts too: recording it is what is left of the
    teardown. False for an unsandboxed run; PmError from `run_sandbox` for
    a malformed block."""
    if sandbox_remains(state, repo=repo):
        return True
    if run_dir is None or not records_sandbox(state):
        return False
    # `sandbox_remains` checked the block (`run_sandbox`), so its socket is a name.
    return _stop_unrecorded(state, run_dir, state["sandbox"]["socket"])


def kill_run_server(socket: str, record: dict[str, Any] | None = None) -> tuple[list[str], bool] | None:
    """The state-free kill of a run's server, for a scavenge whose teardown
    did not run or did not finish: None when no server answers on `socket`;
    otherwise `(remaining, srt_alive)`, the sessions `sessions.kill_server`
    reports still listed and whether `record`'s `srt_pid` still runs after
    up to 5 s. The pid is waited for only when `record` names `socket`.
    PmError when the server cannot be checked or killed."""
    if not sessions.server_running(socket, timeout=START_DEADLINE_SECONDS):
        return None
    srt_pid = _record_srt_pid(record) if record is not None and record.get("socket") == socket else None
    return _kill_and_wait(socket, srt_pid)


def _tear_down(socket: str, control: Path) -> tuple[list[str], bool]:
    """`tear_down` for a run's socket and control dir, with the record the
    control dir holds (none when an earlier teardown removed it); returns
    (notes, whether anything was there to tear down: `_remains`).

    PmError when the teardown is incomplete (`TeardownOutcome.left`):
    sessions still listed on the socket, an `srt` still running, or the
    control dir still there (kept for a retry, or refused as not a
    directory). The message names what remains and carries every note, so
    nothing is reported as torn down that is not. A temp dir left under
    another temp root does not make it incomplete: its note says where it
    is."""
    record = read_record(control) if os.path.lexists(control / RECORD_NAME) else None
    present = bool(_remains(socket, control, record))
    outcome = tear_down(socket=socket, control=control, record=record)
    if outcome.left:
        raise PmError(
            f"the teardown of the sandbox on socket {socket!r} is incomplete: {'; '.join(outcome.left)} (done:"
            f" {'; '.join(outcome.notes) or 'nothing'})"
        )
    return outcome.notes, present


def _retry_teardown(exc: Exception, recorded: str) -> PmError:
    """The PmError a run end raises when its teardown failed with `exc`:
    what the command did record, and that no `sandbox-stopped` event was."""
    return PmError(
        f"{exc}; {recorded}, but no sandbox-stopped event was written: retry the teardown (stop, or start-slice on"
        " a complete run) once that is cleared"
    )


def teardown_run(state: dict[str, Any], *, repo: Path, run_dir: Path, recorded: str) -> list[str] | None:
    """The run end of a sandboxed Mode B run: `tear_down` with the run's
    record, then a `sandbox-stopped` event when anything was there to tear
    down, or when the run is complete and the event recording its run-end
    teardown was lost (`_stop_unrecorded`); returns the notes for the caller
    to print, or None for an unsandboxed run.

    Idempotent: a control dir already removed (an earlier teardown) is torn
    down again from what `init` created, the socket's server if one answers
    and the temp dir `make_tmpdir` names, so a repeated teardown reports both
    as gone rather than failing, and appends no second event. A temp dir
    under another temp root is left in place with a note (`stop_server`).
    The control root `<common git dir>/pm-sandbox` is removed as well when
    nothing else is left in it.

    Every failure is a PmError carrying `recorded`, the caller's word for
    what it recorded before the teardown (the stop, the acceptance, the
    run's completion), and saying that no event was written: an incomplete
    teardown (`_tear_down`: a session still listed, `srt` still running,
    the control dir still there), an OSError, and the refusals made before
    anything is killed or removed: a malformed `sandbox` block in run state
    (an empty one included) or a control dir it names other than `<common
    git dir>/pm-sandbox/<socket>` (`run_sandbox`), an unreadable or
    malformed record (`read_record`), and a record whose pid, temp dir or
    control dir `stop_server` refuses.
    """
    if not records_sandbox(state):
        return None
    try:
        _block, socket, control = run_sandbox(state, repo)
        notes, present = _tear_down(socket, control)
        if present or _stop_unrecorded(state, run_dir, socket):
            state_mod.append_event(run_dir, "sandbox-stopped", note="; ".join(notes), data={"socket": socket})
    except (PmError, OSError) as exc:
        raise _retry_teardown(exc, recorded) from exc
    return notes


def scavenge_run(state: dict[str, Any], *, repo: Path, run_dir: Path) -> list[str] | None:
    """`stop --scavenge` without a token on readable state: tear the run's
    sandbox down from its host-written control record, as `teardown_run`
    does, and write nothing to run state (no event): without the token the
    state read here is unverified. Returns the notes, or None for an
    unsandboxed run.

    The unverified state can only point this at the run directory it was
    read from: PmError, before anything is killed or removed, unless its
    `run_id` is `run_dir`'s name and its socket is `pm-run-<run id>`, and
    for everything `teardown_run` refuses; PmError, after the kill, for an
    incomplete teardown (`_tear_down`)."""
    if not records_sandbox(state):
        return None
    if state.get("run_id") != run_dir.name:
        raise PmError(
            f"run state in {run_dir} names run id {state.get('run_id')!r}, not {run_dir.name!r}; refusing to tear"
            " down a sandbox on its word"
        )
    _block, socket, control = run_sandbox(state, repo)
    expected = f"{RUN_SOCKET_PREFIX}{run_dir.name}"
    if socket != expected:
        raise PmError(
            f"run state names sandbox socket {socket!r}, not this run's {expected!r}; refusing to tear it down"
        )
    try:
        notes, _present = _tear_down(socket, control)
    except (PmError, OSError) as exc:
        raise _retry_teardown(exc, "run state is not updated without the token") from exc
    return notes


# --- A campaign brief's pin ----------------------------------------------------


def _git_toplevel(directory: Path) -> Path | None:
    """The real path of the git toplevel `directory` lies in, or None when it
    lies in no repository (or git cannot say)."""
    code, top, _err = git_ops.git_result(directory, "rev-parse", "--show-toplevel")
    return _real(top.strip()) if code == 0 and top.strip() else None


def find_brief(path: Path | str) -> Path | None:
    """The dossier brief a campaign launcher at `path` sits under: `brief.md`
    at the root of the dossier, which is the git toplevel of the directory
    holding the launcher. None when that root holds no `brief.md`, or the
    launcher lies in no git repository (an unpinned launch, as when no brief
    is found). A `brief.md` anywhere below the root is not the brief.

    PmError when the toplevel of the launcher's directory as given differs
    from that of its real path's (a launcher symbolically linked in from
    another place): which brief governs it would depend on the spelling."""
    given = Path(path).expanduser()
    real = _real(given)
    toplevel, real_toplevel = (
        _git_toplevel(candidate if candidate.is_dir() else candidate.parent) for candidate in (given, real)
    )
    if toplevel != real_toplevel:
        raise PmError(
            f"the launcher {given} lies in {toplevel or 'no git repository'}, but its real path {real} lies in"
            f" {real_toplevel or 'no git repository'}; refusing to guess which dossier's brief governs it"
        )
    if toplevel is None:
        return None
    brief = toplevel / BRIEF_NAME
    return brief if brief.is_file() else None


def brief_sandbox_pin(text: str) -> str | None:
    """The SHA-256 a brief's `## Sandbox` section pins for the dossier's
    `sandbox.json`, or None when the brief has no such section or the
    section is `none` alone.

    A section that mentions `sandbox.json` is read as a pin and must carry
    SHA-256 `<64 hex digits>`; any other section is read as no pin and must
    hold nothing but `none` (one list marker, whitespace and case aside).
    Each failed reading raises PmError saying which reading failed, so a pin
    the owner meant to write is never read as no pin.
    """
    match = _SANDBOX_SECTION.search(text)
    if match is None:
        return None
    section = match.group(1)
    if DOSSIER_POLICY_NAME in section:
        pin = _PIN_SHA256.search(section)
        if pin is None:
            raise PmError(
                f"the brief's ## Sandbox section names {DOSSIER_POLICY_NAME} without its pin: read as a pin, it"
                " carries no SHA-256 `<64 lowercase hex digits>`; write the pin beside it"
            )
        return pin.group(1)
    if _NONE_ONLY.fullmatch(section):
        return None
    raise PmError(
        f"the brief's ## Sandbox section names neither {DOSSIER_POLICY_NAME} nor none: read as a pin, it does not"
        f" name {DOSSIER_POLICY_NAME}, and read as no pin, it holds more than `none` alone; write a pinned"
        f" {DOSSIER_POLICY_NAME}, or `- none` as the whole section"
    )
