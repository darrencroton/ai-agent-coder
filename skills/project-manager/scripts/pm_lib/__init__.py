"""Mode B Lite PM toolkit.

Pure-stdlib package. Nothing in ``pm_lib`` or its submodules may import from
outside the standard library or from ``pm_lib`` itself. In particular, no
module here may import from ``skills/orchestrator/``.
"""

from __future__ import annotations


class PmError(Exception):
    """User-facing PM error: a condition the operator or PM agent must resolve."""


class IntegrityError(PmError):
    """A tamper/verification failure.

    Raised when authenticated state fails verification (a hand-edited
    ``run.json``, a missing MAC file, or similar). Callers must treat this as
    a terminal integrity stop, never as a retryable or steerable condition.
    """
