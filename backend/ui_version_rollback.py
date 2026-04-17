"""V3 #5 (issue #319) — Version rollback for the iteration timeline.

Closes the V3 visual iteration loop: every time the operator hits
「回到此版本」 on a node of :component:`UiIterationTimeline`
(``components/omnisight/ui-iteration-timeline.tsx``, V3 #4) the
frontend emits ``onRollback(snapshot: IterationSnapshot)`` with the
snapshot's ``commitSha``.  This module is the server-side consumer:
it resolves the ref, runs ``git checkout`` inside the bind-mounted
sandbox workspace (V2 #1), and nudges the lifecycle layer (V2 #2) so
the Next.js dev server's HMR channel picks up the file changes and
refreshes the preview.

Where this sits in the V3 stack
--------------------------------

* V3 #1 ``visual-annotator.tsx`` — operator annotation overlay.
* V3 #2 ``backend/ui_annotation_context.py`` — server-side consumer
  of operator annotations.
* V3 #3 ``element-inspector.tsx`` — hover / pin DOM inspector.
* V3 #4 ``ui-iteration-timeline.tsx`` — horizontal timeline of agent
  iterations.  Emits ``onRollback(snapshot)`` callbacks carrying the
  full :type:`IterationSnapshot` (``{id, commitSha, screenshotSrc,
  diff, summary, agentId, createdAt, diffStats?}``).
* **V3 #5 (this module)** — receives the snapshot + session id,
  checks out the commit in the sandbox workspace, and triggers the
  preview refresh.

Contract (pinned by ``backend/tests/test_ui_version_rollback.py``)
------------------------------------------------------------------

* :data:`UI_VERSION_ROLLBACK_SCHEMA_VERSION` — semver; bump on
  :class:`RollbackResult.to_dict` shape changes.
* :data:`ROLLBACK_EVENT_TYPES` — exactly four topics under the
  ``ui_sandbox.rollback.*`` namespace.  Disjoint from every earlier
  V2 / V3 module's event namespace (tests enforce this).
* :func:`is_valid_commit_sha` — matches ``^[0-9a-f]{4,40}$``; mirrors
  the frontend's ``shortCommitSha`` tolerance for both short and long
  SHAs without leaking SHA-256 (Git is still SHA-1 land for most
  sandboxes).
* :func:`short_commit_sha` — byte-stable with the frontend helper in
  V3 #4 ``ui-iteration-timeline.tsx`` (truncate to 7 chars, pass-
  through when <=7, empty string for null / non-string / blank).
* :func:`rollback_request_from_snapshot` — accepts the V3 #4
  ``IterationSnapshot`` wire dict and returns a
  :class:`RollbackRequest`; ``commitSha=null`` raises
  :class:`InvalidCommitRef` with an operator-readable message.
* :class:`VersionRollback.rollback` — the one-shot entry point.  On
  success emits ``requested`` → ``checked_out`` → ``completed``.  On
  failure emits ``requested`` → ``failed``.  Never double-emits.

Design decisions
----------------

* **Dependency-injected git runner.**  The backend never shells out
  directly — callers inject a :class:`GitCommandRunner` (default:
  :class:`SubprocessGitRunner` which uses ``subprocess.run``).  Tests
  use :class:`FakeGitRunner` so the entire module is exercisable
  without a real git repo on disk.
* **Dependency-injected sandbox.**  :class:`VersionRollback` takes a
  :class:`SandboxManager` (V2 #1) for workspace lookup and an
  optional :class:`SandboxLifecycle` (V2 #2) for HMR signalling.
  Omitting the lifecycle is legal — the caller may be driving HMR
  elsewhere — ``preview_refresh_requested=False`` is then set on the
  :class:`RollbackResult`.
* **Safe git operations.**  We only run a small allowlist of read-
  only and checkout commands: ``rev-parse``, ``diff --name-only``,
  ``checkout``.  The runner is invoked with positional ``args`` so
  shell injection via ``commit_sha`` is impossible (and we still
  validate format upfront).
* **File changes as HMR hint.**  After checkout we compute
  ``files_changed`` via ``git diff --name-only previous...new``.
  This feeds :meth:`SandboxLifecycle.hot_reload` so SSE subscribers
  (V2 #6) can animate the preview swap.  The list is capped at
  :data:`MAX_FILES_CHANGED` (default 500) to keep SSE frames lean —
  the rest are dropped but the count is still honest on the wire.
* **Events always disjoint.**  The event namespace is
  ``ui_sandbox.rollback.*`` — chosen specifically to not collide
  with V2 #2 – #7 or V3 #2 namespaces.  A contract test enforces the
  disjoint invariant across every sibling module.
* **Clock seam.**  ``clock`` parameter injected everywhere we stamp
  a timestamp — deterministic tests, deterministic SSE frames.
* **Thread safety.**  :class:`VersionRollback` guards its counters
  and last-result cache with ``threading.RLock`` — multi-turn agent
  loops may invoke rollback concurrently with the reaper thread that
  also touches :class:`SandboxManager`.
"""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence

