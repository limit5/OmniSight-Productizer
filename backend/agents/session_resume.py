"""B9 — Anthropic harness session-resume 3-step opener (OP-1122).

The session-resume module composes :mod:`progress_log` with the
runner-health probes from :mod:`runner_health_checks` and the bridge
heartbeat helper from :mod:`gerrit_jira_bridge` into the
:func:`open_session` entry point that the Anthropic SDK launcher
(``scripts/run_s1_via_anthropic_sdk.py``) calls before every ticket.

The 3-step opener (AC #4 of OP-1122):

1. **CWD verification** — refuse to start if ``os.getcwd()`` does not
   match the supplied ``worktree_path``. Mitigates F17 ("agent edits in
   wrong checkout") and F24 ("two worktrees, runner picked the wrong
   one"). Raises :class:`CwdNotWorktreeError` → ``cwd_not_worktree``.

2. **Read progress.txt** — if absent, ``phase = "fresh"`` and the new
   log is started with ``reset=True``. If present + clean, the parsed
   entries are returned to the caller for context-rebuild and the log
   is opened with ``reset=False`` (which enforces AC #8 owner
   matching). If present + corrupt, the corruption is logged and the
   session falls back to ``fresh`` (AC #7 — "corrupt → treat as
   absent; fresh session; log corruption for operator review").

3. **Smoke test** — import each module listed in
   ``smoke_test_modules`` (caller passes the ticket's
   "Files touched" Python modules) and run ``pytest --collect-only``
   against the supplied test paths. Both probes are best-effort: a
   failure is logged and surfaced via ``SessionOpener.smoke_test`` but
   never aborts (AC #4 step 3 — "Failure → log + warn but proceed").

After the 3-step opener, two cross-cutting freshness probes run:

* **Bridge currency check** (AC #5 / F4/F10 mitigation) — reads the
  ``gerrit_jira_bridge`` heartbeat file. If age > 24h, raise
  :class:`BridgeStaleCriticalError` → ``bridge_stale_critical``. If
  age > 1h, push a warning onto :attr:`SessionOpener.warnings` and
  proceed (``bridge_currency_check_fail``).

* **Stream-events freshness** (AC #6 / F25 mitigation) — reads the
  bridge cursor file (last stream-events timestamp the bridge
  recorded). If age > 5min, push a warning onto
  :attr:`SessionOpener.warnings` and proceed (``stream_events_dead``).

Error catalog → :class:`SessionResumeError` subclasses:

    cwd_not_worktree              -> CwdNotWorktreeError
    progress_txt_corrupt          -> (downgraded to warning, fresh path)
    progress_txt_owner_mismatch   -> ProgressTxtOwnerMismatchError
                                     (re-raised from progress_log)
    smoke_test_fail               -> (downgraded to warning, recorded
                                      on opener.smoke_test)
    bridge_currency_check_fail    -> (warning only)
    bridge_stale_critical         -> BridgeStaleCriticalError
    stream_events_dead            -> (warning only)
    concurrent_runner_conflict    -> ConcurrentRunnerConflictError
                                     (re-raised from progress_log)
"""
from __future__ import annotations

import datetime as _dt
import importlib
import logging
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from backend.agents import progress_log as _plog
from backend.agents.progress_log import (
    ConcurrentRunnerConflictError,
    ProgressEntry,
    ProgressLog,
    progress_file_path,
)
from backend.agents.runner_health_checks import (
    StaleStreamEvents,
    check_stream_events,
)

log = logging.getLogger(__name__)


# ── AC thresholds ──────────────────────────────────────────────────────


# AC #5 — bridge currency
BRIDGE_WARN_AFTER_SECONDS: int = 60 * 60                # 1h
BRIDGE_ABORT_AFTER_SECONDS: int = 24 * 60 * 60          # 24h

# AC #6 — stream-events freshness. The OP-1122 description pins 5min;
# the existing F25 default in ``runner_health_checks`` is 10min, but
# this ticket explicitly overrides for the SDK launcher session-opener.
STREAM_EVENTS_WARN_AFTER_SECONDS: int = 5 * 60


# ── Error catalog ──────────────────────────────────────────────────────


class SessionResumeError(RuntimeError):
    """Base class for fatal session-resume aborts."""


class CwdNotWorktreeError(SessionResumeError):
    """Step 1 of the 3-step opener — CWD does not match the worktree."""

    def __init__(self, observed: Path, expected: Path) -> None:
        super().__init__(
            f"cwd_not_worktree: cwd={observed} != worktree_path={expected}"
        )
        self.observed = observed
        self.expected = expected


