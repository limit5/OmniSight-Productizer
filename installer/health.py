"""BS.4.5 — sidecar health endpoint (heartbeat + last_job + ``/health``).

Surface
───────
``GET /health`` on the sidecar's own embedded HTTP listener (default
``0.0.0.0:9090`` inside the container) returns::

    {
      "status":              "ok" | "stale",
      "ok":                  true | false,
      "sidecar_id":          "omnisight-installer-1",
      "protocol_version":    1,
      "started_at":          "2026-04-27T12:00:00.000000+00:00",
      "uptime_s":            42.351,
      "heartbeat_age_s":     0.118,
      "stale_threshold_s":   90.0,
      "poll_count":          17,
      "last_job": null | {
          "id":     "ij-abcd1234ef56",
          "state":  "claimed" | "completed" | "failed" | "cancelled",
          "at":     "2026-04-27T12:00:18.250000+00:00",
          "age_s":  4.82
      }
    }

* ``ok = True`` ⇒ HTTP 200 (last heartbeat within ``stale_threshold_s``).
* ``ok = False`` ⇒ HTTP 503 (loop appears wedged — docker
  ``HEALTHCHECK`` will mark the container unhealthy and the orchestrator
  will restart per the BS.4.6 compose service block's
  ``restart: unless-stopped``).

Why this is a *separate* endpoint from the backend's ``/healthz``
─────────────────────────────────────────────────────────────────
The sidecar is an outbound-only HTTP **client** of the backend (long-poll
``GET /installer/jobs/poll`` + ``POST /installer/jobs/{id}/progress``).
The backend has no in-band signal that the sidecar process is alive — a
sidecar deadlocked inside an install method's subprocess wait would stop
polling without any HTTP visibility. Docker / compose ``HEALTHCHECK``
needs an in-container HTTP probe to decide "is this PID still doing
work?" — that is what BS.4.5 ships.

Design decisions
────────────────
1. **stdlib ``http.server.ThreadingHTTPServer`` not FastAPI / Starlette**:
   the sidecar's only dependency policy (BS.4.1 / BS.4.2 / BS.4.3 /
   BS.4.4) is "stdlib only — every new wheel = pip-compile + image
   rebuild." A health endpoint is one route, no auth, no JSON body
   parsing — stdlib covers it without adding a single byte of dep.

2. **Daemon thread, not a separate process**: heartbeat state lives in
   the same process as the long-poll loop, so reading it across a fork
   would need IPC. A daemon thread shares the in-process ``HealthState``
   and dies cleanly when ``main()`` exits — no extra teardown coupling
   with SIGTERM.

3. **Threading.Lock for state mutation**: ``HealthState`` is touched by
   (a) the main thread (poll loop heartbeat + job claim/terminal
   records), (b) the progress-callback closure inside
   :func:`installer.main._build_progress_cb` (heartbeats during long
   installs), (c) the HTTP handler thread serving ``/health``. CPython's
   GIL makes individual attribute reads/writes atomic but compound
   updates ("read last_job_id, read last_job_state") need a lock to
   keep the snapshot self-consistent. We hold the lock only for tiny
   bookkeeping bursts; no I/O happens under the lock.

4. **Stale = 2× ``poll_timeout_s`` by default (cap floor 30s, max 600s)**:
   a healthy sidecar heartbeats either every poll round-trip (≤ 30s for
   the default config — long-poll returns 200/204 promptly) or on every
   progress emit during a long install. 90s is "double the worst-case
   idle gap" — a sidecar that misses heartbeats for 90s is genuinely
   stuck (subprocess deadlock, signal-handler livelock, or GIL stall).
   Configurable via ``OMNISIGHT_INSTALLER_HEALTH_STALE_S``.

5. **Bind to ``0.0.0.0`` not ``127.0.0.1``**: the Docker compose
   ``HEALTHCHECK`` directive runs *inside* the container so localhost
   binding works, but binding to ``0.0.0.0`` lets BS-future a sibling
   service in the same compose network (e.g. an out-of-band watchdog)
   probe the sidecar without needing ``docker exec`` gymnastics. The
   container's network namespace is the sole exposure surface — the
   compose service block (BS.4.6) does NOT publish a host port for this
   endpoint, so the host filesystem / external network never sees it.

6. **No POST / mutation routes**: ``/health`` is GET-only. A misbehaving
   probe / curious operator can query but cannot disturb sidecar state.

Module-global state audit (per ``docs/sop/implement_phase_step.md`` Step 1)
──────────────────────────────────────────────────────────────────────────
This module ships **zero** module-level mutable state. Constants
(``DEFAULT_HEALTH_HOST`` / ``DEFAULT_HEALTH_PORT`` / ``DEFAULT_STALE_S``
/ ``MIN_STALE_S`` / ``MAX_STALE_S``) are immutable. ``HealthState`` is
constructed by ``installer.main.main()`` and threaded into
``run_loop`` + ``_handle_claimed_job`` + ``_build_progress_cb``;
``start_health_server`` returns the live ``ThreadingHTTPServer`` and
its ``Thread`` so the caller owns the lifecycle. Each sidecar replica
runs in its own OS process with its own ``HealthState`` — answer #1
from the SOP rubric ("every worker derives its value from the same
source"; nothing crosses process boundaries here, the cross-replica
visibility is the operator's docker / compose monitoring layer).

Read-after-write timing audit
─────────────────────────────
N/A — purely in-process state; ``threading.Lock`` serialises the four
state mutations against the snapshot read.
"""