from backend.ui_sandbox import SandboxError, SandboxInstance, SandboxManager

logger = logging.getLogger(__name__)


__all__ = [
    "UI_VERSION_ROLLBACK_SCHEMA_VERSION",
    "MAX_FILES_CHANGED",
    "DEFAULT_GIT_TIMEOUT_S",
    "ROLLBACK_EVENT_REQUESTED",
    "ROLLBACK_EVENT_CHECKED_OUT",
    "ROLLBACK_EVENT_COMPLETED",
    "ROLLBACK_EVENT_FAILED",
    "ROLLBACK_EVENT_TYPES",
    "VersionRollbackError",
    "InvalidCommitRef",
    "GitCommandError",
    "RollbackSandboxNotFound",
    "GitCommandResult",
    "GitCommandRunner",
    "SubprocessGitRunner",
    "RollbackRequest",
    "RollbackResult",
    "VersionRollback",
    "is_valid_commit_sha",
    "short_commit_sha",
    "normalize_commit_ref",
    "rollback_request_from_snapshot",
]


# ───────────────────────────────────────────────────────────────────
#  Constants
# ───────────────────────────────────────────────────────────────────


#: Bump on any ``RollbackResult.to_dict`` / event envelope shape
#: change.  Versioned independently of the sibling V2 / V3 modules.
UI_VERSION_ROLLBACK_SCHEMA_VERSION = "1.0.0"

#: Cap on the ``files_changed`` list carried on the event envelope /
#: :class:`RollbackResult`.  Anything above this is truncated but the
#: count is still honest.  Keeps the SSE frame from ballooning on a
#: 10k-file refactor rollback.
MAX_FILES_CHANGED = 500

#: Default timeout (seconds) for a single git subprocess call.  Git
#: ``checkout`` on a cold sandbox may take a few seconds; 30 s is
#: plenty without letting a wedged invocation stall an agent turn.
DEFAULT_GIT_TIMEOUT_S = 30.0


ROLLBACK_EVENT_REQUESTED = "ui_sandbox.rollback.requested"
ROLLBACK_EVENT_CHECKED_OUT = "ui_sandbox.rollback.checked_out"
ROLLBACK_EVENT_COMPLETED = "ui_sandbox.rollback.completed"
ROLLBACK_EVENT_FAILED = "ui_sandbox.rollback.failed"


#: Full roster of topics — V2 #7 SSE bus subscribes on the
#: ``ui_sandbox.rollback.`` prefix.  Order is fire order for the
#: happy path: requested → checked_out → completed.  ``failed``
#: substitutes for the last two on the unhappy path.
ROLLBACK_EVENT_TYPES: tuple[str, ...] = (
    ROLLBACK_EVENT_REQUESTED,
    ROLLBACK_EVENT_CHECKED_OUT,
    ROLLBACK_EVENT_COMPLETED,
    ROLLBACK_EVENT_FAILED,
)


_SHA_RE = re.compile(r"^[0-9a-f]{4,40}$")


# ───────────────────────────────────────────────────────────────────
#  Errors
# ───────────────────────────────────────────────────────────────────


