"""G1 #5 — Real-SIGTERM graceful shutdown integration tests.

``backend/tests/test_lifecycle.py`` covers the coordinator's internal
API plus middleware behaviour when ``begin_draining()`` is called
*directly*.  This file exercises the missing end-to-end leg of HA-01:

    kernel sends SIGTERM  →  signal handler flips the drain flag  →
      requests already inside ``call_next`` finish with their normal
      status, while brand-new requests are short-circuited to
      ``503 Service Unavailable`` with ``Retry-After: 30``.

Why this is distinct from the existing lifecycle tests:

  * ``test_new_requests_get_503_while_draining`` flips the flag with
    ``coordinator.begin_draining()`` — it does NOT send a signal.
  * ``test_signal_handler_flips_drain_flag`` sends SIGTERM but has no
    ASGI traffic in flight, so it cannot prove that real requests
    survive.
  * The acceptance criterion on the TODO board is *"送 SIGTERM 時
    in-flight request 仍完成、新連線被拒"* — that requires a real
    signal AND real ASGI traffic mid-drain.

These tests add a thin controllable "slow" endpoint so an in-flight
request can be parked inside the middleware's ``call_next`` while the
test pokes SIGTERM at the running process, then verifies both legs of
the contract simultaneously.
"""

from __future__ import annotations

import asyncio
import os
import signal

import pytest

from backend import lifecycle


TEST_SLOW_PATH = "/__g1_sigterm_slow__"


# ──────────────────────────────────────────────────────────────
#  Fixtures
# ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_coordinator():
    """Every test starts (and ends) with a clean drain gate."""
    lifecycle.coordinator.reset_for_tests()
    yield
    lifecycle.coordinator.reset_for_tests()


def _register_slow_route_once():
    """Inject a barrier-controlled endpoint into the shared FastAPI app.

    Tests share ``backend.main.app`` via the ``client`` fixture; adding
    the same route twice produces duplicate handlers.  Guard against
    it by probing ``app.router.routes`` before inserting.
    """
    from backend.main import app

    for r in app.router.routes:
        if getattr(r, "path", None) == TEST_SLOW_PATH:
            return

    async def _slow():
        # Pull the barrier off app.state so each test can swap in its
        # own event without mutating route state.
        barrier: asyncio.Event = app.state.__g1_slow_barrier__
        await barrier.wait()
        return {"ok": True}

    app.add_api_route(TEST_SLOW_PATH, _slow, methods=["GET"])


