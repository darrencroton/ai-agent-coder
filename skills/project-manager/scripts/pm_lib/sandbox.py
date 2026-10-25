"""The OS sandbox around a run's tmux server: `srt` policies and the server's life.

A sandboxed run's private tmux server runs in the foreground under Anthropic's
`sandbox-runtime` (`srt`), so every pane process it creates, and every
descendant of those, inherits the Seatbelt restrictions of the policy `srt`
was started with. This module is the toolkit's one seam onto that mechanism:

- the operator's base policy, loaded and refused when it defeats the boundary
  (`load_base_policy`);
- the policies the toolkit derives from it for a worktree and branch: the
  server's effective policy, a Mode B reviewer's, and the operator's attach
  client's (`derive_policy`, `derive_reviewer_policy`, `derive_attach_policy`),
  plus the refusal of a write grant reaching `srt`'s own installation
  (`check_binaries`);
- the host-only control directory and its record (`control_dir`,
  `write_record`, `read_record`), and the per-run temp directory
  (`make_tmpdir`, `remove_tmpdir`);
- the server itself: started under `srt`, verified by two probes, stopped
  (`start_server`, `verify_server`, `stop_server`), and a one-shot command
  wrapped the same way (`wrap_command`).

Paths in a policy are judged as `srt` resolves them: `~` and `~/` expanded,
symbolic links followed. A pattern entry (a string holding `*`, `?`, `[` or
`]`) is judged by the directory its fixed prefix names, as though it granted
that whole directory, which fails toward refusing. An entry written as
`{"path": ..., "literal": true}` is never a pattern.

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
from pathlib import Path
from typing import Any, Iterable

from . import PmError
from . import git_ops
from . import ledger
from . import sessions

SRT_MISSING = "srt is required for a sandboxed run; install @anthropic-ai/sandbox-runtime"
SSL_CERT_FILE = Path("/etc/ssl/cert.pem")
CONTROL_ROOT_NAME = "pm-sandbox"
RECORD_NAME = "record.json"
TMPDIR_PREFIX = "pm-sandbox-"
PROBE_DIR_NAME = ".pm"
PROBE_FILE_NAME = "sandbox-probe"

START_DEADLINE_SECONDS = 10.0
START_POLL_SECONDS = 0.1
SRT_EXIT_WAIT_SECONDS = 5.0

# `srt` grants every sandbox `/tmp/claude` (its shared TMPDIR) without being
# asked; a literal deny removes the grant, under both spellings because the
# directory may not exist yet when `srt` resolves the entry.
_SHARED_TMP_DENIES = ("/tmp/claude", "/private/tmp/claude")
# `srt`'s other implicit write grants, denied for the write-less attach client.
_IMPLICIT_GRANT_DENIES = ("~/.npm/_logs", "~/.claude/debug")
_PATTERN_SPLIT = re.compile(r"[*?\[\]]")

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
    filesystem lists. ValueError for anything else."""
    if isinstance(entry, str):
        return entry, False
    if (
        isinstance(entry, dict)
        and set(entry) == {"path", "literal"}
        and entry["literal"] is True
        and isinstance(entry["path"], str)
    ):
        return entry["path"], True
    raise ValueError(f"not a path entry: {entry!r}")


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


def _relation(grant: Path, target: Path) -> str | None:
    """How a resolved write grant stands to `target`: "is", "is an ancestor
    of", "is inside", or None when the two are unrelated."""
    if grant == target:
        return "is"
    if grant in target.parents:
        return "is an ancestor of"
    if target in grant.parents:
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


def _write_entries(policy: dict[str, Any]) -> list[Any]:
    filesystem = policy.get("filesystem")
    entries = filesystem.get("allowWrite", []) if isinstance(filesystem, dict) else []
    if not isinstance(entries, list):
        raise PmError("sandbox policy: filesystem.allowWrite is not a list")
    return entries


# --- The base policy -------------------------------------------------------------


