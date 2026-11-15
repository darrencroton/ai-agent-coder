"""Launch a long command detached from the calling harness, with default signals.

Usage, from the node worktree root:
    python3 <node dir>/detached-launch.py OUT EXIT PID COMMAND...

Runs ``sh -c '"$@" > OUT 2>&1; echo "$?" > EXIT.tmp && mv EXIT.tmp EXIT'`` with COMMAND as its arguments,
in a new session (so a harness that stops its own command's process group
cannot reach it), with standard input from /dev/null and SIGHUP, SIGINT,
SIGQUIT, SIGTERM and SIGPIPE at their default dispositions (nohup and a
shell's background list leave some of them ignored, and a program that
honours an inherited ignore then behaves differently). Writes the wrapper's
process id to PID and returns at once; it is also the run's process group,
so ``kill -- -"$(cat PID)"`` stops the whole run. EXIT appears only when
COMMAND has finished, already holding its exit status; ``kill -0 "$(cat PID)"``
succeeds while it runs. Refuses (exit 2), starting nothing, when OUT, EXIT or
PID already exists or PID cannot be created.
"""

import os
import signal
import subprocess
import sys

RESET = (signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGTERM, signal.SIGPIPE)
WRAPPER = '"$@" > "$LAUNCH_OUT" 2>&1; echo "$?" > "$LAUNCH_EXIT.tmp" && mv "$LAUNCH_EXIT.tmp" "$LAUNCH_EXIT"'


def _reset_signals() -> None:
    for number in RESET:
        signal.signal(number, signal.SIG_DFL)


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        print("usage: detached-launch.py OUT EXIT PID COMMAND...", file=sys.stderr)
        return 2
    out, exit_file, pid_file = argv[:3]
    for path in (out, exit_file, pid_file):
        if os.path.lexists(path):
            print(f"detached-launch.py: {path} already exists", file=sys.stderr)
            return 2
    env = dict(os.environ, LAUNCH_OUT=out, LAUNCH_EXIT=exit_file)
    try:
        handle = open(pid_file, "x", encoding="ascii")
    except OSError as error:
        print(f"detached-launch.py: cannot create {pid_file}: {error}", file=sys.stderr)
        return 2
    with handle:
        process = subprocess.Popen(
            ["sh", "-c", WRAPPER, "launch", *argv[3:]],
            env=env,
            start_new_session=True,
            preexec_fn=_reset_signals,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        handle.write(f"{process.pid}\n")
    print(f"detached-launch.py: started process {process.pid}; exit status will be written to {exit_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
