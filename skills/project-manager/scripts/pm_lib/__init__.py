"""Mode B Lite PM toolkit.

Pure-stdlib package. Nothing in ``pm_lib`` or its submodules may import from
outside the standard library or from ``pm_lib`` itself. In particular, no
module here may import from ``skills/orchestrator/``.

The one deliberate exception is cross-skill, not a package import:
``code_metrics`` loads the sibling ``code-health`` skill's ``health.py`` at
runtime with ``importlib``, from a path relative to this file. It is path-based
because ``skills/`` is not an importable package and ``pm_lib`` must keep
importing nothing outside the standard library; a missing or unloadable
``health.py`` degrades to an ``{"error": ...}`` code block, never a failure.
"""

from __future__ import annotations


class PmError(Exception):
    """User-facing PM error: a condition the operator or PM agent must resolve."""


class TypedNotSubmitted(PmError):
    """A send typed its text into the pane and then refused to press Enter.

    Distinct from a plain `PmError` because the caller's cleanup differs: the
    text is sitting unsubmitted in the Developer's input line, so an artifact
    that text points at must be preserved, not deleted as an undelivered
    attempt would be.
    """


class IntegrityError(PmError):
    """A tamper/verification failure.

    Raised when authenticated state fails verification (a hand-edited
    ``run.json``, a missing MAC file, or similar). Callers must treat this as
    a terminal integrity stop, never as a retryable or steerable condition.
    """