def load_base_policy(path: Path | str) -> tuple[dict[str, Any], list[str]]:
    """Load the operator's base policy; return `(policy, warnings)`.

    PmError, naming the file and the reason, for: an unreadable or non-JSON
    file; a top level that is not an object holding `network` and
    `filesystem` objects; `network.allowAllUnixSockets`, `allowAppleEvents`
    or `filesystem.disabled` set (each defeats the boundary outright); an
    `allowWrite` entry that is not a path entry, is relative, or resolves to
    `/`, the home directory, `toolkit_root()` or the tmux socket directory.

    `warnings` holds one line for `enableWeakerNetworkIsolation` and one for
    `network.allowLocalBinding`, when set, each naming what the option opens;
    the caller prints them. The policy is returned as parsed: deriving from
    it never mutates it, and `srt` applies its own schema check on top.
    """
    path = Path(path)

    def refuse(reason: str) -> PmError:
        return PmError(f"sandbox policy {path}: {reason}")

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise refuse(f"unreadable ({exc})") from exc
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

    entries = filesystem.get("allowWrite", [])
    if not isinstance(entries, list):
        raise refuse("filesystem.allowWrite is not a list")
    forbidden = (
        (Path("/"), "the filesystem root"),
        (_real(Path.home()), "the home directory"),
        (toolkit_root(), "the toolkit checkout"),
        (_socket_dir(), "the tmux socket directory"),
    )
    for entry in entries:
        try:
            text_entry, _literal_entry = _entry_text(entry)
        except ValueError:
            raise refuse(f"allowWrite entry {entry!r} is neither a path nor a {{path, literal: true}} object") from None
        if not _is_absolute_entry(text_entry):
            raise refuse(
                f"allowWrite entry {text_entry!r} is relative: it would resolve against the server's working directory"
            )
        resolved = _resolve_entry(entry)
        for target, label in forbidden:
            if resolved == target:
                raise refuse(f"allowWrite entry {text_entry!r} resolves to {label} ({target})")

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
    needs there, and such an entry would reach the reviewer policy too."""
    for entry in _write_entries(base):
        try:
            text, _literal_entry = _entry_text(entry)
        except ValueError as exc:
            raise PmError(f"sandbox policy: allowWrite {exc}") from None
        resolved = _resolve_entry(entry)
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
    the first four)."""
    policy = copy.deepcopy(base)
    network = policy.setdefault("network", {})
    filesystem = policy.setdefault("filesystem", {})
    if not isinstance(network, dict) or not isinstance(filesystem, dict):
        raise PmError("sandbox policy: network and filesystem must be objects")
    for section, keys in (
        (network, ("allowedDomains", "deniedDomains")),
        (filesystem, ("denyRead", "allowRead", "allowWrite", "denyWrite")),
    ):
        for key in keys:
            if not isinstance(section.setdefault(key, []), list):
                raise PmError(f"sandbox policy: {key} is not a list")
    return policy


def _repository_denies(worktree: Path, git_dir: Path, common: Path, control_root: Path) -> list[dict[str, Any]]:
    """The `denyWrite` entries every server and reviewer policy carries,
    whatever runs inside: the repository's config and hooks, the control
    root, `srt`'s shared temp directory and, where `.git` is a file (a linked
    worktree), the pointer files that would let a pane redirect the host's
    next git command to a repository of its own."""
    denies = [
        _literal(common / "config"),
        _literal(common / "hooks"),
        _literal(control_root),
        *(_literal(path) for path in _SHARED_TMP_DENIES),
    ]
    if (worktree / ".git").is_file():
        denies += [
            _literal(worktree / ".git"),
            _literal(git_dir / "commondir"),
            _literal(git_dir / "gitdir"),
            _literal(git_dir / "config.worktree"),
            _literal(git_dir / "hooks"),
        ]
    return denies