class BridgeStaleCriticalError(SessionResumeError):
    """AC #5 — bridge heartbeat older than 24h; abort pickup."""

    def __init__(self, age_seconds: float, heartbeat_path: Path) -> None:
        super().__init__(
            f"bridge_stale_critical: heartbeat age {age_seconds:.0f}s "
            f"> {BRIDGE_ABORT_AFTER_SECONDS}s (path={heartbeat_path})"
        )
        self.age_seconds = age_seconds
        self.heartbeat_path = heartbeat_path


# ── Result types ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class SmokeTestResult:
    """Outcome of Step 3 — import probe + ``pytest --collect-only``."""

    passed: bool
    failed_imports: list[str] = field(default_factory=list)
    pytest_returncode: int | None = None
    pytest_summary: str = ""

    @property
    def is_clean(self) -> bool:
        return self.passed and not self.failed_imports


@dataclass
class SessionOpener:
    """Return value of :func:`open_session`.

    ``phase`` is ``"fresh"`` (no prior progress.txt, or it was corrupt)
    or ``"resume"`` (prior progress.txt parsed cleanly and owner matched).

    ``prior_entries`` is empty for ``fresh`` sessions; on ``resume`` it
    holds the parsed JSONL rows so the caller can rebuild the model's
    prior conversation outline. The session-header row is included.

    ``warnings`` is the ordered list of non-fatal advisories emitted by
    the opener — bridge currency soft-warn, stream-events soft-warn,
    smoke-test soft-warn, progress.txt-corrupt soft-warn. The caller
    surfaces these as JIRA comments / log lines; they never block.
    """

    worktree_path: Path
    owner: str
    phase: str  # "fresh" | "resume"
    progress_log: ProgressLog
    prior_entries: list[ProgressEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    smoke_test: SmokeTestResult | None = None
    bridge_heartbeat_age_seconds: float | None = None
    stream_events_age_seconds: float | None = None

    def warn(self, code: str, detail: str) -> None:
        """Append a structured warning entry."""
        self.warnings.append(f"{code}: {detail}")
        log.warning("session_resume.warn code=%s detail=%s", code, detail)


# ── Step 1 — CWD verification ──────────────────────────────────────────


def _verify_cwd(worktree_path: Path) -> None:
    """AC #4 step 1 — refuse to start if cwd != worktree_path."""
    cwd = Path(os.getcwd()).resolve()
    target = worktree_path.resolve()
    if cwd != target:
        raise CwdNotWorktreeError(observed=cwd, expected=target)


# ── Step 3 — Smoke test ────────────────────────────────────────────────


def _run_smoke_test(
    *,
    worktree_path: Path,
    modules: list[str],
    pytest_paths: list[str] | None,
    pytest_timeout_seconds: int,
) -> SmokeTestResult:
    """Import each module, then collect-only pytest. Best-effort."""
    failed: list[str] = []
    for mod_name in modules:
        mod_name = mod_name.strip()
        if not mod_name:
            continue
        try:
            importlib.import_module(mod_name)
        except Exception as exc:  # noqa: BLE001 — any import problem counts
            failed.append(f"{mod_name}: {type(exc).__name__}: {exc}")

    pytest_rc: int | None = None
    pytest_summary = ""
    if pytest_paths:
        cmd = [sys.executable, "-m", "pytest", "--collect-only", "-q", *pytest_paths]
        try:
            result = subprocess.run(
                cmd,
                cwd=str(worktree_path),
                capture_output=True,
                text=True,
                timeout=pytest_timeout_seconds,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            pytest_rc = -1
            pytest_summary = (
                f"pytest --collect-only invocation failed: "
                f"{type(exc).__name__}: {exc}"
            )
        else:
            pytest_rc = result.returncode
            tail = (result.stdout + result.stderr).splitlines()
            pytest_summary = "\n".join(tail[-10:])

    passed = not failed and (pytest_rc is None or pytest_rc == 0)
    return SmokeTestResult(
        passed=passed,
        failed_imports=failed,
        pytest_returncode=pytest_rc,
        pytest_summary=pytest_summary,
    )


# ── Bridge currency check (AC #5) ──────────────────────────────────────


def _check_bridge_currency(
    opener: SessionOpener,
    *,
    heartbeat_path: Path | None,
    now: float | None,
) -> None:
    """Probe the bridge heartbeat; warn at 1h, abort at 24h."""
    # Import locally so a missing gerrit_jira_bridge (e.g. in a stripped
    # test environment) doesn't poison the entire session_resume module
    # import path.
    from backend.agents.gerrit_jira_bridge import check_bridge_heartbeat

    try:
        is_fresh, age, resolved_path = check_bridge_heartbeat(
            path=heartbeat_path,
            stale_after_seconds=BRIDGE_WARN_AFTER_SECONDS,
            now=now,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        opener.warn(
            "bridge_currency_check_fail",
            f"heartbeat probe raised {type(exc).__name__}: {exc}",
        )
        return

    opener.bridge_heartbeat_age_seconds = age

    if age > BRIDGE_ABORT_AFTER_SECONDS:
        raise BridgeStaleCriticalError(age_seconds=age, heartbeat_path=resolved_path)
    if not is_fresh:
        opener.warn(
            "bridge_currency_check_fail",
            f"heartbeat age {age:.0f}s > {BRIDGE_WARN_AFTER_SECONDS}s "
            f"(path={resolved_path})",
        )


# ── Stream-events freshness (AC #6) ────────────────────────────────────


def _check_stream_events(
    opener: SessionOpener,
    *,
    cursor_path: Path | None,
    now: float | None,
) -> None:
    """Probe the bridge cursor file mtime; warn at 5min."""
    from backend.agents.gerrit_jira_bridge import CURSOR_FILE, load_cursor

    target = cursor_path if cursor_path is not None else CURSOR_FILE

    last_event_ts: float | None = None
    try:
        cursor = load_cursor(target)
    except Exception as exc:  # noqa: BLE001
        opener.warn(
            "stream_events_dead",
            f"cursor read raised {type(exc).__name__}: {exc} (path={target})",
        )
        return

    if cursor is None:
        # No cursor file → fall back to file mtime if present, otherwise
        # treat as stream-events not yet running (warning, not abort).
        try:
            last_event_ts = target.stat().st_mtime
        except FileNotFoundError:
            opener.warn(
                "stream_events_dead",
                f"no bridge cursor at {target}; stream-events daemon never wrote a row",
            )
            return
    else:
        # ``load_cursor`` returned (event_id, datetime). Convert to epoch.
        _, ts_dt = cursor
        if ts_dt.tzinfo is None:
            ts_dt = ts_dt.replace(tzinfo=_dt.timezone.utc)
        last_event_ts = ts_dt.timestamp()

    current = time.time() if now is None else now
    age = max(0.0, current - last_event_ts)
    opener.stream_events_age_seconds = age

    # Per AC #6 — pin the warning threshold at 5min for this opener,
    # not the F25 10min default in runner_health_checks.
    if age > STREAM_EVENTS_WARN_AFTER_SECONDS:
        outcome = check_stream_events(
            last_event_ts,
            now=current,
            max_age_seconds=STREAM_EVENTS_WARN_AFTER_SECONDS,
        )
        # ``outcome`` will be a StaleStreamEvents when age > threshold.
        if isinstance(outcome, StaleStreamEvents):
            opener.warn(
                "stream_events_dead",
                f"last stream event age {age:.0f}s "
                f"> {STREAM_EVENTS_WARN_AFTER_SECONDS}s (path={target})",
            )


# ── Public entry point ────────────────────────────────────────────────


def open_session(
    *,
    worktree_path: Path | str,
    owner: str,
    ticket_key: str,
    smoke_test_modules: list[str] | None = None,
    smoke_test_pytest_paths: list[str] | None = None,
    smoke_test_pytest_timeout: int = 60,
    bridge_heartbeat_path: Path | None = None,
    bridge_cursor_path: Path | None = None,
    now: float | None = None,
    run_smoke_test: bool = True,
    run_bridge_checks: bool = True,
) -> SessionOpener:
    """Run the 3-step opener + freshness probes.

    Parameters
    ----------
    worktree_path
        Absolute path to the runner worktree. AC #4 step 1 — cwd must
        match this.
    owner
        Bot identity string (e.g. ``"claude-bot"``,
        ``"api-anthropic"``). Stored in the session_header row of
        progress.txt so AC #8 owner mismatch is detectable.
    ticket_key
        JIRA ticket key (e.g. ``"OP-1122"``). Logged in warnings; the
        progress.txt is per-ticket-per-worktree.
    smoke_test_modules
        Python module names from the ticket's "Files touched" section.
        Each is imported via :func:`importlib.import_module`; failure
        is logged but does not abort (AC #4 step 3).
    smoke_test_pytest_paths
        Optional test paths to feed ``pytest --collect-only``. Empty/
        ``None`` skips the collect-only probe.
    bridge_heartbeat_path, bridge_cursor_path, now
        Injection hooks for testing.
    run_smoke_test, run_bridge_checks
        Toggles for the corresponding probes. The launcher passes
        ``True`` by default; tests can disable to isolate steps.
    """
    worktree = Path(worktree_path)

    # === Step 1 — CWD verification (AC #4 step 1; F17/F24 mitigation)
    _verify_cwd(worktree)

    # === Step 2 — read progress.txt (AC #4 step 2; AC #7 corruption tol)
    prior_entries, report = _plog.read_entries(worktree)
    progress_path = progress_file_path(worktree)
    progress_existed = progress_path.is_file()

    plog = ProgressLog(worktree, owner=owner)

    if not progress_existed:
        phase = "fresh"
        plog.start_session(reset=True)
        prior_entries = []
    elif not report.is_clean:
        # AC #7 — corrupt: log + treat as absent. We do NOT delete the
        # corrupt file; the operator may want to inspect it.
        phase = "fresh"
        prior_entries = []
        # Move corrupt file aside so the new log can start clean,
        # preserving the corrupt original under a quarantine name.
        quarantine = progress_path.with_suffix(
            progress_path.suffix + f".corrupt-{int(time.time())}"
        )
        try:
            progress_path.rename(quarantine)
        except OSError as exc:
            log.warning(
                "session_resume.quarantine_failed path=%s err=%s",
                progress_path, exc,
            )
        plog.start_session(reset=True)
    else:
        # Clean file — attempt resume. start_session(reset=False) enforces
        # owner match (AC #8 / concurrent_runner_conflict).
        plog.start_session(reset=False)
        phase = "resume"

    opener = SessionOpener(
        worktree_path=worktree,
        owner=owner,
        phase=phase,
        progress_log=plog,
        prior_entries=prior_entries,
    )

    if progress_existed and not report.is_clean:
        opener.warn(
            "progress_txt_corrupt",
            f"{len(report.bad_line_offsets)} unparseable line(s) at "
            f"offsets {report.bad_line_offsets[:10]}; original quarantined; "
            f"starting fresh session",
        )

    # === Step 3 — Smoke test (AC #4 step 3)
    if run_smoke_test:
        smoke = _run_smoke_test(
            worktree_path=worktree,
            modules=list(smoke_test_modules or []),
            pytest_paths=list(smoke_test_pytest_paths or []),
            pytest_timeout_seconds=smoke_test_pytest_timeout,
        )
        opener.smoke_test = smoke
        if not smoke.is_clean:
            detail_parts: list[str] = []
            if smoke.failed_imports:
                detail_parts.append(
                    f"failed_imports={smoke.failed_imports}"
                )
            if smoke.pytest_returncode not in (None, 0):
                tail = shlex.quote(smoke.pytest_summary)[:200]
                detail_parts.append(
                    f"pytest_returncode={smoke.pytest_returncode} "
                    f"tail={tail}"
                )
            opener.warn("smoke_test_fail", "; ".join(detail_parts) or "unknown")

    # === Bridge currency check (AC #5)
    if run_bridge_checks:
        _check_bridge_currency(
            opener,
            heartbeat_path=bridge_heartbeat_path,
            now=now,
        )

        # === Stream-events freshness (AC #6)
        _check_stream_events(
            opener,
            cursor_path=bridge_cursor_path,
            now=now,
        )

    # Record the opener outcome in the new progress log so any
    # later observer can see the resume decision.
    plog.append(
        role="opener",
        iter=0,
        stop_reason="",
        tool_calls=[],
        content_summary=(
            f"ticket={ticket_key} phase={phase} "
            f"prior_entries={len(opener.prior_entries)} "
            f"warnings={len(opener.warnings)}"
        ),
    )

    return opener


__all__ = [
    "BRIDGE_ABORT_AFTER_SECONDS",
    "BRIDGE_WARN_AFTER_SECONDS",
    "BridgeStaleCriticalError",
    "ConcurrentRunnerConflictError",
    "CwdNotWorktreeError",
    "SessionOpener",
    "SessionResumeError",
    "SmokeTestResult",
    "STREAM_EVENTS_WARN_AFTER_SECONDS",
    "open_session",
]
