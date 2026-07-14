from __future__ import annotations

import subprocess

from .models import CommandResult, PmError


def run_command(command: list[str], *, error_prefix: str = "command failed", allow_failure: bool = False) -> CommandResult:
    result = subprocess.run(command, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    command_result = CommandResult(result.returncode, result.stdout, result.stderr)
    if result.returncode != 0 and not allow_failure:
        message = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise PmError(f"{error_prefix}: {message}")
    return command_result