def _repository_reads(worktree: Path, common: Path, extra_read: Iterable[Path | str]) -> list[str]:
    return [str(worktree), str(common), str(toolkit_root()), *(str(path) for path in extra_read)]


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
    socket becomes reachable and `allowPty` is set.

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
    allow_write += [str(worktree), str(git_dir)]
    if git_dir != common:
        allow_write += [
            str(common / "objects"),
            _literal(common / "refs" / "heads" / branch),
            _literal(common / "refs" / "heads" / f"{branch}.lock"),
            _literal(common / "logs" / "refs" / "heads" / branch),
            _literal(common / "logs" / "refs" / "heads" / f"{branch}.lock"),
            _literal(common / "packed-refs.lock"),
        ]
    allow_write += [socket_grant, str(_real(tmpdir))]
    if pm_inside:
        allow_write.append(str(_real(ledger.ledger_dir())))

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
    filesystem["allowWrite"].append(str(_real(tmpdir)))
    filesystem["denyWrite"] += _repository_denies(worktree, git_dir, common, _real(control_root))
    filesystem["allowRead"] += _repository_reads(worktree, common, extra_read)
    policy["allowPty"] = True
    return policy


def derive_attach_policy(base: dict[str, Any], *, socket: str, control_root: Path) -> dict[str, Any]:
    """The operator's attach client's policy: `base`'s network (the socket
    added) and read lists, no write grant at all, and `base`'s `denyWrite`
    plus the control root and `srt`'s implicit grants. A pane can make an
    attached client run a command (`detach-client -E`); under this policy
    that command can write nothing. `base` is not mutated."""
    network = copy.deepcopy(base.get("network") or {})
    for key in ("allowedDomains", "deniedDomains"):
        network.setdefault(key, [])
    network.setdefault("allowUnixSockets", []).append(_socket_grant(socket))
    filesystem = base.get("filesystem") or {}
    deny_write = copy.deepcopy(filesystem.get("denyWrite") or [])
    deny_write += [_literal(_real(control_root)), *(_literal(path) for path in _SHARED_TMP_DENIES)]
    deny_write += [_literal(path) for path in _IMPLICIT_GRANT_DENIES]
    return {
        "network": network,
        "filesystem": {
            "denyRead": copy.deepcopy(filesystem.get("denyRead") or []),
            "allowRead": copy.deepcopy(filesystem.get("allowRead") or []),
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
    real `node` that runs it: `srt` documents a writable installation as a
    way to run code on the host."""
    package = srt_package_root()
    node = shutil.which("node")
    if node is None:
        raise PmError("node is required to run srt but is not on PATH")
    node_real = _real(node)
    for entry in _write_entries(policy):
        try:
            text, _literal_entry = _entry_text(entry)
        except ValueError as exc:
            raise PmError(f"sandbox policy: allowWrite {exc}") from None
        resolved = _resolve_entry(entry)
        relation = _relation(resolved, package)
        if relation:
            raise PmError(
                f"sandbox write grant {text!r} {relation} srt's installation ({package}): a sandbox that can"
                " write srt can run code on the host"
            )
        if resolved == node_real or resolved in node_real.parents:
            relation = "is" if resolved == node_real else "is an ancestor of"
            raise PmError(
                f"sandbox write grant {text!r} {relation} the node that runs srt ({node_real}): a sandbox that"
                " can write it can run code on the host"
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
    if not socket or "/" in socket or socket in {".", ".."}:
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
    time. Nothing inside the sandbox can write beneath the control root, so
    a record found there was written by the host.
    """
    control = Path(control)
    control.mkdir(parents=True, exist_ok=True)
    target = control / RECORD_NAME
    staging = control / f"{RECORD_NAME}.tmp"
    staging.write_text(json.dumps(fields, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
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


def remove_tmpdir(path: Path | str) -> bool:
    """Remove a sandbox temp dir recursively; True when removed, False when
    already gone. PmError, removing nothing, unless its name starts with
    `pm-sandbox-` and its parent is `tempfile.gettempdir()`, or when it is
    not a directory."""
    path = Path(path)
    if not path.name.startswith(TMPDIR_PREFIX) or _real(path.parent) != _real(tempfile.gettempdir()):
        raise PmError(
            f"refusing to remove {path}: not a {TMPDIR_PREFIX}* directory directly under {tempfile.gettempdir()}"
        )
    if not os.path.lexists(path):
        return False
    if path.is_symlink() or not path.is_dir():
        raise PmError(f"refusing to remove {path}: not a directory")
    try:
        shutil.rmtree(path)
    except OSError as exc:
        raise PmError(f"could not remove sandbox temp dir {path}: {exc}") from exc
    return True


# --- The server -------------------------------------------------------------------


def _server_pid(socket: str) -> int:
    argv = ["tmux", "-L", socket, "display-message", "-p", "#{pid}"]
    try:
        result = subprocess.run(argv, check=False, text=True, capture_output=True)
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
        process.wait(timeout=5)
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

    PmError, before anything starts, off macOS or when a server already
    answers on `socket`. The server must answer within 10 s (polled every
    0.1 s); when `srt` exits first, the deadline passes or the probe cannot
    look, the process group is killed and the PmError quotes the log's last
    three lines. No retry and no fallback to an unsandboxed server.
    """
    if platform.system() != "Darwin":
        raise PmError("sandboxing is implemented for macOS only")
    if sessions.server_running(socket):
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
    while True:
        returncode = process.poll()
        if returncode is not None:
            failure = f"srt exited with status {returncode} before the server answered"
            break
        try:
            if sessions.server_running(socket):
                server_pid = _server_pid(socket)
                _STARTED[process.pid] = process
                return process.pid, server_pid
        except PmError as exc:
            failure = str(exc)
            break
        if time.monotonic() >= deadline:
            failure = f"the server did not answer within {START_DEADLINE_SECONDS:g} s"
            break
        time.sleep(START_POLL_SECONDS)
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
       When it does not, the file is removed, the server is killed, and the
       error names the probe;
    3. the positive probe: a `run-shell` touch of `<worktree>/.pm/sandbox-probe`
       must succeed and leave the file, which is then removed, so a policy
       that does not grant the worktree is caught here. The host creates
       `<worktree>/.pm` first when it is absent (and removes it again).
    """
    value = sessions.server_environment(socket, "SANDBOX_RUNTIME")
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
    returncode, output = sessions.run_shell(socket, f"touch {shlex.quote(str(outside))}")
    if returncode == 0 or os.path.lexists(outside):
        try:
            outside.unlink(missing_ok=True)
        finally:
            sessions.kill_server(socket)
        raise PmError(
            f"negative probe failed: the server on socket {socket!r} could create {outside}, outside any sandbox"
            " grant, so it is not confined; the server was killed"
        )

    probe_dir = Path(worktree) / PROBE_DIR_NAME
    created = not probe_dir.exists()
    inside = probe_dir / PROBE_FILE_NAME
    try:
        probe_dir.mkdir(exist_ok=True)
        inside.unlink(missing_ok=True)
        returncode, output = sessions.run_shell(socket, f"touch {shlex.quote(str(inside))}")
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


def stop_server(*, socket: str, record: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Tear down the server `record` describes; return `(remaining, notes)`.

    `sessions.kill_server(socket)` ends the server (and with it `srt` and
    its proxy); `remaining` is what it reports still listed. Then: up to 5 s
    for `record["srt_pid"]` to exit (`srt still running: <pid>` when it does
    not); the record's temp dir removed through `remove_tmpdir`; the control
    directory, derived from the record's worktree's common git dir and
    `socket`, removed. `notes` holds one line per step's outcome, removals
    and already-gone directories included, for the caller to print.
    """
    remaining = sessions.kill_server(socket)
    notes: list[str] = []

    srt_pid = record.get("srt_pid")
    if isinstance(srt_pid, int) and srt_pid > 0:
        deadline = time.monotonic() + SRT_EXIT_WAIT_SECONDS
        while _pid_alive(srt_pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        if _pid_alive(srt_pid):
            notes.append(f"srt still running: {srt_pid}")

    tmpdir = record.get("tmpdir")
    if tmpdir:
        if remove_tmpdir(tmpdir):
            notes.append(f"removed sandbox temp dir {tmpdir}")
        else:
            notes.append(f"sandbox temp dir already gone: {tmpdir}")

    control = control_dir(_common_dir_of(Path(record["worktree"])), socket)
    if control.is_dir() and not control.is_symlink():
        try:
            shutil.rmtree(control)
        except OSError as exc:
            raise PmError(f"could not remove sandbox control dir {control}: {exc}") from exc
        notes.append(f"removed sandbox control dir {control}")
    else:
        notes.append(f"sandbox control dir already gone: {control}")
    return remaining, notes
