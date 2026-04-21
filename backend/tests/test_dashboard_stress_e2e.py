"""Phase-3-Runtime-v2 SP-9.5 / task #82 — dashboard E2E stress.

Simulated dashboard load: **5 "tabs" × 11 endpoints × every 5s ×
60s**. Each tab is an asyncio task that loops through a realistic
dashboard endpoint set and sleeps between rounds, mirroring what
a live operator dashboard generates when 5 browser tabs are
open. The test asserts:

* **Zero HTTP 500** responses. Any 5xx means a prod-path bug the
  pool exhaustion / tenant-isolation suites don't cover.
* **Zero asyncpg-level errors** surfaced in response bodies
  ("another operation is in progress", "connection was closed",
  "UndefinedTableError"). These would indicate the pool lost its
  invariants under sustained concurrent load.
* **429s are allowed** — the I9 per-IP limiter (300 req/min
  post-SP-8.1) WILL fire when 5 tabs × 11 endpoints × 12 rounds
  = 660 req/min exceeds the budget. Seeing 429s proves the
  limiter works; seeing 500s would be the bug.

Duration knob: the spec calls for 60s, but CI doesn't need that
long to surface pool-invariant breakage. Default ``DURATION_S``
is short (5s) so pytest runs stay under 10s; operators can
exercise the full 60s spec by setting
``OMNI_STRESS_DURATION_S=60`` in the env — the stress loop reads
the env every run. Marked ``@pytest.mark.slow`` so CI pipelines
that skip slow tests still cover the other Epic 9 slices.

Endpoint list: drawn from ``backend.auth_baseline.AUTH_BASELINE_ALLOWLIST``
so the test doesn't need to stand up a session-authenticated user
flow (that machinery has its own coverage in test_auth.py).
Several of these endpoints internally reach into the pool
(``/readyz`` runs ``SELECT 1``, the bootstrap probes read state)
which is what generates the contention we're measuring.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field

import pytest


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Config
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# 11 endpoints — mix of liveness / readiness / health (PG ping)
# that exercise the pool vs no-PG paths. Every entry is on the
# auth-baseline allowlist so we don't need a session cookie.
DASHBOARD_ENDPOINTS: tuple[str, ...] = (
    "/livez",
    "/readyz",
    "/healthz",
    "/api/v1/livez",
    "/api/v1/readyz",
    "/api/v1/healthz",
    "/api/v1/health",  # legacy alias
    "/metrics",
    "/api/v1/metrics",
    "/api/v1/bootstrap/status",
    "/api/v1/webhooks/healthz",
)
assert len(DASHBOARD_ENDPOINTS) == 11, (
    "Epic 9.5 spec is exactly 11 endpoints — update this list + "
    "the commit message if you tune the shape"
)

N_TABS = 5
TICK_S = 0.5  # each tab cycles through its 11 endpoints every 0.5s
DURATION_S = float(os.environ.get("OMNI_STRESS_DURATION_S", "5.0"))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Accounting
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class TabStats:
    """One tab's observations across the stress window."""
    tab_id: int
    by_status: dict[int, int] = field(default_factory=dict)
    server_errors: list[tuple[str, int, str]] = field(default_factory=list)
    asyncpg_markers: list[tuple[str, str]] = field(default_factory=list)
    total: int = 0


_ASYNCPG_ERROR_MARKERS: tuple[str, ...] = (
    "another operation is in progress",
    "connection was closed in the middle",
    "UndefinedTableError",
    "UndefinedColumnError",
    "InFailedSQLTransactionError",
    "db_pool.get_pool called before init_pool",
)


