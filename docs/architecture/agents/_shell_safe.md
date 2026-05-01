# _shell_safe

**Purpose**: Surgical subprocess helpers for the agents layer — provides a shell-free `run_exec` for trusted call sites and a cheap deny-list guard for the LLM-callable `/bash` tool. Part of the "Fix-A S3" remediation against CWE-78 shell injection.

**Key types / public surface**:
- `run_exec(argv, *, timeout, cwd, env)` — async argv-based runner returning `(rc, stdout, stderr)`.
- `assert_no_obvious_injection(command)` — regex deny-list check for raw shell strings.
- `ShellInjectionBlocked` — `ValueError` subclass raised by the guard.
- `_DENY_PATTERNS` — module-private tuple of compiled regexes (rm -rf /, curl|sh, fork bomb, mkfs, dd-to-/dev/sd*, shutdown/reboot).

**Key invariants**:
- `run_exec` rejects non-`list[str]` argv with `TypeError` — the explicit signature is a tripwire against accidentally passing a shell string.
- On timeout, the process is killed and `asyncio.TimeoutError` is re-raised; callers are expected to convert it to their own `[TIMEOUT]` sentinel rather than this module doing it.
- `assert_no_obvious_injection` is explicitly *not* a sandbox or comprehensive filter — only a "speed bump" for prompt-injection catastrophes. Real isolation is deferred to Phase 64's sandbox runtime.
- `returncode or 0` masks `None` returncodes as 0 — slightly surprising; a process that somehow exits without a code reads as success.

**Cross-module touchpoints**:
- Designed for the ~4 host-exec call sites in `agents/tools.py`; `run_bash` there still uses `create_subprocess_shell` and is the intended caller of `assert_no_obvious_injection`.
- No imports from other backend modules — pure stdlib (`asyncio`, `re`, `pathlib`).
