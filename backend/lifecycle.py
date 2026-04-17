"""G1 — Graceful shutdown coordinator.

Intercepts ``SIGTERM`` (and ``SIGINT``) so the FastAPI process can:

  1. flip an in-process "draining" flag → middleware rejects new
     traffic with ``503 Service Unavailable`` (retry-after hinted);
  2. flush SSE subscribers (notify + unsubscribe, close their queues);
  3. wait up to ``drain_timeout_seconds`` for in-flight HTTP requests
     to finish (tracked by the same middleware on every request);
  4. close the SQLite connection.

The module is deliberately stateless at import time and keeps a single
process-wide singleton so uvicorn's pre-fork / asgi test harness can
both import ``backend.main`` without surprising side-effects.

systemd pairs this with ``TimeoutStopSec=40`` and ``KillSignal=SIGTERM``
so the kernel gives us the full 30 s drain budget plus a small cushion
before escalating to ``SIGKILL``.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


DEFAULT_DRAIN_TIMEOUT_SECONDS = 30.0
# Poll cadence while waiting for in-flight requests to complete.
_DRAIN_POLL_INTERVAL = 0.05


class ShutdownCoordinator:
    """Single source of truth for "is this process draining?".

    Safe to reference synchronously from middleware — the flag is a
    plain ``bool`` attribute, no event loop required.  Async coroutines
    use ``wait_in_flight()`` which is driven off ``_completed`` (an
    asyncio Event reset whenever the counter returns to zero).
    """

    def __init__(self, drain_timeout_seconds: float = DEFAULT_DRAIN_TIMEOUT_SECONDS) -> None:
        self.drain_timeout_seconds = drain_timeout_seconds
        self.shutting_down: bool = False
        self._in_flight: int = 0
        # Set when in_flight returns to zero; fresh request clears it.
        self._completed: asyncio.Event | None = None
        self._signals_installed: bool = False

    # ── request accounting ────────────────────────────────────────
    def _ensure_event(self) -> asyncio.Event:
        if self._completed is None:
            self._completed = asyncio.Event()
            self._completed.set()
        return self._completed

    def request_started(self) -> None:
        event = self._ensure_event()
        self._in_flight += 1
        if self._in_flight > 0:
            event.clear()

    def request_finished(self) -> None:
        event = self._ensure_event()
        self._in_flight = max(0, self._in_flight - 1)
        if self._in_flight == 0:
            event.set()

    @property
    def in_flight(self) -> int:
        return self._in_flight

    # ── shutdown orchestration ────────────────────────────────────
    def begin_draining(self) -> None:
        """Flip the gate. Idempotent so duplicate signals are harmless."""
        if not self.shutting_down:
            logger.warning(
                "[lifecycle] shutdown signal received — draining "
                "(in_flight=%d, timeout=%.1fs)",
                self._in_flight, self.drain_timeout_seconds,
            )
        self.shutting_down = True

    async def wait_in_flight(self, timeout: float | None = None) -> bool:
        """Block until ``in_flight`` reaches zero or timeout elapses.

        Returns True if drained cleanly, False on timeout.  Uses a
        polling loop rather than a pure ``Event.wait()`` so a request
        that starts *after* the gate flipped (e.g. middleware races)
        still counts toward the drain.
        """
        budget = self.drain_timeout_seconds if timeout is None else timeout
        deadline = asyncio.get_event_loop().time() + budget
        while self._in_flight > 0:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                logger.error(
                    "[lifecycle] drain timed out after %.1fs with %d "
                    "in-flight request(s) still outstanding",
                    budget, self._in_flight,
                )
                return False
            await asyncio.sleep(min(_DRAIN_POLL_INTERVAL, remaining))
        return True

    def install_signal_handlers(self, loop: asyncio.AbstractEventLoop,
                                drain_cb: Callable[[], Awaitable[None]] | None = None) -> None:
        """Wire SIGTERM / SIGINT to the draining flag.

        ``drain_cb`` (optional) is scheduled on the loop when a signal
        arrives so the full shutdown sequence (flush SSE + close DB)
        runs even if uvicorn's own lifespan teardown is delayed.  The
        callback must be idempotent — both the signal handler and the
        lifespan ``__aexit__`` path can fire it.
        """
        if self._signals_installed:
            return

        def _handler(signum: int) -> None:
            logger.warning("[lifecycle] caught signal %d", signum)
            self.begin_draining()
            if drain_cb is not None:
                try:
                    loop.create_task(drain_cb())
                except RuntimeError as exc:
                    logger.debug("[lifecycle] drain_cb scheduling failed: %s", exc)

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _handler, sig)
            except (NotImplementedError, RuntimeError) as exc:
                # Windows / non-main thread → fall back to signal.signal.
                logger.debug(
                    "[lifecycle] add_signal_handler(%s) unavailable (%s); "
                    "falling back to signal.signal", sig, exc,
                )
                try:
                    signal.signal(sig, lambda s, _f: _handler(s))
                except (ValueError, OSError) as exc2:
                    logger.warning(
                        "[lifecycle] could not install handler for %s: %s",
                        sig, exc2,
                    )
        self._signals_installed = True

    # ── test helpers ──────────────────────────────────────────────
    def reset_for_tests(self) -> None:
        self.shutting_down = False
        self._in_flight = 0
        self._completed = None
        self._signals_installed = False


# Process-wide singleton.  Imported by main.py middleware + lifespan,
# and by tests that want to flip the gate without sending a real signal.
coordinator = ShutdownCoordinator()


async def flush_sse_subscribers() -> int:
    """Wake every SSE subscriber queue and unsubscribe it.

    Returns the number of subscribers that were flushed.  Best-effort:
    missing EventBus (import cycles during test teardown) is logged
    and ignored.
    """
    try:
        from backend.events import bus
    except Exception as exc:  # pragma: no cover
        logger.debug("[lifecycle] events.bus unavailable at shutdown: %s", exc)
        return 0

    subs = list(getattr(bus, "_subscribers", {}).keys())
    flushed = 0
    for q in subs:
        try:
            q.put_nowait({"event": "shutdown", "data": "{\"reason\":\"server_draining\"}"})
        except Exception:
            pass
        try:
            bus.unsubscribe(q)
        except Exception:
            pass
        flushed += 1
    if flushed:
        logger.info("[lifecycle] flushed %d SSE subscriber(s)", flushed)
    return flushed


async def graceful_shutdown(*, close_db: bool = True,
                            timeout: float | None = None) -> dict[str, object]:
    """Execute the full drain sequence.

    Order matters:
      1. ``begin_draining()`` — middleware starts 503-ing new requests.
      2. Flush SSE subscribers — long-lived streams would otherwise
         hold in-flight count > 0 forever.
      3. Wait for remaining in-flight requests (up to ``timeout``).
      4. Close the database connection.

    Returns a small dict describing what happened; useful for tests
    and observability without needing to parse logs.
    """
    coordinator.begin_draining()
    sse_flushed = await flush_sse_subscribers()
    drained = await coordinator.wait_in_flight(timeout=timeout)
    db_closed = False
    if close_db:
        try:
            from backend import db
            await db.close()
            db_closed = True
        except Exception as exc:
            logger.warning("[lifecycle] db.close() failed: %s", exc)
    return {
        "drained": drained,
        "sse_flushed": sse_flushed,
        "db_closed": db_closed,
        "in_flight_remaining": coordinator.in_flight,
    }