async def _wait_for(predicate, timeout: float = 2.0, interval: float = 0.01) -> bool:
    """Poll until ``predicate()`` is truthy or timeout elapses."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


# ──────────────────────────────────────────────────────────────
#  Unit — SIGTERM never clobbers the in-flight counter
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sigterm_preserves_in_flight_counter():
    """Flipping the drain gate via SIGTERM must leave the in-flight
    counter untouched — a real graceful shutdown has to wait for those
    requests, and resetting the counter would let the process exit
    immediately while traffic is still being served."""
    loop = asyncio.get_running_loop()
    c = lifecycle.coordinator
    c.install_signal_handlers(loop)

    c.request_started()
    c.request_started()
    assert c.in_flight == 2
    assert c.shutting_down is False

    os.kill(os.getpid(), signal.SIGTERM)
    assert await _wait_for(lambda: c.shutting_down), "drain flag never flipped"

    # The counter survives SIGTERM — no reset, no double-decrement.
    assert c.in_flight == 2

    # Clean up so wait_in_flight can drain.
    c.request_finished()
    c.request_finished()
    assert await c.wait_in_flight(timeout=1.0) is True


@pytest.mark.asyncio
async def test_sigint_also_flips_drain_flag():
    """HA-01 installs handlers for both SIGTERM and SIGINT (Ctrl-C).
    Both must route through the same drain gate — otherwise a local
    operator hitting Ctrl-C would bypass draining entirely."""
    loop = asyncio.get_running_loop()
    c = lifecycle.coordinator
    c.install_signal_handlers(loop)
    assert c.shutting_down is False

    os.kill(os.getpid(), signal.SIGINT)
    assert await _wait_for(lambda: c.shutting_down), "SIGINT didn't flip flag"


# ──────────────────────────────────────────────────────────────
#  Integration — the full SIGTERM contract against a live app
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sigterm_lets_in_flight_finish_and_rejects_new(client):
    """End-to-end HA-01 #5 contract.

    Timeline:
      t0  client fires GET /__g1_sigterm_slow__  (awaits barrier).
      t1  middleware's try-finally has bumped in_flight to 1.
      t2  kernel delivers SIGTERM → handler flips drain flag.
      t3  test sends a NEW request to a real route → expects 503 +
          Retry-After: 30 + Connection: close.
      t4  test pings /healthz → expects 200 (liveness exempt).
      t5  test releases barrier → the in-flight request returns 200
          with its normal payload (it was past the gate at t2, so
          middleware never short-circuits it).
      t6  in_flight is back to zero, ready for graceful_shutdown() to
          complete cleanly.
    """
    from backend.main import app

    _register_slow_route_once()
    barrier = asyncio.Event()
    app.state.__g1_slow_barrier__ = barrier

    loop = asyncio.get_running_loop()
    lifecycle.coordinator.install_signal_handlers(loop)
    assert lifecycle.coordinator.shutting_down is False

    # t0 — fire the slow request; it will block inside the handler.
    slow_task = asyncio.create_task(client.get(TEST_SLOW_PATH))

    # t1 — wait until the middleware has bumped the counter.
    assert await _wait_for(lambda: lifecycle.coordinator.in_flight >= 1), (
        "slow request never entered call_next"
    )
    in_flight_before_sigterm = lifecycle.coordinator.in_flight
    assert lifecycle.coordinator.shutting_down is False

    # t2 — real SIGTERM to ourselves.
    os.kill(os.getpid(), signal.SIGTERM)
    assert await _wait_for(lambda: lifecycle.coordinator.shutting_down), (
        "SIGTERM did not flip drain flag"
    )

    # The in-flight slow request is still parked inside the middleware.
    assert lifecycle.coordinator.in_flight == in_flight_before_sigterm

    # t3 — new traffic is rejected with the documented contract.
    r_new = await client.get("/api/v1/agents", follow_redirects=False)
    assert r_new.status_code == 503, (
        f"expected 503 for new request during drain, got {r_new.status_code}"
    )
    assert r_new.headers.get("Retry-After") == "30"
    assert r_new.headers.get("Connection") == "close"
    assert "shutting down" in r_new.json()["detail"].lower()

    # t4 — liveness probe must stay alive during drain so orchestrators
    # don't escalate to SIGKILL before the drain window elapses.
    r_live = await client.get("/healthz")
    assert r_live.status_code == 200
    assert r_live.json().get("live") is True

    # t5 — release the barrier; already-in-flight request finishes 200.
    barrier.set()
    r_slow = await slow_task
    assert r_slow.status_code == 200, (
        f"in-flight request must finish normally; got {r_slow.status_code}"
    )
    assert r_slow.json() == {"ok": True}

    # t6 — counter has returned to its pre-request baseline.
    assert await _wait_for(lambda: lifecycle.coordinator.in_flight == 0)


@pytest.mark.asyncio
async def test_sigterm_then_graceful_shutdown_drains_cleanly(client):
    """After SIGTERM, the lifespan teardown calls ``graceful_shutdown``.
    That orchestrator must:
      * flush SSE subscribers (so long-poll streams unblock);
      * wait for in-flight requests to finish;
      * report drained=True with in_flight_remaining=0.

    This is the shape the systemd+docker-compose orchestrators rely
    on: TimeoutStopSec=40 budgets 30 s drain + 10 s slack; if drained
    comes back False we know the process needed SIGKILL escalation.
    """
    from backend.main import app
    from backend.events import bus

    _register_slow_route_once()
    barrier = asyncio.Event()
    app.state.__g1_slow_barrier__ = barrier

    loop = asyncio.get_running_loop()
    lifecycle.coordinator.install_signal_handlers(loop)

    # Register a fake SSE subscriber so flush has something to do.
    bus.subscribe()

    slow_task = asyncio.create_task(client.get(TEST_SLOW_PATH))
    assert await _wait_for(lambda: lifecycle.coordinator.in_flight >= 1)

    os.kill(os.getpid(), signal.SIGTERM)
    assert await _wait_for(lambda: lifecycle.coordinator.shutting_down)

    # Release the barrier a hair after graceful_shutdown starts waiting.
    async def _release_after_drain_begins():
        await asyncio.sleep(0.05)
        barrier.set()

    asyncio.create_task(_release_after_drain_begins())

    result = await lifecycle.graceful_shutdown(close_db=False, timeout=2.0)

    assert result["drained"] is True, (
        "graceful_shutdown timed out — in-flight never completed"
    )
    assert result["sse_flushed"] >= 1, (
        "at least the one subscriber we registered must be flushed"
    )
    assert result["db_closed"] is False  # close_db=False
    assert result["in_flight_remaining"] == 0

    # Bus is fully drained — no lingering subscribers to leak.
    assert bus.subscriber_count == 0

    # The in-flight request finished with its normal payload; it was
    # past the gate, so the drain flag did NOT poison it.
    r_slow = await slow_task
    assert r_slow.status_code == 200
    assert r_slow.json() == {"ok": True}


@pytest.mark.asyncio
async def test_sigterm_drain_timeout_reports_not_drained(client):
    """Drain has a finite budget.  If a buggy handler hangs past
    ``timeout``, graceful_shutdown must surface drained=False with a
    non-zero in_flight_remaining so ops dashboards can alarm on it
    (rather than silently SIGKILL-escalating)."""
    from backend.main import app

    _register_slow_route_once()
    barrier = asyncio.Event()  # never released within the test body.
    app.state.__g1_slow_barrier__ = barrier

    loop = asyncio.get_running_loop()
    lifecycle.coordinator.install_signal_handlers(loop)

    slow_task = asyncio.create_task(client.get(TEST_SLOW_PATH))
    assert await _wait_for(lambda: lifecycle.coordinator.in_flight >= 1)

    os.kill(os.getpid(), signal.SIGTERM)
    assert await _wait_for(lambda: lifecycle.coordinator.shutting_down)

    # Tight timeout so the test itself doesn't sit for 30 s.
    result = await lifecycle.graceful_shutdown(close_db=False, timeout=0.2)
    assert result["drained"] is False
    assert result["in_flight_remaining"] >= 1

    # Cleanup: release the stuck handler and await its task so pytest
    # doesn't warn about "Task was destroyed but it is pending".
    barrier.set()
    await slow_task


@pytest.mark.asyncio
async def test_sigterm_blocks_new_connection_without_any_in_flight(client):
    """Minimal SIGTERM-only path: no concurrent traffic, no slow route.
    Send SIGTERM, then fire a single request → must receive the 503
    contract.  Isolates the "new connection rejected" leg from the
    "in-flight completes" leg so a regression in either can be
    pinpointed."""
    loop = asyncio.get_running_loop()
    lifecycle.coordinator.install_signal_handlers(loop)
    assert lifecycle.coordinator.shutting_down is False

    os.kill(os.getpid(), signal.SIGTERM)
    assert await _wait_for(lambda: lifecycle.coordinator.shutting_down)

    # Any non-exempt route will do — /api/v1/agents is a known-live one.
    r = await client.get("/api/v1/agents", follow_redirects=False)
    assert r.status_code == 503
    assert r.headers.get("Retry-After") == "30"
    assert r.headers.get("Connection") == "close"
    assert "shutting down" in r.json()["detail"].lower()
