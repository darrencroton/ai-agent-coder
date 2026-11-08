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
# Exit status: 0 when every file was captured (whatever the measured statuses
# are); 2 when the runner itself could not run or capture (a pin mismatch, an
# existing output path, a log or status file it cannot write, a HEAD other
# than the measured head, a worktree not clean at start or after, a HEAD that
# moved during the run), in which case the measurement is unusable and the
# node stops. It refuses to run twice, because a correction that would
# re-measure is a new node.
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
  t=$(date -u +%Y-%m-%dT%H:%M:%SZ) || die "date failed"
  nonempty date "$t"
}
ctx() { printf '%s\n' "$1" >> "$OUT/context.txt" || die "cannot write $OUT/context.txt"; }
# put FILE TEXT: write one status or note file under $OUT.
put() { printf '%s\n' "$2" > "$OUT/$1" || die "cannot write $OUT/$1"; }
# measure LOG STATUS CMD...: run CMD with its output in $OUT/LOG and its exit
# status in $OUT/STATUS, with a start and finish timestamp in context.txt. A
# log that cannot be created stops the runner before CMD runs, so a capture
# failure is never recorded as a measurement.
measure() {
  log=$1
  status_file=$2
  shift 2
  : > "$OUT/$log" || die "cannot create $OUT/$log"
  now
  ctx "${log%.log}_started_utc=$t"
  "$@" > "$OUT/$log" 2>&1
  put "$status_file" "$?"
  [ -w "$OUT/$log" ] || die "$OUT/$log is no longer writable after the step"
  now
  ctx "${log%.log}_finished_utc=$t"
}
# shellcheck disable=SC2329 # called by the pin lines the Campaign Manager adds below
pin() {
  got=$(shasum -a 256 "$1" | cut -d' ' -f1) || die "cannot hash $1"
  [ "$got" = "$2" ] || die "$1 has SHA-256 $got, not its pin $2"
}

case $OUT$EXPECTED_HEAD in
  *REPLACE*) die "replace the REPLACE_ values at the top of the runner" ;;
esac
if [ -e "$OUT" ] || [ -L "$OUT" ]; then
  die "$OUT already exists; a second measurement is a new node"
fi
# --- pins ---
# One pin per script beside the runner, from shasum -a 256 of the committed
# file, e.g.:  pin "$NODE_DIR/measure.py" 0123...cdef
# --- end pins ---
mkdir -p "$(dirname "$OUT")" || die "cannot create $(dirname "$OUT")"
# The exclusive mkdir reserves the one run: a concurrent second invocation
# fails here, before any measured command.
mkdir "$OUT" || die "cannot reserve $OUT; a second measurement is a new node"

# Variables that change what make, git or Python do are recorded, then
# removed, so no measurement can be suppressed or redirected by an inherited
# setting (MAKEFLAGS=n would skip a recipe; GIT_DIR would point a build's
# recorded provenance at another repository).
for v in MAKEFLAGS MFLAGS GNUMAKEFLAGS MAKEFILES GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE \
  GIT_COMMON_DIR VIRTUAL_ENV PYTHONPATH; do
  eval "val=\${$v-}"
  eval "isset=\${$v+set}"
  ctx "inherited_$v=${isset:+${val:-<empty>}}"
  unset "$v"
done
status=$(git status --porcelain) || die "git status failed"
head=$(git rev-parse HEAD) || die "git rev-parse failed"
nonempty "git rev-parse" "$head"
# The measurement is valid only at the measured head, from a clean worktree.
[ "$head" = "$EXPECTED_HEAD" ] || die "HEAD is $head, not the measured head $EXPECTED_HEAD"
[ -z "$status" ] || die "the worktree is not clean at start: $status"
branch=$(git branch --show-current) || die "git branch failed"
nonempty "git branch" "$branch"
runner_sha=$(shasum -a 256 "$NODE_DIR/run.sh") || die "cannot hash run.sh"
nonempty shasum "$runner_sha"
now
ctx "measured_sha=$head"
ctx "branch=$branch"
ctx "worktree_clean_at_start=$([ -z "$status" ] && echo yes || echo no)"
ctx "runner_sha256=${runner_sha%% *}"
ctx "started_utc=$t"

# --- measurements ---
# Replace the two example steps. Each is `measure NAME.log NAME.status CMD...`;
# a step that depends on an earlier one reads its status from $OUT and records
# the skip with `put`, never leaving the pair of files out.
echo "run.sh: example 1, the measured commit"
measure commit.log commit.status git log -1 --format=%H
echo "run.sh: example 2, a build or test command"
measure example.log example.status sh -c 'echo "replace me with the measured command"'
# --- end measurements ---

now
ctx "finished_utc=$t"
head_after=$(git rev-parse HEAD) || die "git rev-parse failed"
nonempty "git rev-parse" "$head_after"
ctx "head_after=$head_after"
status=$(git status --porcelain) || die "git status failed"
ctx "worktree_clean_after=$([ -z "$status" ] && echo yes || echo no)"
# A HEAD that moved or a worktree left dirty invalidates every measurement:
# the runner stops before writing the manifest.
[ "$head_after" = "$head" ] || die "HEAD moved during the run: $head -> $head_after"
[ -z "$status" ] || die "the worktree is not clean after the run: $status"

# The manifest lists every file in $OUT but itself, and appears atomically.
files=$(cd "$OUT" && find . -type f ! -path ./manifest.sha256 ! -path ./manifest.tmp | sed 's|^\./||' | sort) || die "cannot list $OUT"
nonempty find "$files"
# shellcheck disable=SC2086 # files is a list of runner-written plain names, split on purpose
(cd "$OUT" && shasum -a 256 $files > manifest.tmp) || die "cannot write the manifest"
count=$(wc -l < "$OUT/manifest.tmp" | tr -d ' ') || die "cannot count the manifest"
mv "$OUT/manifest.tmp" "$OUT/manifest.sha256" || die "cannot write manifest.sha256"
echo "run.sh: captured $count files, listed in $OUT/manifest.sha256"
exit 0
