"""W14.5 — Idle-timeout auto-kill reaper for the web-preview sidecars.

Periodic background sweep that walks every live
:class:`backend.web_sandbox.WebSandboxInstance` and calls
:meth:`backend.web_sandbox.WebSandboxManager.stop` on any sandbox whose
``last_request_at`` has fallen more than ``idle_timeout_s`` (default
**1800s = 30 minutes**) behind the wall clock. The manager's ``stop()``
already deletes the W14.3 CF Tunnel ingress rule and W14.4 CF Access
SSO app on its way out (best-effort, with per-instance warnings on
failure), so reaping an idle sandbox automatically frees the public
hostname slot too — that is the "刪 ingress" half of the W14.5 row.

Why a separate module (sibling to :mod:`backend.web_sandbox`)
=============================================================

* ``backend.web_sandbox`` is **synchronous** and request-scoped — every
  public method runs under the operator's HTTP handler. Adding a
  long-lived background loop inside the manager would mix two
  responsibilities (per-launch lifecycle vs. periodic sweep) and force
  every test fixture to wrestle with a daemon thread it does not need.
* The reaper does **not** need access to the manager's internals.
  ``manager.list()`` + ``manager.stop()`` are the only surfaces it
  touches; both are already public + thread-safe (the manager's RLock
  serialises every state mutation). Keeping the reaper at arm's length
  means a future per-tenant scheduler / W14.10-PG-backed audit log can
  swap implementations without rewriting the launcher.
* Daemon-thread shape (rather than asyncio task) matches the manager's
  sync RLock semantics: the reaper acquires the lock through
  ``manager.list()`` like every other caller, no awkward sync→async
  bridge required. The Y6 row-6 ``backend.workspace_gc`` reaper went
  the asyncio route because it sits inside the FastAPI lifespan and
  competes for the same event loop as the rest of the app — W14.5's
  reaper is independent, so a stdlib ``threading.Thread`` is the
  minimum-viable shape.

Row boundary
============

W14.5 owns:

  1. The reaper module + its config dataclass + 2 pure helpers
     (:func:`compute_idle_seconds`, :func:`select_idle_workspaces`).
  2. The daemon-thread runner (``start`` / ``stop`` / ``tick`` / ``is_running``).
  3. Wiring inside :func:`backend.routers.web_sandbox.get_manager` —
     when the manager is constructed, the reaper is also constructed
     and its thread started. Tests inject a fake reaper through
     :func:`backend.routers.web_sandbox.set_reaper_for_tests`.

W14.5 explicitly does NOT own:

  - The actual ``docker stop`` / ingress delete / access app delete —
    that is :meth:`backend.web_sandbox.WebSandboxManager.stop`'s job
    (already W14.2 / W14.3 / W14.4).
  - Cross-worker reaping of orphaned containers (e.g. uvicorn worker A
    crashed mid-launch and worker B's manager has no record). That
    becomes possible once W14.10 lands the PG-backed
    ``web_sandbox_instances`` table — a single orchestrator-level reaper
    can then scan PG instead of in-process state.
  - PEP HOLD on first touch (W14.8) / cgroup OOM-kill detection (W14.9).
  - Frontend signal that a sandbox was idle-killed — the W14.6
    ``<LivePreviewPanel/>`` will read ``killed_reason`` from the
    manager snapshot and render the ``"Auto-stopped after 30m idle"``
    notice.

Module-global state audit (SOP §1)
==================================

The reaper holds a per-instance ``threading.Thread`` + ``threading.Event``
stop signal. Both are *bound to the reaper instance*, not to a module-
level singleton — every uvicorn worker constructs its own
:class:`WebSandboxIdleReaper` (one per :class:`WebSandboxManager`), and
each worker reaps **only the sandboxes its own manager launched**.

Cross-worker consistency answer = SOP §1 type **#3 (intentionally per-
worker)**: under ``uvicorn --workers N`` each worker has its own dict of
launched sandboxes; each worker is the only one that holds container
references for those sandboxes; each worker reaps its own. The case
"worker A crashed and left a sandbox behind" is **not** something worker
B can collect with this reaper — that is W14.10 territory (PG-backed
orchestrator-level reaper + ``docker ps`` reconciliation). Until W14.10
lands, an orphan after a worker crash gets cleaned up by the operator's
``docker rm`` or the next ``POST /preview`` for the same workspace_id
(which hits docker name-conflict and recovers via inspect — still a
running container, but at least an addressable one).

Read-after-write timing audit (SOP §2)
======================================

Fresh module — no compat→pool migration. The reaper's only mutation
contract: it takes a snapshot of ``manager.list()`` (which acquires the
manager's RLock), filters for idle workspaces, then calls
``manager.stop()`` on each. Between the list snapshot and the stop call
the manager's RLock is released, so a concurrent ``touch()`` may bump
``last_request_at`` *after* the reaper has already decided to collect
the sandbox. We accept the lossy case because:

  * The window is bounded by ``manager.stop()`` itself acquiring the
    RLock — at the moment of ``stop`` the instance state is re-checked,
    so a concurrent ``touch()`` either landed first (in which case the
    instance is now active but stop runs anyway) or lost the race (in
    which case stop is correct).
  * The cost of the lossy case is one false-positive idle-kill, which
    the operator notices on the next request (sandbox missing → POST
    /preview re-launches). The cost of locking the manager's RLock
    across the entire sweep would be a multi-second freeze of every
    operator request, which is much worse than one false-positive.
  * A future "double-check before stop" optimisation could re-read
    ``manager.get(workspace_id).idle_seconds()`` inside the stop path,
    but that is **not** owned by W14.5 — the manager's stop semantics
    are intentionally caller-driven. A better hook is W14.10's audit
    row: when the alembic 0059 row lands, the reaper's stop call is
    already correlated with ``killed_reason='idle_timeout'`` in the
    audit log, so a false-positive shows up there.

Compat fingerprint grep (SOP §3): N/A — fresh module, zero compat
artefacts. No DB connection / async commit / SQLite placeholder
patterns are even possible here.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from backend.web_sandbox import (
    WebSandboxError,
    WebSandboxInstance,
    WebSandboxManager,
    WebSandboxNotFound,
)


logger = logging.getLogger(__name__)


__all__ = [
    "IDLE_REAPER_SCHEMA_VERSION",
    "DEFAULT_IDLE_TIMEOUT_S",
    "DEFAULT_REAP_INTERVAL_S",
    "IDLE_TIMEOUT_REASON",
    "MIN_IDLE_TIMEOUT_S",
    "MIN_REAP_INTERVAL_S",
    "MAX_IDLE_TIMEOUT_S",
    "MAX_REAP_INTERVAL_S",
    "IdleReaperError",
    "IdleReaperConfig",
    "IdleReaperSweepResult",
    "WebSandboxIdleReaper",
    "compute_idle_seconds",
    "select_idle_workspaces",
]


#: Bump when :class:`IdleReaperConfig.to_dict()` /
#: :class:`IdleReaperSweepResult.to_dict()` shape changes. Audited by
#: the contract test suite for forward-compat parity with W14.10
#: alembic 0059 (which will read the same schema field for the audit
#: row's ``killed_reason='idle_timeout'`` rows).
IDLE_REAPER_SCHEMA_VERSION = "1.0.0"

#: Default idle window — 30 minutes per the W14.5 row spec
#: ("Idle timeout 30 分鐘 auto-kill"). Overridable via
#: ``OMNISIGHT_WEB_SANDBOX_IDLE_TIMEOUT_S``.
DEFAULT_IDLE_TIMEOUT_S = 1800.0

#: Default sweep cadence — 60s. Lower bound is soft; a high-frequency
#: sweep (e.g. 1s) is fine for tests but wastes CPU in production
#: because the manager's ``last_request_at`` only ticks on operator
#: HTTP requests, which are minute-scale. Overridable via
#: ``OMNISIGHT_WEB_SANDBOX_REAP_INTERVAL_S``.
DEFAULT_REAP_INTERVAL_S = 60.0

#: Reason string the reaper passes to :meth:`WebSandboxManager.stop`.
#: This shows up in :attr:`WebSandboxInstance.killed_reason` and the
#: future W14.10 audit row's ``killed_reason`` column. The W14.6
#: frontend will format it as "Auto-stopped after 30m idle".
IDLE_TIMEOUT_REASON = "idle_timeout"

#: Hard floor for ``idle_timeout_s``. A 30s cap is plenty for tests
#: that want to force an idle-kill quickly while still rejecting a
#: pathological 0/-1 setting that would collect every sandbox on
#: launch.
MIN_IDLE_TIMEOUT_S = 1.0

#: Hard floor for ``reap_interval_s``. 0.05s is the smallest interval
#: tests need to drive the reaper deterministically; production
#: deployments will always sit at the 60s default.
MIN_REAP_INTERVAL_S = 0.05

#: Hard ceiling for ``idle_timeout_s``. A 24h cap keeps a misconfigured
#: ``OMNISIGHT_WEB_SANDBOX_IDLE_TIMEOUT_S`` from silently disabling the
#: reaper (a 31536000s "1 year" value would never collect anything).
MAX_IDLE_TIMEOUT_S = 86_400.0

#: Hard ceiling for ``reap_interval_s``. 1h is the same scale as the
#: ``workspace_gc_interval_s`` default — much higher and the reaper
#: can't catch an idle sandbox within the row spec's 30-min window.
MAX_REAP_INTERVAL_S = 3600.0


# ───────────────────────────────────────────────────────────────────
#  Errors
# ───────────────────────────────────────────────────────────────────


class IdleReaperError(RuntimeError):
    """Base class for idle-reaper errors. Raised on configuration
    mistakes; never raised from inside :meth:`WebSandboxIdleReaper.tick`
    (per-workspace exceptions are swallowed and surfaced via the
    optional ``error_cb``)."""


# ───────────────────────────────────────────────────────────────────
#  Config dataclass
# ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class IdleReaperConfig:
    """Frozen settings snapshot for :class:`WebSandboxIdleReaper`.

    Values are validated at construction time so a malformed env knob
    fails fast instead of silently disabling the reaper. The
    :meth:`from_settings` classmethod is the production entry point —
    it reads the two ``OMNISIGHT_WEB_SANDBOX_IDLE_TIMEOUT_S`` /
    ``OMNISIGHT_WEB_SANDBOX_REAP_INTERVAL_S`` knobs from
    :class:`backend.config.Settings` and applies the same validation.
    """

    idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S
    reap_interval_s: float = DEFAULT_REAP_INTERVAL_S

    def __post_init__(self) -> None:
        if not isinstance(self.idle_timeout_s, (int, float)):
            raise IdleReaperError(
                f"idle_timeout_s must be a number, got {type(self.idle_timeout_s)!r}"
            )
        if not isinstance(self.reap_interval_s, (int, float)):
            raise IdleReaperError(
                f"reap_interval_s must be a number, got {type(self.reap_interval_s)!r}"
            )
        idle = float(self.idle_timeout_s)
        interval = float(self.reap_interval_s)
        if not (MIN_IDLE_TIMEOUT_S <= idle <= MAX_IDLE_TIMEOUT_S):
            raise IdleReaperError(
                f"idle_timeout_s out of range "
                f"[{MIN_IDLE_TIMEOUT_S}, {MAX_IDLE_TIMEOUT_S}]: {idle!r}"
            )
        if not (MIN_REAP_INTERVAL_S <= interval <= MAX_REAP_INTERVAL_S):
            raise IdleReaperError(
                f"reap_interval_s out of range "
                f"[{MIN_REAP_INTERVAL_S}, {MAX_REAP_INTERVAL_S}]: {interval!r}"
            )
        # Defensive: reaping every 1h with a 60s timeout would let a
        # sandbox sit idle 1h before being collected. Force the
        # interval to be ≤ idle_timeout so the spec's "≤ 30 min" cap
        # actually holds. The check is intentionally <= not < so
        # tests can drive the reaper at exactly the same cadence as
        # the timeout — that still bounds collection latency at one
        # interval after the timeout fires.
        if interval > idle:
            raise IdleReaperError(
                f"reap_interval_s ({interval}) must be <= idle_timeout_s "
                f"({idle}) — otherwise a sandbox can sit idle "
                f"{interval}s before the next sweep collects it."
            )
        object.__setattr__(self, "idle_timeout_s", idle)
        object.__setattr__(self, "reap_interval_s", interval)

    @classmethod
    def from_settings(cls, settings: Any) -> "IdleReaperConfig":
        """Build a config from :class:`backend.config.Settings`.

        Reads ``settings.web_sandbox_idle_timeout_s`` (env knob
        ``OMNISIGHT_WEB_SANDBOX_IDLE_TIMEOUT_S``) and
        ``settings.web_sandbox_reap_interval_s``
        (``OMNISIGHT_WEB_SANDBOX_REAP_INTERVAL_S``). Missing
        attributes fall back to the module defaults so a partial
        Settings stub (e.g. unit-test fixture that does not set every
        knob) does not break the reaper.
        """

        idle = getattr(settings, "web_sandbox_idle_timeout_s", DEFAULT_IDLE_TIMEOUT_S)
        interval = getattr(
            settings, "web_sandbox_reap_interval_s", DEFAULT_REAP_INTERVAL_S
        )
        return cls(idle_timeout_s=float(idle), reap_interval_s=float(interval))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": IDLE_REAPER_SCHEMA_VERSION,
            "idle_timeout_s": float(self.idle_timeout_s),
            "reap_interval_s": float(self.reap_interval_s),
        }


# ───────────────────────────────────────────────────────────────────
#  Sweep result dataclass
# ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class IdleReaperSweepResult:
    """Outcome of one :meth:`WebSandboxIdleReaper.tick` invocation.

    Frozen + JSON-safe so the future W14.10 audit row can persist a
    sweep summary without re-serialising. The reaper's daemon-thread
    loop discards each result after emitting the optional event
    callback; tests use the structured result to assert exact
    behaviour (which workspace ids were collected, which raised, how
    many were skipped).
    """

    started_at: float
    finished_at: float
    scanned: int
    reaped: tuple[str, ...]
    skipped_active: tuple[str, ...]
    skipped_terminal: tuple[str, ...]
    errors: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        # Defensive normalisation — accept lists in tests defensively
        # and pin to tuples so the dataclass stays hashable.
        object.__setattr__(self, "reaped", tuple(self.reaped))
        object.__setattr__(self, "skipped_active", tuple(self.skipped_active))
        object.__setattr__(self, "skipped_terminal", tuple(self.skipped_terminal))
        normalised_errors: list[tuple[str, str]] = []
        for entry in self.errors:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise IdleReaperError(
                    f"errors entries must be 2-tuples (workspace_id, message); "
                    f"got {entry!r}"
                )
            wid, msg = entry
            if not isinstance(wid, str) or not isinstance(msg, str):
                raise IdleReaperError(
                    f"errors entries must be (str, str); got {entry!r}"
                )
            normalised_errors.append((wid, msg))
        object.__setattr__(self, "errors", tuple(normalised_errors))

    @property
    def duration_s(self) -> float:
        return max(0.0, float(self.finished_at) - float(self.started_at))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": IDLE_REAPER_SCHEMA_VERSION,
            "started_at": float(self.started_at),
            "finished_at": float(self.finished_at),
            "duration_s": self.duration_s,
            "scanned": int(self.scanned),
            "reaped": list(self.reaped),
            "skipped_active": list(self.skipped_active),
            "skipped_terminal": list(self.skipped_terminal),
            "errors": [list(e) for e in self.errors],
        }


# ───────────────────────────────────────────────────────────────────
#  Pure helpers
# ───────────────────────────────────────────────────────────────────


def compute_idle_seconds(
    instance: WebSandboxInstance, *, now: float | None = None
) -> float:
    """Return seconds since ``instance.last_request_at``.

    Thin wrapper around :meth:`WebSandboxInstance.idle_seconds` that
    also rejects non-instance inputs at the type-error boundary (the
    reaper is the only caller; anything else is a contract bug worth
    failing loudly).
    """

    if not isinstance(instance, WebSandboxInstance):
        raise TypeError(
            f"instance must be WebSandboxInstance, got {type(instance)!r}"
        )
    return instance.idle_seconds(now=now)


def select_idle_workspaces(
    instances: Iterable[WebSandboxInstance],
    *,
    idle_timeout_s: float,
    now: float,
) -> tuple[str, ...]:
    """Return workspace_ids whose ``last_request_at`` is older than
    ``idle_timeout_s`` and whose status is non-terminal.

    Pure function — same input always returns the same output. The
    reaper's daemon-thread loop calls this on the snapshot returned
    by :meth:`WebSandboxManager.list`, so the function never touches
    the manager's internals.

    Filtering rules (in order):
      1. Skip terminal instances (``stopped`` / ``failed``) — they
         are already collected; calling ``stop()`` on them would
         be a no-op but still incurs a lock acquisition.
      2. Skip instances whose ``last_request_at`` is exactly 0 — that
         is the freshly-constructed default before the launcher
         records a clock tick. Picking those up would idle-kill every
         pre-launch instance instantly. (The current launcher always
         sets ``last_request_at`` on construction, so this branch is
         defence-in-depth.)
      3. Compare ``now - last_request_at >= idle_timeout_s``.

    The output is sorted by workspace_id so the daemon thread's
    behaviour is reproducible across runs (helpful for the W14.10
    audit row's "what got reaped this sweep" line).
    """

    if not isinstance(idle_timeout_s, (int, float)) or idle_timeout_s <= 0:
        raise ValueError(
            f"idle_timeout_s must be a positive number, got {idle_timeout_s!r}"
        )
    if not isinstance(now, (int, float)):
        raise ValueError(f"now must be a number, got {type(now)!r}")
    threshold = float(idle_timeout_s)
    ref = float(now)
    candidates: list[str] = []
    for inst in instances:
        if not isinstance(inst, WebSandboxInstance):
            raise TypeError(
                f"instances must yield WebSandboxInstance, got {type(inst)!r}"
            )
        if inst.is_terminal:
            continue
        if inst.last_request_at <= 0:
            continue
        if ref - inst.last_request_at >= threshold:
            candidates.append(inst.workspace_id)
    candidates.sort()
    return tuple(candidates)


# ───────────────────────────────────────────────────────────────────
#  Reaper
# ───────────────────────────────────────────────────────────────────


SweepEventCallback = Callable[[str, Mapping[str, Any]], None]
ErrorCallback = Callable[[str, BaseException], None]


class WebSandboxIdleReaper:
    """Daemon-thread reaper that idle-kills web-preview sidecars.

    Lifecycle:

      * ``__init__`` — bind to a manager + config; the thread is
        **not** started yet so unit tests can construct without
        leaking a thread.
      * :meth:`start` — spawn a daemon thread that calls :meth:`tick`
        every ``config.reap_interval_s`` until :meth:`stop` is called.
        Idempotent: a second ``start()`` is a no-op so the FastAPI
        lifespan can't accidentally fork a duplicate.
      * :meth:`stop` — set the stop event, join the thread (with a
        bounded grace period so a stuck ``manager.list()`` cannot
        wedge ``shutdown``).
      * :meth:`tick` — synchronous one-shot sweep. Public so tests
        can drive the reaper deterministically without spawning the
        thread; the daemon loop also calls it.

    Per-workspace exceptions are caught + surfaced via the optional
    ``error_cb`` (and via :class:`IdleReaperSweepResult.errors`); they
    never propagate out of :meth:`tick` so one bad sandbox cannot kill
    the whole reaper.
    """

    def __init__(
        self,
        *,
        manager: WebSandboxManager,
        config: IdleReaperConfig | None = None,
        clock: Callable[[], float] = time.time,
        event_cb: SweepEventCallback | None = None,
        error_cb: ErrorCallback | None = None,
        thread_name: str = "web-sandbox-idle-reaper",
    ) -> None:
        if not isinstance(manager, WebSandboxManager):
            raise TypeError(
                f"manager must be a WebSandboxManager, got {type(manager)!r}"
            )
        self._manager = manager
        self._config = config or IdleReaperConfig()
        if not isinstance(self._config, IdleReaperConfig):
            raise TypeError(
                f"config must be an IdleReaperConfig, got {type(self._config)!r}"
            )
        self._clock = clock
        self._event_cb = event_cb
        self._error_cb = error_cb
        self._thread_name = thread_name
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._sweep_count = 0
        self._last_result: IdleReaperSweepResult | None = None

    # ─────────────── Properties ───────────────

    @property
    def config(self) -> IdleReaperConfig:
        return self._config

    @property
    def is_running(self) -> bool:
        with self._lock:
            t = self._thread
            return t is not None and t.is_alive() and not self._stop_event.is_set()

    @property
    def sweep_count(self) -> int:
        with self._lock:
            return self._sweep_count

    @property
    def last_result(self) -> IdleReaperSweepResult | None:
        with self._lock:
            return self._last_result

    # ─────────────── Public API ───────────────

    def start(self) -> bool:
        """Spawn the daemon thread if not already running.

        Returns ``True`` when this call started the thread, ``False``
        when a thread was already running (idempotent).
        """

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name=self._thread_name,
                daemon=True,
            )
            self._thread.start()
        logger.info(
            "web_sandbox_idle_reaper: started "
            "(idle_timeout_s=%.0f reap_interval_s=%.2f)",
            self._config.idle_timeout_s,
            self._config.reap_interval_s,
        )
        return True

    def stop(self, *, timeout_s: float = 5.0) -> bool:
        """Signal the daemon thread to exit and wait for it.

        Returns ``True`` when the thread was joined cleanly within
        ``timeout_s``, ``False`` if the join timed out (the thread is
        still alive but we proceed anyway — daemon threads die with
        the process). Idempotent.
        """

        with self._lock:
            t = self._thread
            self._stop_event.set()
            if t is None or not t.is_alive():
                self._thread = None
                return True
        # Release the lock before joining so the daemon loop can
        # acquire it for ``last_result`` updates as it winds down.
        t.join(timeout=max(0.0, float(timeout_s)))
        with self._lock:
            joined = not t.is_alive()
            if joined:
                self._thread = None
        if not joined:
            logger.warning(
                "web_sandbox_idle_reaper: stop() timed out after %.1fs — "
                "thread is still alive but will die with the process",
                timeout_s,
            )
        return joined

    def tick(self) -> IdleReaperSweepResult:
        """Run one synchronous sweep and return its outcome.

        Idempotent across repeated invocations — every call
        re-snapshots :meth:`WebSandboxManager.list` and re-applies the
        idle filter. Per-workspace ``manager.stop`` exceptions are
        captured into the result's ``errors`` field; they do **not**
        abort the sweep, so one wedged container cannot starve the
        rest of the reaper queue.
        """

        started_at = float(self._clock())
        instances = tuple(self._manager.list())
        scanned = len(instances)
        idle_ids = select_idle_workspaces(
            instances,
            idle_timeout_s=self._config.idle_timeout_s,
            now=started_at,
        )
        skipped_active: list[str] = sorted(
            inst.workspace_id
            for inst in instances
            if not inst.is_terminal
            and inst.workspace_id not in idle_ids
        )
        skipped_terminal: list[str] = sorted(
            inst.workspace_id for inst in instances if inst.is_terminal
        )

        reaped: list[str] = []
        errors: list[tuple[str, str]] = []
        for workspace_id in idle_ids:
            try:
                self._manager.stop(workspace_id, reason=IDLE_TIMEOUT_REASON)
            except WebSandboxNotFound as exc:
                # The instance was removed between the snapshot and
                # the stop call — that is a benign race (e.g. operator
                # DELETE arrived first). Record it under errors so the
                # sweep summary still surfaces the unusual pairing,
                # but do not invoke the error_cb (it is not actionable).
                errors.append((workspace_id, f"not_found: {exc}"))
            except WebSandboxError as exc:
                errors.append((workspace_id, f"web_sandbox_error: {exc}"))
                self._call_error_cb(workspace_id, exc)
            except Exception as exc:  # pragma: no cover - defensive
                # Any other exception is unexpected; capture and keep
                # going so one bad sandbox does not stall the sweep.
                errors.append((workspace_id, f"unexpected: {exc}"))
                self._call_error_cb(workspace_id, exc)
                logger.warning(
                    "web_sandbox_idle_reaper: unexpected error stopping %s: %s",
                    workspace_id,
                    exc,
                )
            else:
                reaped.append(workspace_id)

        finished_at = float(self._clock())
        result = IdleReaperSweepResult(
            started_at=started_at,
            finished_at=finished_at,
            scanned=scanned,
            reaped=tuple(reaped),
            skipped_active=tuple(skipped_active),
            skipped_terminal=tuple(skipped_terminal),
            errors=tuple(errors),
        )
        with self._lock:
            self._sweep_count += 1
            self._last_result = result
        self._call_event_cb("web_sandbox_idle_reaper.sweep", result.to_dict())
        if reaped:
            logger.info(
                "web_sandbox_idle_reaper: collected %d idle sandbox(es): %s",
                len(reaped),
                ", ".join(reaped),
            )
        return result

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe view of the reaper's current state.

        Used by the future W14.6 frontend to render "next sweep in N
        seconds" + "last reaped: ws-X at TS"; until then, exposed
        purely for operator triage via the management endpoints W14
        will accrete.
        """

        with self._lock:
            last = self._last_result.to_dict() if self._last_result else None
            return {
                "schema_version": IDLE_REAPER_SCHEMA_VERSION,
                "config": self._config.to_dict(),
                "is_running": self.is_running,
                "sweep_count": self._sweep_count,
                "last_result": last,
            }

    # ─────────────── Internal ───────────────

    def _run_loop(self) -> None:
        interval = max(MIN_REAP_INTERVAL_S, float(self._config.reap_interval_s))
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "web_sandbox_idle_reaper: tick raised at top level: %s", exc
                )
                self._call_error_cb("__tick__", exc)
            # ``Event.wait`` returns True immediately when stop() is
            # called, so we don't burn the full interval on shutdown.
            self._stop_event.wait(timeout=interval)

    def _call_event_cb(
        self, event_type: str, payload: Mapping[str, Any]
    ) -> None:
        if self._event_cb is None:
            return
        try:
            self._event_cb(event_type, payload)
        except Exception as exc:  # pragma: no cover - callback must not kill us
            logger.warning(
                "web_sandbox_idle_reaper: event callback raised: %s", exc
            )

    def _call_error_cb(
        self, workspace_id: str, exc: BaseException
    ) -> None:
        if self._error_cb is None:
            return
        try:
            self._error_cb(workspace_id, exc)
        except Exception as cb_exc:  # pragma: no cover - callback must not kill us
            logger.warning(
                "web_sandbox_idle_reaper: error callback raised: %s", cb_exc
            )
