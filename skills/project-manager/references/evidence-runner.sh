#!/bin/sh
# A capture-only evidence runner: the skeleton an evidence node's measurements
# hang on. Copy it into the dossier as nodes/<node>/run.sh, replace the two
# REPLACE_ values and the example steps under "--- measurements ---", pin the
# result by SHA-256 in the node plan, and leave everything else alone. It is a
# starting point the Campaign Manager adapts, not a runner it must use.
#
# Usage, once, from the node worktree root, before any commit on the node
# branch:   sh <node dir>/run.sh
#
# It checks the SHA-256 pins of the scripts beside it, records the inherited
# environment that could change what make, git or Python do and then removes
# it, records HEAD and requires a clean worktree at the start, runs each
# measurement with its full output and exit status kept under $OUT (which must
# be git-ignored, or the clean-tree check after the run refuses), and ends
# with a SHA-256 manifest of every file in $OUT but itself. It judges nothing:
# each outcome is the measured command's own exit status, which the report
# states.
#
# Provided here, so a runner adds none of them itself:
#   sparse_clone DEST PATTERN...  clone this repository at the measured head to $OUT/DEST, limited to the patterns (the brief's exclusions read '/*' '!/docs/dev').
#     Inside a sandbox the clone needs filesystem.allowGitConfig in the base policy, since srt denies .git/config writes beneath the worktree.
#   require FILE [EXPECTED]  stop unless $OUT/FILE is a non-empty file (with its trailing newline stripped equal to EXPECTED, if given), e.g. require build.status 0.
#   declared files  measure declares its log and status file, expect FILE... declares the outputs a stage must write; before the manifest each must exist and
#     each but a measure log must be non-empty (a silent command's log is legitimately empty). declared.txt is a runner-written file and is in the manifest.
#   heredoc exception  the clean-tree checks ignore untracked sh-thd-* files, the transient heredoc files a sandboxed shell leaves in the worktree.
#   names  POSIX sh has no local variables, so every variable the runner itself sets, in its helpers and in its own top-level code, starts with _ev_; a
#     step's own names (a loop over f, a t or a got) are never overwritten. OUT, NODE_DIR and EXPECTED_HEAD are the interface and keep their names.
#
# Exit status: 0 when every file was captured (whatever the measured statuses
# are); 2 when the runner itself could not run or capture (a pin mismatch, an
# existing output path, a log or status file it cannot write, a HEAD other
# than the measured head, a worktree not clean at start or after, a HEAD that
# moved during the run, a failed clone, an unmet require, a declared file that
# is missing or empty), in which case the measurement is unusable and the
# node stops. It refuses to run twice, because a correction that would
# re-measure is a new node. Once it has reserved $OUT it also writes its own
# exit status to $OUT.exit, beside $OUT and so outside the manifest (git-ignore
# it like $OUT), so the status is a record and not an inference from how it was
# launched; a run that did not reach its end (a signal) records 2, or nothing
# under a shell that runs no EXIT trap on a signal (dash), and a record
# it cannot write is a capture failure (exit 2). A refusal before the
# reservation (a pin mismatch, an existing $OUT or $OUT.exit) writes none and
# never touches an earlier run's record.
set -u
NODE_DIR=$(cd "$(dirname "$0")" && pwd -P) || exit 2
OUT=output/REPLACE_NODE_ID
EXPECTED_HEAD=REPLACE_WITH_THE_MEASURED_PRODUCT_SHA

