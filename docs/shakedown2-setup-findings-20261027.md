# Shakedown 2 setup: toolkit findings and campaign-setup lessons

Written 2026-10-27 while setting up the second shakedown campaign (dossier `/Users/dcroton/Local/git-repos/campaigns/shakedown2`, owner guide `/Users/dcroton/Local/git-repos/campaigns/shakedown2-OWNER.md`). Setup ran five model-free rehearsals: a real `launch-child --sandbox` of a fake child whose "PM" ran a probe and Mimic's gates inside the sandbox. It also ran two design-review rounds with Codex `gpt-6.1-sol` high through `orchestrator` (artifacts under `.orchestrator/shakedown2-design/`).

Part 1 lists ai-agent-coder issues to decide or fix before the campaign runs. Part 2 lists what setup taught about preparing a campaign, for the campaign docs. Product-side (Mimic) fixes and campaign-specific setup are in Part 3, for the record only.

## Part 1: ai-agent-coder issues

### Fixed during setup

1. **`srt` swallowed a wrapped command's own flags** (fixed in `8c11b14`). `sandbox.wrap_command` built `srt -s <policy> env … <argv>` with no `--`. `srt` 0.0.81 parses its `-s`, `--settings`, `-c` and `-d` anywhere in the line, so in a sandboxed Mode B run a Codex reviewer's `-c model_reasoning_effort=…` became `srt`'s command string, and a Claude reviewer's `--settings '{…}'` became `srt`'s policy file. Mode C was not affected, because there `srt` wraps only `tmux -D`. A real-`srt` test now pins the fix. The suite never caught it because its harnesses are stubs and no test ran a wrapped argv through a real `srt`.
2. **Claude Code seats needed `CLAUDE_CODE_TMPDIR`** (fixed in `8c11b14`). Claude Code keeps its working files in `/tmp/claude-<uid>`, shared by every Claude Code session on the machine, whatever `TMPDIR` says, and refuses to start when that directory is unreadable. A policy that keeps other sessions' scratch away from children therefore broke every Claude seat. The wrap now exports `CLAUDE_CODE_TMPDIR=<run temp dir>`. This was tested with a sandboxed `claude -p` running a Bash tool call while `/private/tmp/claude-<uid>` and `~/.ssh` were denied.

### Open: decide before the campaign

3. **Children inherit the supervising harness's own session environment.** The probe, run inside a sandboxed child launched from a Claude Code session, recorded several inherited variables:
   - `CLAUDE_CODE_MESSAGING_TOKEN` and `CLAUDE_CODE_MESSAGING_SOCKET`, which are the CM session's inter-session messaging credentials;
   - `CLAUDE_CODE_SESSION_ID`, `CLAUDECODE` and `CLAUDE_EFFORT`;
   - the launching shell's `VIRTUAL_ENV`, the first shakedown's P1.

   Inside the sandbox the socket is unreachable, because only the tmux socket is allowed, so the token is inert there. But a token from the trusted seat should not reach untrusted seats at all. `CLAUDE_EFFORT` may also override a child seat's configured effort; this is untested. The first shakedown's review declined environment scrubbing (its D4). This is new evidence for revisiting that. A narrow option is for `launch-child` to drop `CLAUDE_CODE_*`, `CLAUDECODE` and `VIRTUAL_ENV`, the way it already drops `PM_RUN_TOKEN`. The campaign brief currently lists this as an accepted residual.
4. **`sandbox.md` does not mention `srt`'s built-in write denies or `filesystem.allowGitConfig`.** `srt` always denies writes to `.git/config`, `.git/hooks/`, shell rc files, `.vscode/`, `.idea/` and `.claude/commands|agents/`, even inside a granted directory. So nothing inside the sandbox can `git clone` or `git init`, and any product gate or test that creates a repository fails. `filesystem.allowGitConfig: true` lifts only the `.git/config` part. The toolkit passes the key through unchecked. Its own literal denies on the product repository's `config`, `config.worktree` and `hooks` still hold, which was tested. Decide whether `load_base_policy` should accept the key knowingly, and document it under *The base policy* and in the enforcement table. Shakedown 2's `sandbox.json` sets it for the reference-identity runner.
5. **`sandbox.md` does not warn that a product's gates may not run inside.** Setup hit three incompatibilities, each of which breaks a product silently or with a hang. These are product properties, but the operator guide should name them, and *Producing your policy* step 7 should ask for the product's own gates to be rehearsed inside, not only the README sequence:
   - `git worktree add` is denied in a linked node worktree by design, so any gate that builds a reference in a worktree fails. Mimic's reference-identity gate does this.
   - macOS `mktemp` with no template ignores `TMPDIR` and fails inside. Mimic's test tally then hung on `awk` reading the pane's terminal, so the unit tier never finished.
   - Literal `/tmp/...` paths fail. Mimic had 33 C unit tests that use them.
6. **`srt` glob grants do not reach inside a matched directory** (tested with 0.0.81). `allowWrite` `"/private/tmp/mimic_*"` lets `mkdtemp` create `/tmp/mimic_x_abc` but not write a file inside it, and adding `"/private/tmp/mimic_*/**"` does not help. `sandbox.md` says an entry may be a glob without this caveat. Document it, or test whether another spelling works.
7. **Exclusions cannot be OS-enforced inside a clone a seat makes.** A `denyRead` on `docs/dev` inside a clone the seat creates makes the clone's checkout fail. The reference-identity gate then failed in all ten legs. So an exclusion holds mechanically only in checkouts the host creates, the node worktree included, and rests on instructions inside seat-made clones and git history. Add this to *What it is not*.
8. **No end-to-end check of a real harness in sandboxed Mode B.** Item 1 survived the sandbox plan, its panel reviews and the post-plan review because every test used stub harnesses. Consider a manual check in the README's Mode B verify section: `init --sandbox` plus one real `review` per harness.

