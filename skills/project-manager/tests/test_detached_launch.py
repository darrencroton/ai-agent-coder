"""Protected behaviours: the detached launcher reference script.

`references/detached-launch.py` starts an evidence run that outlives a
harness's tool timeout, so it is tested the way it is used: run as a
subprocess in a temporary directory. It must return at once with a pid file,
write the command's exit status only when the command has finished, start the
command with SIGINT and SIGHUP at their default dispositions even when the
caller ignores them, and refuse to overwrite an existing OUT, EXIT or PID file.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

LAUNCHER = Path(__file__).resolve().parents[1] / "references" / "detached-launch.py"
PROBE = (
    "import signal, sys\n"
    "print('SIGINT ignored', signal.getsignal(signal.SIGINT) is signal.SIG_IGN)\n"
    "print('SIGHUP ignored', signal.getsignal(signal.SIGHUP) is signal.SIG_IGN)\n"
    "sys.exit(3)\n"
)


def _ignore_signals() -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)


class TestDetachedLaunch(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.out = self.dir / "out.log"
        self.exit = self.dir / "exit.status"
        self.pid = self.dir / "run.pid"

    def launch(self, *command: str, preexec_fn=None) -> subprocess.CompletedProcess:
        argv = [sys.executable, str(LAUNCHER), str(self.out), str(self.exit), str(self.pid), *command]
        return subprocess.run(
            argv, cwd=self.dir, text=True, capture_output=True, check=False, timeout=30, preexec_fn=preexec_fn
        )

    def test_command_runs_detached_with_default_signals_and_records_its_exit_status(self) -> None:
        result = self.launch(sys.executable, "-c", PROBE, preexec_fn=_ignore_signals)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.pid.read_text(encoding="ascii").strip().isdigit())
        deadline = time.monotonic() + 20
        status = ""
        while not status and time.monotonic() < deadline:
            time.sleep(0.05)
            status = self.exit.read_text(encoding="ascii").strip() if self.exit.exists() else ""
        self.assertEqual(status, "3", "EXIT did not hold the command's status within the bound")
        # Python installs its own handler for SIGINT at the default disposition, so the
        # probe asserts "not ignored", the property an inherited ignore would break.
        self.assertEqual(self.out.read_text(encoding="utf-8"), "SIGINT ignored False\nSIGHUP ignored False\n")

    def test_refuses_and_starts_nothing_when_a_file_already_exists(self) -> None:
        self.out.write_text("earlier run\n", encoding="utf-8")
        result = self.launch(sys.executable, "-c", PROBE)
        self.assertEqual(result.returncode, 2)
        self.assertIn("already exists", result.stderr)
        self.assertEqual(self.out.read_text(encoding="utf-8"), "earlier run\n")
        self.assertFalse(self.exit.exists())
        self.assertFalse(self.pid.exists())


if __name__ == "__main__":
    unittest.main()
