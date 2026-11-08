#!/bin/sh
# Boundary probe for a sandboxed project-manager seat: the model-free rehearsal
# of what the toolkit itself controls (references/sandbox.md, The boundary and
# What the toolkit adds). It asserts nothing about the operator's base policy.
#
#   sh sandbox-probe.sh [OUTDIR]     run inside a seat, from its worktree
#
# Writes OUTDIR/results.tsv (default $TMPDIR/sandbox-probe) and prints it. Each
# row is `KIND NAME EXPECT GOT DETAIL`, tab separated, under a header row:
#   ALLOW  EXPECT success  GOT ok when the command succeeded, else unexpected
#   DENY   EXPECT denied   GOT ok when it failed with "Operation not permitted"
#                          or "Permission denied"; unexpected when it succeeded
#                          or failed for any other reason (a missing path proves
#                          nothing about the boundary)
#   INFO   EXPECT -        GOT recorded; names and paths only, never values
# Run on the host, outside a sandbox, every DENY row reads `unexpected`: that is
# the probe working, not a boundary. A DENY write that succeeds is removed again.
#
# Exit codes: 0 when every ALLOW and DENY row met its expectation; 1 when one
# did not; 3 when the probe stopped early (not in a git worktree, or OUTDIR
# cannot be created) and probed nothing.
# shellcheck disable=SC2016  # `sh -c` scripts are single-quoted on purpose: the child shell expands them
set -u

# The variable names the toolkit keeps out of every seat (SEAT_ENV_DROPPED in
# scripts/pm_lib/sessions.py; a test keeps this list equal to it).
DROPPED="PM_RUN_TOKEN CLAUDECODE CLAUDE_PID AI_AGENT CLAUDE_EFFORT CLAUDE_CODE_ENTRYPOINT CLAUDE_CODE_EXECPATH
CLAUDE_CODE_SESSION_ID CLAUDE_CODE_BRIDGE_SESSION_ID CLAUDE_CODE_CHILD_SESSION CLAUDE_CODE_SESSION_ATTENDED
CLAUDE_CODE_MESSAGING_SOCKET CLAUDE_CODE_MESSAGING_TOKEN"
DENIED='Operation not permitted|Permission denied'

OUT=${1:-${TMPDIR:-/tmp}/sandbox-probe}
TAB=$(printf '\t')
CANARY=pm-probe-$$
GITDIR=$(git rev-parse --absolute-git-dir 2>/dev/null) || { echo "probe: not in a git worktree" >&2; exit 3; }
TOP=$(git rev-parse --show-toplevel) || exit 3
COMMON=$(cd "$(git rev-parse --git-common-dir)" && pwd -P) || exit 3
mkdir -p "$OUT" 2>/dev/null || { echo "probe: cannot create $OUT" >&2; exit 3; }
TSV=$OUT/results.tsv
ERR=$OUT/stderr.tmp
printf 'kind\tname\texpect\tgot\tdetail\n' > "$TSV" || exit 3
fails=0

row() { # row KIND NAME COMMAND [ARG...]
  kind=$1; name=$2; shift 2
  ( "$@" ) >/dev/null 2>"$ERR"; rc=$?
  detail="rc=$rc $(tail -n 1 "$ERR" | tr '\t' ' ' | cut -c1-100)"
  case $kind in
    ALLOW) expect=success; if [ "$rc" -eq 0 ]; then got=ok; else got=unexpected; fi ;;
    DENY) expect=denied
      if [ "$rc" -ne 0 ] && grep -q -E "$DENIED" "$ERR"; then got=ok; else got=unexpected; fi ;;
  esac
  [ "$got" = ok ] || fails=$((fails + 1))
  printf '%s\t%s\t%s\t%s\t%s\n' "$kind" "$name" "$expect" "$got" "$detail" >> "$TSV"
}
info() { printf 'INFO\t%s\t-\trecorded\t%s\n' "$1" "$2" >> "$TSV"; }
names() { env | cut -d= -f1 | grep -E "$1" | tr '\n' ' '; }
deny_create() { row DENY "$1" touch "$2"; rm -f "$2" 2>/dev/null; } # removes the file where the write wrongly succeeded
heredoc() { got=$(cat <<EOF
x
EOF
); [ "$got" = x ]; }
tmux_env_clean() { # no DROPPED name in the tmux server's global environment, read through this seat's $TMUX
  [ -n "${TMUX:-}" ] || { echo 'no $TMUX: not inside a toolkit seat' >&2; return 2; }
  global=$(tmux show-environment -g) || return 2
  present=$(printf '%s\n' "$global" | cut -d= -f1)
  for n in $DROPPED; do
    if printf '%s\n' "$present" | grep -qx "$n"; then echo "tmux global environment holds $n" >&2; return 1; fi
  done
}

