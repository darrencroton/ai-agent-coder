# Implementation Plan: Sandboxed Sessions for Mode B and Mode C (macOS, `srt`)

Status: revision 4 for panel review, 2026-10-25, after three panel rounds (Claude Opus 5.5 and Codex gpt-6.1-sol); every blocking finding was reproduced or refuted by experiment before each revision. Branch `feature/pm-campaign-mode`. Nothing in this plan is implemented yet; every fact marked *tested* was established in this session on macOS 27.0.1 with tmux 3.5a, git 2.54, srt 0.0.81 and Python 3.14, by direct experiment, and is recorded with its outcome in the evidence file beside the review requests (`.orchestrator/sandbox-plan-review/evidence-notes.md`, which Slice 5 condenses into `references/sandbox.md`).

## 1. Purpose

Give the operator an optional, OS-enforced boundary around the processes a run launches: in Mode C the child PM, its Developers and its reviewers; in Mode B the Developers and reviewers. The boundary is Anthropic's `sandbox-runtime` (`srt`), a generic macOS Seatbelt plus egress-proxy wrapper, used as the environment's mechanism and wired in through one seam, so that `docs/VISION.md`'s non-goal ("not a sandbox or container system: isolation, when needed, is the environment's job") holds: the toolkit starts the environment's sandbox and verifies it is there; it does not become a containment layer of its own.

When sandboxing is not requested, nothing changes.

## 2. Architectural assessment

### 2.1 The boundary: the private tmux server, run in the foreground under `srt`

The child PM already runs in a session on a private tmux server (`launch-child`), and the Developers it starts share that server (`start-slice` honours `PM_TMUX_SOCKET`). Seatbelt restrictions are per-process kernel state inherited across `fork` and `exec`, so sandboxing the server sandboxes every pane process it ever creates, whichever client asked for the pane, and every descendant of those panes, including reviewers that `review` starts in their own process groups. The host's one-shot tmux clients (the CM's `wait-child`, `answer-child`, `stop-child`; `start-slice`'s `new-session`) connect to the server's socket unrestricted, because connecting to a sandboxed process's socket is not something the sandbox governs.

The server command is:

```sh
srt -s <control dir>/policy.json env TMPDIR=<per-run temp dir> SSL_CERT_FILE=/etc/ssl/cert.pem tmux -L <socket> -D
```

started by the host as a long-lived process (its own session, stdin closed, output to `<control dir>/srt.log`), with its working directory set to the worktree. tmux's `-D` keeps the server in the foreground, so `srt` stays its parent for the server's whole life and `srt`'s in-process egress proxy stays up; `tmux kill-server` ends the server, `srt` exits and the proxy closes (*tested*). The session is then created *through the server*: the host runs `tmux -L <socket> run-shell '<the tmux new-session command start_session composes today>'`, so the `new-session` client runs inside the sandboxed server, and a socket with no server answering fails (`run-shell` never starts a server, *tested*) instead of letting a plain `tmux new-session` start an unsandboxed replacement. The pane, its environment prefix and the `PM_RUN_TOKEN` unset are exactly as today (*tested*).

**An attached client is part of the boundary.** A pane can run `tmux detach-client -t <client> -E '<command>'`, and tmux executes that command *in the client process*, on the host, unsandboxed; a read-only attach (`-r`) does not prevent it (*tested, demonstrated*). One-shot command clients carry no session and cannot be detached, so the toolkit's own clients are not exposed. The operator's watch therefore attaches through a sandbox of its own: `srt -s <control dir>/attach-policy.json tmux -L <socket> attach -t <session>`, where the attach policy is the base policy's denies plus the toolkit's denies (including `srt`'s own implicit grants `/tmp/claude`, `~/.npm/_logs` and `~/.claude/debug`, which a `denyWrite` entry removes, *tested*), the socket and `allowPty`, and *no write grant at all*; a tmux client writes nothing but its terminal, the session renders normally, and the injected command fails with EPERM inside the sandboxed client, whatever directory the operator attached from (*tested*). `launch-child` prints that form as its `watch:` line, the contract says never to attach to a sandboxed server any other way, and that rule is the operator's discipline: nothing can stop a same-user process from attaching plainly.

### 2.2 Why not the alternatives

- **`srt` around `tmux new-session -d`, as the hypothesis was stated.** The restrictions do inherit (*tested*), but the server daemonises, the wrapping `srt` returns after 0.2 s, and its proxy dies with it: every pane keeps `HTTPS_PROXY` pointing at a dead port and has no network at all, allowed domains included (*tested*). `-D` fixes exactly this and changes nothing else.
- **`srt` around each harness command (pane-level wrap).** One sandbox per session, an unsandboxed server in between, and a sandboxed pane that may still talk to that server through the one socket it must be allowed to reach, asking it to spawn an unsandboxed window. It also needs nesting for reviewers, which fails (below).
- **The harnesses' own sandboxes** (Codex `workspace-write`/`read-only`, Claude Code's sandbox setting, Qwen `--sandbox`). Three mechanisms with three vocabularies; none confines reads for Claude Code or OpenCode; none covers processes that escape their tool layer; and on macOS none can run inside any outer sandbox, because a Seatbelt-sandboxed process cannot apply a second profile: `sandbox_apply: Operation not permitted`, exit 71 (*tested*). The design therefore turns them off inside the boundary rather than stacking them.
- **A full clone per node instead of a linked worktree.** It would avoid the shared `.git`, but campaign mode's reconcile and merge model reads `git worktree list` and merges local branches. Exact grants on the shared git directory are sufficient (*tested*, §2.4) and keep that model.
- **A VM or container.** A larger change to the operator's environment than this toolkit should force, with Keychain-backed logins, local tools and tmux fidelity all to be re-established. `srt` is the environment's mechanism for this exact job.

### 2.3 Tested facts the design rests on