die() {
  echo "run.sh: $*" >&2
  exit 2
}
# Every provenance value is checked: a command that fails or prints nothing
# stops the runner rather than writing a blank into context.txt.
nonempty() { [ -n "$2" ] || die "$1 printed nothing"; }
now() {
  _ev_t=$(date -u +%Y-%m-%dT%H:%M:%SZ) || die "date failed"
  nonempty date "$_ev_t"
}
ctx() { printf '%s\n' "$1" >> "$OUT/context.txt" || die "cannot write $OUT/context.txt"; }
# put FILE TEXT: write one status or note file under $OUT.
put() { printf '%s\n' "$2" > "$OUT/$1" || die "cannot write $OUT/$1"; }
# expect FILE...: declare files a stage must write; each must be a non-empty
# file under $OUT before the manifest is written. measure declares its own
# log through declare_log, since a silent command's log is legitimately
# empty; declared.txt records which rule each name is under.
expect() {
  for _ev_f in "$@"; do
    printf 'file %s\n' "$_ev_f" >> "$OUT/declared.txt" || die "cannot write $OUT/declared.txt"
  done
}
declare_log() { printf 'log %s\n' "$1" >> "$OUT/declared.txt" || die "cannot write $OUT/declared.txt"; }
# require FILE [EXPECTED]: stop unless $OUT/FILE is a non-empty regular file
# and, when EXPECTED is given, its content less the trailing newline is
# EXPECTED. For a stage whose precondition is an earlier stage's result, as
# `require build.status 0`, where recording a skip with put would not stop a
# next stage that cannot run at all.
# shellcheck disable=SC2329 # called by the steps the Campaign Manager adds below
require() {
  if [ ! -f "$OUT/$1" ] || [ ! -s "$OUT/$1" ]; then
    die "required $OUT/$1 is missing or empty"
  fi
  [ "$#" -lt 2 ] && return 0
  _ev_got=$(cat "$OUT/$1") || die "cannot read $OUT/$1"
  [ "$_ev_got" = "$2" ] || die "required $OUT/$1 holds '$_ev_got', not '$2'"
}
# sparse_clone DEST PATTERN...: clone this worktree's repository at
# $EXPECTED_HEAD to $OUT/DEST, for a measurement that needs a pristine
# checkout of the measured commit; with patterns, only those are checked out.
# shellcheck disable=SC2329 # called by the steps the Campaign Manager adds below
sparse_clone() {
  _ev_dest=$1
  shift
  # An empty template: the default one copies hook samples into the clone's
  # .git/hooks, which a sandbox denies, and the clone needs no hooks.
  git clone -q --no-checkout --template= -- "$PWD" "$OUT/$_ev_dest" || die "cannot clone into $OUT/$_ev_dest"
  if [ "$#" -gt 0 ]; then
    git -C "$OUT/$_ev_dest" sparse-checkout set --no-cone -- "$@" || die "cannot set the sparse patterns in $OUT/$_ev_dest"
  fi
  git -C "$OUT/$_ev_dest" checkout -q "$EXPECTED_HEAD" || die "cannot check out $EXPECTED_HEAD in $OUT/$_ev_dest"
  _ev_clone_head=$(git -C "$OUT/$_ev_dest" rev-parse HEAD) || die "git rev-parse failed in $OUT/$_ev_dest"
  [ "$_ev_clone_head" = "$EXPECTED_HEAD" ] || die "the clone's HEAD is $_ev_clone_head, not $EXPECTED_HEAD"
}
# tree_status: set `_ev_status` to the worktree's porcelain status, less the
# transient sh-thd-* heredoc files a sandboxed shell leaves while a heredoc
# runs. grep exits 1 when it filters every line, which is not a failure; a
# git status failure is.
tree_status() {
  _ev_raw=$(git status --porcelain) || die "git status failed"
  _ev_status=$(printf '%s\n' "$_ev_raw" | grep -Ev '^\?\? (.*/)?sh-thd-[^/]*$') || [ "$?" -eq 1 ] || die "cannot filter the git status"
}
# measure LOG STATUS CMD...: run CMD with its output in $OUT/LOG and its exit
# status in $OUT/STATUS, with a start and finish timestamp in context.txt. A
# log that cannot be created stops the runner before CMD runs, so a capture
# failure is never recorded as a measurement. Both files are declared, so a
# missing one stops the runner before the manifest.
measure() {
  _ev_log=$1
  _ev_status_file=$2
  shift 2
  declare_log "$_ev_log"
  expect "$_ev_status_file"
  : > "$OUT/$_ev_log" || die "cannot create $OUT/$_ev_log"
  now
  ctx "${_ev_log%.log}_started_utc=$_ev_t"
  "$@" > "$OUT/$_ev_log" 2>&1
  put "$_ev_status_file" "$?"
  [ -w "$OUT/$_ev_log" ] || die "$OUT/$_ev_log is no longer writable after the step"
  now
  ctx "${_ev_log%.log}_finished_utc=$_ev_t"
}
# shellcheck disable=SC2329 # called by the pin lines the Campaign Manager adds below
pin() {
  _ev_got=$(shasum -a 256 "$1" | cut -d' ' -f1) || die "cannot hash $1"
  [ "$_ev_got" = "$2" ] || die "$1 has SHA-256 $_ev_got, not its pin $2"
}

case $OUT$EXPECTED_HEAD in
  *REPLACE*) die "replace the REPLACE_ values at the top of the runner" ;;
esac
if [ -e "$OUT" ] || [ -L "$OUT" ]; then
  die "$OUT already exists; a second measurement is a new node"
fi
if [ -e "$OUT.exit" ] || [ -L "$OUT.exit" ]; then
  die "$OUT.exit already exists; a second measurement is a new node"
fi
# --- pins ---
# One pin per script beside the runner, from shasum -a 256 of the committed
# file, e.g.:  pin "$NODE_DIR/measure.py" 0123...cdef
# --- end pins ---
mkdir -p "$(dirname "$OUT")" || die "cannot create $(dirname "$OUT")"
# The exclusive mkdir reserves the one run: a concurrent second invocation
# fails here, before any measured command.
mkdir "$OUT" || die "cannot reserve $OUT; a second measurement is a new node"
# Installed only now, so a refused invocation cannot overwrite the record of
# the run that holds the reservation. $? is the status the shell is exiting
# with; under some shells (macOS /bin/sh) a signal leaves it at the last
# completed command's, so a 0 before _ev_done is set is recorded as 2, while
# a shell that runs no EXIT trap on a signal (dash) writes no record at all.
# The record appears atomically, and a runner that cannot write it exits 2.
_ev_done=0
_ev_rc=0
trap '_ev_rc=$?; [ "$_ev_rc" -ne 0 ] || [ "$_ev_done" -eq 1 ] || _ev_rc=2
  printf "%s\n" "$_ev_rc" > "$OUT.exit.tmp" && mv "$OUT.exit.tmp" "$OUT.exit" || exit 2' EXIT
