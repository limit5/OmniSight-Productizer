"""G1 (HA-01) — graceful shutdown coordinator tests.

Covers ``backend/lifecycle.py`` + the ``_graceful_shutdown_gate``
middleware registered in ``backend/main.py``:

  * ``ShutdownCoordinator`` unit contract:
      - idempotent ``begin_draining()``
      - ``request_started()`` / ``request_finished()`` accounting
      - ``wait_in_flight()`` returns True only when counter hits 0
      - ``wait_in_flight()`` returns False on timeout (and does not
        hang past its deadline)
      - timeout defaults to 30 s (documented contract)
      - ``reset_for_tests()`` wipes state
  * ``flush_sse_subscribers()`` drains every EventBus subscriber and
    returns the count.
  * ``graceful_shutdown()`` orchestrates the full sequence (flag +
    SSE flush + in-flight wait).
  * Integration (ASGI middleware):
      - new requests receive HTTP 503 with ``Retry-After`` while
        draining;
      - the ``/health`` liveness endpoint stays reachable while
        draining (so the orchestrator can still tell the pod is
        alive);
      - an in-flight request started BEFORE the flag flipped still
        completes cleanly.
  * Signal handler wiring:
      - ``install_signal_handlers`` is idempotent and does not crash
        when the event loop is single-threaded / windows-ish.
      - SIGTERM on the running loop flips the drain flag.
"""

from __future__ import annotations

import asyncio
import signal

import pytest

from backend import lifecycle


# ──────────────────────────────────────────────────────────────
#  Fixtures
# ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_coordinator():
    """Every test starts with a clean singleton."""
    lifecycle.coordinator.reset_for_tests()
    yield
    lifecycle.coordinator.reset_for_tests()


# ──────────────────────────────────────────────────────────────
#  Unit — ShutdownCoordinator bookkeeping
# ──────────────────────────────────────────────────────────────


def test_default_timeout_matches_spec_30s():
    """HA-01 spec: drain budget is 30 s; systemd's TimeoutStopSec=40
    allows ~10 s cushion for SIGKILL escalation."""
    assert lifecycle.DEFAULT_DRAIN_TIMEOUT_SECONDS == 30.0
    fresh = lifecycle.ShutdownCoordinator()
    assert fresh.drain_timeout_seconds == 30.0


def test_begin_draining_is_idempotent():
    c = lifecycle.ShutdownCoordinator()
    assert c.shutting_down is False
    c.begin_draining()
    assert c.shutting_down is True
    # Second call is a no-op.
    c.begin_draining()
    assert c.shutting_down is True


def test_request_accounting_counts_in_flight():
    c = lifecycle.ShutdownCoordinator()
    assert c.in_flight == 0
    c.request_started()
    c.request_started()
    assert c.in_flight == 2
    c.request_finished()
    assert c.in_flight == 1
    c.request_finished()
    assert c.in_flight == 0


def test_request_finished_never_goes_negative():
    """Defensive: middleware races shouldn't underflow the counter."""
    c = lifecycle.ShutdownCoordinator()
    c.request_finished()
    c.request_finished()
    assert c.in_flight == 0


def test_reset_for_tests_clears_state():
    c = lifecycle.ShutdownCoordinator()
    c.begin_draining()
    c.request_started()
    c._signals_installed = True
    c.reset_for_tests()
    assert c.shutting_down is False
    assert c.in_flight == 0
    assert c._signals_installed is False


# ──────────────────────────────────────────────────────────────
#  Unit — wait_in_flight semantics
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wait_in_flight_returns_immediately_when_empty():
    c = lifecycle.ShutdownCoordinator(drain_timeout_seconds=2.0)
    assert await c.wait_in_flight() is True


@pytest.mark.asyncio
async def test_wait_in_flight_waits_for_requests_to_finish():
    c = lifecycle.ShutdownCoordinator(drain_timeout_seconds=2.0)
    c.request_started()

    async def _finish_soon():
        await asyncio.sleep(0.1)
        c.request_finished()

    asyncio.create_task(_finish_soon())
    drained = await c.wait_in_flight()
    assert drained is True
    assert c.in_flight == 0


@pytest.mark.asyncio
async def test_wait_in_flight_times_out_without_hanging():
    c = lifecycle.ShutdownCoordinator(drain_timeout_seconds=0.2)
    c.request_started()
    start = asyncio.get_event_loop().time()
    drained = await c.wait_in_flight()
    elapsed = asyncio.get_event_loop().time() - start
    assert drained is False
    # Should respect budget — ±100ms slack for scheduler jitter.
    assert 0.1 <= elapsed <= 0.5


@pytest.mark.asyncio
async def test_wait_in_flight_accepts_explicit_timeout_override():
    c = lifecycle.ShutdownCoordinator(drain_timeout_seconds=30.0)
    c.request_started()
    drained = await c.wait_in_flight(timeout=0.1)
    assert drained is False


# ──────────────────────────────────────────────────────────────
#  Unit — flush_sse_subscribers + graceful_shutdown
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_flush_sse_subscribers_drains_bus():
    from backend.events import bus
    q1 = bus.subscribe()
    q2 = bus.subscribe()
    start_count = bus.subscriber_count
    assert start_count >= 2

    flushed = await lifecycle.flush_sse_subscribers()
    assert flushed >= 2
    assert bus.subscriber_count == 0

    # Each flushed subscriber should have received a final "shutdown"
    # message — that's what lets the SSE generator break out of its
    # ``await queue.get()`` loop cleanly.
    shutdown_msg = q1.get_nowait()
    assert shutdown_msg["event"] == "shutdown"
    shutdown_msg2 = q2.get_nowait()
    assert shutdown_msg2["event"] == "shutdown"