### Not issues (checked)

- The reviewer's missing `PM_RUN_TOKEN` is enforced at `review.py:540` and pinned by a test in `test_review.py`.
- The brief's `## Sandbox` section may carry explanatory prose after the pin line. `brief_sandbox_pin` takes the first `SHA-256` it finds.
- Denying the whole campaigns directory still leaves a child its own `nodes/<node>/` through the toolkit's derived `allowRead` (tested).
- `stop-child` tore down every rehearsal completely: server, `srt`, temp dir, control dir, control root and socket file.

## Part 2: campaign-setup lessons for the campaign docs

These belong in `campaign-mode.md`, `dossier-template.md` or `sandbox.md`, as guidance for the owner preparing a campaign. None needs code.

1. **Rehearse the product inside the sandbox before freezing the brief.** Run `launch-child --sandbox <policy> --harness fake --harness-command "sh run.sh"` on a scratch clone of the product. `run.sh` runs every gate the targets name and writes `PM_CHILD_RESULT_PATH` at the end. It is model-free, costs minutes, and would have caught every blocker in Part 1 items 4 to 7 before a real child stalled on them. The README's model-free check proves the toolkit, not the product.
2. **Owner scripts pinned in the brief.** Shakedown 2 pins a boundary probe and a reference-gate runner in the brief by SHA-256, as `setup.sh` is pinned. Children cannot read the dossier outside their node directory, so the CM copies each script byte for byte into `nodes/<node>/` and pins the same hash in the plan. The dossier template could name this pattern: an owner-written measurement script, pinned in the brief, fixed beyond the CM's reach.
3. **A deterministic restart test.** "Restart while a child runs" is racy: a fast child can finish before the new CM process reconciles. A barrier is deterministic. The node's launcher has the child PM ask one fixed question before slice 1, and the CM records the in-flight state and stops without answering. The owner then restarts the process twice, first with a wrong digest and then a right one. The question channel is exercised too.
4. **Mid-campaign instrument re-pin.** sage26's owner file has the procedure, but the campaign docs do not. Give it a home in `campaign-mode.md` *Validation*: controls pass, the owner protects the instrument on the integration branch, re-pins the brief, and resumes the CM with the new digest. Every command should use absolute paths; the first draft of shakedown 2's guide hashed the brief from the wrong directory.
5. **External reference workspaces.** A campaign that builds a reference outside the product repository needs three things. The owner stages the reference source read-only, which avoids GitHub egress and its push exposure. `sandbox.json` grants a writable workspace beside it. The brief names both. sage26's brief currently says to clone SAGE26 from GitHub, which needs `github.com` in the egress list and so permits a push. It should switch to an owner-staged source.
6. **Exclusions in `sandbox.json`.** Deny the whole campaigns directory, the other campaigns' results, the toolkit checkouts' `archive/` and `.orchestrator/`, and `/private/tmp/claude-<uid>`; the last works now that item 2 of Part 1 is fixed. Re-allow only the campaign's own Claude transcript glob. Then say in the brief which exclusions the OS enforces and which rest on instructions: git history, seat-made clones, and `~/.codex/sessions`, which Codex must read.
7. **The CM's terminal.** Launch the CM from a shell without `VIRTUAL_ENV`, and restart it from the same shell. A different `TMPDIR` makes `stop-child` leave the sandbox temp dir behind.
8. **Directory trust for TUI seats.** A Codex Developer runs as a TUI and cannot record trust from inside the sandbox, because its `config.toml` is read-only. The owner trusts the worktree parent in every TUI harness before launch.
9. **Probe design.** A boundary probe's expected denial must match a denial message, not any non-zero exit. A missing path or tool otherwise passes as confinement, which happened in a rehearsal before the fix. Its ref canaries need unique names and a disposable push target, and it should stop on the first unexpected success. A `--host-check` mode lets the owner confirm every target exists before launch.
10. **Keep evidence branches after a campaign.** The first guide's cleanup deleted the integration and node branches. Evidence reports and instruments live only on them, and receipts cite their commits. Remove worktrees, prune, and keep the branches, or bundle them first.

## Part 3: product and campaign setup (record only)

- **Mimic branch `fix/sandbox-safe-mktemp`**, not merged; the owner merges it. It has two commits:
  - `c6b1b593` gives the test tally's and the `Makefile`'s summary-mode `mktemp` calls a template.
  - `03ac85c0` adds a `test_temp_template` helper to `tests/framework/test_framework.h` and uses it at the 33 C test sites.

  The unit tier passes on the host with `TMPDIR` set and unset, and inside the sandbox. It touches protected paths, so it must land on `main` before the base is pinned.
- **The shakedown2 dossier** holds the brief with eight targets, the owner's `sandbox.json`, `sandbox-probe.sh`, `refid.sh` and `setup.sh`, and empty work, evidence and decisions files. It is committed locally. `preflight.sh` now accepts `shakedown2`.
- **Rehearsal results with the final policy:** every probe ALLOW and DENY row met its expectation, except two rows that target the integration checkout, which only preflight creates. All three harnesses authenticated inside. Every T1 gate passed for `halos-only`. `refid.sh` passed `tests-reference-identity` in about 65 s.