from __future__ import annotations

import http.server
import json
import logging
import os
import socketserver
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("omnisight.installer.health")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Constants — env-tunable via ``OMNISIGHT_INSTALLER_HEALTH_*``
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DEFAULT_HEALTH_HOST = "0.0.0.0"
DEFAULT_HEALTH_PORT = 9090
DEFAULT_STALE_S = 90.0
# Don't let an operator set the threshold so loose that a 30-min wedged
# sidecar still reports ``ok``, nor so tight that a normal poll cycle
# trips it.
MIN_STALE_S = 5.0
MAX_STALE_S = 600.0

# Allowed terminal labels for the ``last_job.state`` field. ``claimed``
# is the in-flight phase between long-poll-200 and method-dispatch
# return; the three terminal values mirror
# ``installer.methods.base.InstallState`` so an external observer
# eyeballing ``/health`` sees the same vocabulary as the install_jobs
# row in PG.
_VALID_LAST_JOB_STATES = frozenset({
    "claimed", "completed", "failed", "cancelled",
})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Health state (thread-safe via Lock — see module docstring §3)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass(frozen=True)
class HealthConfig:
    """Subset of installer Config relevant to ``/health`` rendering.

    Kept narrow so the factory call site reads what it actually depends
    on (sidecar identity + the staleness budget). The HTTP host/port are
    resolution concerns for ``start_health_server``, not state.
    """

    sidecar_id: str
    protocol_version: int
    stale_threshold_s: float = DEFAULT_STALE_S