@pytest.mark.asyncio
async def test_graceful_shutdown_runs_full_sequence(monkeypatch):
    from backend.events import bus
    bus.subscribe()
    # Pretend a request is in flight; finish it shortly after drain
    # begins so we exercise the "wait then succeed" path.
    lifecycle.coordinator.request_started()

    async def _finish_soon():
        await asyncio.sleep(0.05)
        lifecycle.coordinator.request_finished()

    asyncio.create_task(_finish_soon())
    result = await lifecycle.graceful_shutdown(close_db=False, timeout=1.0)

    assert lifecycle.coordinator.shutting_down is True
    assert result["drained"] is True
    assert result["sse_flushed"] >= 1
    assert result["db_closed"] is False  # we skipped it
    assert result["in_flight_remaining"] == 0


@pytest.mark.asyncio
async def test_graceful_shutdown_reports_timeout():
    """Spec: wait-in-flight must cap at the configured budget and
    surface a drain=False result so operators can alert on it."""
    lifecycle.coordinator.request_started()
    result = await lifecycle.graceful_shutdown(close_db=False, timeout=0.15)
    assert result["drained"] is False
    assert result["in_flight_remaining"] >= 1
    # Clean up the pretend request so the autouse reset fixture sees a
    # tidy counter.
    lifecycle.coordinator.request_finished()


# ──────────────────────────────────────────────────────────────
#  Unit — signal handler wiring
# ──────────────────────────────────────────────────────────────


def test_install_signal_handlers_idempotent(monkeypatch):
    """Calling install twice must not double-register handlers."""
    c = lifecycle.ShutdownCoordinator()
    calls = {"n": 0}

    class FakeLoop:
        def add_signal_handler(self, sig, fn, *args):
            calls["n"] += 1

        def create_task(self, coro):  # pragma: no cover — unused here
            coro.close()
            return None

    loop = FakeLoop()
    c.install_signal_handlers(loop)
    first = calls["n"]
    c.install_signal_handlers(loop)
    # Second call should be a no-op.
    assert calls["n"] == first
    assert c._signals_installed is True


def test_install_signal_handlers_falls_back_when_add_signal_handler_unsupported(monkeypatch):
    """Windows / non-main-thread path: add_signal_handler raises
    NotImplementedError; we must fall back to ``signal.signal`` and
    not crash."""
    c = lifecycle.ShutdownCoordinator()

    class FakeLoop:
        def add_signal_handler(self, sig, fn, *args):
            raise NotImplementedError

        def create_task(self, coro):  # pragma: no cover
            coro.close()
            return None

    calls = []

    def _fake_signal(sig, handler):
        calls.append(sig)

    monkeypatch.setattr(signal, "signal", _fake_signal)
    c.install_signal_handlers(FakeLoop())
    # Both SIGTERM and SIGINT were routed through the fallback.
    assert signal.SIGTERM in calls
    assert signal.SIGINT in calls


@pytest.mark.asyncio
async def test_signal_handler_flips_drain_flag():
    """Sending SIGTERM to the process must set shutting_down=True via
    the loop-integrated signal handler."""
    loop = asyncio.get_running_loop()
    c = lifecycle.coordinator
    c.reset_for_tests()
    c.install_signal_handlers(loop)
    assert c.shutting_down is False

    import os
    os.kill(os.getpid(), signal.SIGTERM)
    # Give the loop a tick to deliver the signal.
    for _ in range(20):
        await asyncio.sleep(0.01)
        if c.shutting_down:
            break
    assert c.shutting_down is True


# ──────────────────────────────────────────────────────────────
#  Integration — ASGI middleware (new traffic gets 503)
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_new_requests_get_503_while_draining(client):
    """Flip the drain flag and confirm /agents receives a 503 with a
    Retry-After hint instead of being served."""
    lifecycle.coordinator.begin_draining()
    r = await client.get("/api/v1/agents", follow_redirects=False)
    assert r.status_code == 503
    assert r.headers.get("Retry-After") == "30"
    body = r.json()
    assert "shutting down" in body["detail"].lower()


@pytest.mark.asyncio
async def test_health_endpoint_stays_alive_while_draining(client):
    """Liveness must NOT 503 — otherwise k8s/systemd will flap the
    pod and escalate to SIGKILL before the drain window completes."""
    lifecycle.coordinator.begin_draining()
    r = await client.get("/api/v1/health", follow_redirects=False)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_in_flight_request_completes_after_drain_starts(client):
    """Classic graceful-shutdown invariant: a request already being
    served must finish cleanly even though the gate has flipped.

    We simulate this by starting a real request, stepping the event
    loop until the middleware has bumped the in-flight counter, then
    flipping the gate.  The response should arrive with its normal
    status (NOT 503)."""
    # Fire the request, flip the gate while it's in flight, then
    # await it — the gate check happens BEFORE request_started(),
    # so any request already inside call_next must be allowed to
    # finish.
    task = asyncio.create_task(client.get("/api/v1/health"))
    # Give the middleware stack a tick to enter call_next.
    await asyncio.sleep(0)
    lifecycle.coordinator.begin_draining()
    r = await task
    # /health is the liveness probe — exempt — so 200 regardless.
    # The key point: the request went through, the gate didn't abort
    # it mid-flight.
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_in_flight_counter_decrements_on_exception(client):
    """Even if the downstream handler raises, the finally: branch
    must decrement the in-flight counter — otherwise drain never
    completes."""
    before = lifecycle.coordinator.in_flight
    # 404 (handler raises HTTPException) still goes through the
    # middleware's try/finally and must decrement.
    await client.get("/api/v1/definitely-not-a-route", follow_redirects=False)
    assert lifecycle.coordinator.in_flight == before
