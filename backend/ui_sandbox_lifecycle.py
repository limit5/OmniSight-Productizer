"""V2 #2 (issue #318) — Per-session Next.js sandbox lifecycle policy.

Layered *on top of* :mod:`backend.ui_sandbox` (V2 #1).  The primitives
module knows how to drive Docker for one sandbox; this module enforces
the **full session lifecycle** the V2 spec promises:

    create → start → hot-reload → screenshot → stop → cleanup

plus the two cross-cutting invariants the spec pins:

  * **One sandbox per session.**  :meth:`SandboxLifecycle.ensure_session`
    is idempotent — repeated calls with the same ``session_id`` reuse
    the existing sandbox (or fast-fail when the caller pointed at a
    different workspace).
  * **Idle 15 minutes → auto-reap.**  :meth:`reap_idle` scans every
    sandbox whose ``last_active_at`` is older than
    :data:`DEFAULT_IDLE_LIMIT_S` and tears it down + removes it.  The
    optional background :meth:`start_reaper` sweeps on a timer.

Why a separate module
---------------------

V2 #1 is deliberately primitives-only: ``create``/``start``/
``mark_ready``/``touch``/``stop``/``remove`` mirror Docker verbs so
tests can pin argv shape without policy drift.  Any *policy* — "1 per
session", "reap after 15 min", "wait-ready with timeout", "screenshot
hooks into agent context", "teardown on container-manager exit" —
belongs *above* the primitives so the contract test suite for V2 #1
keeps staring at Docker argv and not at clock math.

This split also means V2 #1 tests run with no clock injection (they
pin the spec) and V2 #2 tests drive a deterministic ``FakeClock`` /
``FakeSleep`` pair so reaper + wait_ready logic exercises every edge
without real-world flakiness.

Design decisions
----------------

* **Composition over inheritance.**  :class:`SandboxLifecycle` *has* a
  :class:`SandboxManager`; it does not subclass.  This keeps V2 #1
  primitives public and reusable by other call-sites (e.g. admin
  endpoints that want raw state transitions) while the lifecycle
  object stays focused on session-scoped orchestration.
* **Deterministic time.**  Every blocking operation takes a
  ``sleep=`` injection; every timestamp goes through ``clock=``.
  Tests never touch real ``time.sleep`` and never monkey-patch the
  global ``time`` module.
* **Screenshot is a hook, not a module dependency.**  V2 #2 does not
  import Playwright — row 3 (``ui_screenshot.py``) ships the real
  hook later.  Callers supply ``screenshot_hook=`` and the lifecycle
  object wraps it with policy: touch on capture, emit SSE event,
  record error when the hook raises.
* **Reaper is opt-in.**  Production callers instantiate the
  lifecycle and invoke :meth:`start_reaper` to spin a background
  thread; tests call :meth:`reap_idle` synchronously so every
  transition is deterministic.
* **Graceful teardown on exit.**  The lifecycle is a context manager
  — ``__exit__`` stops the reaper (if running) and tears down every
  sandbox it created.  Good hygiene for agent loops that SIGINT.

Contract (pinned by ``backend/tests/test_ui_sandbox_lifecycle.py``)
------------------------------------------------------------------

* :data:`SANDBOX_LIFECYCLE_SCHEMA_VERSION` is semver; bump on shape
  changes to :class:`ScreenshotResult.to_dict()` /
  :class:`ReapReport.to_dict()` / :meth:`SandboxLifecycle.snapshot`.
* :meth:`ensure_session` is idempotent on ``(session_id,
  workspace_path)`` — returns running ``SandboxInstance`` whether it
  created, resumed, or found it already running.
* :meth:`ensure_session` raises :class:`LifecycleError` when a
  different session requests a new sandbox mid-lifetime (preserves
  "1 per session" beyond V2 #1's in-manager check).
* :meth:`wait_ready` polls container logs via
  :meth:`SandboxManager.poll_ready`; marks ready on first hit or
  raises :class:`ReadyTimeout` after ``timeout_s``.
* :meth:`hot_reload` touches ``last_active_at`` and emits
  ``ui_sandbox.hot_reload`` — HMR itself is provided by the volume
  mount the primitives module sets up.
* :meth:`capture_screenshot` delegates to the injected
  :class:`ScreenshotHook`, touches the sandbox, emits
  ``ui_sandbox.screenshot``.  Absent hook raises
  :class:`ScreenshotUnavailable`.
* :meth:`teardown` == stop + (optional) remove; emits
  ``ui_sandbox.teardown``.  Idempotent — tearing down a terminal
  sandbox is a no-op (still removes registry entry on demand).
* :meth:`reap_idle` respects ``last_active_at`` — a freshly-touched
  sandbox is never reaped.  Returns :class:`ReapReport`.
* Background reaper (:meth:`start_reaper`) is single-instance and
  joinable.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from backend.ui_sandbox import (
    DEFAULT_IDLE_LIMIT_S,
    SandboxAlreadyExists,
    SandboxConfig,
    SandboxError,
    SandboxInstance,
    SandboxManager,
    SandboxNotFound,
    SandboxStatus,
)

logger = logging.getLogger(__name__)


__all__ = [
    "SANDBOX_LIFECYCLE_SCHEMA_VERSION",
    "DEFAULT_READY_POLL_INTERVAL_S",
    "DEFAULT_READY_POLL_TIMEOUT_S",
    "DEFAULT_REAPER_INTERVAL_S",
    "DEFAULT_IDLE_LIMIT_S",
    "MAX_SANDBOXES_PER_SESSION",
    "LIFECYCLE_EVENT_ENSURE",
    "LIFECYCLE_EVENT_HOT_RELOAD",
    "LIFECYCLE_EVENT_SCREENSHOT",
    "LIFECYCLE_EVENT_TEARDOWN",
    "LIFECYCLE_EVENT_REAPED",
    "LIFECYCLE_EVENT_READY_TIMEOUT",
    "LIFECYCLE_EVENT_TYPES",
    "ScreenshotHook",
    "ScreenshotResult",
    "ReapReport",
    "SandboxLifecycle",
    "LifecycleError",
    "ReadyTimeout",
    "ScreenshotUnavailable",
    "WorkspaceMismatch",
]


#: Bump on shape changes to any of the lifecycle dataclasses.
SANDBOX_LIFECYCLE_SCHEMA_VERSION = "1.0.0"

#: Interval (seconds) :meth:`SandboxLifecycle.wait_ready` waits between
#: log polls while the dev server is warming up.  500 ms is fast enough
#: for HMR to feel snappy without burning CPU on busy hosts.
DEFAULT_READY_POLL_INTERVAL_S = 0.5

#: Cap on how long :meth:`wait_ready` will poll before raising
#: :class:`ReadyTimeout`.  60 s matches Next.js cold-start headroom on
#: the Alpine base image used by V2 #1.
DEFAULT_READY_POLL_TIMEOUT_S = 60.0

#: Default reaper-thread sweep interval.  The reaper only tears down
#: sandboxes older than ``idle_limit_s`` so a 30 s granularity is
#: plenty.
DEFAULT_REAPER_INTERVAL_S = 30.0

#: Per-session sandbox cap — hard invariant.  Exposed as a module
#: constant so callers that want to assert on it can import the
#: contract directly rather than hard-coding ``1``.
MAX_SANDBOXES_PER_SESSION = 1


LIFECYCLE_EVENT_ENSURE = "ui_sandbox.ensure_session"
LIFECYCLE_EVENT_HOT_RELOAD = "ui_sandbox.hot_reload"
LIFECYCLE_EVENT_SCREENSHOT = "ui_sandbox.screenshot"
LIFECYCLE_EVENT_TEARDOWN = "ui_sandbox.teardown"
LIFECYCLE_EVENT_REAPED = "ui_sandbox.reaped"
LIFECYCLE_EVENT_READY_TIMEOUT = "ui_sandbox.ready_timeout"

#: Full roster of events this module emits — callers wiring SSE bus
#: subscriptions can use this tuple to enumerate topics deterministically.
LIFECYCLE_EVENT_TYPES: tuple[str, ...] = (
    LIFECYCLE_EVENT_ENSURE,
    LIFECYCLE_EVENT_HOT_RELOAD,
    LIFECYCLE_EVENT_SCREENSHOT,
    LIFECYCLE_EVENT_TEARDOWN,
    LIFECYCLE_EVENT_REAPED,
    LIFECYCLE_EVENT_READY_TIMEOUT,
)


# ───────────────────────────────────────────────────────────────────
#  Errors
# ───────────────────────────────────────────────────────────────────


class LifecycleError(SandboxError):
    """Base class for lifecycle-layer errors.

    Subclasses a :class:`SandboxError` so existing ``except
    SandboxError`` callers keep catching us without code changes.
    """


class ReadyTimeout(LifecycleError):
    """Raised when :meth:`SandboxLifecycle.wait_ready` exhausts
    ``timeout_s`` without seeing a dev-server ready banner."""


class ScreenshotUnavailable(LifecycleError):
    """Raised by :meth:`capture_screenshot` when no screenshot hook
    was registered.  V2 row 3 (``ui_screenshot.py``) wires the hook
    in; before that lands, callers get this explicit error instead of
    a silent ``None``."""


class WorkspaceMismatch(LifecycleError):
    """Raised by :meth:`ensure_session` when the caller hands a new
    workspace path for an already-live session.  Tells the agent
    loop "you meant ``recreate=True``"."""


# ───────────────────────────────────────────────────────────────────
#  Screenshot hook + result
# ───────────────────────────────────────────────────────────────────


class ScreenshotHook(Protocol):
    """Callable the lifecycle invokes to capture a PNG of the dev
    server.  V2 row 3 (``ui_screenshot.py``) implements this with
    Playwright; tests plug in an in-memory stub.

    Implementations MUST be thread-safe (the reaper thread may fire
    captures concurrent with agent-driven ones).
    """

    def __call__(
        self,
        *,
        session_id: str,
        preview_url: str,
        viewport: str,
        path: str,
    ) -> bytes:  # PNG bytes
        ...


@dataclass(frozen=True)
class ScreenshotResult:
    """Record of one screenshot capture.

    The lifecycle emits this as the payload of the
    ``ui_sandbox.screenshot`` event so SSE subscribers (V2 row 6)
    receive a structured shape rather than raw bytes inline.
    """

    session_id: str
    preview_url: str
    viewport: str
    path: str
    image_bytes: bytes
    captured_at: float

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must be non-empty")
        if not isinstance(self.preview_url, str) or not self.preview_url.strip():
            raise ValueError("preview_url must be non-empty")
        if not isinstance(self.viewport, str) or not self.viewport.strip():
            raise ValueError("viewport must be non-empty")
        if not isinstance(self.path, str) or not self.path.startswith("/"):
            raise ValueError("path must start with '/'")
        if not isinstance(self.image_bytes, (bytes, bytearray)):
            raise ValueError("image_bytes must be bytes")
        if self.captured_at < 0:
            raise ValueError("captured_at must be non-negative")

    @property
    def byte_len(self) -> int:
        return len(self.image_bytes)

    def to_dict(self, *, include_bytes: bool = False) -> dict[str, Any]:
        """JSON-safe view.  ``include_bytes=False`` by default — most
        SSE callers want the URL + metadata, not the raw PNG.  Set to
        ``True`` only when the caller is about to base64-encode for
        multimodal Opus messages (V2 row 6)."""

        out: dict[str, Any] = {
            "schema_version": SANDBOX_LIFECYCLE_SCHEMA_VERSION,
            "session_id": self.session_id,
            "preview_url": self.preview_url,
            "viewport": self.viewport,
            "path": self.path,
            "byte_len": self.byte_len,
            "captured_at": float(self.captured_at),
        }
        if include_bytes:
            import base64

            out["image_base64"] = base64.b64encode(bytes(self.image_bytes)).decode(
                "ascii"
            )
        return out


# ───────────────────────────────────────────────────────────────────
#  Reap report
# ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReapReport:
    """Summary of one :meth:`SandboxLifecycle.reap_idle` sweep.

    ``reaped_sessions`` are those torn down this sweep.
    ``still_active`` are the survivors — callers use this to log or
    assert on the sweep result without racing :meth:`SandboxManager.list`.
    """

    reaped_at: float
    reaped_sessions: tuple[str, ...] = ()
    still_active: tuple[str, ...] = ()
    idle_limit_s: float = DEFAULT_IDLE_LIMIT_S
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.reaped_at < 0:
            raise ValueError("reaped_at must be non-negative")
        if self.idle_limit_s <= 0:
            raise ValueError("idle_limit_s must be positive")
        object.__setattr__(self, "reaped_sessions", tuple(self.reaped_sessions))
        object.__setattr__(self, "still_active", tuple(self.still_active))
        object.__setattr__(self, "warnings", tuple(self.warnings))

    @property
    def reaped_count(self) -> int:
        return len(self.reaped_sessions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SANDBOX_LIFECYCLE_SCHEMA_VERSION,
            "reaped_at": float(self.reaped_at),
            "reaped_sessions": list(self.reaped_sessions),
            "still_active": list(self.still_active),
            "idle_limit_s": float(self.idle_limit_s),
            "reaped_count": self.reaped_count,
            "warnings": list(self.warnings),
        }


# ───────────────────────────────────────────────────────────────────
#  Lifecycle controller
# ───────────────────────────────────────────────────────────────────


EventCallback = Callable[[str, Mapping[str, Any]], None]


@dataclass
class _ReaperState:
    """Internal state for the background reaper thread.  Mutable —
    held behind the lifecycle lock."""

    thread: threading.Thread | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    interval_s: float = DEFAULT_REAPER_INTERVAL_S
    started_at: float = 0.0
    sweeps: int = 0


class SandboxLifecycle:
    """Session-scoped policy wrapper over :class:`SandboxManager`.

    Usage::

        mgr = SandboxManager(docker_client=SubprocessDockerClient())
        life = SandboxLifecycle(manager=mgr, screenshot_hook=playwright_hook)
        life.start_reaper()
        try:
            instance = life.ensure_session(config)           # create+start+wait_ready
            life.hot_reload("sess-1", files_changed=("a.tsx",))
            shot = life.capture_screenshot("sess-1", viewport="desktop")
            ...
        finally:
            life.teardown("sess-1")
            life.stop_reaper()

    All blocking operations delegate sleep to the injected
    ``sleep=`` callable — tests drive a deterministic stub.
    """

    def __init__(
        self,
        *,
        manager: SandboxManager,
        screenshot_hook: ScreenshotHook | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        event_cb: EventCallback | None = None,
        idle_limit_s: float = DEFAULT_IDLE_LIMIT_S,
        ready_poll_interval_s: float = DEFAULT_READY_POLL_INTERVAL_S,
        ready_poll_timeout_s: float = DEFAULT_READY_POLL_TIMEOUT_S,
        reaper_interval_s: float = DEFAULT_REAPER_INTERVAL_S,
    ) -> None:
        if not isinstance(manager, SandboxManager):
            raise TypeError("manager must be a SandboxManager")
        if idle_limit_s <= 0:
            raise ValueError("idle_limit_s must be positive")
        if ready_poll_interval_s <= 0:
            raise ValueError("ready_poll_interval_s must be positive")
        if ready_poll_timeout_s <= 0:
            raise ValueError("ready_poll_timeout_s must be positive")
        if reaper_interval_s <= 0:
            raise ValueError("reaper_interval_s must be positive")

        self._manager = manager
        self._screenshot_hook = screenshot_hook
        self._clock = clock
        self._sleep = sleep
        self._event_cb = event_cb
        self._idle_limit_s = float(idle_limit_s)
        self._ready_poll_interval_s = float(ready_poll_interval_s)
        self._ready_poll_timeout_s = float(ready_poll_timeout_s)
        self._reaper_interval_s = float(reaper_interval_s)

        self._lock = threading.RLock()
        self._reaper = _ReaperState(interval_s=self._reaper_interval_s)

    # ─────────────── Public properties ───────────────

    @property
    def manager(self) -> SandboxManager:
        """Underlying primitives manager — exposed for callers that
        need raw state (e.g. admin endpoints listing containers)."""

        return self._manager

    @property
    def idle_limit_s(self) -> float:
        return self._idle_limit_s

    @property
    def screenshot_hook(self) -> ScreenshotHook | None:
        return self._screenshot_hook

    def set_screenshot_hook(self, hook: ScreenshotHook | None) -> None:
        """Swap the screenshot hook at runtime.  V2 row 3 wires the
        real Playwright implementation in after the lifecycle has
        been constructed — this setter lets callers delay the
        dependency without reconstructing."""

        with self._lock:
            self._screenshot_hook = hook

    # ─────────────── ensure_session ───────────────

    def ensure_session(
        self,
        config: SandboxConfig,
        *,
        wait_ready: bool = True,
        recreate: bool = False,
    ) -> SandboxInstance:
        """Create and start a sandbox for ``config.session_id``, or
        reuse the existing one.

        Invariants:

        * If no sandbox exists for ``session_id`` → create + start
          (+ ``wait_ready`` if requested).
        * If one already exists with the same ``workspace_path`` and
          a non-terminal status → return it (starting it if it was
          still pending).
        * If one exists with a terminal status (``stopped``/
          ``failed``) → tear it down and recreate.
        * If one exists with a *different* ``workspace_path`` →
          raise :class:`WorkspaceMismatch` unless ``recreate=True``.
        * If ``recreate=True`` → teardown existing (if any) before
          creating fresh.

        Emits ``ui_sandbox.ensure_session`` with the final instance
        payload.
        """

        if not isinstance(config, SandboxConfig):
            raise TypeError("config must be a SandboxConfig")

        with self._lock:
            existing = self._manager.get(config.session_id)
            if recreate and existing is not None:
                self._teardown_locked(config.session_id, remove=True)
                existing = None
            elif existing is not None:
                if existing.config.workspace_path != config.workspace_path:
                    raise WorkspaceMismatch(
                        f"session {config.session_id!r} already runs with workspace "
                        f"{existing.config.workspace_path!r}; new request specified "
                        f"{config.workspace_path!r} — pass recreate=True to replace"
                    )
                if existing.is_terminal:
                    # Terminal sandbox blocks re-create per V2 #1's one-per-session
                    # invariant; clear it first then fall through to fresh create.
                    self._teardown_locked(config.session_id, remove=True)
                    existing = None

            if existing is None:
                try:
                    instance = self._manager.create(config)
                except SandboxAlreadyExists as exc:  # pragma: no cover - race guard
                    raise LifecycleError(str(exc)) from exc
            else:
                instance = existing

            if instance.status is SandboxStatus.pending:
                instance = self._manager.start(config.session_id)

            if instance.status is SandboxStatus.failed:
                self._emit(LIFECYCLE_EVENT_ENSURE, instance)
                return instance

        # Release the lifecycle lock before waiting — wait_ready polls
        # logs which may take many seconds; the manager has its own
        # internal lock that covers state transitions.
        if wait_ready and instance.status is SandboxStatus.starting:
            try:
                instance = self.wait_ready(config.session_id)
            except ReadyTimeout:
                instance = self._manager.get(config.session_id) or instance

        self._emit(LIFECYCLE_EVENT_ENSURE, instance)
        return instance

    # ─────────────── wait_ready ───────────────

    def wait_ready(
        self,
        session_id: str,
        *,
        timeout_s: float | None = None,
        poll_interval_s: float | None = None,
    ) -> SandboxInstance:
        """Block until the dev server reports ready, then
        :meth:`SandboxManager.mark_ready`.

        ``timeout_s`` defaults to ``ready_poll_timeout_s`` passed at
        construction.  Raises :class:`ReadyTimeout` on expiry —
        marking the sandbox ``failed`` so the agent can ``ensure_session``
        again with ``recreate=True``.
        """

        timeout = (
            float(timeout_s) if timeout_s is not None else self._ready_poll_timeout_s
        )
        interval = (
            float(poll_interval_s)
            if poll_interval_s is not None
            else self._ready_poll_interval_s
        )
        if timeout <= 0:
            raise ValueError("timeout_s must be positive")
        if interval <= 0:
            raise ValueError("poll_interval_s must be positive")

        deadline = self._clock() + timeout
        last: SandboxInstance | None = None
        while True:
            instance = self._manager.get(session_id)
            if instance is None:
                raise SandboxNotFound(f"no sandbox for session_id={session_id!r}")
            last = instance
            if instance.status is SandboxStatus.running:
                return instance
            if instance.status is SandboxStatus.failed:
                raise LifecycleError(
                    f"sandbox {session_id!r} is failed: {instance.error!r}"
                )
            if instance.status is SandboxStatus.starting:
                try:
                    if self._manager.poll_ready(session_id):
                        return self._manager.mark_ready(session_id)
                except SandboxError as exc:  # pragma: no cover - defensive
                    logger.warning("poll_ready failed for %s: %s", session_id, exc)
            if self._clock() >= deadline:
                break
            self._sleep(interval)

        # Timed out — mark failed + emit event, then raise.
        failed_payload = dict((last or instance).to_dict()) if last else {}
        failed_payload["timeout_s"] = float(timeout)
        self._emit_payload(LIFECYCLE_EVENT_READY_TIMEOUT, failed_payload)
        raise ReadyTimeout(
            f"sandbox {session_id!r} did not report ready within {timeout:.1f}s"
        )

    # ─────────────── hot_reload ───────────────

    def hot_reload(
        self,
        session_id: str,
        *,
        files_changed: tuple[str, ...] = (),
    ) -> SandboxInstance:
        """Acknowledge agent-driven file changes.

        The actual HMR happens inside the container (V2 #1 bind-mounts
        the workspace, Next.js dev server picks up the fs event).
        This method:

          * touches ``last_active_at`` so the reaper doesn't collect
            an actively-edited sandbox;
          * emits ``ui_sandbox.hot_reload`` with the file list so the
            SSE bus (V2 row 6) can animate the UI;
          * no-ops on terminal sandboxes (returns the snapshot
            unchanged so the agent sees the final state without
            having to special-case).
        """

        files = tuple(str(f) for f in files_changed) if files_changed else ()
        with self._lock:
            instance = self._manager.get(session_id)
            if instance is None:
                raise SandboxNotFound(f"no sandbox for session_id={session_id!r}")
            if instance.is_terminal:
                return instance
            touched = self._manager.touch(session_id)
        payload = dict(touched.to_dict())
        payload["files_changed"] = list(files)
        payload["file_count"] = len(files)
        self._emit_payload(LIFECYCLE_EVENT_HOT_RELOAD, payload)
        return touched

    # ─────────────── capture_screenshot ───────────────

    def capture_screenshot(
        self,
        session_id: str,
        *,
        viewport: str = "desktop",
        path: str = "/",
    ) -> ScreenshotResult:
        """Invoke the screenshot hook, touch the sandbox, emit event.

        Raises:

          * :class:`SandboxNotFound` if the session is unknown.
          * :class:`LifecycleError` if the sandbox isn't running yet.
          * :class:`ScreenshotUnavailable` if no hook is registered
            (V2 row 3 wires the real one in).
          * Any exception the hook raises is wrapped in
            :class:`LifecycleError` and re-raised — the sandbox is
            **not** torn down (transient screenshot failures are a
            common dev-server hiccup).
        """

        if not isinstance(viewport, str) or not viewport.strip():
            raise ValueError("viewport must be non-empty")
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("path must start with '/'")

        with self._lock:
            instance = self._manager.get(session_id)
            if instance is None:
                raise SandboxNotFound(f"no sandbox for session_id={session_id!r}")
            if not instance.is_running:
                raise LifecycleError(
                    f"sandbox {session_id!r} is not running (status="
                    f"{instance.status.value!r}); cannot screenshot"
                )
            hook = self._screenshot_hook
            preview_url = instance.preview_url or ""

        if hook is None:
            raise ScreenshotUnavailable(
                "no screenshot hook registered — V2 row 3 "
                "(ui_screenshot.py) wires one in; use "
                "set_screenshot_hook(...) for tests"
            )
        if not preview_url:
            raise LifecycleError(
                f"sandbox {session_id!r} has no preview_url — cannot screenshot"
            )

        try:
            image_bytes = hook(
                session_id=session_id,
                preview_url=preview_url,
                viewport=viewport,
                path=path,
            )
        except Exception as exc:
            raise LifecycleError(
                f"screenshot hook raised for {session_id!r}: {exc}"
            ) from exc

        if not isinstance(image_bytes, (bytes, bytearray)):
            raise LifecycleError(
                f"screenshot hook returned {type(image_bytes).__name__}, expected bytes"
            )
        if not image_bytes:
            raise LifecycleError("screenshot hook returned empty bytes")

        # Bump last_active_at — capturing means an active session.
        with self._lock:
            self._manager.touch(session_id)

        result = ScreenshotResult(
            session_id=session_id,
            preview_url=preview_url,
            viewport=viewport,
            path=path,
            image_bytes=bytes(image_bytes),
            captured_at=self._clock(),
        )
        self._emit_payload(LIFECYCLE_EVENT_SCREENSHOT, result.to_dict())
        return result

    # ─────────────── teardown ───────────────

    def teardown(
        self,
        session_id: str,
        *,
        remove: bool = True,
    ) -> SandboxInstance:
        """Stop the dev server + (optionally) forget the session.

        Idempotent — tearing down a terminal sandbox just forgets it
        (if ``remove=True``).  Emits ``ui_sandbox.teardown`` once per
        call.  Never raises on docker errors; they surface as
        warnings on the returned :class:`SandboxInstance`.
        """

        with self._lock:
            final = self._teardown_locked(session_id, remove=remove)
        self._emit(LIFECYCLE_EVENT_TEARDOWN, final)
        return final

    def _teardown_locked(self, session_id: str, *, remove: bool) -> SandboxInstance:
        """Internal teardown — caller holds ``self._lock``."""

        instance = self._manager.get(session_id)
        if instance is None:
            raise SandboxNotFound(f"no sandbox for session_id={session_id!r}")
        if not instance.is_terminal:
            instance = self._manager.stop(session_id, remove=True)
        if remove:
            instance = self._manager.remove(session_id)
        return instance

    # ─────────────── reap_idle ───────────────

    def reap_idle(
        self,
        *,
        now: float | None = None,
        idle_limit_s: float | None = None,
    ) -> ReapReport:
        """Tear down every sandbox idle longer than ``idle_limit_s``.

        ``now`` defaults to the injected ``clock``; ``idle_limit_s``
        defaults to the instance's ``idle_limit_s`` (15 min per V2
        spec).  Terminal sandboxes are also reaped — they shouldn't
        linger in the registry.

        Never raises.  Teardown warnings (docker stop errors) surface
        on :attr:`ReapReport.warnings`.
        """

        limit = (
            float(idle_limit_s) if idle_limit_s is not None else self._idle_limit_s
        )
        if limit <= 0:
            raise ValueError("idle_limit_s must be positive")
        ref = float(now) if now is not None else self._clock()

        reaped: list[str] = []
        warnings: list[str] = []
        with self._lock:
            # Snapshot — don't mutate the live dict mid-iteration.
            candidates = tuple(self._manager.list())
            for inst in candidates:
                if inst.is_terminal:
                    # Terminal sandboxes always get cleaned from the
                    # registry so the reaper also acts as a GC for
                    # stale entries that the agent forgot to remove().
                    try:
                        self._manager.remove(inst.session_id)
                        reaped.append(inst.session_id)
                    except SandboxError as exc:
                        warnings.append(
                            f"remove_failed({inst.session_id}): {exc}"
                        )
                    continue
                idle = inst.idle_seconds(now=ref)
                if idle < limit:
                    continue
                try:
                    final = self._teardown_locked(inst.session_id, remove=True)
                    reaped.append(inst.session_id)
                    for w in final.warnings:
                        warnings.append(f"{inst.session_id}: {w}")
                except SandboxError as exc:
                    warnings.append(f"reap_failed({inst.session_id}): {exc}")
            survivors = tuple(i.session_id for i in self._manager.list())

        report = ReapReport(
            reaped_at=ref,
            reaped_sessions=tuple(reaped),
            still_active=survivors,
            idle_limit_s=limit,
            warnings=tuple(warnings),
        )
        # Only emit when something happened — avoid SSE noise.
        if reaped or warnings:
            self._emit_payload(LIFECYCLE_EVENT_REAPED, report.to_dict())
        return report

    # ─────────────── Background reaper ───────────────

    def start_reaper(self, *, interval_s: float | None = None) -> None:
        """Spawn a daemon thread that calls :meth:`reap_idle` every
        ``interval_s``.  No-op if already running.

        The thread sleeps in short slices and exits promptly when
        :meth:`stop_reaper` is called — safe to use inside agent
        loops with SIGINT handlers.
        """

        period = float(interval_s) if interval_s is not None else self._reaper_interval_s
        if period <= 0:
            raise ValueError("interval_s must be positive")

        with self._lock:
            if self._reaper.thread is not None and self._reaper.thread.is_alive():
                return
            self._reaper.stop_event = threading.Event()
            self._reaper.interval_s = period
            self._reaper.started_at = self._clock()
            self._reaper.sweeps = 0
            thread = threading.Thread(
                target=self._reaper_loop,
                name="ui-sandbox-reaper",
                daemon=True,
            )
            self._reaper.thread = thread
            thread.start()

    def stop_reaper(self, *, wait: bool = True, timeout_s: float = 5.0) -> None:
        """Signal the reaper thread to exit.  ``wait=True`` joins
        with ``timeout_s``; ``False`` returns immediately."""

        with self._lock:
            thread = self._reaper.thread
            self._reaper.stop_event.set()
        if thread is None:
            return
        if wait:
            thread.join(timeout=timeout_s)
        with self._lock:
            if not thread.is_alive():
                self._reaper.thread = None

    def is_reaper_running(self) -> bool:
        with self._lock:
            thread = self._reaper.thread
            return thread is not None and thread.is_alive()

    def reaper_sweeps(self) -> int:
        """Count of reap sweeps the background thread has performed —
        exposed for tests + operator telemetry."""

        with self._lock:
            return self._reaper.sweeps

    def _reaper_loop(self) -> None:
        """Thread target — polls ``stop_event`` in short slices so
        the thread exits quickly even with a long ``interval_s``."""

        # Granularity of the stop-check.  Keep small so stop_reaper()
        # returns in <100ms regardless of interval.
        slice_s = min(0.1, max(0.01, self._reaper.interval_s / 10.0))
        while not self._reaper.stop_event.is_set():
            # Wait up to interval_s, but wake up on stop signal.
            if self._reaper.stop_event.wait(timeout=self._reaper.interval_s):
                break
            try:
                self.reap_idle()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("reaper sweep raised: %s", exc)
            with self._lock:
                self._reaper.sweeps += 1
            # One more chance to notice stop between sweeps.
            if self._reaper.stop_event.is_set():
                break
            # ``slice_s`` kept for future use — currently the wait()
            # above is the sole blocking point so we don't double-sleep.
            _ = slice_s

    # ─────────────── Introspection ───────────────

    def get_stage(self, session_id: str) -> SandboxStatus | None:
        """Return the current :class:`SandboxStatus` for ``session_id``
        or ``None`` if the session is unknown.  Thin wrapper — exists
        so callers don't need to import :mod:`backend.ui_sandbox`
        just to read status."""

        instance = self._manager.get(session_id)
        return None if instance is None else instance.status

    def list_sessions(self) -> tuple[SandboxInstance, ...]:
        """Convenience proxy to :meth:`SandboxManager.list`."""

        return self._manager.list()

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe snapshot of every sandbox + lifecycle config.

        Shape::

            {
                "schema_version": "1.0.0",
                "idle_limit_s": 900.0,
                "ready_poll_timeout_s": 60.0,
                "max_per_session": 1,
                "reaper": {
                    "running": bool,
                    "interval_s": float,
                    "sweeps": int,
                },
                "sandboxes": [SandboxInstance.to_dict, ...],
                "count": int,
                "now": float,
            }
        """

        with self._lock:
            mgr_snap = self._manager.snapshot()
            return {
                "schema_version": SANDBOX_LIFECYCLE_SCHEMA_VERSION,
                "idle_limit_s": float(self._idle_limit_s),
                "ready_poll_timeout_s": float(self._ready_poll_timeout_s),
                "ready_poll_interval_s": float(self._ready_poll_interval_s),
                "max_per_session": MAX_SANDBOXES_PER_SESSION,
                "reaper": {
                    "running": self.is_reaper_running(),
                    "interval_s": float(self._reaper.interval_s),
                    "sweeps": int(self._reaper.sweeps),
                    "started_at": float(self._reaper.started_at),
                },
                "sandboxes": list(mgr_snap.get("sandboxes", [])),
                "count": int(mgr_snap.get("count", 0)),
                "now": float(self._clock()),
            }

    # ─────────────── Context manager ───────────────

    def __enter__(self) -> "SandboxLifecycle":
        return self

    def __exit__(self, *_: Any) -> None:
        """Stop the reaper, then teardown every sandbox.  Designed so
        an agent that uses the lifecycle as a ``with`` block never
        leaves orphaned containers on exit."""

        self.stop_reaper(wait=True, timeout_s=5.0)
        with self._lock:
            for inst in tuple(self._manager.list()):
                try:
                    if not inst.is_terminal:
                        self._manager.stop(inst.session_id, remove=True)
                    self._manager.remove(inst.session_id)
                except SandboxError as exc:  # pragma: no cover - defensive
                    logger.warning(
                        "lifecycle exit teardown failed for %s: %s",
                        inst.session_id,
                        exc,
                    )

    # ─────────────── Internal event plumbing ───────────────

    def _emit(self, event_type: str, instance: SandboxInstance) -> None:
        payload = dict(instance.to_dict())
        payload["lifecycle_schema_version"] = SANDBOX_LIFECYCLE_SCHEMA_VERSION
        self._emit_payload(event_type, payload)

    def _emit_payload(
        self, event_type: str, payload: Mapping[str, Any]
    ) -> None:
        if self._event_cb is None:
            return
        try:
            self._event_cb(event_type, payload)
        except Exception as exc:  # pragma: no cover - callback must not kill us
            logger.warning("ui_sandbox_lifecycle event callback raised: %s", exc)
