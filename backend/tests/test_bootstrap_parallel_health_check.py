"""L5 Step 4 — ``POST /api/v1/bootstrap/parallel-health-check`` tests.

Covers the wizard's Step-4 four-in-one readiness probe: backend,
frontend, DB migration, and Cloudflare tunnel connector. The four
checks fan out in parallel and the endpoint returns a single
aggregated response so the UI can light all four ticks from one
server observation.

Scenarios covered:

  * all green — backend 2xx + frontend reachable + DB migration
    invariants present + CF tunnel operator-skipped → ``all_green=True``
  * cf_tunnel tri-state: skipped (Step 3 marker) still counts as green
  * cf_tunnel tri-state: Step 3 not yet run → skipped (not red)
  * cf_tunnel green: marker configured + mocked CF connector online
  * cf_tunnel red: configured but CF API reports every connector
    ``is_pending_reconnect=True``
  * backend red on 5xx / transport failure still reports the other
    three checks (aggregated response is never 500)
  * frontend red on transport failure — redirect (3xx) still counts
    as reachable (the landing page doesn't have to fully render)
  * DB migration red path is hard to synthesise against a live
    aiosqlite conn without trashing state, so we exercise the happy
    path (all invariants present via the test fixture DB) and trust
    the PRAGMA code path via unit-level isolation.
  * endpoint is unauthenticated (mirrors the other wizard steps)
  * audit row ``bootstrap.parallel_health_check`` is emitted
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from backend import bootstrap as _boot


# ── helpers ───────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _FakeAsyncClient:
    """Minimal httpx.AsyncClient stand-in keyed by URL → response.

    Each URL can map to either an int status code or an Exception.
    The endpoint uses GET only, so we implement just that.
    """

    def __init__(self, url_map: dict, **_kw):
        self._url_map = url_map

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url: str):
        outcome = self._url_map.get(url)
        if outcome is None:
            # Default: connection-refused-ish error so tests that don't
            # stub a URL fail loud rather than silently greening.
            raise httpx.ConnectError(f"no stub for {url}")
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResponse(outcome)


def _patch_httpx(monkeypatch, url_map: dict):
    """Replace :class:`httpx.AsyncClient` inside the router module."""
    from backend.routers import bootstrap as _br

    def _factory(**kw):
        return _FakeAsyncClient(url_map, **kw)

    monkeypatch.setattr(_br.httpx, "AsyncClient", _factory)


@pytest.fixture()
def _marker_tmp():
    """Isolate the bootstrap marker + CF router state between tests."""
    from backend.routers import cloudflare_tunnel as _cft

    tmp = tempfile.mkdtemp(prefix="omnisight_parallel_")
    _boot._reset_for_tests(Path(tmp) / "marker.json")
    _cft._reset_for_tests()
    try:
        yield
    finally:
        _boot._reset_for_tests()
        _cft._reset_for_tests()


# ── all green: skipped CF tunnel path ────────────────────────────────


@pytest.mark.asyncio
async def test_parallel_health_check_all_green_with_skipped_cf(
    client, monkeypatch, _marker_tmp,
):
    """Backend 200 + frontend 200 + DB ok + CF skipped → all_green."""
    _boot.mark_cf_tunnel(skipped=True)
    _patch_httpx(monkeypatch, {
        "http://backend/healthz": 200,
        "http://frontend": 200,
    })

    r = await client.post(
        "/api/v1/bootstrap/parallel-health-check",
        json={
            "timeout_secs": 5,
            "backend_url": "http://backend/healthz",
            "frontend_url": "http://frontend",
        },
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["all_green"] is True
    assert body["backend"]["status"] == "green"
    assert body["frontend"]["status"] == "green"
    assert body["db_migration"]["status"] == "green"
    assert body["cf_tunnel"]["status"] == "skipped"
    assert body["cf_tunnel"]["ok"] is True
    assert isinstance(body["elapsed_ms"], int)


# ── cf tunnel: not yet run ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_parallel_health_check_cf_tunnel_not_yet_run_is_skipped(
    client, monkeypatch, _marker_tmp,
):
    """No Step-3 marker + no tunnel_id → skipped, not red."""
    _patch_httpx(monkeypatch, {
        "http://backend/healthz": 200,
        "http://frontend": 200,
    })

    r = await client.post(
        "/api/v1/bootstrap/parallel-health-check",
        json={
            "backend_url": "http://backend/healthz",
            "frontend_url": "http://frontend",
        },
        follow_redirects=False,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["cf_tunnel"]["status"] == "skipped"
    assert body["cf_tunnel"]["ok"] is True
    assert body["all_green"] is True


# ── cf tunnel: configured + connector online ─────────────────────────


@pytest.mark.asyncio
async def test_parallel_health_check_cf_tunnel_green_when_connector_online(
    client, monkeypatch, _marker_tmp,
):
    """configured marker + CF API reports online connector → cf green."""
    from backend.routers import cloudflare_tunnel as _cft

    _boot.mark_cf_tunnel(configured=True)
    _cft._set_state("tunnel_id", "t-abc")
    _cft._set_state("account_id", "acc-1")
    _cft._set_state("tunnel_name", "omnisight")
    _cft._set_state("encrypted_token", b"fake-encrypted")

    fake_tunnel = SimpleNamespace(
        id="t-abc",
        name="omnisight",
        status="healthy",
        connections=[{"is_pending_reconnect": False}],
    )

    class _FakeClient:
        async def list_tunnels(self, account_id, name=None):
            assert account_id == "acc-1"
            assert name == "omnisight"
            return [fake_tunnel]


    monkeypatch.setattr(_cft, "_client_from_stored", lambda: _FakeClient())
    _patch_httpx(monkeypatch, {
        "http://backend/healthz": 200,
        "http://frontend": 200,
    })

    r = await client.post(
        "/api/v1/bootstrap/parallel-health-check",
        json={
            "backend_url": "http://backend/healthz",
            "frontend_url": "http://frontend",
        },
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cf_tunnel"]["status"] == "green"
    assert body["cf_tunnel"]["ok"] is True
    assert body["all_green"] is True


# ── cf tunnel: configured but connector offline ──────────────────────


@pytest.mark.asyncio
async def test_parallel_health_check_cf_tunnel_red_when_connector_offline(
    client, monkeypatch, _marker_tmp,
):
    """configured + every connector pending_reconnect → cf red, all_green false."""
    from backend.routers import cloudflare_tunnel as _cft

    _boot.mark_cf_tunnel(configured=True)
    _cft._set_state("tunnel_id", "t-abc")
    _cft._set_state("account_id", "acc-1")
    _cft._set_state("tunnel_name", "omnisight")
    _cft._set_state("encrypted_token", b"fake-encrypted")

    fake_tunnel = SimpleNamespace(
        id="t-abc",
        name="omnisight",
        status="degraded",
        connections=[{"is_pending_reconnect": True}],
    )

    class _FakeClient:
        async def list_tunnels(self, account_id, name=None):
            return [fake_tunnel]

    monkeypatch.setattr(_cft, "_client_from_stored", lambda: _FakeClient())
    _patch_httpx(monkeypatch, {
        "http://backend/healthz": 200,
        "http://frontend": 200,
    })

    r = await client.post(
        "/api/v1/bootstrap/parallel-health-check",
        json={
            "backend_url": "http://backend/healthz",
            "frontend_url": "http://frontend",
        },
        follow_redirects=False,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["cf_tunnel"]["status"] == "red"
    assert body["cf_tunnel"]["ok"] is False
    assert body["all_green"] is False


# ── backend red ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parallel_health_check_backend_red_on_5xx(
    client, monkeypatch, _marker_tmp,
):
    """Backend /healthz returning 503 → backend red, aggregate still HTTP 200."""
    _boot.mark_cf_tunnel(skipped=True)
    _patch_httpx(monkeypatch, {
        "http://backend/healthz": 503,
        "http://frontend": 200,
    })

    r = await client.post(
        "/api/v1/bootstrap/parallel-health-check",
        json={
            "backend_url": "http://backend/healthz",
            "frontend_url": "http://frontend",
        },
        follow_redirects=False,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["backend"]["status"] == "red"
    assert "503" in (body["backend"]["detail"] or "")
    assert body["all_green"] is False


@pytest.mark.asyncio
async def test_parallel_health_check_backend_red_on_transport_failure(
    client, monkeypatch, _marker_tmp,
):
    """Backend connection refused → backend red with transport detail."""
    _boot.mark_cf_tunnel(skipped=True)
    _patch_httpx(monkeypatch, {
        "http://backend/healthz": httpx.ConnectError("Connection refused"),
        "http://frontend": 200,
    })

    r = await client.post(
        "/api/v1/bootstrap/parallel-health-check",
        json={
            "backend_url": "http://backend/healthz",
            "frontend_url": "http://frontend",
        },
        follow_redirects=False,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["backend"]["status"] == "red"
    assert "ConnectError" in (body["backend"]["detail"] or "")
    assert body["all_green"] is False


# ── frontend ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parallel_health_check_frontend_redirect_counts_as_ready(
    client, monkeypatch, _marker_tmp,
):
    """Next.js answering 302 (login redirect) still counts as reachable."""
    _boot.mark_cf_tunnel(skipped=True)
    _patch_httpx(monkeypatch, {
        "http://backend/healthz": 200,
        "http://frontend": 302,
    })

    r = await client.post(
        "/api/v1/bootstrap/parallel-health-check",
        json={
            "backend_url": "http://backend/healthz",
            "frontend_url": "http://frontend",
        },
        follow_redirects=False,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["frontend"]["status"] == "green"
    assert body["all_green"] is True


@pytest.mark.asyncio
async def test_parallel_health_check_frontend_red_on_5xx(
    client, monkeypatch, _marker_tmp,
):
    """Frontend returning 500 → frontend red."""
    _boot.mark_cf_tunnel(skipped=True)
    _patch_httpx(monkeypatch, {
        "http://backend/healthz": 200,
        "http://frontend": 500,
    })

    r = await client.post(
        "/api/v1/bootstrap/parallel-health-check",
        json={
            "backend_url": "http://backend/healthz",
            "frontend_url": "http://frontend",
        },
        follow_redirects=False,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["frontend"]["status"] == "red"
    assert body["all_green"] is False


# ── db migration ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parallel_health_check_db_migration_green_on_fresh_fixture(
    client, monkeypatch, _marker_tmp,
):
    """The test fixture runs ``db.init()`` so all invariant columns exist."""
    _boot.mark_cf_tunnel(skipped=True)
    _patch_httpx(monkeypatch, {
        "http://backend/healthz": 200,
        "http://frontend": 200,
    })

    r = await client.post(
        "/api/v1/bootstrap/parallel-health-check",
        json={
            "backend_url": "http://backend/healthz",
            "frontend_url": "http://frontend",
        },
        follow_redirects=False,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["db_migration"]["status"] == "green"
    assert body["db_migration"]["ok"] is True


# ── audit ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parallel_health_check_emits_audit_row(
    client, monkeypatch, _marker_tmp,
):
    """The call writes a ``bootstrap.parallel_health_check`` audit entry."""
    from backend import audit as _audit

    _boot.mark_cf_tunnel(skipped=True)
    _patch_httpx(monkeypatch, {
        "http://backend/healthz": 200,
        "http://frontend": 200,
    })

    r = await client.post(
        "/api/v1/bootstrap/parallel-health-check",
        json={
            "backend_url": "http://backend/healthz",
            "frontend_url": "http://frontend",
        },
        follow_redirects=False,
    )
    assert r.status_code == 200

    rows = await _audit.query(limit=50)
    hits = [
        row for row in rows
        if row.get("action") == "bootstrap.parallel_health_check"
    ]
    assert len(hits) >= 1


# ── unauthenticated: bare POST reaches the handler ───────────────────


@pytest.mark.asyncio
async def test_parallel_health_check_bare_post_works(
    client, monkeypatch, _marker_tmp,
):
    """Empty body falls back to env / settings defaults without 422."""
    _boot.mark_cf_tunnel(skipped=True)
    # The default URLs point at 127.0.0.1:8000 and localhost:3000 —
    # neither is running in the test; stub both so the probes return.
    _patch_httpx(monkeypatch, {
        "http://127.0.0.1:8000/api/v1/healthz": 200,
        "http://localhost:3000": 200,
    })

    r = await client.post(
        "/api/v1/bootstrap/parallel-health-check",
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["all_green"] is True


# ── input validation ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parallel_health_check_rejects_out_of_range_timeout(client):
    """``timeout_secs`` > max → HTTP 422 before any probe fires."""
    r = await client.post(
        "/api/v1/bootstrap/parallel-health-check",
        json={"timeout_secs": 9999},
        follow_redirects=False,
    )
    assert r.status_code == 422
