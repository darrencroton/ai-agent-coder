"""Project Manager library.

Implementation split by responsibility across sibling modules (`cli`,
`commands`, `plan`, `state`, `gates`, `git_ops`, `runner`, `runtime`,
`hints`, `prompts`, `context`, `tmux_adapter`, `observation`, `profiles`,
`constants`, `models`, `utils`, `process`). This file intentionally carries no re-exports: `scripts/pm.py`
imports `main` directly from `pm_lib.cli`, and consumers (including the test
suite) import the owning submodule directly (e.g. `pm_lib.state`,
`pm_lib.gates`). Do not reintroduce a facade re-export block here — adding a
helper to a submodule must never require an edit to this file.
"""

from __future__ import annotations
