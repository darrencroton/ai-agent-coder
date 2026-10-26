# Sandboxed Runs: the Operator's Guide and the Sandbox Contract

An optional, OS-enforced boundary around the processes a run launches: in Mode C the child PM, its Developers and its reviewers; in Mode B the Developers and reviewers. The boundary is Anthropic's `sandbox-runtime` (`srt`), a macOS Seatbelt plus egress-proxy wrapper. The toolkit starts it, verifies it and tears it down through one seam (`scripts/pm_lib/sandbox.py`); it does not become a containment layer of its own, so isolation stays the environment's job as [VISION.md](../../../docs/VISION.md) says. When no sandbox is asked for, nothing changes.

This file is the single home of the sandbox contract: the enforcement table, the limitations, the base-policy refusals and warnings, the grants and denies the toolkit derives, the example policy and the per-harness facts. Every other document links here. What the Campaign Manager does is in [campaign-mode.md](campaign-mode.md) *Sandbox*; the run-state keys are in [run-state.md](run-state.md).

Every statement below names the layer that enforces it:

- **OS**: Seatbelt restrictions and `srt`'s egress proxy, applied by the kernel to every process inside, whatever it is told or tries.
- **Toolkit check**: a mechanical check the host-side toolkit makes before or while it starts a server (a policy refusal, a probe, a pin), failing closed.
- **Floor**: PM's seven floor facts ([SKILL.md](../SKILL.md)), unchanged by sandboxing.
- **Harness mode**: whatever a harness's own permission mode still restricts.
- **Instructions**: what a seat's prompt tells it.
- **Operator judgement** and **operator discipline**: what the operator chooses to grant, and rules only the operator can keep.

Facts marked *tested* were established on macOS 27.0.1 with tmux 3.5a, git 2.54, `srt` 0.0.81 and Python 3.14.

## The boundary

The run's private tmux server runs in the foreground under `srt`:

```sh
srt -s <control dir>/policy.json env TMPDIR=<per-run temp dir> SSL_CERT_FILE=/etc/ssl/cert.pem tmux -L <socket> -D
```

Seatbelt restrictions are per-process kernel state inherited across `fork` and `exec`, so every pane the server ever creates, whichever client asked for it, and every descendant of those panes (reviewers started in their own process groups included) runs under the policy (OS, *tested*). `-D` keeps tmux in the foreground, so `srt` stays its parent and `srt`'s in-process proxy stays up for the server's whole life; `tmux kill-server` ends the server, `srt` exits and the proxy closes (*tested*). The `SSL_CERT_FILE` pair is passed only when `/etc/ssl/cert.pem` exists. The server's working directory is the worktree, its stdin is closed, its output goes to `<control dir>/srt.log`, and `PM_RUN_TOKEN` is absent from its environment.

Sessions are created *through* the server (`tmux -L <socket> run-shell '<new-session …>'`), so the `new-session` client runs inside the sandbox, and a socket with no server answering fails instead of letting a plain `new-session` start an unsandboxed replacement (toolkit check; `run-shell` never starts a server, *tested*). The host's one-shot clients (`wait-child`, `answer-child`, `stop-child`, `observe`, `send`) connect to the socket unrestricted: the OS does not govern the host connecting to a sandboxed process's socket, and a one-shot client carries no session, so by tmux's semantics (reasoned from them, not tested) a pane cannot make it run anything.

**An attached client is part of the boundary.** A pane can run `tmux detach-client -t <client> -E '<command>'`, and tmux runs that command *in the attached client process*, on the host, unsandboxed; a read-only attach (`-r`) does not prevent it (*tested, demonstrated*). So a sandboxed run is watched only through a client sandboxed under the attach policy, which has no write grant at all:

```sh
srt -s <control dir>/attach-policy.json tmux -L <socket> attach -t <session>
```

The session renders normally and an injected command fails with "operation not permitted" inside the sandboxed client, whatever directory the operator attached from (OS, *tested*). Never attach to a sandboxed server any other way: that rule is operator discipline, since nothing can stop a same-user process from attaching plainly.

## What enforces what