: > "$OUT/declared.txt" || die "cannot create $OUT/declared.txt"

# Variables that change what make, git or Python do are recorded, then
# removed, so no measurement can be suppressed or redirected by an inherited
# setting (MAKEFLAGS=n would skip a recipe; GIT_DIR would point a build's
# recorded provenance at another repository).
for _ev_v in MAKEFLAGS MFLAGS GNUMAKEFLAGS MAKEFILES GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE \
  GIT_COMMON_DIR VIRTUAL_ENV PYTHONPATH; do
  eval "_ev_val=\${$_ev_v-}"
  eval "_ev_isset=\${$_ev_v+set}"
  ctx "inherited_$_ev_v=${_ev_isset:+${_ev_val:-<empty>}}"
  unset "$_ev_v"
done
tree_status
_ev_head=$(git rev-parse HEAD) || die "git rev-parse failed"
nonempty "git rev-parse" "$_ev_head"
# The measurement is valid only at the measured head, from a clean worktree.
[ "$_ev_head" = "$EXPECTED_HEAD" ] || die "HEAD is $_ev_head, not the measured head $EXPECTED_HEAD"
[ -z "$_ev_status" ] || die "the worktree is not clean at start: $_ev_status"
_ev_branch=$(git branch --show-current) || die "git branch failed"
nonempty "git branch" "$_ev_branch"
_ev_runner_sha=$(shasum -a 256 "$NODE_DIR/run.sh") || die "cannot hash run.sh"
nonempty shasum "$_ev_runner_sha"
now
ctx "measured_sha=$_ev_head"
ctx "branch=$_ev_branch"
ctx "worktree_clean_at_start=$([ -z "$_ev_status" ] && echo yes || echo no)"
ctx "runner_sha256=${_ev_runner_sha%% *}"
ctx "started_utc=$_ev_t"

# --- measurements ---
# Replace the two example steps. Each is `measure NAME.log NAME.status CMD...`;
# a step that depends on an earlier one reads its status from $OUT and either
# records the skip with `put`, never leaving the pair of files out, or, when
# it cannot run at all, stops with `require`.
echo "run.sh: example 1, the measured commit"
measure commit.log commit.status git log -1 --format=%H
echo "run.sh: example 2, a build or test command"
measure example.log example.status sh -c 'echo "replace me with the measured command"'
# --- end measurements ---

now
ctx "finished_utc=$_ev_t"
_ev_head_after=$(git rev-parse HEAD) || die "git rev-parse failed"
nonempty "git rev-parse" "$_ev_head_after"
ctx "head_after=$_ev_head_after"
tree_status
ctx "worktree_clean_after=$([ -z "$_ev_status" ] && echo yes || echo no)"
# A HEAD that moved or a worktree left dirty invalidates every measurement:
# the runner stops before writing the manifest.
[ "$_ev_head_after" = "$_ev_head" ] || die "HEAD moved during the run: $_ev_head -> $_ev_head_after"
[ -z "$_ev_status" ] || die "the worktree is not clean after the run: $_ev_status"
# Every declared file must exist, and every expected file must be non-empty:
# a status file always holds the exit status and an expected output must
# hold the output, while a measure log is empty when the command printed
# nothing (cmp or diff -q on identical outputs), which is a measurement,
# not a capture failure.
while read -r _ev_kind _ev_f; do
  [ -f "$OUT/$_ev_f" ] || die "stage wrote nothing: $OUT/$_ev_f is missing"
  [ "$_ev_kind" = log ] || [ -s "$OUT/$_ev_f" ] || die "stage wrote nothing: $OUT/$_ev_f is empty"
done < "$OUT/declared.txt"

# The manifest lists every file in $OUT but itself and a clone's git
# metadata (a sparse_clone's .git is machinery, not capture), and appears
# atomically.
_ev_files=$(cd "$OUT" && find . -type f ! -path ./manifest.sha256 ! -path ./manifest.tmp ! -path '*/.git/*' | sed 's|^\./||' | sort) || die "cannot list $OUT"
nonempty find "$_ev_files"
# The names go through xargs -0, never one command line: a capture of a few
# thousand files exceeds ARG_MAX, and a manifest that fails then would lose
# the whole measurement. NUL-separated, so a quote, backslash or space in a
# copied product file's name is not parsed. xargs exits non-zero if any batch fails.
(cd "$OUT" && printf '%s\n' "$_ev_files" | tr '\n' '\0' | xargs -0 shasum -a 256 > manifest.tmp) || die "cannot write the manifest"
_ev_count=$(wc -l < "$OUT/manifest.tmp" | tr -d ' ') || die "cannot count the manifest"
mv "$OUT/manifest.tmp" "$OUT/manifest.sha256" || die "cannot write manifest.sha256"
echo "run.sh: captured $_ev_count files, listed in $OUT/manifest.sha256"
_ev_done=1
exit 0