class HealthState:
    """Mutable health snapshot shared between the main loop, the install
    progress callback, and the ``/health`` HTTP handler thread.

    Mutation methods (``heartbeat`` / ``record_job_claimed`` /
    ``record_job_terminal``) hold an internal :class:`threading.Lock` so
    the :func:`snapshot` reader always sees a consistent view (same
    ``last_job_id``/``state``/timestamp).
    """

    __slots__ = (
        "_lock", "_cfg",
        "_started_at_wall", "_started_at_mono",
        "_last_heartbeat_mono", "_poll_count",
        "_last_job_id", "_last_job_state",
        "_last_job_at_wall", "_last_job_at_mono",
    )

    def __init__(
        self,
        cfg: HealthConfig,
        *,
        started_at_wall: float | None = None,
        started_at_mono: float | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._cfg = cfg
        self._started_at_wall = (
            started_at_wall if started_at_wall is not None else time.time()
        )
        self._started_at_mono = (
            started_at_mono if started_at_mono is not None else time.monotonic()
        )
        # First heartbeat = startup time; otherwise a process that
        # crashes during the first poll-attempt would render as "stale"
        # immediately.
        self._last_heartbeat_mono = self._started_at_mono
        self._poll_count = 0
        self._last_job_id: str | None = None
        self._last_job_state: str | None = None
        self._last_job_at_wall: float | None = None
        self._last_job_at_mono: float | None = None

    # ── mutation ────────────────────────────────────────────────────

    def heartbeat(self, *, now_mono: float | None = None) -> None:
        """Mark the sidecar as alive at *now_mono* (monotonic clock).

        Called from :func:`installer.main.run_loop` at the top of every
        iteration AND from the progress-callback wrapper in
        :func:`installer.main._build_progress_cb` so a long install
        (subprocess held the loop for minutes) still beats while the
        method emits progress.
        """
        ts = now_mono if now_mono is not None else time.monotonic()
        with self._lock:
            self._last_heartbeat_mono = ts
            self._poll_count += 1

    def record_job_claimed(self, job_id: str) -> None:
        """Snapshot a job into ``last_job`` as ``state='claimed'``.

        Called when the long-poll returns 200 + a job row, before the
        install method runs. The terminal :func:`record_job_terminal`
        replaces this with the final state.
        """
        if not job_id:
            return
        with self._lock:
            self._last_job_id = str(job_id)
            self._last_job_state = "claimed"
            self._last_job_at_wall = time.time()
            self._last_job_at_mono = time.monotonic()

    def record_job_terminal(self, job_id: str, state: str) -> None:
        """Update ``last_job`` with the terminal state of an install.

        ``state`` must be one of ``completed`` / ``failed`` /
        ``cancelled`` (the three :class:`installer.methods.base.InstallState`
        values). Anything else is coerced to ``failed`` so the snapshot
        cannot leak a malformed label into operator dashboards.
        """
        if not job_id:
            return
        normalised = state if state in _VALID_LAST_JOB_STATES else "failed"
        with self._lock:
            self._last_job_id = str(job_id)
            self._last_job_state = normalised
            self._last_job_at_wall = time.time()
            self._last_job_at_mono = time.monotonic()

    # ── read ────────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        """Build a JSON-serialisable dict for ``/health`` rendering.

        Held under the lock for the duration of a few attribute reads;
        no I/O. The ``ok`` flag is computed on the fly so the staleness
        verdict reflects "now" at the moment the handler asks.
        """
        with self._lock:
            now_mono = time.monotonic()
            heartbeat_age = now_mono - self._last_heartbeat_mono
            uptime_s = now_mono - self._started_at_mono
            ok = heartbeat_age <= self._cfg.stale_threshold_s
            last_job: dict[str, Any] | None
            if self._last_job_id is None:
                last_job = None
            else:
                age = (
                    now_mono - self._last_job_at_mono
                    if self._last_job_at_mono is not None else 0.0
                )
                last_job = {
                    "id": self._last_job_id,
                    "state": self._last_job_state,
                    "at": _iso_utc(self._last_job_at_wall),
                    "age_s": round(age, 3),
                }
            return {
                "status": "ok" if ok else "stale",
                "ok": ok,
                "sidecar_id": self._cfg.sidecar_id,
                "protocol_version": int(self._cfg.protocol_version),
                "started_at": _iso_utc(self._started_at_wall),
                "uptime_s": round(uptime_s, 3),
                "heartbeat_age_s": round(heartbeat_age, 3),
                "stale_threshold_s": float(self._cfg.stale_threshold_s),
                "poll_count": int(self._poll_count),
                "last_job": last_job,
            }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP server — one route, GET /health
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _make_handler(state: HealthState) -> type[http.server.BaseHTTPRequestHandler]:
    """Build a request handler class bound to *state*.

    A class-factory rather than a closure so ``ThreadingHTTPServer``
    (which instantiates the handler per request) gets a fresh handler
    instance with the bound state attribute on every connection.
    """

    class _HealthHandler(http.server.BaseHTTPRequestHandler):
        # Bound state shared across all requests.
        health_state: HealthState = state

        def do_GET(self) -> None:  # noqa: N802 — http.server protocol
            # Strip query-string before path comparison (curl might
            # add ``?ts=…`` to bypass intermediary caches; we don't
            # depend on the value but accept it).
            path = self.path.split("?", 1)[0]
            if path != "/health":
                self._send_json(404, {"error": "not_found", "path": path})
                return
            snap = self.health_state.snapshot()
            self._send_json(200 if snap["ok"] else 503, snap)

        def do_HEAD(self) -> None:  # noqa: N802
            # Some probes (curl --head, k8s) issue HEAD; mirror GET's
            # status code without a body.
            path = self.path.split("?", 1)[0]
            if path != "/health":
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            snap = self.health_state.snapshot()
            self.send_response(200 if snap["ok"] else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # Probe disconnected before we could write; nothing to
                # do — health checkers do this routinely.
                pass

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, N802
            # Silence the default per-request stderr line (compose
            # `HEALTHCHECK` polls every 30s; we'd flood the log).
            # Errors still go through ``log_error`` (overridden below).
            return

        def log_error(self, format: str, *args: Any) -> None:  # noqa: A002, N802
            # Surface 4xx/5xx at WARNING — they should be rare given
            # the single-route surface, so worth seeing if they happen.
            logger.warning("health http: " + format, *args)

    return _HealthHandler


class _ThreadingHealthServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """One server, daemon worker threads, allow_reuse_address.

    ``ThreadingMixIn.daemon_threads = True`` is critical: any handler
    thread still running when ``main()`` returns must not block process
    exit (a 30s SIGTERM grace must shut the sidecar cleanly even if a
    probe is mid-response).
    """

    daemon_threads = True
    allow_reuse_address = True
    # Default 5 — we don't expect a stampede of probes; one or two
    # concurrent connections is the realistic ceiling.
    request_queue_size = 5


def start_health_server(
    state: HealthState,
    *,
    host: str = DEFAULT_HEALTH_HOST,
    port: int = DEFAULT_HEALTH_PORT,
) -> tuple[_ThreadingHealthServer, threading.Thread]:
    """Start the embedded ``/health`` HTTP listener in a daemon thread.

    Returns ``(server, thread)`` so the caller can ``server.shutdown()``
    on graceful teardown (not strictly necessary because the thread is
    a daemon, but explicit shutdown lets ``main()`` close the listening
    socket promptly during SIGTERM).
    """
    handler_cls = _make_handler(state)
    server = _ThreadingHealthServer((host, port), handler_cls)
    thread = threading.Thread(
        target=server.serve_forever,
        name="installer-health-http",
        daemon=True,
    )
    thread.start()
    actual_host, actual_port = server.server_address[:2]
    logger.info(
        "health endpoint listening on http://%s:%d/health "
        "(stale_threshold_s=%.1f)",
        actual_host, actual_port, state._cfg.stale_threshold_s,
    )
    return server, thread


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Env-driven factory used by installer.main.main()
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def load_health_config(
    *,
    sidecar_id: str,
    protocol_version: int,
    poll_timeout_s: int,
) -> HealthConfig:
    """Resolve the staleness threshold from env / poll-timeout heuristic.

    Resolution order:

    1. ``OMNISIGHT_INSTALLER_HEALTH_STALE_S`` — explicit operator override.
    2. ``2 × poll_timeout_s`` — generous "should have heard a heartbeat"
       window. With the default 30s poll timeout this lands at 60s, so
       we floor at the :data:`DEFAULT_STALE_S` (90s) to leave headroom
       for a slow first-tick install.
    3. Floor / cap to ``[MIN_STALE_S, MAX_STALE_S]`` regardless of input
       so an operator typo can't disable the staleness gate entirely.
    """
    raw = os.environ.get("OMNISIGHT_INSTALLER_HEALTH_STALE_S", "").strip()
    derived: float
    if raw:
        try:
            derived = float(raw)
        except ValueError:
            logger.warning(
                "OMNISIGHT_INSTALLER_HEALTH_STALE_S=%r is not a number; "
                "falling back to default", raw,
            )
            derived = max(DEFAULT_STALE_S, 2.0 * float(poll_timeout_s))
    else:
        derived = max(DEFAULT_STALE_S, 2.0 * float(poll_timeout_s))

    derived = max(MIN_STALE_S, min(MAX_STALE_S, derived))
    return HealthConfig(
        sidecar_id=sidecar_id,
        protocol_version=int(protocol_version),
        stale_threshold_s=derived,
    )


def load_health_listen(
) -> tuple[str, int]:
    """Resolve ``(host, port)`` for the listener from env vars.

    Defaults bind to ``0.0.0.0:9090`` inside the container so the
    docker ``HEALTHCHECK`` directive (BS.4.6 will land it) can probe
    over the loopback interface without needing a published host port.
    """
    host = (
        os.environ.get("OMNISIGHT_INSTALLER_HEALTH_HOST")
        or DEFAULT_HEALTH_HOST
    )
    raw_port = os.environ.get("OMNISIGHT_INSTALLER_HEALTH_PORT") or ""
    try:
        port = int(raw_port) if raw_port else DEFAULT_HEALTH_PORT
    except ValueError:
        logger.warning(
            "OMNISIGHT_INSTALLER_HEALTH_PORT=%r is not an int; falling back to %d",
            raw_port, DEFAULT_HEALTH_PORT,
        )
        port = DEFAULT_HEALTH_PORT
    if port < 1 or port > 65535:
        logger.warning(
            "OMNISIGHT_INSTALLER_HEALTH_PORT=%d out of range; falling back to %d",
            port, DEFAULT_HEALTH_PORT,
        )
        port = DEFAULT_HEALTH_PORT
    return host, port


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _iso_utc(epoch: float | None) -> str | None:
    """Render a UNIX epoch as ISO-8601 UTC with microsecond precision."""
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


__all__ = [
    "DEFAULT_HEALTH_HOST",
    "DEFAULT_HEALTH_PORT",
    "DEFAULT_STALE_S",
    "MAX_STALE_S",
    "MIN_STALE_S",
    "HealthConfig",
    "HealthState",
    "load_health_config",
    "load_health_listen",
    "start_health_server",
]
