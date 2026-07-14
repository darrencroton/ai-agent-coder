#!/usr/bin/env python3
"""Project Manager CLI entrypoint.

The implementation lives in pm_lib so the executable path remains stable while
core concerns stay split into focused modules.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from pm_lib import *  # noqa: F401,F403
from pm_lib.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