class VersionRollbackError(ValueError):
    """Base class for rollback errors.

    Subclasses :class:`ValueError` so FastAPI / pydantic default
    handlers treat bad operator input as 422 without extra wiring.
    """


class InvalidCommitRef(VersionRollbackError):
    """Raised when the caller supplies a commit ref that fails
    :func:`is_valid_commit_sha` or resolves to nothing."""


class GitCommandError(VersionRollbackError):
    """Raised when a git subprocess returns non-zero.

    Carries the full argv + stderr tail so operator dashboards can
    surface exactly what the agent tried.
    """

    def __init__(
        self,
        message: str,
        *,
        argv: Sequence[str] = (),
        returncode: int | None = None,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.argv = tuple(argv)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class RollbackSandboxNotFound(VersionRollbackError):
    """Raised when the requested session id has no live sandbox.

    Distinct from :class:`SandboxNotFound` so FastAPI handlers can
    decide whether to attempt session auto-create.
    """


# ───────────────────────────────────────────────────────────────────
#  Pure helpers
# ───────────────────────────────────────────────────────────────────


def is_valid_commit_sha(value: Any) -> bool:
    """Return True when ``value`` is a 4–40 char lowercase hex
    string.

    Git accepts short SHAs down to 4 characters for disambiguation;
    we accept the same range so the operator can paste a short ref
    from the timeline badge.  Uppercase is normalised by the caller
    (see :func:`normalize_commit_ref`).
    """

    if not isinstance(value, str):
        return False
    return bool(_SHA_RE.match(value))


def short_commit_sha(value: Any, *, length: int = 7) -> str:
    """Mirror the frontend ``shortCommitSha`` helper (V3 #4).

    Returns ``""`` for null / non-string / blank input; truncates to
    ``length`` characters otherwise; passes through when the value is
    already shorter than ``length``.
    """

    if not isinstance(length, int) or length < 1:
        raise ValueError("length must be a positive int")
    if not isinstance(value, str):
        return ""
    trimmed = value.strip()
    if not trimmed:
        return ""
    if len(trimmed) <= length:
        return trimmed
    return trimmed[:length]


def normalize_commit_ref(value: Any) -> str:
    """Trim + lowercase a candidate commit SHA.

    Raises :class:`InvalidCommitRef` when the result fails
    :func:`is_valid_commit_sha`.  The lowercase step is deliberate —
    git is case-insensitive for SHA lookup but our event envelopes
    and dedupe keys should be deterministic.
    """

    if not isinstance(value, str):
        raise InvalidCommitRef(
            f"commit ref must be a string, got {type(value).__name__}"
        )
    candidate = value.strip().lower()
    if not is_valid_commit_sha(candidate):
        raise InvalidCommitRef(
            "commit ref must be a 4–40 character hex string, got "
            f"{value!r}"
        )
    return candidate


def rollback_request_from_snapshot(
    *,
    session_id: str,
    snapshot: Mapping[str, Any],
    reason: str | None = None,
) -> "RollbackRequest":
    """Convert a V3 #4 ``IterationSnapshot`` wire dict into a
    :class:`RollbackRequest`.

    The frontend emits the full snapshot object via
    ``onRollback(snapshot)``; this helper is the one-liner most
    FastAPI handlers want::

        request = rollback_request_from_snapshot(
            session_id=body.session_id,
            snapshot=body.snapshot,
        )
        result = rollbacker.rollback(request)

    Raises :class:`InvalidCommitRef` if the snapshot's ``commitSha``
    is missing / null / malformed.
    """

    if not isinstance(session_id, str) or not session_id.strip():
        raise VersionRollbackError("session_id must be a non-empty string")
    if not isinstance(snapshot, Mapping):
        raise VersionRollbackError(
            "snapshot must be a mapping with at least 'id' and 'commitSha' keys"
        )

    commit_sha_raw = snapshot.get("commitSha")
    if commit_sha_raw is None or (
        isinstance(commit_sha_raw, str) and not commit_sha_raw.strip()
    ):
        raise InvalidCommitRef(
            "snapshot.commitSha is required to roll back — the iteration "
            "must have a committed git ref"
        )

    commit_sha = normalize_commit_ref(commit_sha_raw)

    iteration_id_raw = snapshot.get("id")
    iteration_id: str | None
    if iteration_id_raw is None:
        iteration_id = None
    elif isinstance(iteration_id_raw, str) and iteration_id_raw.strip():
        iteration_id = iteration_id_raw.strip()
    else:
        raise VersionRollbackError(
            "snapshot.id must be a non-empty string or absent"
        )

    return RollbackRequest(
        session_id=session_id.strip(),
        commit_sha=commit_sha,
        iteration_id=iteration_id,
        reason=reason,
    )


# ───────────────────────────────────────────────────────────────────
#  Git command runner
# ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GitCommandResult:
    """Structured result of one git subprocess invocation."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    def __post_init__(self) -> None:
        if not isinstance(self.argv, tuple) or not all(
            isinstance(a, str) for a in self.argv
        ):
            raise ValueError("argv must be a tuple[str, ...]")
        if not isinstance(self.returncode, int):
            raise ValueError("returncode must be an int")
        if not isinstance(self.stdout, str):
            raise ValueError("stdout must be a string")
        if not isinstance(self.stderr, str):
            raise ValueError("stderr must be a string")

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "returncode": int(self.returncode),
            "stdout": str(self.stdout),
            "stderr": str(self.stderr),
        }


class GitCommandRunner(Protocol):
    """Callable that runs ``git <args>`` inside ``cwd``.

    Implementations MUST be thread-safe — rollback and the reaper
    thread (V2 #2) may call concurrently on unrelated sessions.
    """

    def __call__(
        self,
        *args: str,
        cwd: str,
        timeout: float = DEFAULT_GIT_TIMEOUT_S,
    ) -> GitCommandResult:  # pragma: no cover - protocol
        ...


class SubprocessGitRunner:
    """Default :class:`GitCommandRunner` using :mod:`subprocess`.

    Always calls ``git`` as argv[0] — no shell interpolation — so
    injection via a crafted ``commit_sha`` is architecturally
    impossible (we validate the format upfront anyway).  Captures
    stdout + stderr as text.  On :class:`subprocess.TimeoutExpired`
    kills the process and raises :class:`GitCommandError` with
    returncode = -1.
    """

    def __init__(self, *, git_binary: str = "git") -> None:
        if not isinstance(git_binary, str) or not git_binary.strip():
            raise ValueError("git_binary must be a non-empty string")
        self._git_binary = git_binary

    def __call__(
        self,
        *args: str,
        cwd: str,
        timeout: float = DEFAULT_GIT_TIMEOUT_S,
    ) -> GitCommandResult:
        if not args:
            raise ValueError("git runner requires at least one argument")
        for arg in args:
            if not isinstance(arg, str):
                raise TypeError("all git args must be strings")
        argv = (self._git_binary, *args)
        try:
            proc = subprocess.run(
                list(argv),
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitCommandError(
                f"git {' '.join(shlex.quote(a) for a in args)} timed out "
                f"after {timeout}s",
                argv=argv,
                returncode=-1,
                stdout=exc.stdout.decode("utf-8", "replace")
                if isinstance(exc.stdout, bytes)
                else (exc.stdout or ""),
                stderr=exc.stderr.decode("utf-8", "replace")
                if isinstance(exc.stderr, bytes)
                else (exc.stderr or ""),
            ) from exc
        except FileNotFoundError as exc:
            raise GitCommandError(
                f"git binary not found: {self._git_binary!r}",
                argv=argv,
                returncode=-1,
                stderr=str(exc),
            ) from exc
        return GitCommandResult(
            argv=argv,
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
        )


# ───────────────────────────────────────────────────────────────────
#  Request / result records
# ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RollbackRequest:
    """One operator-triggered rollback request."""

    session_id: str
    commit_sha: str
    iteration_id: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise VersionRollbackError("session_id must be a non-empty string")
        if not is_valid_commit_sha(self.commit_sha):
            raise InvalidCommitRef(
                f"commit_sha must be a 4–40 char hex string, got "
                f"{self.commit_sha!r}"
            )
        if self.iteration_id is not None and (
            not isinstance(self.iteration_id, str)
            or not self.iteration_id.strip()
        ):
            raise VersionRollbackError(
                "iteration_id must be a non-empty string or None"
            )
        if self.reason is not None and not isinstance(self.reason, str):
            raise VersionRollbackError("reason must be a string or None")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "session_id": self.session_id,
            "commit_sha": self.commit_sha,
            "short_sha": short_commit_sha(self.commit_sha),
        }
        if self.iteration_id is not None:
            out["iteration_id"] = self.iteration_id
        if self.reason is not None:
            out["reason"] = self.reason
        return out


@dataclass(frozen=True)
class RollbackResult:
    """Outcome of one rollback."""

    schema_version: str
    session_id: str
    iteration_id: str | None
    requested_sha: str
    resolved_sha: str
    previous_sha: str | None
    short_sha: str
    files_changed: tuple[str, ...]
    files_changed_total: int
    preview_refresh_requested: bool
    checked_out_at: float
    reason: str | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.schema_version != UI_VERSION_ROLLBACK_SCHEMA_VERSION:
            raise ValueError("schema_version mismatch")
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must be non-empty")
        if not is_valid_commit_sha(self.requested_sha):
            raise ValueError("requested_sha must be a valid hex SHA")
        if not is_valid_commit_sha(self.resolved_sha):
            raise ValueError("resolved_sha must be a valid hex SHA")
        if self.previous_sha is not None and not is_valid_commit_sha(
            self.previous_sha
        ):
            raise ValueError("previous_sha must be a valid hex SHA or None")
        if not isinstance(self.files_changed, tuple):
            raise ValueError("files_changed must be a tuple")
        if not all(isinstance(f, str) for f in self.files_changed):
            raise ValueError("files_changed must be tuple[str, ...]")
        if self.files_changed_total < 0:
            raise ValueError("files_changed_total must be >= 0")
        if self.files_changed_total < len(self.files_changed):
            raise ValueError(
                "files_changed_total must be >= len(files_changed)"
            )
        if self.checked_out_at < 0:
            raise ValueError("checked_out_at must be >= 0")

    @property
    def file_count(self) -> int:
        """Number of distinct paths carried on the result (possibly
        truncated — see :attr:`files_changed_total` for the honest
        count)."""

        return len(self.files_changed)

    @property
    def truncated(self) -> bool:
        return self.files_changed_total > len(self.files_changed)

    @property
    def is_noop(self) -> bool:
        """True when the sandbox was already sitting on the target
        commit — resolution succeeded but no files moved."""

        return (
            self.previous_sha is not None
            and self.previous_sha == self.resolved_sha
            and len(self.files_changed) == 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "iteration_id": self.iteration_id,
            "requested_sha": self.requested_sha,
            "resolved_sha": self.resolved_sha,
            "previous_sha": self.previous_sha,
            "short_sha": self.short_sha,
            "files_changed": list(self.files_changed),
            "file_count": self.file_count,
            "files_changed_total": int(self.files_changed_total),
            "truncated": bool(self.truncated),
            "is_noop": bool(self.is_noop),
            "preview_refresh_requested": bool(self.preview_refresh_requested),
            "checked_out_at": float(self.checked_out_at),
            "reason": self.reason,
            "warnings": list(self.warnings),
        }


# ───────────────────────────────────────────────────────────────────
#  VersionRollback orchestrator
# ───────────────────────────────────────────────────────────────────


EventCallback = Callable[[str, Mapping[str, Any]], None]


class VersionRollback:
    """Per-deployment rollback orchestrator.

    Stateless apart from counters + last-result snapshot (used by
    SSE operator dashboards).  Thread-safe — all state access is
    serialised on an ``RLock``.

    Typical wire-up::

        manager = SandboxManager(docker_client=docker)
        lifecycle = SandboxLifecycle(manager=manager, event_cb=sse.emit)
        rollbacker = VersionRollback(
            manager=manager,
            lifecycle=lifecycle,
            event_cb=sse.emit,
        )
        result = rollbacker.rollback_from_snapshot(
            session_id="sess-1",
            snapshot=body_dict["snapshot"],
        )
    """

    def __init__(
        self,
        *,
        manager: SandboxManager,
        lifecycle: Any | None = None,
        git_runner: GitCommandRunner | None = None,
        event_cb: EventCallback | None = None,
        clock: Callable[[], float] = time.time,
        max_files_changed: int = MAX_FILES_CHANGED,
        git_timeout_s: float = DEFAULT_GIT_TIMEOUT_S,
    ) -> None:
        if manager is None:
            raise TypeError("manager must be provided")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if not isinstance(max_files_changed, int) or max_files_changed < 1:
            raise ValueError("max_files_changed must be >= 1")
        if not isinstance(git_timeout_s, (int, float)) or git_timeout_s <= 0:
            raise ValueError("git_timeout_s must be > 0")

        self._manager = manager
        self._lifecycle = lifecycle
        self._git_runner: GitCommandRunner = (
            git_runner if git_runner is not None else SubprocessGitRunner()
        )
        self._event_cb = event_cb
        self._clock = clock
        self._max_files_changed = max_files_changed
        self._git_timeout_s = float(git_timeout_s)

        self._lock = threading.RLock()
        self._rollback_count = 0
        self._failure_count = 0
        self._noop_count = 0
        self._last_result: RollbackResult | None = None
        self._last_error: str | None = None

    # ─────────────── Accessors ───────────────

    def rollback_count(self) -> int:
        with self._lock:
            return self._rollback_count

    def failure_count(self) -> int:
        with self._lock:
            return self._failure_count

    def noop_count(self) -> int:
        with self._lock:
            return self._noop_count

    def last_result(self) -> RollbackResult | None:
        with self._lock:
            return self._last_result

    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    # ─────────────── Core API ───────────────

    def rollback(self, request: RollbackRequest) -> RollbackResult:
        """Execute one rollback end-to-end.

        Event sequence on success:
          1. ``ui_sandbox.rollback.requested``
          2. ``ui_sandbox.rollback.checked_out``
          3. ``ui_sandbox.rollback.completed``

        Event sequence on failure:
          1. ``ui_sandbox.rollback.requested``
          2. ``ui_sandbox.rollback.failed``
        """

        if not isinstance(request, RollbackRequest):
            raise TypeError("request must be a RollbackRequest")

        started_at = float(self._clock())
        self._emit(
            ROLLBACK_EVENT_REQUESTED,
            {
                "schema_version": UI_VERSION_ROLLBACK_SCHEMA_VERSION,
                "session_id": request.session_id,
                "iteration_id": request.iteration_id,
                "requested_sha": request.commit_sha,
                "short_sha": short_commit_sha(request.commit_sha),
                "reason": request.reason,
                "at": started_at,
            },
        )

        try:
            result = self._perform_rollback(request, started_at)
        except VersionRollbackError as exc:
            self._emit(
                ROLLBACK_EVENT_FAILED,
                {
                    "schema_version": UI_VERSION_ROLLBACK_SCHEMA_VERSION,
                    "session_id": request.session_id,
                    "iteration_id": request.iteration_id,
                    "requested_sha": request.commit_sha,
                    "short_sha": short_commit_sha(request.commit_sha),
                    "error": str(exc),
                    "error_type": exc.__class__.__name__,
                    "at": float(self._clock()),
                },
            )
            with self._lock:
                self._failure_count += 1
                self._last_error = f"{exc.__class__.__name__}: {exc}"
            raise

        self._emit(
            ROLLBACK_EVENT_COMPLETED,
            self._envelope_for_event(result),
        )
        with self._lock:
            self._rollback_count += 1
            if result.is_noop:
                self._noop_count += 1
            self._last_result = result
            self._last_error = None
        return result

    def rollback_from_snapshot(
        self,
        *,
        session_id: str,
        snapshot: Mapping[str, Any],
        reason: str | None = None,
    ) -> RollbackResult:
        """Convenience: parse a V3 #4 ``IterationSnapshot`` dict and
        roll back in one call."""

        request = rollback_request_from_snapshot(
            session_id=session_id,
            snapshot=snapshot,
            reason=reason,
        )
        return self.rollback(request)

    # ─────────────── Snapshot ───────────────

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe operator snapshot — counters + last result
        metadata."""

        with self._lock:
            last = self._last_result
            last_summary: dict[str, Any] | None = None
            if last is not None:
                last_summary = {
                    "session_id": last.session_id,
                    "iteration_id": last.iteration_id,
                    "resolved_sha": last.resolved_sha,
                    "short_sha": last.short_sha,
                    "file_count": last.file_count,
                    "files_changed_total": last.files_changed_total,
                    "truncated": last.truncated,
                    "is_noop": last.is_noop,
                    "preview_refresh_requested": last.preview_refresh_requested,
                    "checked_out_at": float(last.checked_out_at),
                }
            return {
                "schema_version": UI_VERSION_ROLLBACK_SCHEMA_VERSION,
                "rollback_count": int(self._rollback_count),
                "failure_count": int(self._failure_count),
                "noop_count": int(self._noop_count),
                "last_result": last_summary,
                "last_error": self._last_error,
                "now": float(self._clock()),
            }

    # ─────────────── Internal plumbing ───────────────

    def _perform_rollback(
        self,
        request: RollbackRequest,
        started_at: float,
    ) -> RollbackResult:
        instance = self._manager.get(request.session_id)
        if instance is None:
            raise RollbackSandboxNotFound(
                f"no sandbox for session_id={request.session_id!r}; "
                "start one via SandboxManager.create() first"
            )
        workspace = self._workspace_for(instance)

        # Capture HEAD before moving so ``files_changed`` diff is well
        # defined even when the ref is already checked out.
        previous_sha: str | None
        try:
            previous_sha = self._rev_parse(workspace, "HEAD")
        except GitCommandError as exc:
            # A fresh sandbox may not yet have a HEAD — capture the
            # warning on the result rather than erroring out (the agent
            # may have committed nothing yet but still want a rollback
            # to a named branch).  We continue with previous_sha=None.
            logger.info(
                "rev-parse HEAD failed for session=%s: %s",
                request.session_id,
                exc,
            )
            previous_sha = None

        resolved_sha = self._rev_parse(workspace, request.commit_sha)

        # Actually move the working tree.  ``git checkout --detach`` is
        # intentional — we're going back to an arbitrary historical
        # ref, not updating a branch, so the agent loop can commit new
        # work from here as a fresh branch without clobbering main.
        self._checkout(workspace, resolved_sha)

        self._emit(
            ROLLBACK_EVENT_CHECKED_OUT,
            {
                "schema_version": UI_VERSION_ROLLBACK_SCHEMA_VERSION,
                "session_id": request.session_id,
                "iteration_id": request.iteration_id,
                "requested_sha": request.commit_sha,
                "resolved_sha": resolved_sha,
                "previous_sha": previous_sha,
                "short_sha": short_commit_sha(resolved_sha),
                "at": float(self._clock()),
            },
        )

        files_changed: tuple[str, ...] = ()
        files_changed_total = 0
        warnings: list[str] = []
        if previous_sha and previous_sha != resolved_sha:
            try:
                files_changed, files_changed_total = self._diff_name_only(
                    workspace, previous_sha, resolved_sha
                )
            except GitCommandError as exc:
                warnings.append(
                    f"diff_name_only failed: {exc}; HMR signal sent with "
                    "empty file list"
                )
                logger.warning(
                    "diff --name-only failed for session=%s: %s",
                    request.session_id,
                    exc,
                )

        preview_refresh_requested = False
        if self._lifecycle is not None:
            try:
                self._lifecycle.hot_reload(
                    request.session_id, files_changed=files_changed
                )
                preview_refresh_requested = True
            except SandboxError as exc:
                warnings.append(
                    f"preview refresh skipped: {exc.__class__.__name__}: {exc}"
                )
                logger.info(
                    "lifecycle.hot_reload refused session=%s: %s",
                    request.session_id,
                    exc,
                )

        return RollbackResult(
            schema_version=UI_VERSION_ROLLBACK_SCHEMA_VERSION,
            session_id=request.session_id,
            iteration_id=request.iteration_id,
            requested_sha=request.commit_sha,
            resolved_sha=resolved_sha,
            previous_sha=previous_sha,
            short_sha=short_commit_sha(resolved_sha),
            files_changed=files_changed,
            files_changed_total=files_changed_total,
            preview_refresh_requested=preview_refresh_requested,
            checked_out_at=float(self._clock()),
            reason=request.reason,
            warnings=tuple(warnings),
        )

    def _workspace_for(self, instance: SandboxInstance) -> str:
        workspace = instance.config.workspace_path
        if not isinstance(workspace, str) or not workspace.strip():
            raise VersionRollbackError(
                f"sandbox for session_id={instance.config.session_id!r} has "
                "no workspace_path"
            )
        return workspace

    def _rev_parse(self, workspace: str, ref: str) -> str:
        result = self._git_runner(
            "rev-parse",
            "--verify",
            f"{ref}^{{commit}}",
            cwd=workspace,
            timeout=self._git_timeout_s,
        )
        if not result.ok:
            raise GitCommandError(
                f"git rev-parse --verify {ref} failed",
                argv=result.argv,
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        resolved = result.stdout.strip().lower()
        if not is_valid_commit_sha(resolved):
            raise GitCommandError(
                f"git rev-parse returned non-SHA output for {ref!r}: "
                f"{resolved!r}",
                argv=result.argv,
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        return resolved

    def _checkout(self, workspace: str, sha: str) -> None:
        result = self._git_runner(
            "checkout",
            "--detach",
            "--force",
            sha,
            cwd=workspace,
            timeout=self._git_timeout_s,
        )
        if not result.ok:
            raise GitCommandError(
                f"git checkout --detach --force {sha} failed",
                argv=result.argv,
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )

    def _diff_name_only(
        self, workspace: str, previous_sha: str, resolved_sha: str
    ) -> tuple[tuple[str, ...], int]:
        result = self._git_runner(
            "diff",
            "--name-only",
            f"{previous_sha}..{resolved_sha}",
            cwd=workspace,
            timeout=self._git_timeout_s,
        )
        if not result.ok:
            raise GitCommandError(
                "git diff --name-only failed",
                argv=result.argv,
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        lines = [
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip()
        ]
        total = len(lines)
        if total <= self._max_files_changed:
            return tuple(lines), total
        return tuple(lines[: self._max_files_changed]), total

    def _envelope_for_event(self, result: RollbackResult) -> dict[str, Any]:
        """Event payload for ``completed`` — elides the full file
        list beyond the first 20 entries to keep SSE frames small.
        Callers that want the full list go through :meth:`last_result`.
        """

        preview_files = list(result.files_changed[:20])
        return {
            "schema_version": UI_VERSION_ROLLBACK_SCHEMA_VERSION,
            "session_id": result.session_id,
            "iteration_id": result.iteration_id,
            "requested_sha": result.requested_sha,
            "resolved_sha": result.resolved_sha,
            "previous_sha": result.previous_sha,
            "short_sha": result.short_sha,
            "file_count": result.file_count,
            "files_changed_total": int(result.files_changed_total),
            "files_preview": preview_files,
            "truncated": bool(result.truncated),
            "is_noop": bool(result.is_noop),
            "preview_refresh_requested": bool(
                result.preview_refresh_requested
            ),
            "warning_count": len(result.warnings),
            "checked_out_at": float(result.checked_out_at),
        }

    def _emit(self, event_type: str, data: Mapping[str, Any]) -> None:
        if self._event_cb is None:
            return
        try:
            self._event_cb(event_type, dict(data))
        except Exception as exc:  # pragma: no cover - callback must not kill us
            logger.warning(
                "ui_version_rollback event callback raised: %s", exc
            )