| Area | Fact |
|---|---|
| Inheritance | Pane processes of a server started under `srt` cannot write outside the policy's write grants (EPERM) and can write inside them, whether the pane was created by the sandboxed server's own command or by an unsandboxed host client later. `tmux run-shell` executes its command through the server, under the same restrictions, and returns the command's exit status on tmux 3.5a. |
| Lifecycle | Host `kill-server` ends the server; `srt` exits 0; the proxy port closes; the socket file is left behind (tmux never unlinks it; `sessions.kill_server` already removes it). `SIGTERM` to `srt` is forwarded and ends everything. `SIGKILL` of `srt` alone orphans a sandboxed, network-less server; the wrapped command shares `srt`'s process group, and `killpg` of that group kills `srt` and the command together (*tested*). Startup to a listening socket: 0.20 s median. Two servers run side by side with separate proxies. `tmux display-message -p '#{pid}'` fails with no server and answers on an empty one; `list-sessions` cannot tell those apart. `tmux run-shell` against a socket with no server fails and starts nothing; a `new-session -d` issued through `run-shell` creates the session inside the server with its environment prefix intact. |
| Attached clients | `detach-client -E` runs a command in the attached client process on the host, also for a read-only client; an attach client run under `srt` with no write grants renders the session and executes the command inside its own sandbox, where every write fails (EPERM), including from a cwd outside the repository. |
| Required policy for tmux | `allowPty: true` (without it the server starts but every `new-session` fails with `create window failed: fork failed: Operation not permitted`, rc 1, which the host sees); `network.allowUnixSockets` naming the socket path; `filesystem.allowWrite` naming the socket path only, a plain string even though the path does not exist yet, which lets the server create its socket and nothing else in `/private/tmp/tmux-<uid>/`. A `touch` of any other file in that directory fails under `srt` and succeeds on a plain server, which makes it the probe; the directory is `sessions.socket_path(socket).parent`, which honours `TMUX_TMPDIR`. |
| Temp space | `srt` sets `TMPDIR=/tmp/claude` and grants it (shared by every sandbox on the machine, created by `srt` on the host); `denyWrite` entries for `/tmp/claude` and `/private/tmp/claude` remove that grant, and an `env TMPDIR=<dir>` placed after `srt` wins for the server and its panes. Python `tempfile`, git and most tools honour it; macOS's own `mktemp` with no template does not and fails. |
| Network | All network denied by default; egress only through `srt`'s HTTP CONNECT / SOCKS5 proxy on loopback; DNS happens in the proxy. Allow entries are `host[:port]`, `*.host[:port]` or an IP literal with a port; an entry without a port allows every port; no CIDRs. A hostname that resolves to one of this host's own addresses is refused unless the IP literal with its port is also listed (this machine's MagicDNS name resolves to its own Tailscale address, so both entries are needed; the laptop's name alone works). `NO_PROXY` covers loopback and RFC 1918 but not `100.64.0.0/10`, so Tailscale addresses go through the proxy. Raw TCP, UDP, ICMP and direct loopback are denied unless `allowLocalBinding` is set, which opens loopback and this host's own interface addresses outside the proxy. `git push` to GitHub fails on the egress policy (the user's `insteadOf` rewrites to SSH, which the proxy refuses, 403). The proxy filters by host and port, never by request content. |
| Nesting | `sandbox-exec` inside `srt` fails (exit 71). Codex `exec` with `sandbox_mode="read-only"` answers a trivial prompt but fails its first tool call, reporting the failure in prose with overall rc 0; with `danger-full-access` it works. Claude Code's sandbox setting (`sandbox.enabled`, off by default, overridable per launch with `--settings <json>`) and Qwen `--sandbox` would fail the same way; Claude Code documents that it falls back to running commands unsandboxed when its sandbox cannot start. |
| Reads | Allowed everywhere by default; `denyRead` gives EPERM and hides existence; a more specific `denyRead` inside an `allowRead` region wins; a symlink inside the worktree to a denied target is denied, to an allowed target is allowed; a symlink's *link* path and its *target* both need `allowRead` under a home-wide deny. `denyRead: ["~"]` breaks git until `~/.gitconfig` is re-allowed. A nonexistent `allowRead` path is accepted silently (fails toward tighter). |
| Git in a linked worktree | Commits succeed with `allowWrite` on: the worktree; its git dir `<common>/worktrees/<name>`; `<common>/objects`; the literal files `<common>/refs/heads/<branch>`, `<common>/refs/heads/<branch>.lock`, `<common>/logs/refs/heads/<branch>`, `<common>/logs/refs/heads/<branch>.lock` and `<common>/packed-refs.lock` (without the last, every ref update prints an error and `branch -D` fails). With those: a sibling branch under the same prefix, any other ref (`main`, `integration/*`, tags, stash, `refs/remotes/*`), `git worktree add`, `gc`, `pack-refs`, `git config` (writes `<common>/config`), hooks and the main checkout's index are all denied; a local-path `git push` into the product repository writes only the granted ref. |
| Git in a plain checkout | With `allowWrite` on the repository: commits succeed; `.git/config`, `.git/hooks/*`, `git config` and `mv .git` are denied by `srt`'s own mandatory denies when `srt` starts in the repository, and equally by explicit literal `denyWrite` entries on `.git/config` and `.git/hooks` whatever `srt`'s working directory (*tested* from `/`), which is why the toolkit writes those two denies itself; `.git/info/attributes` and ordinary git-dir files stay writable. A literal `denyWrite` on `.git` *as a directory* denies everything beneath it and breaks `git add` (`index.lock`), so that deny applies only where `.git` is a file. A literal `denyWrite` on `.git/pm` keeps the state root unwritable. |
| Sandbox-to-host escape through git metadata, closed | In a linked worktree the worktree's `.git` *file* and the git dir's `commondir`, `gitdir`, `hooks` and `config.worktree` were writable; a pane rewrote `.git` to point at a bare repository of its own with `core.fsmonitor` set, and the next host-side `git -C <worktree> status` (which the CM's cleanup runs) executed that script on the host (*demonstrated*). Literal `denyWrite` entries on those five paths close it (*verified twice*), and `srt` gives `denyWrite` precedence over any `allowWrite`, so the toolkit can add them as non-overridable. |
| Processes and signals | Inside: `ps` fails, a signal to a process outside the sandbox is EPERM, signals within the sandbox work (so `review`'s `killpg` of its own reviewer works). A reviewer-style detached process survives `kill-server`, stays sandboxed, and loses network (its first API call fails); the host can kill it. |
| Harness behaviour inside | Claude Code one-shot and TUI work with `api.anthropic.com` plus, for the TUI, `platform.claude.com`, a readable `~/.claude.json` and a readable `~/Library/Keychains/login.keychain-db` (its OAuth token lives in the Keychain); it writes under `~/.claude/projects/<mangled cwd>/`. Codex works with `chatgpt.com`, `ab.chatgpt.com`, `SSL_CERT_FILE=/etc/ssl/cert.pem` (it verifies TLS through macOS `trustd`, which Seatbelt blocks; the symptom is "error sending request" while the proxy logs the host as allowed), writable `~/.codex` state (`installation_id` and `thread-writer-locks` are essential), and `--no-daemon` for the TUI (it otherwise wants a shared app-server daemon whose lock it cannot take). OpenCode works with the LLM endpoints and writable `~/.local/share/opencode`; it resolves every `{file:…}` key reference in its config at load, so each must be readable. As a Developer seat driven by the toolkit's own launch path (`start_session`, `wait_until_ready` with the model-identity check, `send_prompt`) inside a sandboxed server, `opencode-go/qwen3.8-flash` (its hosted provider, host `opencode.ai:443`) and `macstudio/qwen/qwen3.8-27b-q8` (the Tailscale llama-server) each reached the prompt in about 6 s and answered in under 10 s; OpenCode's probes of `api.githubcopilot.com` and Homebrew's `analytics.brew.sh` were refused without effect. Copilot works only with `enableWeakerNetworkIsolation: true` (its TLS stack needs `trustd`; `srt` documents this as an exfiltration path), writable `~/.copilot` and `~/Library/Caches/copilot`, and `api.github.com` plus `api.individual.githubcopilot.com`; `-p --silent` may return an empty report (the model answered through a tool call), which the toolkit already records as `review-failed: empty report`. Qwen: not tested. |
| Global configuration | With the write lists in §2.10, appends to `~/.claude/settings.json`, `~/.claude/CLAUDE.md`, `~/.claude/skills/*`, `~/.claude/hooks/*`, `~/.claude/plugins/*` and `~/.zshrc` all fail (EPERM, hashes unchanged), as do reads of `~/.ssh`, `~/Documents` and the denied key files. |
| `srt` configuration | `network.allowedDomains`, `network.deniedDomains`, `filesystem.denyRead` and `filesystem.denyWrite` are required keys (an incomplete, missing or malformed file is refused with exit 1 and no server). `--settings` names the file; the file is read once, when `srt` starts. Exit codes of the wrapped command propagate. `srt` injects `SANDBOX_RUNTIME=1`, the proxy variables, `GIT_SSH_COMMAND` through the proxy and `JAVA_TOOL_OPTIONS`, and always grants writes to the child's stdio, `~/.npm/_logs` and `~/.claude/debug`. Nothing is scrubbed from the environment. `srt` on this machine resolves to `/opt/homebrew/lib/node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js`, run by `/opt/homebrew/bin/node`. |

### 2.4 The policy model: one operator file, toolkit-derived grants, host-only control files

Three things make up what a server runs under:

1. **The operator's base policy**, a plain `srt` settings file (`sandbox.json`), written and owned by the operator: the egress allowlist, the harness homes and their writable state, reference-data directories, the paths that must be unreadable, and any `srt` option the operator consciously enables. In Mode C it lives in the dossier beside `setup.sh`, pinned in `brief.md` by SHA-256 and checked by `campaign-preflight` at every reconcile; in Mode B it is a path given to `init`, whose SHA-256 is recorded in `run.json`. The toolkit never edits it and never writes policy content of its own into it.

2. **The toolkit's derived grants**, computed at server start from facts only the toolkit has at that moment:
   - `allowWrite`: the worktree; its git dir; when the git dir differs from the common dir, `<common>/objects` and the literal files `<common>/refs/heads/<branch>`, `<common>/refs/heads/<branch>.lock`, `<common>/logs/refs/heads/<branch>`, `<common>/logs/refs/heads/<branch>.lock` and `<common>/packed-refs.lock`; the tmux socket path; the per-run temp directory; and, only when the PM itself runs inside the sandbox (Mode C), the ledger directory (`ledger.ledger_dir()`), because the child PM writes the run's ledger file from inside. In Mode B the host-side PM writes the ledger, so the Developer server gets no such grant.
   - `denyWrite` (literal, non-overridable because `srt` ranks `denyWrite` above `allowWrite`): always, `<common>/config`, `<common>/hooks`, the control root `<common>/pm-sandbox` (below), `/tmp/claude` and `/private/tmp/claude`; when the worktree's `.git` is a file, `<worktree>/.git`, `<git dir>/commondir`, `<git dir>/gitdir`, `<git dir>/config.worktree` and `<git dir>/hooks`; and, when the PM runs outside the sandbox (Mode B), the state root `<git dir>/pm`, which only the PM writes. The two git-dir denies duplicate what `srt`'s mandatory denies give when `srt` starts in the repository, so that no protection depends on a working directory (*tested*: with them, `mv .git` is refused too).
   - `allowRead`: the worktree, the common git dir, the toolkit's own checkout root and, in Mode C, the directory holding the launcher (`nodes/<node>/`, where the node plan and policy sit; in Mode B the plan file's directory), so that an operator who denies reads of the home directory does not have to rediscover these.
   - `network.allowUnixSockets`: the socket path. `allowPty: true`.

   The base policy is refused (fail closed, before anything starts) when it is not an object with `network` and `filesystem` objects; when it sets `allowAllUnixSockets`, `allowAppleEvents` or `filesystem.disabled` (each defeats the boundary outright); when an `allowWrite` entry is relative (it would resolve against the server's working directory) or resolves to `/`, the home directory, the toolkit checkout, or the tmux socket directory (`sessions.socket_path(socket).parent`, the probe's ground). At derivation the toolkit also refuses a base `allowWrite` entry that is the worktree or the common git dir, an ancestor of either, or a path inside either (the toolkit grants exactly what a run needs there, and such an entry would reach the reviewer policy, which must not write the repository), and refuses when any write grant, base or derived, is equal to, above, or inside `srt`'s package directory (the real path of the `srt` entry point's package root) or the real path of the `node` that runs it, because `srt` documents a writable installation as a host-execution path. `enableWeakerNetworkIsolation` and `allowLocalBinding` are allowed with a printed warning naming what each opens, because Copilot needs the first and the operator may choose either. `srt` applies its own schema check on top and refuses an incomplete file.

3. **The control directory**, `<common git dir>/pm-sandbox/<socket>/`, written by the host and never writable from inside (the toolkit's own deny, above): `policy.json` (the effective policy `srt` reads at start), `attach-policy.json` (the operator's watch), `reviewer-policy.json` (Mode B), `record.json` (worktree, branch, socket, temp dir, base policy path and SHA-256, `srt` version, the `srt` pid and the server pid, started-at) and `srt.log`. Its location is never read from a child-writable file: `stop-child` derives it from the worktree's common git dir and the manifest's socket, and the record it finds there must name this worktree, so a manifest pointing at another socket meets a host-written record for another worktree and is refused, while a forged record cannot exist because nothing inside can write beneath the control root. `srt` reads its settings once at start, but a Mode B server restart and every Mode B reviewer wrap re-read a policy file, and a Developer-writable file there would let a Developer hand the next host-launched `srt` a policy of its own choosing; the control directory is what makes the handoff trustworthy. `run.json` (Mode B, MAC-protected and host-written) names the control directory outright; the Mode C manifest carries it for the reader's convenience only.