| Property | Layer |
|---|---|
| Writes confined to the effective policy's grants: the worktree, its git dir, the granted git-dir files, the temp dir, the socket, the harness state the operator listed, `srt`'s implicit `~/.npm/_logs` and `~/.claude/debug`, and in Mode C the ledger directory | OS (Seatbelt) |
| Reads of denied paths impossible, and their existence hidden | OS, to the extent of the operator's `denyRead` list; every other read is allowed |
| Egress only to listed `host:port`s through the proxy; no raw sockets; no DNS from inside | OS deny plus `srt`'s proxy, subject to the exceptions the operator enables (`enableWeakerNetworkIsolation` opens the `trustd` path; `allowLocalBinding` opens loopback and this host's addresses) and filtering by host and port only, never by content |
| No second sandbox, no shared Codex daemon socket, no `ps` of host processes, no signals to host processes | OS (the daemon lock is unwritable and no socket but tmux's is reachable); Codex `--no-daemon` and Claude Code's `--settings` override are harness launch settings that keep the harness from trying |
| The escape through git metadata the host's next git command reads, in each checkout shape: the pointer files `commondir`, `gitdir` and `config.worktree` and the common git dir's `config` and `hooks` (every checkout), the `worktrees` directory (plain checkout), the `.git` file and the git dir's `hooks` (linked worktree); the state root (Mode B) and the control root unwritable; `/tmp/claude` unwritable | OS, by toolkit-derived literal denies the base policy cannot override; the list is under *The effective policy* |
| A base policy that defeats the boundary, or a write grant reaching `srt` or `node`, never starts a server | toolkit check, before anything is created |
| The server is really sandboxed before a session starts, and a session can only ever be created inside it | toolkit probes (host-checked) and session creation through `run-shell`, fail closed |
| A host-launched `srt` reads a policy the sandbox could not have written | the control directory, unwritable from inside (OS) |
| Every host open (read or write) of a file under the sandbox-writable `<worktree>/.pm/` follows no symbolic link and accepts no hard link, FIFO or directory, so a planted link cannot redirect it | toolkit check (`pm_paths`, whose module docstring holds the rule; a refusal is a `PmError`, and the floor's result fact fails on one); [run-state.md](run-state.md) says where it applies and names the exceptions that are not opens |
| `detach-client -E` cannot run on the host, and under the attach policy can write nothing but the client's terminal | OS, when the operator attaches under the attach policy; the plain-attach prohibition is operator discipline |
| Changed files within the authorized surface, protected paths untouched, clean worktree, ancestry | the floor, unchanged |
| A reviewer stays read-only inside a Mode C child | instructions and harness mode where one applies; the floor for final Git-visible state only; not the OS |
| A Mode B reviewer stays read-only on the repository and off the Developer server | OS (the reviewer policy) |
| Which paths and hosts are granted, and whether that is sensible | operator judgement, recorded in the pinned (Mode C) or hashed (Mode B) base policy |
| A Campaign Manager passes `--sandbox` on every launch | toolkit check: `launch-child` refuses a launch without `--sandbox`, or with a file that does not match the pin, when the launcher's dossier brief pins a policy |
| Every command that drives a sandboxed Mode B run's tmux server (listed under *Mode B: a sandboxed run*) reaches only that server | toolkit check: each refuses when `PM_TMUX_SOCKET` is unset or names another server |

**The Mode C reviewer caveat.** Inside a Mode C child the reviewer runs inside the child's sandbox, where its harness's own sandbox cannot start (a second Seatbelt profile cannot be applied inside one), so `review` launches it with that sandbox turned off: Codex at `sandbox_mode="danger-full-access"`, Qwen without `--sandbox`, Claude Code with `--settings '{"sandbox":{"enabled":false}}'`. It has the same write grants as the Developer, so its read-only-ness rests on its instructions and on whatever its harness mode still restricts (`plan` agents for Claude Code and OpenCode; nothing mechanical for Codex at full access, Copilot at `--allow-all` or Qwen at `--yolo` without `--sandbox`), and on the floor only for what the floor sees: a tree left dirty fails fact 7, while an ignored file written, or a tracked file edited and restored, is invisible to it.

## What it is not: limitations and residuals

- **Reads are allow-by-default** (OS). Transcripts, configuration and credential files of the harnesses themselves are readable unless the operator denies them; the egress policy, not read confinement, is what keeps them on the machine. The model API a seat may reach already sees whatever that seat reads, and a host allowed for one purpose (`api.github.com` for Copilot) carries any traffic to that host. Credentials one seat needs (`~/.codex/auth.json`, the Keychain database for Claude Code, OpenCode's key files) are readable by every seat on the same server, because one server carries one policy.
- **A Mode C reviewer can write the worktree**: the caveat above. A detached reviewer survives `stop-child`, sandboxed and network-less; `stop-child` prints its process group for the by-hand check and never signals a pgid read from a child-writable file.
- **The Mode C ledger.** The child PM can write any file in the ledger directory, so a sandboxed child could alter another run's ledger file; the ledger is the toolkit's model-performance record, not run evidence. The default ledger directory is `ledger/` inside the toolkit checkout, a deliberate Mode C grant and the only derived write grant inside that checkout; `PM_LEDGER_DIR` moves it wherever the operator prefers.
- **Its own branch (linked worktree).** The sandbox can force-update or delete its own branch's ref (OS allows it, by the exact-ref grant); the floor's ancestry fact catches a rewritten head at the next `finalize`.
- **A plain checkout has no exact-ref grants.** A plain checkout is possible only in Mode B (`launch-child` always makes a linked worktree). There the worktree grant covers `.git/`, so a Developer can create or move any branch, tag or stash, not only the run branch (OS allows it, by the worktree grant). The floor's ancestry fact sees only the run branch's head, so no fact sees the rest. The denies under *The effective policy* remove only the paths that redirect the host's next git command.
- **Repositories with submodules are not supported** (toolkit check): `git_ops.git_status_text`, the host-side `git status` on every repository a sandbox can write, and the worktree-mode `git diff` in `git_diff_text` refuse a repository whose index holds a gitlink (found with `git ls-files --stage`, which executes no repository configuration), because `git status` and `git diff` would run a submodule's configuration on the host, and a gitlink planted from inside the sandbox can point at configuration it controls. The refusal applies to every run, sandboxed or not; the one host-side `status` it does not cover is `campaign-preflight`'s `git status --porcelain` on the dossier, the Campaign Manager's own repository: the toolkit grants no sandbox a write there, and keeping the dossier out of the base policy's write grants is operator judgement. Residuals: the window between the `ls-files` check and the `status` (a detached sandboxed process could add a gitlink in between), and the PM/CM agents' own git commands in such a repository stay exposed.
- **`core.hooksPath` set to a path inside the worktree** (both modes, with or without `extensions.worktreeConfig`; operator judgement): when the repository's configuration already points `core.hooksPath` at a directory the Developer can write (`.githooks`, a husky directory), hooks planted there run when the host or the operator next runs a git command that has one. The derived denies cover only the git dirs' `hooks`, and the floor sees the edit only when the hooks directory is Git-visible rather than ignored. Check the repository's configuration before a sandboxed run, and keep `core.hooksPath` outside the worktree or unset.
- **When the repository under development is the toolkit checkout itself**, the worktree grant covers the code the host-side PM runs, so a Developer can change what the host executes next (OS allows it, by the worktree grant); nothing here closes that, so running the PM from a separate checkout of the toolkit is operator judgement.
- **Case-insensitive volumes.** Seatbelt enforces case-variant spellings of a path as the same path on case-insensitive APFS; the toolkit judges grants by file identity to match.
- **Temp space.** macOS `mktemp` without a template ignores `TMPDIR` and fails (*Troubleshooting*). In either mode, a teardown run under a `TMPDIR` other than the one the sandbox was set up under cannot reach the run's temp dir: it still kills the server and removes the control directory, leaves the temp dir in place and says so, naming the difference, and that never makes the teardown incomplete (*Teardown*).
- **One temp dir for the Mode B Developer server and every reviewer** (OS): the run's temp dir is a write grant of the server's policy and the only one of the reviewer policy, so it is a channel between a Developer and a reviewer that the reviewer policy does not close.
- **Halts keep the server up** (Mode B): a changed plan file, an exhausted attempt budget (at a relaunch or at `finalize --steer`) and `finalize --stop` each leave the sandboxed server and its proxy running until a human acts; only a run's end tears the sandbox down.
- **A base-policy edit mid-run does not reach a running server**: `srt` reads its policy once, at start. A Mode B restart (`start-slice` with no server answering) re-reads the file and refuses it when its SHA-256 no longer matches the one `init` recorded, so an edit takes effect only for a run or child started afterwards; in Mode C the policy is the pinned `sandbox.json`, and a changed pin is the owner's decision.
- The proxy's per-run credentials sit in every pane's environment; they are local and die with the server.
- Claude Code's directory trust is per path and stored in `~/.claude.json`, which stays read-only (it also holds user-scope MCP server definitions, so a writable copy would be a way to run code in the owner's own sessions); node worktrees under a parent the owner trusted in each harness prevent the dialog, and readiness still fails closed on one.
- Harness self-updates fail inside (denied downloads or an unwritable install directory) and print warnings; Copilot still downloads a package into its own home.
- MagicDNS resolution through the proxy failed once in testing and succeeded on retry.
- `srt` is a pre-1.0 package (0.0.81 tested); its configuration surface has changed across releases. The toolkit records `srt --version` in the control record, uses only keys verified here, and `campaign-preflight` reports the installed version.
- **Two residuals in the toolkit's own bookkeeping.** A policy grant is compared by file identity, so a hard link to a protected file under a granted directory is outside the path model; `srt` enforces by canonical path, so the write still goes through the inode (OS; low). And the `srt` pid recorded for a teardown can be reused by another process between the record and the wait: the wait then reports `srt still running` for a stranger, and the toolkit never signals it (toolkit check).
- **Not enforced by anything here**: what a harness sends to its model provider; the toolkit's own host-side processes (the Campaign Manager, `launch-child`, the setup script, the Campaign Manager's read-only task delegates through `orchestrator`, the Mode B PM), which stay unsandboxed by design, the supervising seat being the trusted one.

## Requirements

- macOS: the toolkit refuses to start a sandboxed server elsewhere ("sandboxing is implemented for macOS only").
- `srt` on `PATH` (`npm install -g @anthropic-ai/sandbox-runtime`), which needs Node 22.12 or newer (its `engines.node`) and, on macOS, ripgrep, which `srt`'s README lists for macOS (`brew install ripgrep`). Without `srt`, a sandboxed launch is refused: "srt is required for a sandboxed run; install @anthropic-ai/sandbox-runtime".
- `tmux` 3.5a was tested; `run-shell` must return its command's exit status, which the probes rely on.

## The base policy

The operator's base policy is a plain `srt` settings file, written and owned by the operator: the egress allowlist, the harness homes and their writable state, reference-data directories, the paths that must be unreadable, and any `srt` option the operator consciously enables. The toolkit never edits it. In Mode C it is `sandbox.json` in the dossier, pinned in `brief.md` by SHA-256 and checked by `campaign-preflight` at every reconcile; in Mode B it is any path given to `init --sandbox`, whose SHA-256 `run.json` records.

Its shape is `srt`'s own. `srt` 0.0.81 requires five keys, `network.allowedDomains`, `network.deniedDomains`, `filesystem.denyRead`, `filesystem.allowWrite` and `filesystem.denyWrite` (a missing key, or a malformed file, is refused by `srt` with exit 1 and no server); the toolkit adds them, and `filesystem.allowRead`, as empty lists to every policy it derives when the base omits them. The other keys used here are `network.allowUnixSockets`, `network.allowLocalBinding`, `filesystem.allowRead`, `allowPty` and `enableWeakerNetworkIsolation`. A filesystem entry is an absolute path or one starting with `~`, optionally a glob (`*`, `?`, `[` or `]`), or an object `{"path": …, "literal": true}` that is never a glob; `srt` expands `~` and follows symbolic links, and `denyWrite` wins over any `allowWrite`. Network entries are `host[:port]`, `*.host[:port]` or an IP literal with a port; an entry without a port allows every port; there are no CIDRs.

### Refusals (toolkit check, before anything starts)

`init --sandbox`, `launch-child --sandbox` and `campaign-preflight` refuse, naming the file and the reason, a base policy:

- that cannot be read, is not JSON, or whose top level is not an object holding `network` and `filesystem` objects;
- that sets `network.allowAllUnixSockets` (every Unix socket on the host reachable), `allowAppleEvents` (drive applications on the host) or `filesystem.disabled` (filesystem confinement off), each of which defeats the boundary outright;
- in which `allowedDomains`, `deniedDomains`, `allowUnixSockets`, `denyRead`, `allowRead`, `allowWrite` or `denyWrite` is present but not a list;
- with an `allowWrite` entry that is not a path entry, holds a NUL, control or lone-surrogate character, or is relative (it would resolve against the server's working directory; a `~user` spelling counts as relative, because `srt` expands only `~`);
- with an `allowWrite` entry that resolves, as `srt` resolves it, to the filesystem root or the home directory (by equality only: every grant lies inside `/`, and grants inside the home directory are normal);
- with an `allowWrite` entry that is, contains or lies inside the toolkit checkout (the directory holding `skills/project-manager`) or the tmux socket directory (`/private/tmp/tmux-<uid>/`, or under `TMUX_TMPDIR`; the negative probe's ground), however spelled;
- with a `network.allowUnixSockets` entry that is malformed or relative as an `allowWrite` entry would be, or that is, contains or lies inside the tmux socket directory (`srt` renders each entry as a `subpath` connect rule, so a pane could connect to the operator's own unsandboxed tmux server).

When the policy is derived for a worktree, the toolkit also refuses:

- an `allowWrite` entry that is the worktree or the common git dir, an ancestor of either, or a path inside either: the toolkit grants exactly what a run needs inside the repository, and such an entry would reach the reviewer policy, which must not write the repository;
- any write grant, base or derived, that is equal to, above or inside `srt`'s package directory (the real path of the `srt` entry point, walked up to its `package.json`), or equal to or above the real path of the `node` that runs it or the `srt` and `node` launchers as `PATH` spells them (a grant on a launcher's directory, such as `/opt/homebrew/bin`, would let a sandbox replace the `srt` the host runs next), because `srt` documents a writable installation as a host-execution path. `campaign-preflight` makes this check on the base policy, and the macOS check, so it reports what `launch-child` would refuse.

A pattern entry is judged by the directory its fixed prefix names, as though it granted that whole directory, which fails toward refusing. `srt` applies its own schema check on top.

### Warnings

Two options are allowed with a printed `WARNING:` line, because a seat may need one and the operator may choose either (operator judgement):

- `enableWeakerNetworkIsolation`: opens the macOS `trustd` service to the sandbox, which `srt` documents as a data-exfiltration path. Copilot needs it.
- `network.allowLocalBinding`: opens loopback and this host's own interface addresses to the sandbox, outside the egress proxy.

`campaign-preflight` prints each as a `note`.

## What the toolkit adds: the three derived policies

At each server start the toolkit derives the policies from the base and from facts only it has at that moment, and writes them into the control directory. Every filesystem entry it adds is a literal path entry (`{"path": …, "literal": true}`), so a worktree under `work[1]` never also grants `work1`; `network.allowUnixSockets` takes the socket path as a plain string. The base is never modified.

**The effective policy** (`policy.json`, what the server runs under):

- `allowWrite` gains: the worktree; its git dir (`git rev-parse --absolute-git-dir`); when the git dir differs from the common dir (a linked worktree), `<common>/objects` and the literal files `<common>/refs/heads/<branch>`, `<common>/refs/heads/<branch>.lock`, `<common>/logs/refs/heads/<branch>`, `<common>/logs/refs/heads/<branch>.lock` and `<common>/packed-refs.lock`; the tmux socket path, under its real directory; the per-run temp dir; and, only when the PM itself runs inside (Mode C), the ledger directory, because the child PM writes its run's ledger file from inside. In Mode B the host-side PM writes the ledger, so the server gets no such grant.
- `denyWrite` gains, always: `<common>/config`, `<common>/hooks`, the control root `<common>/pm-sandbox`, `/tmp/claude` and `/private/tmp/claude` (`srt`'s shared temp directory, which it grants every sandbox unasked) and, in every checkout, the pointer files `<git dir>/commondir`, `<git dir>/gitdir` and `<git dir>/config.worktree` (git honours them in a plain checkout too, where it never writes them, so a pane that wrote one could point the host's next git command at a repository of its own; `config.worktree` is read when `extensions.worktreeConfig` is set). In a plain checkout, where the git dir is the common dir, also `<git dir>/worktrees` (the other linked worktrees' pointer files; `git worktree add` fails inside). Where the worktree's `.git` is a file (a linked worktree): `<worktree>/.git` and `<git dir>/hooks`, which close a demonstrated escape (a pane rewrote `.git` to point at a repository with `core.fsmonitor` set, and the host's next `git status` ran it). `modules` stays writable in both shapes, because the toolkit refuses a gitlink before any host `status` on a repository a sandbox can write (the submodules bullet under *What it is not*). In Mode B, where the PM runs outside: the state root `<git dir>/pm`. `.git` as a *directory* is never denied, because that breaks `git add` (`index.lock`).
- `allowRead` gains the worktree, the common git dir, the toolkit checkout and the launcher's directory (Mode C: `nodes/<node>/`, where the node plan sits; Mode B: the plan file's directory), so an operator who denies reads of the home directory need not rediscover these.
- `network.allowUnixSockets` gains the socket path, and `allowPty` is `true` (without it every `new-session` fails with `create window failed: fork failed`).

In a linked worktree the exact-file ref grants mean the sandbox updates its own branch and nothing else under `refs/heads` (OS, *tested*): a sibling branch, `main`, `integration/*`, tags, stash, `refs/remotes/*`, `git worktree add`, `gc`, `pack-refs`, `git config` and the main checkout's index are all denied, and the Developer prompt's sandbox paragraph says so. A plain checkout has no such grants (the plain-checkout bullet under *What it is not*).

**The reviewer policy** (`reviewer-policy.json`, Mode B only): the base, plus the same reads, the same repository denies and a write grant on the temp dir only. No worktree or git-dir write and no tmux socket, so a Mode B reviewer is mechanically read-only on the repository and cannot reach the Developer server (OS).

**The attach policy** (`attach-policy.json`): the base's `network` with the socket added, the base's `denyRead` and `allowRead`, an empty `allowWrite`, `allowPty` true, and the base's `denyWrite` plus the control root, `/tmp/claude`, `/private/tmp/claude`, `~/.npm/_logs` and `~/.claude/debug` (the last four remove `srt`'s implicit write grants, *tested*).

## The control directory

`<common git dir>/pm-sandbox/<socket>/`, written by the host and never writable from inside (the derived deny on the control root, OS). It holds:

| File | Purpose |
|---|---|
| `policy.json` | the effective policy `srt` reads when the server starts |
| `attach-policy.json` | the operator's watch |
| `reviewer-policy.json` | the Mode B reviewer wrap |
| `record.json` | worktree, branch, socket, temp dir, base policy path and SHA-256, `srt` version, the `srt` pid and the server pid (null until a Mode B server starts), started-at, and the control directory itself |
| `srt.log` | the server's `srt` output |

`srt` reads its settings once, at start; but a Mode B server restart and every Mode B reviewer wrap re-read a policy file, and a Developer-writable file there would let a Developer hand the next host-launched `srt` a policy of its own choosing. The control directory is what makes that handoff trustworthy. Its location is never read from a child-writable file: `stop-child` derives it from the worktree's common git dir and the manifest's socket, and the record found there must name this worktree, so a manifest pointing at another socket meets a host-written record for another worktree and is refused (toolkit check). `run.json` (Mode B, MAC-protected and host-written) names it outright; the Mode C manifest carries it for the reader's convenience only.

The per-run temp dir is `pm-sandbox-<socket>` under the launching process's temp directory (`$TMPDIR`), exported to the server as `TMPDIR`.

## Starting a server: the probes and the refusals

A sandbox that was asked for is established and verified, or the launch fails naming what was left behind (toolkit check); an empty or blank `--sandbox` value counts as asked for, and `init` and `launch-child` refuse it before anything is created rather than reading it as no flag. `launch-child --sandbox` and `start-slice` on a sandboxed run refuse when:

- `srt` is not on `PATH`, or `node` is not;
- the base policy fails a refusal above, or a write grant reaches `srt` or `node`;
- a tmux server already answers on the socket before the toolkit starts one;
- `srt` exits, or the server's socket does not answer, within 10 s (the last three lines of `srt.log` are quoted; an interrupt during the wait kills the server's process group too);
- the server's global environment does not carry `SANDBOX_RUNTIME=1`;
- the negative probe succeeds: `run-shell` asks the server to `touch <socket directory>/<socket>.probe`, which must fail and leave no file; the host checks the file itself, and on success removes it, kills the server and refuses;
- the positive probe fails: `run-shell` touching `<worktree>/.pm/sandbox-probe` must succeed and leave a file the host then removes, so a misderived policy is caught before a session starts;
- the session cannot be created through the server.

Each of the three probes must return within 10 s, or verification fails. There is no fallback to an unsandboxed server on any of these paths, and no flag that requests one. A `launch-child` that fails after the server started stops it and removes the temp directory and the control directory, except that the shared teardown keeps the control directory and its record while a session is listed or `srt` survives; a `start-slice` whose server fails after it started kills the server and keeps the run's control and temp directories, so the next `start-slice` retries.

## The example policy

For one machine: an OpenCode child PM, Codex Developers, a Copilot reviewer (optional, with its warning), Claude Code available for any seat; a local LLM on Tailscale; the home directory readable except the listed paths. Harness configuration on this machine lives behind the symlink `~/.agents` to `~/Documents/AI`; with reads allowed by default that needs no entry, and an operator who instead denies `~` must re-allow both the link and its target (*Per-harness facts*, *Reads*).

```json
{
  "network": {
    "allowedDomains": [
      "api.anthropic.com", "platform.claude.com",
      "chatgpt.com", "ab.chatgpt.com",
      "opencode.ai:443",
      "djcmacstudio.tail98bbb1.ts.net:443", "100.102.21.13:443",
      "djcmacbookpro.tail98bbb1.ts.net:443",
      "api.github.com", "api.individual.githubcopilot.com"
    ],
    "deniedDomains": [],
    "allowUnixSockets": [],
    "allowLocalBinding": false
  },
  "filesystem": {
    "denyRead": [
      "~/.ssh", "~/.aws", "~/.gnupg", "~/.config/gh", "~/.netrc", "~/.git-credentials",
      "~/.llm/adacs",
      "~/Obsidian", "~/.claude/projects",
      "/Users/dcroton/Local/git-repos/mimic/docs/dev"
    ],
    "allowRead": [
      "~/.claude/projects/-Users-dcroton-Local-git-repos-campaign-worktrees-shakedown2-*",
      "~/.llm/adacs/api_keys",
      "/Volumes/Internal/data/mini-millennium"
    ],
    "allowWrite": [
      "~/.claude/projects/-Users-dcroton-Local-git-repos-campaign-worktrees-shakedown2-*",
      "~/.claude/todos", "~/.claude/debug", "~/.claude/statsig", "~/.claude/shell-snapshots",
      "~/.claude/session-env", "~/.claude/sessions", "~/.claude/tasks", "~/.claude/file-history",
      "~/.claude/cache", "~/.claude/history.jsonl", "~/.claude/stats-cache.json",
      "~/.codex/sessions", "~/.codex/log", "~/.codex/history.jsonl", "~/.codex/sqlite",
      "~/.codex/logs_*", "~/.codex/installation_id", "~/.codex/thread-writer-locks",
      "~/.codex/state_*.sqlite*", "~/.codex/thread_history_*.sqlite*", "~/.codex/shell_snapshots",
      "~/.codex/tmp", "~/.codex/models_cache.json",
      "~/.local/share/opencode", "~/.local/state/opencode",
      "~/.copilot", "~/Library/Caches/copilot"
    ],
    "denyWrite": [
      "~/.claude/settings.json", "~/.claude/CLAUDE.md", "~/.claude/skills", "~/.claude/hooks",
      "~/.claude/plugins", "~/.claude.json", "~/.codex/config.toml", "~/.codex/auth.json",
      "~/.config/opencode"
    ]
  },
  "allowPty": true,
  "enableWeakerNetworkIsolation": true
}
```

Reading it, line by line:

- `allowedDomains`: `api.anthropic.com` for Claude Code, plus `platform.claude.com` for its TUI; `chatgpt.com` and `ab.chatgpt.com` for Codex; `opencode.ai:443` for OpenCode's hosted provider (`opencode-go/*` models), dropped when every OpenCode seat uses the local models; the Tailscale LLM as its MagicDNS name *and* its IP literal, both with the port, because this machine's own name resolves to its own Tailscale address and `srt` refuses that unless the IP literal is listed too (the laptop's name alone works); `api.github.com` and `api.individual.githubcopilot.com` for Copilot, where `api.github.com` carries any GitHub API traffic, not only Copilot's. No git host is listed, so a push to a remote fails (*Network*); a local-path push to this branch's own granted ref can still succeed.
- `allowLocalBinding: false`: no loopback or host-address traffic outside the proxy.
- `denyRead`: the brief's exclusions (`docs/dev`), the owner's private directories and the credential files no seat here needs. `~/.llm/adacs` is denied as a directory and its key re-allowed in `allowRead`, because OpenCode resolves every key reference in its configuration at load. `~/.claude/projects` is denied and this campaign's own transcripts re-allowed (and made writable) by the glob naming the campaign id in the mangled worktree path, so other sessions' Claude Code transcripts stay unreadable. Codex transcripts under `~/.codex/sessions` stay readable, because Codex reads its own session files back and names them without a per-run prefix: a disclosed residual.
- `allowRead`: the two re-allows above and the reference-data directory the product reads.
- `allowWrite`: each harness's writable state, from *Per-harness facts*.
- `denyWrite`: the global configuration, explicit even though nothing grants it, so a later widening of `allowWrite` cannot silently expose it.
- `allowPty: true`: the toolkit sets it anyway; keeping it in the file makes the file runnable by `srt` on its own.
- `enableWeakerNetworkIsolation: true`: only for Copilot, and dropped, with the two GitHub hosts, when Copilot is not a seat. It prints its warning.

Lines an operator changes per machine: the Tailscale names and address, the data directory, the campaign id in the two project globs, and the `docs/dev` exclusion.

### Producing your policy

1. List the harness of every seat that will run inside: in Mode C the child PM, Developers and reviewers; in Mode B the Developers and reviewers.
2. Start from the skeleton `{"network": {"allowedDomains": [], "deniedDomains": []}, "filesystem": {"denyRead": [], "allowRead": [], "allowWrite": [], "denyWrite": []}}`.
3. For each harness, add its hosts to `allowedDomains` and its writable state to `allowWrite` from *Per-harness facts*. Add each local model server as `name:port`, and also as `ip:port` when the name resolves to one of this machine's own addresses.
4. Add to `denyRead` every path no seat may read: the brief's exclusions, private directories, credential files no seat needs. Re-allow, in `allowRead`, a file a harness insists on reading beneath a denied directory. Denying `~` itself breaks git until `~/.gitconfig` is re-allowed, and needs every harness home re-allowed.
5. Add the global configuration files to `denyWrite`.
6. Add `enableWeakerNetworkIsolation: true` only when Copilot is a seat; `allowLocalBinding` only by conscious choice.
7. Check it: in Mode C, commit it in the dossier as `sandbox.json`, pin its SHA-256 (`shasum -a 256 sandbox.json`) in the brief's `## Sandbox` section and run `campaign-preflight`; in Mode B, `init --sandbox <file>` checks it before creating the run. Then run the README's model-free sequence once sandboxed, and one real-model launch per harness, before an unattended run: [Verify campaign mode](../README.md#verify-campaign-mode-no-real-model-2-minutes) in Mode C with `--sandbox <your policy>` added to its `launch-child` line (its `SOCK=` capture reads the launch line's `socket=` field, which the extra `WARNING:` and `sandbox:` lines do not disturb), or [Verify your setup](../README.md#verify-your-setup-no-real-model-1-minute) in Mode B with `--sandbox <your policy>` added to its `init` line and the `export PM_TMUX_SOCKET=…` line `init` prints in place of the sequence's own.

## Network

All network is denied by default; egress goes only through `srt`'s HTTP CONNECT / SOCKS5 proxy on loopback, and DNS happens in the proxy (OS). Raw TCP, UDP, ICMP and direct loopback are denied unless `allowLocalBinding` is set. `NO_PROXY` covers loopback and RFC 1918 but not `100.64.0.0/10`, so Tailscale addresses go through the proxy. The proxy filters by host and port, never by request content. `git push` to GitHub fails on the egress policy (an `insteadOf` rewrite to SSH is refused by the proxy, 403); a product gate that needs `git fetch` from a remote needs that host listed, which also permits push to it: the owner decides per campaign, in the policy (operator judgement).

## Per-harness facts

- **Claude Code**: one-shot and TUI work with `api.anthropic.com` plus, for the TUI, `platform.claude.com`, a readable `~/.claude.json` and a readable `~/Library/Keychains/login.keychain-db` (its OAuth token lives in the Keychain). It writes its transcripts under `~/.claude/projects/<mangled cwd>/` and its session state under `~/.claude/`: the example grants `todos`, `debug`, `statsig`, `shell-snapshots`, `session-env`, `sessions`, `tasks`, `file-history`, `cache`, `history.jsonl` and `stats-cache.json`, and keeps `settings.json`, `CLAUDE.md`, `skills`, `hooks`, `plugins` and `~/.claude.json` unwritable. Its own sandbox setting (`sandbox.enabled`) cannot start inside and is turned off with `--settings '{"sandbox":{"enabled":false}}'` (Claude Code documents that it otherwise falls back to running commands unsandboxed).
- **Codex**: works with `chatgpt.com` and `ab.chatgpt.com`, `SSL_CERT_FILE=/etc/ssl/cert.pem` (it otherwise verifies TLS through `trustd`, which Seatbelt blocks), writable `~/.codex` state (`installation_id` and `thread-writer-locks` are essential; it also writes `sessions/`, `state_*.sqlite*`, `thread_history_*.sqlite*`, `logs_*`, `shell_snapshots/`, `tmp/`, and the example grants `log`, `history.jsonl`, `sqlite` and `models_cache.json` too, keeping `config.toml` and `auth.json` unwritable), and `--no-daemon` for the TUI (it otherwise wants a shared app-server daemon whose lock it cannot take). `sandbox_mode="read-only"` answers a trivial prompt but fails its first tool call with rc 0; `danger-full-access` works.
- **OpenCode**: works with its LLM endpoints and writable `~/.local/share/opencode` (the example also grants `~/.local/state/opencode`, whose locks it otherwise skips, harmlessly, and keeps `~/.config/opencode` unwritable); it resolves every `{file:…}` key reference in its configuration at load, so each must be readable. As a Developer seat driven by the toolkit's own launch path inside a sandboxed server, `opencode-go/qwen3.8-flash` (host `opencode.ai:443`) and a Tailscale llama-server model each reached the prompt in about 6 s and answered in under 10 s; its probes of `api.githubcopilot.com` and Homebrew's `analytics.brew.sh` were refused without effect.
- **Copilot**: works only with `enableWeakerNetworkIsolation: true` (its TLS stack needs `trustd`; `SSL_CERT_FILE` does not help), writable `~/.copilot` and `~/Library/Caches/copilot`, and `api.github.com` plus `api.individual.githubcopilot.com`. `-p --silent` may return an empty report (the model answered through a tool call), which the toolkit records as `review-failed: empty report`. It tries to self-update, and its TUI shows a folder-trust dialog on a folder it has not remembered.
- **Qwen**: not tested. The toolkit omits `--sandbox` inside a sandbox, as for the other harnesses' own sandboxes.
- **Global configuration** (OS, *tested*): with the example's write lists, appends to `~/.claude/settings.json`, `~/.claude/CLAUDE.md`, `~/.claude/skills/*`, `~/.claude/hooks/*`, `~/.claude/plugins/*` and `~/.zshrc` all fail (EPERM, hashes unchanged), as do reads of `~/.ssh`, `~/Documents` and the denied key files.
- **`srt` configuration**: the five required keys above; `-s`/`--settings` names the file, read once when `srt` starts; exit codes of the wrapped command propagate. `srt` injects `SANDBOX_RUNTIME=1`, the proxy variables, `GIT_SSH_COMMAND` through the proxy and `JAVA_TOOL_OPTIONS`, and always grants writes to the child's stdio, `~/.npm/_logs` and `~/.claude/debug`. Nothing is scrubbed from the environment.
- **Reads** (OS, *tested*): `denyRead` gives EPERM and hides existence; a more specific `denyRead` inside an `allowRead` region wins; a symlink inside the worktree to a denied target is denied, to an allowed target allowed; under a home-wide deny both a symlink's link path and its target need `allowRead`. A nonexistent `allowRead` path is accepted silently (fails toward tighter).
- **Processes and signals** (OS): inside, `ps` fails and a signal to a process outside is EPERM; signals within the sandbox work, so `review`'s `killpg` of its own reviewer works. A detached reviewer survives `kill-server`, stays sandboxed and loses network.

## Mode C: a sandboxed campaign

The owner writes the policy, commits it in the dossier as `sandbox.json` and pins it in the brief's `## Sandbox` section ([dossier-template.md](dossier-template.md) shows the pin line); `campaign-preflight` checks at every reconcile that it is tracked and unchanged, matches its pin, passes the base-policy refusals (`load_base_policy`; each warning a `note`) and `launch-child`'s check of write grants reaching `srt` and `node` (`check_binaries`), that the host is macOS, and that `srt` is on `PATH` (its version in the `ok` line). The derivation refusal of grants into the repository needs the worktree, so `launch-child` makes it, not `campaign-preflight`. The brief is the `brief.md` at the dossier root, the git toplevel of the launcher's directory: a `brief.md` anywhere else is not the brief, a launcher in no git repository has no brief and so is unpinned, any other failure to find the dossier root (a `git` that cannot say which repository the launcher lies in) refuses the launch rather than reading as unpinned, and a launcher whose given and real paths lie in different repositories (a symbolic link in from elsewhere) is refused, because which brief governs it would depend on the spelling (toolkit check, `find_brief`). A brief whose section is `none`, or that has none, runs unsandboxed.

`launch-child --sandbox <dossier>/sandbox.json` validates the policy before anything is created; after the worktree exists and `--setup` has run (on the host, unsandboxed, before any session exists), it creates the control and temp directories, derives and writes the effective and attach policies (`pm_inside` true; the launcher's directory readable), starts and verifies the server, writes the record and creates the child's session through the server. The manifest gains a `sandbox` block (`policy`, `policy_sha256`, `socket`, `control_dir`), and the command prints `WARNING:` lines for the policy's warnings, `sandbox: control dir <path>` and the `watch:` line in the attach-policy form. When the launcher's dossier brief pins a policy, `--sandbox` is required and must match the pin (toolkit check).

Inside, the child PM runs unchanged: its own `init` takes no `--sandbox`, because the child is sandboxed by the server it runs on; its Developers (`start-slice`) and reviewers (`review`) inherit the boundary, and `SANDBOX_RUNTIME=1` makes `review` and the harness profiles use the no-inner-sandbox command variants and render the sandbox paragraph into the Developer and reviewer prompts. `wait-child` and `answer-child` are unchanged.

## Mode B: a sandboxed run

`init --sandbox <policy>` validates the policy (printing its warnings) and derives the three policies before it creates or checks out a branch, so `--create-branch` leaves no branch behind a refused policy; it then records `sandbox: {policy, policy_sha256, socket: "pm-run-<run id>", control_dir}` in `run.json` (`policy` being the file's resolved absolute path), creates the control and temp directories, writes `policy.json` (`pm_inside` false; the plan file's directory readable), `reviewer-policy.json`, `attach-policy.json` and the record, and prints `sandbox: control dir <path>` before the token line and `export PM_TMUX_SOCKET=pm-run-<run id>` after it. It does not start the server. Run the export: every command that drives the run's tmux server (`status` (including `--report`), `start-slice`, `observe`, `send`, `answer`, `finalize`, `review`, `stop`, and `stop --scavenge` on readable state) refuses on a sandboxed run when `PM_TMUX_SOCKET` is unset or names another server, so nothing can reach the operator's default server by accident (toolkit check); `approve`, `grant`, `notes`, `judge-reviews`, `judge-developer` and `ledger` touch no tmux server and do not refuse. This is the one list of those commands; every other document links here.

`start-slice` ensures the server: when none answers, it re-derives the policies from the recorded base file (refusing when the file's SHA-256 no longer matches the recorded one) and rewrites all three policy files (a missing `reviewer-policy.json` or `attach-policy.json` is repaired), starts and verifies the server, updates the record's pids and prints `sandbox: started the run's sandboxed tmux server on socket <socket>`; when one answers, it checks `SANDBOX_RUNTIME=1` in its environment and the record's server pid, refusing otherwise; when none answers but the record's `srt` still runs (a `stop` that left it behind keeps the record), it refuses to start a second server beside it and names the pid to end. After a `stop` removed them, it rebuilds the control directory, policies, temp dir and record from the verified run state. The Developer's session is created through the server.

`review` wraps each reviewer in its own `srt` (one proxy per reviewer, working directory the repository) under `reviewer-policy.json`, with the run's `TMPDIR` and `SSL_CERT_FILE` in its environment and the no-inner-sandbox command variants, and refuses, before any commission artifact exists, when the reviewer policy is missing, the record names no temp dir, or `srt` is missing. It wraps whenever the run records a sandbox, even when the PM's own environment carries `SANDBOX_RUNTIME=1`: an inherited sandbox never stands in for the run's, and a PM that itself runs inside a sandbox meets a nested `srt` failing closed (exit 71). The wrapped `srt` leads the reviewer's process group, so the timeout's `killpg` reaches the whole group.

Watch a Developer with the attach command under *The boundary*, with the socket `pm-run-<run id>`, the control directory the one `init` printed and the session the one `start-slice` printed.

## Teardown

- **`stop-child`** (Mode C) verifies the server's ownership mark as for any child, derives the control directory itself and refuses when its record names another worktree, kills the server (which ends `srt` and its proxy), waits up to 5 s for the recorded `srt` pid to exit (`srt still running: <pid>` otherwise), and removes the temp dir and the control directory, printing `removed sandbox temp dir <path>` and `removed sandbox control dir <path>`, or that each was already gone on a re-run. While a session or the server's `srt` survives it keeps the record and the control directory, so a re-run can finish the teardown. With no record (an earlier stop removed it, or the launch never wrote one) it tears down what `launch-child` created, the temp dir and the control directory, reporting each gone when it already is; a control directory whose record cannot be read is checked by the server mark alone and left in place, with the command that removes it by hand. It exits 2 when the teardown is skipped, fails, leaves a session or `srt` running, or refuses the control path, and a re-run exits 2 for as long as `srt` survives. A temp dir left under another `TMPDIR` is reported in the notes and does not make it exit 2 (*Under a different `TMPDIR`* below).
- **Mode B run end**: `stop`, the `finalize --accept` that completes the run, and a `start-slice` that finds every slice accepted or attested each kill the server, wait for `srt`, remove the temp and control directories and append a `sandbox-stopped` event. An incomplete teardown, one that leaves a session, the server's `srt` or the control directory behind, keeps the record and the control directory, exits non-zero and records no event, so a later `stop` or `start-slice` retries it, and the retry that completes records the event once; a completing `finalize --accept` whose teardown fails leaves the run complete for the next `start-slice` to finish.
- **Under a different `TMPDIR`** (both modes): the run's temp dir lies under the temp root the sandbox was set up under. A teardown run under another `TMPDIR` still kills the server and removes the control directory, leaves the temp dir in place and reports it, naming the difference; remove it by hand, or repeat the teardown under the original `TMPDIR`. One rule for both modes: the temp dir is reported in the notes and never makes a teardown incomplete, so `stop-child` exits 0 with the note and a Mode B run end records its `sandbox-stopped` event and exits 0, the note saying where the temp dir is. Either mode removes the control root `<common git dir>/pm-sandbox` too, once nothing else is in it.
- **`stop --scavenge`**: with readable state and no token, it tears the sandbox down from the host-written record and writes no event, and a later `stop` with the token then finds nothing left to tear down and records none either (the event is an audit record; the teardown itself is complete); with unreadable state, it kills the server on `pm-run-<id>` when one answers, the id being `--run`'s or, when `--run` is absent, the run directory's name; no record is available then, so the `srt` pid is unknown and not waited for, and it exits 2 when the kill leaves sessions listed or the server cannot be checked; on a readable sandboxed run with the wrong `PM_TMUX_SOCKET` it prints the refusal and sweeps nothing.
- **By hand**, the operator's step when the toolkit cannot (never the Campaign Manager's, for whom a failed teardown is a question to the owner): `tmux -L <socket> kill-server`, then remove `<common git dir>/pm-sandbox/<socket>/` and `$TMPDIR/pm-sandbox-<socket>`. A detached reviewer outlives the server, sandboxed and network-less; find it by the process group `stop-child` printed and kill it from the host.

## Troubleshooting

- **"error sending request" from Codex while `srt.log` shows the host as allowed**: TLS verification through `trustd`, which Seatbelt blocks. The server and every wrapped reviewer get `SSL_CERT_FILE=/etc/ssl/cert.pem` when that file exists, so this shows on a machine without the file, or for a command that unsets the variable. Copilot's equivalent (OSStatus -26276) needs `enableWeakerNetworkIsolation`, not `SSL_CERT_FILE`.
- **`mktemp` fails inside**: macOS's `mktemp` with no template ignores `TMPDIR`. Give it a template under `$TMPDIR` (`mktemp "$TMPDIR/x.XXXXXX"`), or add the launching shell's `$TMPDIR` to `allowWrite`; the negative probe does not depend on that directory.
- **A Copilot review fails with `review-failed: empty report`**: `-p --silent` returned nothing because the model answered through a tool call. Re-commission it, or choose another reviewer harness.
- **`sandbox_apply: Operation not permitted`, exit 71, or Codex's "sandbox could not start"**: a harness tried to apply its own sandbox inside `srt`, which cannot nest. The toolkit's command variants turn the harness sandboxes off; a custom `--harness-command` that turns one on must not.
- **Codex "failed to initialize in-process app-server client: Operation not permitted"**: its `~/.codex` state is not writable; add the entries in *Per-harness facts*. "failed to open daemon operation lock" in the TUI: `--no-daemon` is missing (the toolkit adds it to the Codex profile inside a sandbox).
- **OpenCode rejects its configuration**: a `{file:…}` key reference names an unreadable file; re-allow it in `allowRead`.
- **A local model's name is refused** ("resolved to one of this host's addresses"): add its IP literal with the port beside the name.
- **The server does not start within 10 s**: read the quoted tail of `<control dir>/srt.log`; a missing required key or a malformed file is `srt`'s own refusal.
- **"this run's Developer server is sandboxed on tmux socket …"**: run the `export PM_TMUX_SOCKET=…` line `init` printed.
- **A probe failed**: the negative probe succeeding means the server is not sandboxed (a plain server on the socket, or `srt` not applying the policy); the positive probe failing means the derived policy does not let the server write `<worktree>/.pm` (a base `denyWrite` covering it).
- **In a linked worktree, `git stash`, `tag`, `worktree add`, `gc`, another branch, or a push to an unlisted remote or to a ref outside the grant fails with "Operation not permitted" or a 403**: the boundary, by the exact-ref grants and the egress policy; not a bug to work around. A plain checkout has no ref grants, so branch, tag and stash operations are not refused there; `worktree add` and `git config` are, by the derived denies, and a push there to a remote host fails on the egress policy, while a local-path push into a granted path and a push to an allowed network host still succeed (OS and egress policy).
- **A trust dialog at launch**: the worktree is not under a parent the owner trusted in that harness; `~/.claude.json` is read-only inside, so trust cannot be granted from inside.