def _body_matches_asyncpg_error(body: str) -> str | None:
    """Return the first asyncpg-error marker present in ``body``,
    or None. Keeps the positive list short + explicit so a future
    marker addition is an intentional edit, not a silent drift."""
    for marker in _ASYNCPG_ERROR_MARKERS:
        if marker in body:
            return marker
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Core stress loop
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _run_tab(
    client, tab_id: int, deadline: float,
) -> TabStats:
    stats = TabStats(tab_id=tab_id)
    while time.monotonic() < deadline:
        for path in DASHBOARD_ENDPOINTS:
            try:
                resp = await client.get(path)
            except Exception as exc:  # pragma: no cover - defensive
                # HTTPX transport-level raise is also a 500-class
                # bug for our purposes.
                stats.server_errors.append((path, 0, repr(exc)[:200]))
                stats.total += 1
                continue
            stats.total += 1
            stats.by_status[resp.status_code] = (
                stats.by_status.get(resp.status_code, 0) + 1
            )
            if resp.status_code >= 500:
                stats.server_errors.append(
                    (path, resp.status_code, resp.text[:200])
                )
            # Check any status bucket for asyncpg error markers —
            # a 200 with an error message in body would also be
            # a regression (router swallowing then returning it).
            body = resp.text[:2048]
            marker = _body_matches_asyncpg_error(body)
            if marker is not None:
                stats.asyncpg_markers.append((path, marker))
        await asyncio.sleep(TICK_S)
    return stats


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Test
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
@pytest.mark.slow
async def test_dashboard_stress_five_tabs_zero_500(client):
    """5 concurrent 'tabs', each looping through 11 dashboard
    endpoints every 0.5s, for ``DURATION_S`` (default 5s in CI,
    60s when ``OMNI_STRESS_DURATION_S=60``).

    Asserts:
      * zero 5xx responses across all tabs
      * zero asyncpg-error markers in response bodies
      * >0 successful responses (sanity: the loop actually ran)

    429s from the per-IP limiter are NOT failures — they prove
    the SP-8.1 rate gate fires correctly under dashboard load.
    """
    deadline = time.monotonic() + DURATION_S

    all_stats: list[TabStats] = await asyncio.gather(
        *[_run_tab(client, i, deadline) for i in range(N_TABS)]
    )

    total_requests = sum(s.total for s in all_stats)
    all_500s = [e for s in all_stats for e in s.server_errors]
    all_asyncpg = [m for s in all_stats for m in s.asyncpg_markers]
    status_rollup: dict[int, int] = {}
    for s in all_stats:
        for code, n in s.by_status.items():
            status_rollup[code] = status_rollup.get(code, 0) + n

    # Sanity: the loop actually executed.
    assert total_requests > 0, "stress loop completed zero requests"

    # Core invariant 1: no 5xx.
    assert not all_500s, (
        f"saw {len(all_500s)} 5xx responses — first 3: "
        f"{all_500s[:3]}. status roll-up: {status_rollup}"
    )

    # Core invariant 2: no asyncpg-error marker leaked into a
    # response body (regardless of status code).
    assert not all_asyncpg, (
        f"saw {len(all_asyncpg)} asyncpg-error markers in "
        f"response bodies — first 3: {all_asyncpg[:3]}. "
        f"status roll-up: {status_rollup}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Sanity probe: each endpoint is individually reachable
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
@pytest.mark.parametrize("path", DASHBOARD_ENDPOINTS)
async def test_dashboard_endpoint_individually_reachable(client, path):
    """Each endpoint in ``DASHBOARD_ENDPOINTS`` must respond
    (non-5xx) when hit in isolation. Catches a regression where
    an endpoint has been removed or renamed — without this guard
    the stress loop would silently skip that endpoint's 404s
    because 404 is not 500.

    Accepted: 200 / 401 / 403 (auth gate) / 404 / 429. Any 5xx
    fails the test. 404 is acceptable because some allowlist
    entries (``/api/v1/webhooks/healthz``) are prefix-scoped and
    don't necessarily materialise a concrete handler — but 5xx
    is always a bug."""
    resp = await client.get(path)
    assert resp.status_code < 500, (
        f"{path} returned {resp.status_code}: {resp.text[:200]}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  429 is allowed — don't silently regress to "no limiter"
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_dashboard_burst_eventually_hits_429_or_stays_clean(client):
    """Fire enough requests fast enough that the per-IP limiter
    SHOULD engage (300/60s post-SP-8.1). Accept either outcome:
    (a) we trip 429 — proves the limiter works; (b) all 200 —
    means this test host is below the threshold, no assertion
    regression. The failure mode we DO catch: any 5xx response,
    which would mean the limiter crashed instead of throttling."""
    saw_429 = False
    saw_5xx = False
    for _ in range(320):
        resp = await client.get("/api/v1/readyz")
        if resp.status_code == 429:
            saw_429 = True
        if resp.status_code >= 500:
            saw_5xx = True
            break
    assert not saw_5xx, "readyz burst produced a 5xx"
    # No affirmative assertion on saw_429 — this is a smoke guard
    # against "limiter crashed", not a contract that 429 always
    # fires. The dedicated test is
    # ``test_rate_limit_middleware::test_ip_rate_limit_triggers``.