# Environment.
row ALLOW "TMPDIR is the run's temp dir" sh -c 'case ${TMPDIR:-} in */pm-sandbox-*) [ -d "$TMPDIR" ] ;; *) exit 1 ;; esac'
row ALLOW "TMPDIR is writable" sh -c 'f=$TMPDIR/$1 && : > "$f" && rm -f "$f"' sh "$CANARY"
row ALLOW "mktemp with a template works" sh -c 'f=$(mktemp "$TMPDIR/probe.XXXXXX") && rm -f "$f"'
row ALLOW "a here-document works" heredoc
row ALLOW "PM_RUN_TOKEN is absent from this seat" test -z "${PM_RUN_TOKEN:-}"
row ALLOW "no dropped variable in the tmux server's global environment" tmux_env_clean
# shellcheck disable=SC2086 # DROPPED is split on purpose, so its newlines collapse into the alternation
info "dropped names present in this seat (a seat's own harness may set some)" "$(names "^($(printf '%s\n' $DROPPED | paste -sd '|' -))\$")"
info "harness variable names" "$(names '^(CLAUDE|CODEX|OPENCODE|COPILOT|ANTHROPIC|OPENAI)')"
info "proxy variable names" "$(names '(^|_)(PROXY|proxy)(_|$)|^GIT_SSH_COMMAND$|^SANDBOX_RUNTIME$')"
info "secret-like variable names" "$(names 'TOKEN|SECRET|KEY|PASSWORD')"

# Writes: the worktree is the seat's; the git metadata the host trusts, the tmux socket's directory, the shared
# Claude temp directory and the toolkit's control directory are not. Appending nothing (`: >>`) never changes a file.
row ALLOW "the worktree is writable" sh -c 'f=$1/$2 && : > "$f" && rm -f "$f"' sh "$TOP" "$CANARY"
row DENY "the git dir's config is not writable" sh -c ': >> "$1"' sh "$COMMON/config"
deny_create "the git dir's hooks are not writable" "$COMMON/hooks/$CANARY"
if [ "$GITDIR" != "$COMMON" ]; then
  row DENY "a branch other than the run's is not updatable" git update-ref "refs/heads/$CANARY" HEAD
  git update-ref -d "refs/heads/$CANARY" 2>/dev/null
else
  info "a branch other than the run's" "not probed: a plain checkout has no ref grants, only a linked worktree does"
fi
socket=${TMUX:-}; socket=${socket%%,*}
deny_create "the tmux socket directory is not writable" "$(dirname "${socket:-/tmp/tmux-$(id -u)/default}")/$CANARY"
deny_create "/tmp/claude is not writable" "/tmp/claude/$CANARY"
deny_create "the control directory is not writable" "$COMMON/pm-sandbox/$(basename "${socket:-socket}")/$CANARY"

rm -f "$ERR"
column -t -s "$TAB" "$TSV" 2>/dev/null || cat "$TSV"
echo "unexpected rows: $fails"
[ "$fails" -eq 0 ]
