"""Fix-A S3' — exec-based subprocess helpers.

`create_subprocess_shell` + f-string is the classic CWE-78 vector. Code
that used to look like:

    proc = await asyncio.create_subprocess_shell(f"git {cmd}", ...)

is migrated to:

    rc, out, err = await run_exec(["git", *shlex.split(cmd)], ...)

We intentionally keep the signature small — this is a surgical helper
for the 4 host-exec call sites in `agents/tools.py`, not a general
subprocess replacement.

`run_bash` (the LLM-callable /bash tool) intentionally still uses
`create_subprocess_shell` because shell semantics are the point. It
relies on `assert_no_obvious_injection` below as a cheap second line
of defence against the worst patterns (`rm -rf /`, `curl | sh`,
`:(){ :|:& };:`, etc.). This is *not* a sandbox — proper sandboxing
is Phase 64's sandbox runtime.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path


async def run_exec(
    argv: list[str],
    *,
    timeout: float,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run a command as argv list (no shell interpretation).

    Returns (returncode, stdout_str, stderr_str). On timeout kills the
    process and re-raises asyncio.TimeoutError so callers can convert
    to their usual [TIMEOUT] sentinel.
    """
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        raise TypeError("argv must be list[str] — did you pass a shell string?")
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        raise
    return (
        proc.returncode or 0,
        stdout.decode(errors="replace"),
        stderr.decode(errors="replace"),
    )


# Obvious-evil patterns that no legitimate tool call should contain.
# Not exhaustive — defence in depth, not the only defence.
_DENY_PATTERNS = (
    re.compile(r"\brm\s+-rf?\s+/\s*(?:$|\s)"),        # rm -rf /
    re.compile(r"\bcurl\b[^|]*\|\s*(?:sh|bash)\b"),   # curl … | sh
    re.compile(r"\bwget\b[^|]*\|\s*(?:sh|bash)\b"),
    re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),  # fork bomb
    re.compile(r"\bmkfs\.\w+\s+/dev/"),               # format a disk
    re.compile(r">\s*/dev/sd[a-z]"),                  # dd-like overwrite
    re.compile(r"\bshutdown\b|\breboot\b\s*$"),
)


class ShellInjectionBlocked(ValueError):
    """Raised when `assert_no_obvious_injection` refuses a command."""


def assert_no_obvious_injection(command: str) -> None:
    """Cheap guard for /bash tool. Blocks the worst offenders.

    Does NOT protect against sophisticated injection — this is just a
    speed bump for prompt-injection triggered catastrophes. Real
    isolation comes from running inside a container (preferred) or the
    Phase 64 sandbox.
    """
    for pat in _DENY_PATTERNS:
        if pat.search(command):
            raise ShellInjectionBlocked(
                f"command matches deny-list pattern: {pat.pattern!r}"
            )