The exact-file ref grants mean the sandbox can update its own branch and nothing else under `refs/heads`; a sibling branch of the same prefix is refused (*tested*). `git stash`, `tag`, `worktree add`, `gc`, `pack-refs` and fetches into `refs/remotes/*` are unavailable inside, and the Developer prompt says so (Slice 3).

### 2.5 Process model per mode

**Mode C.** `launch-child --sandbox <policy>` validates the base policy, creates the branch and worktree and runs `--setup` as today (the owner's pinned script runs on the host, unsandboxed, before any session exists), creates the control directory and the temp directory, derives and writes the effective and attach policies, starts the sandboxed server, verifies it, then creates the child's session through the server. The manifest gains a `sandbox` block (base policy path and SHA-256, socket, control directory). `wait-child` and `answer-child` are unchanged. `stop-child` verifies the server's ownership mark as today, reads the record from the control directory it derives, kills the server (which ends `srt` and its proxy), waits for the recorded `srt` pid to exit, removes the temp directory the record names, and removes the control directory. Developers (`start-slice` inside the child) and reviewers (`review` inside the child) are sandboxed by inheritance; the toolkit's only change inside is that `review` and `profiles` use the no-inner-sandbox command variants when `SANDBOX_RUNTIME` is set.

**Mode B.** `init --sandbox <policy>` validates the base policy, records `sandbox: {policy, policy_sha256, socket: "pm-run-<run-id>", control_dir}` in `run.json`, creates the control and temp directories, derives and writes the effective and reviewer policies, and prints `export PM_TMUX_SOCKET=<socket>` beside the token line. `start-slice` ensures the sandboxed server is up (starting and verifying it when absent, which also covers a server that died; re-deriving from the recorded base file and refusing when its SHA-256 no longer matches), creates the Developer's session through the server, and refuses when `PM_TMUX_SOCKET` is unset or names another server; `status`, `observe`, `send`, `answer`, `finalize`, `review` and `stop` make the same refusal, so no command of a sandboxed run can reach the operator's default server by accident. `review` wraps the reviewer command in its own `srt` (one proxy per reviewer, cwd the repository) under the *reviewer* policy: the base policy (whose write grants cannot reach the repository, by the refusal in §2.4) plus the derived reads, denies and temp grant, with no worktree or git-dir write and no tmux socket, so a Mode B reviewer is mechanically read-only on the repository and cannot reach the Developer server; `SSL_CERT_FILE` and the run's `TMPDIR` are in its environment. The run end tears the server down: `stop`, the `finalize --accept` that completes the run, and `start-slice` reaching an all-attested completion each kill the server, wait for `srt` to exit, and remove the temp and control directories; `stop --scavenge` with readable state does the same, and with unreadable state and `--run` given kills `pm-run-<run-id>` as it sweeps the run's sessions.

**Reviewer command variants.** Whenever a reviewer will run inside an `srt` sandbox (inherited, because `SANDBOX_RUNTIME` is set in the PM's environment; or wrapped, because the run records a sandbox), `compose_reviewer_command` emits Codex with `sandbox_mode="danger-full-access"`, Qwen without `--sandbox`, and Claude with `--settings '{"sandbox":{"enabled":false}}'`; the interactive profiles add `--no-daemon` for Codex and the same `--settings` for Claude. In Mode C this means a reviewer's read-only-ness rests on its instructions and on whatever its harness mode still restricts (`plan` agents for Claude Code and OpenCode; nothing mechanical for Codex at full access or Copilot at `--allow-all`), and on the floor only for what the floor sees: a tree left dirty fails fact 7, while an ignored file written or a tracked file edited and restored is invisible to it. The contract says so.

### 2.6 Fail-closed rules

A sandbox that was asked for is established and verified, or the launch fails naming what was left behind, exactly as a setup-script failure does today. `launch-child --sandbox` and `start-slice` on a sandboxed run refuse when: `srt` is not on `PATH`; the base policy fails validation; `srt` or `node` resolves under a write grant; a server already answers on the socket before the toolkit starts one; `srt` exits, or the server's socket does not answer, within 10 s (the log tail is quoted); the server's global environment does not carry `SANDBOX_RUNTIME=1`; the negative probe succeeds (`tmux run-shell` asks the server to `touch <socket directory>/<socket>.probe`, the socket directory being `sessions.socket_path(socket).parent`, which must fail and leave no file; the host checks the file itself, and on success removes it, kills the server and refuses); the positive probe fails (`run-shell` touching `<worktree>/.pm/sandbox-probe`, which must succeed and leave a file the host then removes, so a misderived policy is caught before a session starts); or the session cannot be created through the server (`run-shell` fails when no server answers, and a plain `tmux new-session` from the host, which would start an unsandboxed server when none answers, is never issued for a sandboxed run). There is no fallback to an unsandboxed server on any of these paths, and no flag that requests one.

### 2.7 What enforces what

| Property | Layer |
|---|---|
| Writes confined to the effective policy's grants: the worktree, its git dir, the granted git-dir files, the temp dir, the socket, the harness state the operator listed, `srt`'s implicit `~/.npm/_logs` and `~/.claude/debug`, and in Mode C the ledger directory | OS (Seatbelt) |
| Reads of denied paths impossible; hidden existence | OS, to the extent of the operator's `denyRead` list; reads are otherwise allowed |
| Egress only to listed `host:port`s through the proxy; no raw sockets; no DNS | OS deny plus `srt`'s proxy, subject to the exceptions the operator enables (`enableWeakerNetworkIsolation` opens the `trustd` path; `allowLocalBinding` opens loopback and this host's addresses) and filtering by host and port only, never by content |
| No second sandbox, no shared Codex daemon socket, no `ps` of host processes, no signals to host processes | OS (the daemon lock is unwritable and no socket but tmux's is reachable); `--no-daemon` and Claude's `--settings` override are harness launch settings that keep the harness from trying |
| The escape through the worktree's `.git` file and git-dir pointer files; `.git/config` and `.git/hooks`; the state root and control directory unwritable; `/tmp/claude` unwritable | OS, by toolkit-derived literal denies the base policy cannot override |
| The server is really sandboxed before a session starts, and a session can only ever be created inside it | toolkit probes (mechanical, host-checked) and session creation through `run-shell`, fail closed |
| A host-launched `srt` reads a policy the sandbox could not have written | the control directory, unwritable from inside (OS) |
| `detach-client -E` cannot run on the host, and under the attach policy can write nothing but the client's terminal | OS, when the operator attaches through `srt` under the attach policy; the plain-attach prohibition is the operator's discipline |
| Changed files within the authorized surface, protected paths untouched, clean worktree, ancestry | the floor, unchanged |
| A reviewer stays read-only inside a Mode C child | reviewer instructions and harness mode where one applies; the floor for final Git-visible state only; not the OS |
| A Mode B reviewer stays read-only on the repository and off the Developer server | OS (the reviewer policy) |
| Which paths and hosts the operator grants, and whether that is sensible | the operator's judgement, recorded in the pinned policy |
| A CM passing `--sandbox` on every launch | the brief's pin, made mechanical: `launch-child` refuses a launch without `--sandbox`, or with a file that does not match the pin, when the launcher's dossier brief names a sandbox policy (Slice 2) |

### 2.8 Known limitations and residuals (to be documented where operators read)

- macOS `mktemp` without a template ignores `TMPDIR` and fails. Operators who need it add the host's per-user temp directory (`$TMPDIR` of the launching shell) to `allowWrite`; the negative probe does not depend on that directory.
- The proxy's per-run credentials sit in every pane's environment; they are local and die with the server.
- Reads are allow-by-default: transcripts, configuration and credential files of the harnesses themselves are readable unless the operator denies them, and the egress policy, not read confinement, is what keeps them on the machine; the model API a seat is allowed to reach already sees whatever that seat reads, and a host allowed for one purpose (`api.github.com` for Copilot) carries any traffic to that host. Credentials one seat needs (`~/.codex/auth.json`, the Keychain database for Claude Code, OpenCode's key files) are readable by every seat on the same server, because one server carries one policy.
- A Mode C reviewer can write the worktree (§2.5). A detached reviewer survives `stop-child`, sandboxed and network-less; `stop-child` keeps printing its process group for the by-hand check (it still never signals a pgid read from a child-writable file).
- In Mode C the child PM can write any file in the ledger directory, so a sandboxed child could alter another run's ledger file; the ledger is the toolkit's model-performance record, not run evidence, and `PM_LEDGER_DIR` moves it wherever the operator prefers.
- The sandbox can force-update or delete its own branch's ref; the floor's ancestry fact catches a rewritten head at the next `finalize`.
- Claude Code's directory trust is per path and stored in `~/.claude.json`, which stays read-only (it also holds user-scope MCP server definitions, so a writable copy would be a way to run code in the owner's own sessions); the existing rule that node worktrees sit under a parent the owner trusted in each harness is what prevents the dialog, and the readiness check still fails closed on one.
- Harness self-updates fail inside (denied downloads or an unwritable install directory) and print warnings; Copilot still downloads a package into its own home.
- MagicDNS resolution through the proxy failed once in testing and succeeded on retry.
- `srt` is a pre-1.0 package (0.0.81); its configuration surface has changed across releases. The toolkit records `srt --version` in the control record, uses only keys verified here, and `campaign-preflight` reports the installed version.
- Not enforced by anything here: what a harness sends to its model provider; the toolkit's own host-side processes (the CM, `launch-child`, the setup script, the CM's read-only task delegates through `orchestrator`), which remain unsandboxed by design, the CM being the trusted seat.

### 2.9 Decisions recorded (the HANDOFF's open questions)

- **Mechanism:** one OS wrapper (`srt`) around the private tmux server; the harnesses' own sandboxes are turned off inside it because they cannot nest.
- **Read exclusions:** the brief's exclusions become `denyRead` entries in the owner's base policy and are then OS-enforced; the contract keeps the copy into non-goals for the seats' understanding, and says which layer enforces what.
- **Egress:** per-host allowlists are achievable per harness with the host lists in §2.3, and the Tailscale LLM is a `name:443` plus `ip:443` pair. `git push` is denied because no git host is allowlisted; a product gate that needs `git fetch` from a remote would need that host listed, which also permits push to it: the owner decides per campaign, in the policy.
- **Developers and reviewers:** by inheritance in Mode C; by the same server plus a per-reviewer wrap in Mode B.
- **Temp space:** a per-run directory the toolkit creates under the launching environment's `TMPDIR`, exported as `TMPDIR` to the server, removed by `stop-child` and by the Mode B run end; `srt`'s shared `/tmp/claude` is denied.

### 2.10 Example base policy (the operator's file; the toolkit adds §2.4's grants and denies)

For this machine: OpenCode child PM, Codex Developers, Copilot reviewer (optional, with its warning), Claude Code available for any seat; the local LLM on Tailscale; the home directory readable except the listed paths. Harness configuration on this machine lives behind the symlink `~/.agents` to `~/Documents/AI`; with reads allowed by default that needs no entry, and an operator who instead denies `~` must re-allow both the link and its target (§2.3, *Reads*).

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

Reading it: `denyRead` holds the brief's exclusions (`docs/dev`), the owner's private directories and the credential files no seat here needs; `~/.llm/adacs` is denied as a directory and its key re-allowed because OpenCode insists on resolving every key reference; the `~/.claude/projects` pair keeps other sessions' Claude Code transcripts unreadable while this campaign's own, named by the campaign id in the mangled worktree path, stay readable and writable; Codex transcripts under `~/.codex/sessions` stay readable because Codex reads its own session files back and names them without a per-run prefix, a disclosed residual; `denyWrite` is explicit about the global configuration even though nothing grants it, so a later widening of `allowWrite` cannot silently expose it; `opencode.ai:443` is OpenCode's hosted provider (`opencode-go/*` models) and is dropped when every OpenCode seat uses the local models; `api.github.com` is there for Copilot's login and carries any GitHub API traffic, not only Copilot's; `enableWeakerNetworkIsolation` is there only for Copilot and is dropped, with the two GitHub hosts, when Copilot is not a seat. Lines an operator will change per machine: the Tailscale names and address, the data directory, the campaign id in the two project globs, and the `docs/dev` exclusion.

## 3. Implementation Profiles

- Recommended for frontier/senior implementer: Slices 1 to 5 in order, one at a time; they build on each other and each has its own tests.
- Recommended for standard implementer: the same; Slices 1, 2 and 4 deserve the strongest Developer.
- Recommended for weaker implementer: not recommended for Slices 1, 2 and 4.

No batches: every slice is independently gateable and Slices 1 to 4 each touch a risky surface.

## Slice 1: `sandbox.py`, the policy and server mechanics

### Intended Change

- Add `skills/project-manager/scripts/pm_lib/sandbox.py`: load and validate a base policy; derive the effective, reviewer and attach policies for a worktree and branch; the control directory and its record; start, verify and stop a sandboxed tmux server; wrap a one-shot command; detect `SANDBOX_RUNTIME`; create and remove the per-run temp directory.
- Add `sessions.server_running(socket)` (a server answers on the socket, with or without sessions), `sessions.run_shell(socket, command)` (`tmux -L <socket> run-shell <command>`, returning rc and output) and `sessions.start_session_via_server(...)` (today's `start_session` argv issued through `run-shell`, so it can only succeed inside a live server), all taking the socket as an argument like `kill_server`.
- No command uses any of this yet; this slice is the mechanism and its tests.

### Acceptance Criteria

- [ ] `sandbox.load_base_policy(path)` returns the parsed object for a file whose top level is an object holding `network` and `filesystem` objects.
- [ ] `load_base_policy` raises `PmError` naming the file and the reason for each of: an unreadable or non-JSON file; a non-object top level; a missing `network` or `filesystem` object; `allowAllUnixSockets` true; `allowAppleEvents` true; `filesystem.disabled` true; an `allowWrite` entry that does not start with `/` or `~`; an `allowWrite` entry that resolves (as `srt` resolves, with `~` expanded and symlinks followed) to `/`, the home directory, `sandbox.toolkit_root()` (the directory containing `skills/project-manager`), or the real path of `sessions.socket_path("x").parent` (the tmux socket directory, which honours `TMUX_TMPDIR`).
- [ ] `load_base_policy` returns a list of warning strings, one for `enableWeakerNetworkIsolation` true and one for `network.allowLocalBinding` true, each naming what the option opens; callers print them.
- [ ] `sandbox.derive_policy(base, *, worktree, branch, socket, tmpdir, control_root, pm_inside, extra_read=())` raises `PmError` when a base `allowWrite` entry resolves to the worktree or the common git dir, to an ancestor of either, or to a path inside either; otherwise it returns a new object (the base is not mutated) whose `allowWrite` is the base's plus the worktree, the worktree's git dir (`git rev-parse --absolute-git-dir`) and, only when the git dir differs from the common dir, `<common>/objects` and the literal entries `<common>/refs/heads/<branch>`, `<common>/refs/heads/<branch>.lock`, `<common>/logs/refs/heads/<branch>`, `<common>/logs/refs/heads/<branch>.lock` and `<common>/packed-refs.lock`; then `sessions.socket_path(socket)`, `tmpdir`, and, only when `pm_inside` is true, `ledger.ledger_dir()`.
- [ ] The derived `denyWrite` is the base's plus literal entries for `<common>/config`, `<common>/hooks`, `control_root`, `/tmp/claude` and `/private/tmp/claude`; plus, only when `<worktree>/.git` is a file, literal entries for `<worktree>/.git`, `<git dir>/commondir`, `<git dir>/gitdir`, `<git dir>/config.worktree` and `<git dir>/hooks`; plus, only when `pm_inside` is false, a literal entry for `<git dir>/pm`.
- [ ] The derived `allowRead` is the base's plus the worktree, the common dir, `sandbox.toolkit_root()` and every path in `extra_read`; `network.allowUnixSockets` gains `sessions.socket_path(socket)`; `allowPty` is `true`; `network.allowedDomains`, `network.deniedDomains`, `filesystem.denyRead` and `filesystem.denyWrite` exist as lists even when the base omitted them.
- [ ] `sandbox.derive_reviewer_policy(base, *, worktree, tmpdir, control_root, extra_read=())` applies the same base refusal and returns the base plus the same `allowRead`, the same `denyWrite` entries that do not depend on `pm_inside`, `allowWrite` extended by `tmpdir` only, `allowPty` true, and `network.allowUnixSockets` exactly as the base had it (no socket added); `sandbox.derive_attach_policy(base, *, socket, control_root)` returns the base's `network` (with the socket added to `allowUnixSockets`), `denyRead` and `allowRead`, an empty `allowWrite`, `allowPty` true, and the base's `denyWrite` plus literal entries for `control_root`, `/tmp/claude`, `/private/tmp/claude`, `~/.npm/_logs` and `~/.claude/debug`.
- [ ] `sandbox.check_binaries(policy)` raises `PmError` when any `allowWrite` entry of `policy` (literal or directory, resolved as above) is equal to, an ancestor of, or inside `sandbox.srt_package_root()` (the directory of the real path of the `srt` entry point, walked up to the directory holding its `package.json`) or equal to or an ancestor of the real path of `shutil.which("node")`, and when `srt` is absent ("srt is required for a sandboxed run; install @anthropic-ai/sandbox-runtime"); `sandbox.srt_version()` returns `srt --version`'s output.
- [ ] `sandbox.control_dir(common_dir, socket)` is `<common dir>/pm-sandbox/<socket>`; `sandbox.write_record(control, **fields)` and `read_record(control)` round-trip a JSON object holding at least `worktree`, `branch`, `socket`, `tmpdir`, `policy`, `policy_sha256`, `srt_version`, `srt_pid`, `server_pid`, `started_at`; `read_record` raises `PmError` on a missing or malformed file.
- [ ] `sandbox.make_tmpdir(socket)` creates `pm-sandbox-<socket>` under `tempfile.gettempdir()` and raises `PmError` when it already exists; `sandbox.remove_tmpdir(path)` removes it recursively only when its name starts with `pm-sandbox-` and its parent is `tempfile.gettempdir()`, and is a no-op when it is already gone.
- [ ] `sandbox.start_server(*, socket, policy_path, worktree, tmpdir, log_path)` raises `PmError` when `sessions.server_running(socket)` is already true; otherwise runs `srt -s <policy_path> env TMPDIR=<tmpdir> SSL_CERT_FILE=/etc/ssl/cert.pem tmux -L <socket> -D` (the `SSL_CERT_FILE` pair only when that file exists) with cwd `worktree`, stdin closed, stdout and stderr appended to `log_path`, in its own session, with `PM_RUN_TOKEN` absent from its environment, on Darwin only (`PmError` "sandboxing is implemented for macOS only" elsewhere); it polls `server_running` every 0.1 s for up to 10 s and returns `(srt_pid, server_pid)` where `server_pid` is `tmux display-message -p '#{pid}'`; it raises `PmError` quoting the last three log lines when the process exits first or the deadline passes, after killing the process group.
- [ ] `sandbox.verify_server(*, socket, worktree)` raises `PmError` unless all of: `sessions.server_environment(socket, "SANDBOX_RUNTIME") == "1"`; the negative probe `sessions.run_shell(socket, "touch <sessions.socket_path(socket).parent>/<socket>.probe")` returns non-zero and the host finds no such file (a file found is removed, the server killed with `sessions.kill_server`, and the error names the probe); the positive probe `run_shell(socket, "touch <worktree>/.pm/sandbox-probe")` returns zero and the host finds the file, which it then removes.
- [ ] `sessions.start_session_via_server(socket, session, repo, command, env)` composes exactly the `new-session` argv `start_session` composes today (same shell command, same `PM_RUN_TOKEN` handling) and issues it as `tmux -L <socket> run-shell <that argv, shell-quoted>`; it raises `PmError` when `run-shell` fails, and never runs a bare `new-session`.
- [ ] `sandbox.stop_server(*, socket, record)` calls `sessions.kill_server(socket)`, waits up to 5 s for `record["srt_pid"]` to exit (reporting `srt still running: <pid>` when it does not), removes `record["tmpdir"]` through `remove_tmpdir`, removes the control directory, and returns the remaining-sessions list from `kill_server` together with those notes.
- [ ] `sandbox.inside_sandbox()` is `os.environ.get("SANDBOX_RUNTIME") == "1"`; `sandbox.wrap_command(policy_path, argv, *, tmpdir)` returns `["srt", "-s", policy_path, "env", "TMPDIR=<tmpdir>", ("SSL_CERT_FILE=/etc/ssl/cert.pem" when that file exists), *argv]`.
- [ ] `sessions.server_running(socket)` is true when `tmux -L <socket> display-message -p '#{pid}'` succeeds, false on `_no_server` replies, and raises `PmError` on any other failure; `sessions.run_shell` returns `(returncode, output)`, raises only when tmux itself cannot be run, and against a socket with no server returns non-zero without starting one.
- Behaviour that must not change: nothing outside the new module and the two `sessions` helpers; the existing `sessions` API and the test suite's socket pinning.

### Authorized Surface

- Files allowed to change:
  - `skills/project-manager/scripts/pm_lib/sandbox.py` (new file)
  - `skills/project-manager/scripts/pm_lib/sessions.py`
  - `skills/project-manager/tests/test_sandbox.py` (new file)
  - `skills/project-manager/tests/pm_test_helpers.py`
- Functions/classes/components allowed to change: everything in the new module; `sessions.server_running`, `sessions.run_shell` and `sessions.start_session_via_server` (new; `start_session`'s argv composition may be factored into a helper both use, with its behaviour unchanged); a shared sandbox-test fixture (a minimal valid base policy writer, a `requires_srt` skip decorator, a linked-worktree builder) in `pm_test_helpers`.
- Tests allowed or expected to change: `test_sandbox.py` (new); `pm_test_helpers.py` additions only.

### Explicit Non-Goals

- No CLI flag, no change to `campaign.py`, `slice_ops.py`, `review.py`, `profiles.py`, `preflight.py` or any document.
- No scrubbing or recording of the inherited environment (declined, D4 of the shakedown review).
- No Linux or Windows support.
- No retries, no fallback to an unsandboxed server, no partial policy on any validation failure.

### Risk Flags

- Risky surfaces touched: global state (the operator's tmux socket directory, a per-run temp directory, a control directory in the repository's git dir), permissions (the Seatbelt policy derivation; the escape-closing denies).
- Approval needed before implementation: no
- Independent audit required: yes
- Difficulty: hard
- Recommended Developer: a frontier model; the derivation and the probes are security-bearing.

### Validation Plan

- Tests to add/update, in `tests/test_sandbox.py`, without `srt`: one test per `load_base_policy` refusal class (including a tilde-spelled home `allowWrite`, the toolkit root and the tmux directory) and one for the two warnings; derivation for a linked worktree (the common-dir grants and the five escape denies present, the state-root deny present only with `pm_inside` false, the ledger grant only with it true) and for a plain checkout (no common-dir grants, no escape denies); the reviewer and attach derivations (the attach policy's `denyWrite` carrying the four implicit-grant denies); the base refusal of a worktree-covering, ancestor or inside grant; `check_binaries` with a fake `srt` package placed under a granted directory and with a grant inside it; the record round-trip; `make_tmpdir`/`remove_tmpdir` refusals; `wrap_command`. With `srt` (skipped with a reason when `srt` or `tmux` is absent, each on a `pm-tests-<pid>-sb*` socket): a started server passes `verify_server`; a session created with `start_session_via_server` has the environment prefix and no `PM_RUN_TOKEN`, cannot write beside the worktree and can write inside it; `start_session_via_server` against a socket with no server raises and leaves no server; a plain `tmux` server on the socket fails `verify_server` on the negative probe and is killed; a policy whose `denyWrite` covers `<worktree>/.pm` fails the positive probe; `stop_server` leaves no server, no temp dir and no control dir; `start_server` on a socket with a live server refuses.
- Commands to run: `cd skills/project-manager/tests && python3 -m unittest test_sandbox test_sessions`; then the full parallel loop from CONTRIBUTING.
- Lint (differential, via the `lint` skill): required.
- Manual checks: with a real `srt`, start a server through the module on a throwaway linked worktree, create a session from the host, and confirm in the pane that `git commit` works, `git branch <sibling>` and `git update-ref refs/heads/main HEAD` fail, `echo x > .git` fails, `touch /tmp/claude/x` fails, and `curl https://example.com` fails without an allowlist entry and returns 200 with one.

### Rollback Path

- Delete `sandbox.py` and `test_sandbox.py`; revert the two `sessions` helpers and the helper additions. Nothing else references them.

## Slice 2: Mode C integration: `launch-child --sandbox`, `stop-child`, the brief pin, `campaign-preflight`

### Intended Change

- `launch-child` gains `--sandbox FILE`. With it, after the worktree exists and `--setup` has run, the command creates the control directory `<common>/pm-sandbox/<socket>/` and the temp directory, derives the effective policy (`pm_inside` true; extra read: the directory holding `--prompt-file`) and the attach policy, writes both to the control directory, starts and verifies the server, writes the record, and creates the child's session through the server (`sessions.start_session_via_server`); the manifest gains `sandbox: {policy, policy_sha256, socket, control_dir}`; the `watch:` line becomes `srt -s <control dir>/attach-policy.json tmux -L <socket> attach -t <session>`; the launch-failure path stops the server through `sandbox.stop_server` (temp and control directories included) where it kills the server today.
- `launch-child` refuses, before anything is created, a launch whose launcher file lies inside a dossier whose `brief.md` (found by walking up from `--prompt-file` to the nearest directory holding `brief.md`) names a sandbox policy in a `## Sandbox` section, unless `--sandbox` names a file whose SHA-256 equals that pin; a brief whose `## Sandbox` section says `none`, a brief without the section, or no brief found, leaves `--sandbox` optional.
- `stop-child` on a sandboxed child derives the control directory from the worktree's common git dir and the manifest's socket (never from a manifest path), reads the record there, refuses when the record's `worktree` is not this worktree, and after the ownership checks and the kill that exist today calls `sandbox.stop_server` with that record, printing what it removed.
- `campaign-preflight` reads the brief's `## Sandbox` section: absent or `none` passes with `ok sandbox: none`; a named `sandbox.json` must exist in the dossier, be tracked and unchanged (added to `OWNER_FILES`), match its SHA-256 pin, pass `sandbox.load_base_policy` (each warning printed as a `note`), and `srt` must be on `PATH` (its version printed in the `ok` line).

### Acceptance Criteria

- [ ] `launch-child --sandbox FILE` with a fake harness starts a server on the child's socket whose global environment carries `SANDBOX_RUNTIME=1` and `PM_CHILD_WORKTREE=<worktree>`, writes `<control dir>/policy.json` whose `allowWrite` contains the worktree and whose `denyWrite` contains the literal `<worktree>/.git`, writes `<control dir>/record.json` naming the worktree, and writes a manifest with the `sandbox` block (all four keys non-empty strings).
- [ ] The fake child in that launch records that `touch <worktree parent>/leak` fails (rc non-zero, no file created), that `touch <control dir>/x` fails, that `touch <worktree>/.pm/inside` succeeds, that `$TMPDIR` is the record's `tmpdir`, and that `PM_RUN_TOKEN` is unset.
- [ ] `launch-child --sandbox` writes `<control dir>/attach-policy.json` with an empty `allowWrite` and prints a `watch:` line of the form `srt -s <control dir>/attach-policy.json tmux -L <socket> attach -t <session>`.
- [ ] `launch-child` without `--sandbox` behaves exactly as before: no control directory, no `sandbox` key in the manifest, the `watch:` line unchanged, the existing campaign tests unchanged.
- [ ] `launch-child --sandbox` refuses, before any branch or worktree exists, with `srt` absent from `PATH` (message names srt), with a base policy that fails validation (message names the file and reason), and when the prompt file's dossier brief pins a sandbox policy and `--sandbox` is absent or names a file with a different SHA-256 (message quotes the pin); a brief with `## Sandbox` saying `none` or without the section leaves `--sandbox` optional.
- [ ] When the server fails verification (tested by a base policy whose `denyWrite` covers `<worktree>/.pm`, which fails the positive probe), the launch fails after the worktree exists, the error names the probe, no server remains on the socket, the temp and control directories are gone, and the branch and worktree are named as left behind.
- [ ] `stop-child` on a sandboxed child kills the server, waits for the recorded `srt` pid to exit, removes the record's temp dir and the control directory, and prints `removed sandbox temp dir <path>` and `removed sandbox control dir <path>`; re-run, it prints that both were already gone; on an unsandboxed child its output is unchanged.
- [ ] `stop-child` refuses, killing nothing, when the record found under the derived control directory names another worktree; a manifest `sandbox.control_dir` pointing elsewhere is ignored.
- [ ] `campaign-preflight` prints `ok sandbox: none` for a brief without a `## Sandbox` section and for one whose section is `none`; for a pinned `sandbox.json` it prints `ok` lines for tracked-and-unchanged, pin match, policy valid and `srt <version> on PATH`, and `FAIL` lines with instructions for each of: missing file, SHA-256 mismatch, invalid policy, `srt` absent; each `load_base_policy` warning is a `note`.
- [ ] The launch-failure cleanup path stops the server and removes the temp and control directories when the session fails to start after the server was verified (test: a fake harness that exits immediately so readiness fails).
- Behaviour that must not change: every existing `test_campaign.py` and `test_preflight.py` test; manifest fields and their order for an unsandboxed child; `stop-child`'s ownership refusals.

### Authorized Surface

- Files allowed to change:
  - `skills/project-manager/scripts/pm_lib/campaign.py`
  - `skills/project-manager/scripts/pm_lib/preflight.py`
  - `skills/project-manager/scripts/pm_lib/sandbox.py`
  - `skills/project-manager/scripts/pm_lib/cli.py`
  - `skills/project-manager/tests/test_campaign.py`
  - `skills/project-manager/tests/test_preflight.py`
  - `skills/project-manager/tests/pm_test_helpers.py`
- Functions/classes/components allowed to change: `campaign.launch_child`, `_check_launch_preconditions`, `_ChildLaunch`, `_start_child_session`, `stop_child`, `StopChildOutcome`, `LaunchChildOutcome` (the `watch` form), `_MANIFEST_FIELDS` (the `sandbox` key is optional and not added to the required tuple); `preflight.campaign_preflight`, `OWNER_FILES`; `sandbox.brief_sandbox_pin(text)` (new: the `## Sandbox` section reader, used by both) and `sandbox.find_brief(path)` (new); the `launch-child` parser and the `launch-child`/`stop-child`/`campaign-preflight` handlers in `cli.py`.
- Tests allowed or expected to change: `test_campaign.py`, `test_preflight.py`, `pm_test_helpers.py` (a fake child that records the outcomes above).

### Explicit Non-Goals

- No change to `wait-child`, `answer-child`, the question channel, or `sessions.start_session`'s behaviour for unsandboxed launches.
- No killing of reviewer process groups by `stop-child` (unchanged: printed for the by-hand check).
- No Mode B behaviour (Slice 4); no reviewer or profile change (Slice 3); no documentation (Slice 5) beyond docstrings.
- No reading of the brief for anything but the `## Sandbox` pin, and no parsing of `work.md`.

### Risk Flags

- Risky surfaces touched: public CLI flags (`launch-child --sandbox`), permissions (the launch-time policy and probes), global state (servers, temp and control directories on the host).
- Approval needed before implementation: no
- Independent audit required: yes
- Difficulty: hard
- Recommended Developer: a frontier model.

### Validation Plan

- Tests to add/update: one test per criterion above, in `test_campaign.py` and `test_preflight.py`; `srt`-dependent tests skip with a reason when `srt` is absent.
- Commands to run: `cd skills/project-manager/tests && python3 -m unittest test_campaign test_preflight test_sandbox`; the full parallel loop; `python3 skills/project-manager/scripts/pm.py campaign-preflight` against a scratch dossier with and without a sandbox pin.
- Lint (differential, via the `lint` skill): required.
- Manual checks: the README's "Verify campaign mode" sequence with `--sandbox` added and a policy allowing nothing beyond the derived grants: the fake child runs, the printed `watch:` command attaches and detaches cleanly, `stop-child` cleans up, `ls /private/tmp/tmux-$(id -u)/` shows no `pm-child-*` socket, `pgrep -f "tmux -L pm-child"` is empty and `ls $TMPDIR | grep pm-sandbox` is empty.

### Rollback Path

- Revert the four modules' changes; `sandbox.py` from Slice 1 stays unused. No state format changes: the manifest key is optional.

## Slice 3: reviewer and profile variants inside a sandbox, and the prompt paragraph

### Intended Change

- `review.compose_reviewer_command` takes `sandboxed: bool`; when true, Codex gets `-c sandbox_mode="danger-full-access"` instead of `read-only`, Qwen omits `--sandbox`, and Claude adds `--settings '{"sandbox":{"enabled":false}}'`; `review.run_review` passes `sandbox.inside_sandbox() or bool(state.get("sandbox"))`, and when the run records a sandbox and the process is not inside one it wraps the composed command with `sandbox.wrap_command` under `<control dir>/reviewer-policy.json` (written by `init` in Slice 4; this slice reads the path from `state["sandbox"]["control_dir"]`).
- `profiles.compose_command` and `profiles.launch_command` take `sandboxed: bool`; the Codex profile gains `sandboxed_args: ["--no-daemon"]` and the Claude profile `sandboxed_args: ["--settings", "{\"sandbox\":{\"enabled\":false}}"]`, appended when true; `slice_ops.start_slice` passes `sandbox.inside_sandbox() or bool(state.get("sandbox"))` and `campaign.launch_child` passes `sandbox is not None`.
- `references/developer-prompt.md` and `references/reviewer-prompt.md` gain one short conditional paragraph, rendered by `prompts` when sandboxed: writes outside the repository and `$TMPDIR` fail with "Operation not permitted"; the network reaches only the hosts the operator listed; `git push`, `stash`, `tag`, `worktree`, `gc` and other branches are unavailable; a denied operation is a boundary, not a bug to work around.

### Acceptance Criteria

- [ ] `compose_reviewer_command("codex", …, sandboxed=True)` contains `sandbox_mode="danger-full-access"` and not `read-only`; `sandboxed=False` is byte-identical to today's command.
- [ ] `compose_reviewer_command("qwen", …, sandboxed=True)` omits `--sandbox`; `compose_reviewer_command("claude", …, sandboxed=True)` ends with `--settings` and the sandbox-disabling JSON; Copilot and OpenCode commands are unchanged in both cases, and every tool's `sandboxed=False` command is unchanged.
- [ ] With `SANDBOX_RUNTIME=1` in the environment and no sandbox recorded in the run, `review` runs the reviewer unwrapped with the sandboxed variants (fake reviewer records its argv and environment).
- [ ] With a sandbox recorded in the run and `SANDBOX_RUNTIME` unset, `review` runs `srt -s <control dir>/reviewer-policy.json env TMPDIR=<run tmpdir> … <reviewer argv>`; with `srt` present, the fake reviewer records that it could read the repository and the diff path, could not write the repository, and could not list sessions on the run's tmux socket.
- [ ] With neither, `review` is byte-identical to today (existing `test_review.py` tests unchanged).
- [ ] `profiles.compose_command("codex", sandboxed=True)` ends with `--no-daemon` and `compose_command("claude", sandboxed=True)` carries the `--settings` pair; every other harness and `sandboxed=False` are unchanged; `launch_command` threads the flag.
- [ ] `start_slice` in a run with a recorded sandbox, or with `SANDBOX_RUNTIME=1`, launches the Codex profile with `--no-daemon` and renders the sandbox paragraph into `prompt.md`; otherwise neither (existing prompt tests unchanged).
- [ ] The reviewer's recorded `command_override` and ledger rows are unchanged by the wrap: the wrap is applied at `Popen` time, after the command used for recording is composed; the wrapped `srt` is the process-group leader and its children share the group (*tested*), so the existing `killpg` timeout path kills the whole group (the `srt`-gated test asserts no `sleep` survives a timed-out fake reviewer).
- Behaviour that must not change: reviewer timeout and `killpg` semantics; every existing `test_review.py`, `test_profiles.py`, `test_prompts.py` and `test_slice_ops.py` test.

### Authorized Surface

- Files allowed to change:
  - `skills/project-manager/scripts/pm_lib/review.py`
  - `skills/project-manager/scripts/pm_lib/profiles.py`
  - `skills/project-manager/scripts/pm_lib/prompts.py`
  - `skills/project-manager/scripts/pm_lib/sandbox.py`
  - `skills/project-manager/scripts/pm_lib/slice_ops.py`
  - `skills/project-manager/scripts/pm_lib/campaign.py`
  - `skills/project-manager/references/developer-prompt.md`
  - `skills/project-manager/references/reviewer-prompt.md`
  - `skills/project-manager/tests/test_review.py`
  - `skills/project-manager/tests/test_profiles.py`
  - `skills/project-manager/tests/test_prompts.py`
  - `skills/project-manager/tests/test_slice_ops.py`
- Functions/classes/components allowed to change: `compose_reviewer_command`, `_build_reviewer_command`, `run_review` (the composition and `Popen` call sites only); `HARNESS_PROFILES["codex"]`, `HARNESS_PROFILES["claude"]`, `compose_command`, `launch_command`; `render_developer_prompt`, `render_reviewer_prompt` and the two reference templates' fenced blocks; in `slice_ops.start_slice` and `campaign._check_launch_preconditions` only the `launch_command` call and the `prompts` call.
- Tests allowed or expected to change: the four test modules listed.

### Explicit Non-Goals

- No change to which reviewers are mandatory, to review freshness, to judgments, or to the ledger schema.
- No Copilot or Qwen behaviour beyond the `--sandbox` omission; Qwen is not tested against a real sandbox.
- No new run-state fields (Slice 4 adds the Mode B `sandbox` block; this slice reads it when present and treats absence as unsandboxed).
- No change to any reference document other than the two prompt templates' fenced blocks.

### Risk Flags

- Risky surfaces touched: permissions (the reviewer's sandbox mode inside a boundary).
- Approval needed before implementation: no
- Independent audit required: yes
- Difficulty: moderate
- Recommended Developer: a strong model.

### Validation Plan

- Tests to add/update: one test per criterion; the wrapped-reviewer test is `srt`-gated.
- Commands to run: `cd skills/project-manager/tests && python3 -m unittest test_review test_profiles test_prompts test_slice_ops`; the full parallel loop.
- Lint (differential, via the `lint` skill): required.
- Manual checks: none beyond the tests; the real-model behaviour of Codex `danger-full-access` inside `srt` was established in the proof of concept.

### Rollback Path

- Revert the module and template changes; the `sandboxed` parameters default to false, so callers compile either way.

## Slice 4: Mode B: `init --sandbox`, the run socket, the server's life with the run

### Intended Change

- `init --sandbox FILE` validates the base policy (printing its warnings), records `sandbox: {policy, policy_sha256, socket: "pm-run-<run-id>", control_dir}` in `run.json` (MAC-protected like the rest), creates the control directory `<common>/pm-sandbox/<socket>/` and the temp directory, derives and writes `policy.json` (`pm_inside` false; extra read: the plan file's directory), `reviewer-policy.json` and `attach-policy.json` there, writes the record (without pids), and prints `export PM_TMUX_SOCKET=<socket>` immediately after the token line. It does not start the server.
- `sandbox.require_run_socket(state)` raises `PmError` when the run records a sandbox and `PM_TMUX_SOCKET` is unset or differs from the recorded socket (the message gives the export line); `status`, `start-slice`, `observe`, `send`, `answer`, `finalize`, `review` and `stop` call it after loading state.
- `start-slice` on a sandboxed run ensures the server: when `sessions.server_running` is false it re-derives the policy from the recorded base file (refusing when the file's SHA-256 no longer matches), rewrites `policy.json`, starts and verifies the server and updates the record's pids; when it is running it checks `SANDBOX_RUNTIME=1` in the server environment and the record's server pid, refusing otherwise. It then creates the Developer's session with `sessions.start_session_via_server`, never with a bare `new-session`.
- The run end tears the server down: `stop` (including `--scavenge` when state is readable), the `finalize --accept` that sets the run `complete`, and `start-slice`'s all-attested completion call `sandbox.stop_server` with the record and append a `sandbox-stopped` event; `stop --scavenge` with unreadable state and `--run` given also kills `pm-run-<run-id>`; a `require_run_socket` refusal inside `stop --scavenge` is reported and never falls through to the ambient-server sweep.

### Acceptance Criteria

- [ ] `init --sandbox FILE` records the `sandbox` block with all four keys, writes `<control dir>/policy.json` whose `allowWrite` contains the repository and whose `denyWrite` contains the literal `<git dir>/pm`, writes `reviewer-policy.json`, `attach-policy.json` and `record.json`, creates the temp dir, and prints the `export PM_TMUX_SOCKET=pm-run-<run-id>` line after the token line; without `--sandbox`, `run.json` has no `sandbox` key and the output is unchanged.
- [ ] `init --sandbox` refuses with `srt` absent or an invalid policy, creating no run directory, no control directory and no temp dir.
- [ ] On a sandboxed run, `start-slice` with `PM_TMUX_SOCKET` unset or set to another name refuses, naming the export line, and launches nothing; the same refusal from `status`, `observe`, `send`, `finalize`, `review` and `stop` (one test each, asserting no tmux call was made).
- [ ] On a sandboxed run with the right socket and no server, `start-slice` starts a server whose environment carries `SANDBOX_RUNTIME=1`, launches the fake Developer on it through `start_session_via_server` (asserted by a `sessions.start_session` spy that is never called), and the fake records that a write beside the repository fails, that `mkdir <git dir>/pm/x` fails, and that `$TMPDIR` is the recorded temp dir; a second `start-slice` (relaunch) reuses the running server (the record's `srt_pid` is unchanged).
- [ ] `start-slice` refuses when a server is running on the recorded socket without `SANDBOX_RUNTIME=1` (test: start a plain `tmux -L pm-run-<id>` server first), and when the recorded base policy file's SHA-256 differs from the recorded one.
- [ ] `stop --reason …` on a sandboxed run leaves no server on the socket, no temp dir and no control dir and appends a `sandbox-stopped` event; `finalize --accept` of the last slice does the same; `start-slice` on a run whose slices are all attested does the same; an unsandboxed run's `stop`, `finalize` and `start-slice` are byte-identical to today.
- [ ] `stop --scavenge --run <id>` with unreadable state kills the server on `pm-run-<id>` when one exists and reports it; `stop --scavenge` on a readable sandboxed run with the wrong `PM_TMUX_SOCKET` prints the refusal and sweeps nothing.
- [ ] `status` shows one line `sandbox: <policy path> (sha256 <12 chars>) socket <name>` for a sandboxed run and nothing new otherwise; `run-report.md` carries the same line in its header block.
- Behaviour that must not change: the floor; every existing `test_slice_ops.py`, `test_finalize.py`, `test_state.py` test; `PM_TMUX_SOCKET` semantics for unsandboxed runs.

### Authorized Surface

- Files allowed to change:
  - `skills/project-manager/scripts/pm_lib/slice_ops.py`
  - `skills/project-manager/scripts/pm_lib/sandbox.py`
  - `skills/project-manager/scripts/pm_lib/state.py`
  - `skills/project-manager/scripts/pm_lib/cli.py`
  - `skills/project-manager/scripts/pm_lib/review.py`
  - `skills/project-manager/tests/test_slice_ops.py`
  - `skills/project-manager/tests/test_finalize.py`
  - `skills/project-manager/tests/test_state.py`
  - `skills/project-manager/tests/pm_test_helpers.py`
- Functions/classes/components allowed to change: `slice_ops.init_run`, `start_slice`, `status`, `finalize_accept`, `stop`, `stop_scavenge_sweep`, `regenerate_report`'s header; `state` only for the `sandbox` key in the schema comment and the report header; the `init` parser, `_run_stop` and the handlers named above in `cli.py`; `review.run_review` only to call `require_run_socket`; `sandbox.require_run_socket`, `sandbox.ensure_run_server`, `sandbox.teardown_run` (new).
- Tests allowed or expected to change: the three test modules and the helpers.

### Explicit Non-Goals

- No automatic adoption of the socket from state into the environment: the PM exports it, and the toolkit refuses a mismatch.
- No sandboxing of the PM's own process; no wrapping of `check-plan`, `init`, `ledger`, `judge-reviews`, `judge-developer`, `notes`, `approve` or `grant`, which touch no tmux server.
- No change to the campaign commands beyond what Slice 2 made.
- No new `pm` subcommand.

### Risk Flags

- Risky surfaces touched: public CLI flags (`init --sandbox`), persistence (a new optional `run.json` key and event kind), global state (a per-run server, temp and control directory).
- Approval needed before implementation: no
- Independent audit required: yes
- Difficulty: hard
- Recommended Developer: a frontier model.

### Validation Plan

- Tests to add/update: one test per criterion; server-starting tests are `srt`-gated.
- Commands to run: `cd skills/project-manager/tests && python3 -m unittest test_slice_ops test_finalize test_state test_sandbox`; the full parallel loop.
- Lint (differential, via the `lint` skill): required.
- Manual checks: the README's "Verify your setup" hello-file run with `init --sandbox` and the example policy reduced to its `network`/`filesystem` skeleton: the fake Developer commits inside the sandbox, `finalize --accept` completes the run, and no `pm-run-*` server, `pm-sandbox-*` temp dir or `.git/pm-sandbox/` control dir remains.

### Rollback Path

- Revert the module changes; runs recorded without a `sandbox` key are unaffected, and a run recorded with one can be stopped by `stop --scavenge` and the server by `tmux -L pm-run-<id> kill-server`.

## Slice 5: documentation, the example policy, and the changelog

### Intended Change

- New `skills/project-manager/references/sandbox.md`: the operator's guide and the single home of the sandbox contract: what the boundary is and is not (§2.7 and §2.8 of this plan, kept current), the base-policy format and the toolkit's derived grants and denies (§2.4), the control directory and its three policies, the refusals and warnings, the example policy (§2.10) with its per-line reading and the per-harness facts (§2.3 last three rows), the sandboxed `attach` rule (the write-less attach policy) and why, Mode B and Mode C usage, teardown, and troubleshooting (the `trustd` symptom, `mktemp`, the empty Copilot report, nested-sandbox errors).
- `references/campaign-mode.md`: a short *Sandbox* subsection under *Launching and watching a child* that links to `sandbox.md` and states only what the CM does: the brief's `## Sandbox` pin, `--sandbox` on every launch and the mechanical refusal, the `watch:` line as printed, `stop-child`'s removals; the launcher block gains `Sandbox policy: <dossier>/sandbox.json, or none`; the *attach* sentence in *Launching and watching a child* points at `sandbox.md` for a sandboxed child.
- `references/dossier-template.md`: `sandbox.json` in the layout and the `## Sandbox` section in the brief template with the pin line shape `campaign-preflight` reads.
- `references/run-state.md`: the `sandbox` key, the `sandbox-stopped` event, and the control directory beside the state root.
- `README.md` (project-manager): CLI rows for `init --sandbox`, `launch-child --sandbox`, `stop-child`, `campaign-preflight`; *Requirements* (`srt` optional, macOS, Node 22+, ripgrep); *Trust model, honestly* gains the sentence that with a sandbox the containment line moves from "your sandbox around the run" to the OS boundary `sandbox.md` describes, and what stays outside it; *Start a campaign* step for the policy and pin.
- `skills/implementation-plan/SKILL.md` Mode B launcher: an optional `Sandbox policy: <path>` line and, in the per-run steps, `init --sandbox <path>` when given plus `export PM_TMUX_SOCKET` from init's output; the campaign-child paragraph unchanged (the child is sandboxed by the server, not by its own `init`).
- Top-level `README.md` Mode C requirements line: `srt` when sandboxing; `CHANGELOG.md` Unreleased entry; `CONTRIBUTING.md` source-of-truth map row for the sandbox contract (`references/sandbox.md`) and the test note that `srt`-gated tests skip without it.

### Acceptance Criteria

- [ ] `references/sandbox.md` exists and is the only file that states the derived grant and deny lists, the refusal and warning lists, the example policy, the per-harness facts and the enforcement table; every other document links to it rather than restating them.
- [ ] `campaign-mode.md`, `dossier-template.md`, `run-state.md`, the two READMEs, `implementation-plan/SKILL.md`, `CONTRIBUTING.md` and `CHANGELOG.md` carry the changes listed, and every statement of enforcement names its layer (OS, toolkit probe, floor, harness mode, operator judgement).
- [ ] The campaign launcher and the Mode B launcher each gain exactly one optional sandbox line, and the dossier template's pin line parses with `preflight.SHA256` and `_section(brief, "Sandbox")`.
- [ ] CI's doc-link reachability check passes (`python3 -m unittest discover -s skills/lint/tests` and the repository's link check, as CONTRIBUTING names them).
- [ ] No document claims read confinement beyond the operator's `denyRead` list, and the Mode C reviewer caveat (§2.5) appears once, in `sandbox.md`.
- Behaviour that must not change: no code change in this slice.

### Authorized Surface

- Files allowed to change:
  - `skills/project-manager/references/sandbox.md` (new file)
  - `skills/project-manager/references/campaign-mode.md`
  - `skills/project-manager/references/dossier-template.md`
  - `skills/project-manager/references/run-state.md`
  - `skills/project-manager/README.md`
  - `skills/implementation-plan/SKILL.md`
  - `README.md`
  - `CONTRIBUTING.md`
  - `CHANGELOG.md`
- Functions/classes/components allowed to change: none (documentation only).
- Tests allowed or expected to change: none.

### Explicit Non-Goals

- No change to `docs/VISION.md`: the design keeps isolation the environment's job and only wires it in.
- No change to `SKILL.md`'s charter, floor or always-stop rules, and none to the two prompt templates (Slice 3 owns their sandbox paragraph).
- No per-harness sections beyond the facts established in this plan; Qwen is listed as untested.

### Risk Flags

- Risky surfaces touched: none
- Approval needed before implementation: no
- Difficulty: moderate
- Recommended Developer: a strong model with the whole plan in context.

### Validation Plan

- Tests to add/update: none.
- Commands to run: the lint suite and the link check CONTRIBUTING names; `grep -rn "sandbox" skills/project-manager/references README.md` to confirm single-sourcing.
- Lint (differential, via the `lint` skill): required.
- Manual checks: read `sandbox.md` end to end as an operator setting up the example machine; follow it to produce the example policy.

### Rollback Path

- Revert the document changes; `sandbox.md` is new and unreferenced after the revert.

## Next Chat Prompts

### Mode A — Human-managed plan

```md
Plan file: docs/implementation-plan-child-pm-sandbox.md
Scope: all remaining slices, in plan order
Supervision: checkpointed — I am present: ask me before coding an approval-flagged slice and before each commit
Reviewers (optional): drift auditor codex gpt-6.1-sol high; code reviewers claude claude-opus-5-5 high; codex gpt-6.1-sol high

Read the full plan file first. If a slice or batch receipt in scope is incomplete or the plan state is unclear, stop and tell me before coding.

Work on the current feature branch for this plan; if none exists, create one and tell me the name.

Use orchestrator as the controlling skill. Act as the Developer: keep implementation, validation, Git operations, and commits local. Use a read-only Reviewer (the reviewers above, when given) only for investigation, evidence gathering, the hostile drift-audit skill, and an independent code-review skill pass. If no Reviewer is configured or available, perform Developer self-audit on a default slice and record that provenance explicitly; for a slice whose Risk Flags say `Independent audit required: yes`, stop and report instead of self-auditing.

For each slice or batch in scope, in plan order:
1. Restate the frozen contract (authorized surface + non-goals) from the plan.
2. If any included slice's Risk Flags mark approval-needed, stop before coding: checkpointed, get my approval; autonomous, end the run and report — never self-approve a slice the plan gated for a human.
3. apply the scoped-implementation skill against the selected contract.
4. apply the drift-audit skill using a read-only Reviewer when available; otherwise perform Developer self-audit. Report the authorization gate result and who performed it before any quality review.
5. If the gate fails, fix the drift inside the contract and re-audit; if it cannot be fixed inside the contract, stop and report.
6. On a passing gate: for a broad or structural change, first run the code-health skill differentially against the slice's starting commit and supply its report as review evidence. Then apply the code-review skill using the code reviewers when available, in parallel if there are several; otherwise perform Developer self-audit through the code-review skill. Record who performed it.
7. Collect every finding from the current audit or review round before fixing (checkpointed, surface them to me first), record them in the slice summary, fix them as one batch, then re-run the relevant gate. If consecutive reviews return only minor findings and have clearly converged, record residuals in the slice summary and proceed.
8. Commit with the commit skill: checkpointed, ask me first; autonomous, commit a slice that has passed validation, the drift-audit gate and the code-review gate — and only such a slice.

Stop early on: a blocker, an unapproved scope change, a gate or validation failure unfixable inside the contract, or context pressure. When the scope is done, or on any stop with slices remaining, use the handoff skill to record state, audit provenance (Reviewer tool/label or Developer self-audit and fallback context), and the next slice or batch to resume from. Do not continue past the scope.

When the scope was all remaining slices and the plan is complete, use the report skill instead to write a final report covering slices committed, gate results and audit provenance per slice, validation, and every residual or post-plan consideration left for me to assess. Do not lose a non-blocking observation merely because it did not belong in the frozen plan.

Confirm before starting: plan file read, scope, supervision, branch, and the first slice. Then begin.
```

### Mode B — PM-managed plan

```md
Plan file: docs/implementation-plan-child-pm-sandbox.md
Repo: /Users/dcroton/Local/git-repos/ai-agent-coder
Developer: claude claude-opus-5-5 high
Drift auditor: codex gpt-6.1-sol high
Code reviewers: claude claude-opus-5-5 high; codex gpt-6.1-sol high
Run tag (optional): sandbox

Use the project-manager skill. You are the PM: the accountable supervisor of this run. You never write slice code.

Handoff: HANDOFF.md in the repo root, git-ignored (add it to .git/info/exclude if it is not), is the sole entry point for a restart and for my post-run review. If it names an unfinished run of this plan, read it fully and resume that run (I will supply the token). Otherwise create it after init. Update it after every slice decision and before any stop with: run identity (never the token), seats, branch, commit authorisation, review protocol, slice log, plan decisions, residuals and follow-ups, and PM lessons.

For a new run, prepare, then confirm with me once:
- Read the whole plan, run check-plan with repo context, and check that the repo still matches what the plan assumes. Init freezes the plan, so raise any drift or warning now.
- Require a clean worktree with the plan committed. Propose the plan's feature branch, or a new branch name to create.
- Confirm: plan read, check-plan result, seats, branch, first slice, any approval-flagged slices (each stops the run until I approve it), and my standing authorisation for Developers to commit each slice.
After my go-ahead, run init with full model ids, efforts where the harness supports them, and the run tag. Give me the run id and token, then begin.

Per slice, in plan order:
1. start-slice with the Developer seat's model and effort. Wait with one long background `observe --wait`; never poll.
2. Run finalize, then your own checks: pane, diff, validation output, full test suite, lint, differential code-health if the structure changed, and a style-guide audit (the project's guide, otherwise the baseline). Treat newly introduced, in-contract style deviations as cleanup to steer.
3. Run the review panel when the plan or risk calls for it. Run the drift audit first, then read and rate it. Then start all code reviewers ~90 s apart, sharing one `--adjudicated-file`. Let reviews run to completion without watching them.
4. Rate every report, order the code panel, and score the Developer (criteria met, confirmed defects).
5. Decide: accept, steer or stop. Steer only once every review is in, with all findings batched into one correction. Each follow-up round re-runs the panel, focused on the change since the last round through an adjudication line.
6. Update `pm notes` for the next Developer, and HANDOFF.md for me.

Plan defects: where the plan's goals make one resolution natural and its impact stays within the slice or touches only a few slices, decide it yourself and record it as a delegated plan decision. Never edit the plan, widen the surface except by grant, or relax a floor fact. Stop for anything serious, contested or cross-cutting, and whenever the floor or a plan gate needs a human.

Finish: run `status --report`, confirm the ledger was written, run `ledger render`, and read `run-report.md` end to end. Record the run's actual status in HANDOFF.md. Report:
- total run time, quoted from the report with its endpoints
- commits (unpushed)
- what was accepted, and on what evidence
- what stopped, and why
- plan defects, with the decisions you took and the amendments you recommend
- residual risks and follow-ups, with a convergence read
- the leaderboard path
```
