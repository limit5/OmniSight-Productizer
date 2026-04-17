"""L5 Step 4 — ``POST /api/v1/bootstrap/wait-ready`` polling tests.

Covers the wizard's readiness-polling endpoint that blocks until the
backend's ``/readyz`` probe reports green (HTTP 2xx) or ``timeout_secs``
elapses. The polling loop itself is monkeypatched via the module-level
``_probe_ready_once`` coroutine so tests can sequence probe outcomes
without spinning up a real server.

Scenarios covered:

  * immediate-ready short-circuit (single probe returns 200)
  * eventually-ready (first N probes fail, later probe returns 2xx)
  * all-probes-fail timeout → ``ready=false`` + ``reason=timeout`` /
    ``connection_error``
  * /readyz → /healthz fallback after a 404 (pre-G1 backends)
  * audit row emitted on both success and timeout paths
  * invalid timeout / interval bounds surface HTTP 422 before any probe
  * default URL resolution honours ``OMNISIGHT_READYZ_URL`` env override
"""

from __future__ import annotations

import pytest


# ── helpers ───────────────────────────────────────────────────────────


def _queue_probe_outcomes(
    monkeypatch, outcomes, *, sleep_noop: bool = True,
):
    """Sequence ``_probe_ready_once`` return values across polls.

    ``outcomes`` is a list of ``(status_code|None, error|None)`` tuples
    — one per expected call. After the list is exhausted the fake
    keeps returning the last entry (defensive for retry loops). When
    ``sleep_noop`` is True the endpoint's ``asyncio.sleep`` is also
    patched to a no-op so timeout tests run in ms.
    """
    from backend.routers import bootstrap as _br

    calls: list[str] = []
    idx = {"n": 0}

    async def fake_probe(url, *, timeout_secs=3.0):  # noqa: ARG001
        calls.append(url)
        i = idx["n"]
        idx["n"] += 1
        if i < len(outcomes):
            return outcomes[i]
        return outcomes[-1]

    monkeypatch.setattr(_br, "_probe_ready_once", fake_probe)

    if sleep_noop:
        async def noop_sleep(_secs):
            return None
        monkeypatch.setattr(_br.asyncio, "sleep", noop_sleep)

    return calls


# ── immediate ready ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wait_ready_returns_200_on_first_green_probe(client, monkeypatch):
    """A single 200 response short-circuits the poll loop."""
    calls = _queue_probe_outcomes(monkeypatch, [(200, None)])

    r = await client.post(
        "/api/v1/bootstrap/wait-ready",
        json={"timeout_secs": 5, "interval_secs": 0.25,
              "url": "http://probe/readyz"},
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ready"] is True
    assert body["reason"] == "ready"
    assert body["attempts"] == 1
    assert body["url"] == "http://probe/readyz"
    assert body["last_status_code"] == 200
    assert body["fallback_applied"] is False
    assert calls == ["http://probe/readyz"]


# ── eventually ready ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wait_ready_retries_until_green(client, monkeypatch):
    """Probe returns 503, 503, 200 — wait-ready reports ready after 3 attempts."""
    calls = _queue_probe_outcomes(
        monkeypatch,
        [(503, None), (503, None), (200, None)],
    )

    r = await client.post(
        "/api/v1/bootstrap/wait-ready",
        json={"timeout_secs": 30, "interval_secs": 0.1,
              "url": "http://probe/readyz"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert body["attempts"] == 3
    assert body["last_status_code"] == 200
    assert len(calls) == 3


# ── timeout: all probes fail with 503 ────────────────────────────────


@pytest.mark.asyncio
async def test_wait_ready_timeout_on_persistent_503(client, monkeypatch):
    """Never-green probe → ``ready=false`` + ``reason=timeout``."""
    _queue_probe_outcomes(
        monkeypatch,
        [(503, None)] * 50,
    )
    # Ultra-short timeout so the loop exits after a couple of probes.
    r = await client.post(
        "/api/v1/bootstrap/wait-ready",
        json={"timeout_secs": 1.0, "interval_secs": 0.25,
              "url": "http://probe/readyz",
              "fallback_healthz": False},
        follow_redirects=False,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is False
    assert body["reason"] == "timeout"
    assert body["last_status_code"] == 503
    assert body["attempts"] >= 1


# ── timeout: every probe errors at transport layer ───────────────────


@pytest.mark.asyncio
async def test_wait_ready_reports_connection_error_when_no_probe_ever_lands(
    client, monkeypatch,
):
    """No probe returns a status → reason=connection_error."""
    _queue_probe_outcomes(
        monkeypatch,
        [(None, "ConnectError: Connection refused")] * 10,
    )

    r = await client.post(
        "/api/v1/bootstrap/wait-ready",
        json={"timeout_secs": 1.0, "interval_secs": 0.25,
              "url": "http://probe/readyz"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is False
    assert body["reason"] == "connection_error"
    assert body["last_status_code"] is None
    assert "ConnectError" in (body["last_error"] or "")


# ── fallback to /healthz on pre-G1 backends ──────────────────────────


@pytest.mark.asyncio
async def test_wait_ready_falls_back_to_healthz_on_404(client, monkeypatch):
    """404 on /readyz → swap suffix to /healthz exactly once, then keep polling."""
    calls = _queue_probe_outcomes(
        monkeypatch,
        [
            (404, None),  # /readyz 404 → fallback to /healthz
            (200, None),  # /healthz green
        ],
    )

    r = await client.post(
        "/api/v1/bootstrap/wait-ready",
        json={"timeout_secs": 5, "interval_secs": 0.1,
              "url": "http://probe/readyz"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert body["fallback_applied"] is True
    assert body["url"] == "http://probe/healthz"
    assert calls == ["http://probe/readyz", "http://probe/healthz"]


@pytest.mark.asyncio
async def test_wait_ready_does_not_fall_back_when_disabled(client, monkeypatch):
    """fallback_healthz=False keeps polling /readyz even on 404."""
    calls = _queue_probe_outcomes(monkeypatch, [(404, None)] * 50)

    r = await client.post(
        "/api/v1/bootstrap/wait-ready",
        json={"timeout_secs": 0.6, "interval_secs": 0.1,
              "url": "http://probe/readyz",
              "fallback_healthz": False},
        follow_redirects=False,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is False
    assert body["fallback_applied"] is False
    assert body["url"] == "http://probe/readyz"
    assert all(u == "http://probe/readyz" for u in calls)


# ── audit logging ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wait_ready_emits_audit_row_on_success(client, monkeypatch):
    from backend import audit

    _queue_probe_outcomes(monkeypatch, [(200, None)])

    r = await client.post(
        "/api/v1/bootstrap/wait-ready",
        json={"timeout_secs": 5, "url": "http://probe/readyz"},
        follow_redirects=False,
    )
    assert r.status_code == 200

    rows = await audit.query(limit=50)
    wr_rows = [r for r in rows if r.get("action") == "bootstrap.wait_ready"]
    assert len(wr_rows) == 1
    after = wr_rows[0].get("after") or {}
    assert after.get("reason") == "ready"
    assert after.get("last_status_code") == 200


@pytest.mark.asyncio
async def test_wait_ready_emits_audit_row_on_timeout(client, monkeypatch):
    from backend import audit

    _queue_probe_outcomes(monkeypatch, [(503, None)] * 20)

    r = await client.post(
        "/api/v1/bootstrap/wait-ready",
        json={"timeout_secs": 0.5, "interval_secs": 0.1,
              "url": "http://probe/readyz",
              "fallback_healthz": False},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert r.json()["ready"] is False

    rows = await audit.query(limit=50)
    wr_rows = [r for r in rows if r.get("action") == "bootstrap.wait_ready"]
    assert len(wr_rows) == 1
    after = wr_rows[0].get("after") or {}
    assert after.get("reason") == "timeout"
    assert after.get("last_status_code") == 503


# ── validation ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wait_ready_rejects_over_max_timeout(client):
    """timeout_secs > 600 is rejected before any probe runs."""
    r = await client.post(
        "/api/v1/bootstrap/wait-ready",
        json={"timeout_secs": 1000},
        follow_redirects=False,
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_wait_ready_rejects_too_small_interval(client):
    """interval_secs below the 0.05s floor is rejected."""
    r = await client.post(
        "/api/v1/bootstrap/wait-ready",
        json={"interval_secs": 0.001},
        follow_redirects=False,
    )
    assert r.status_code == 422, r.text


# ── default URL resolution ───────────────────────────────────────────


def test_default_readyz_url_uses_env_override(monkeypatch):
    from backend.routers import bootstrap as _br

    monkeypatch.setenv("OMNISIGHT_READYZ_URL", "http://gateway.local/readyz")
    assert _br._default_readyz_url() == "http://gateway.local/readyz"


def test_default_readyz_url_falls_back_to_localhost(monkeypatch):
    from backend.routers import bootstrap as _br

    monkeypatch.delenv("OMNISIGHT_READYZ_URL", raising=False)
    monkeypatch.delenv("OMNISIGHT_PORT", raising=False)
    url = _br._default_readyz_url()
    assert url.startswith("http://127.0.0.1:8000")
    assert url.endswith("/readyz")


def test_default_readyz_url_honours_custom_port(monkeypatch):
    from backend.routers import bootstrap as _br

    monkeypatch.delenv("OMNISIGHT_READYZ_URL", raising=False)
    monkeypatch.setenv("OMNISIGHT_PORT", "9099")
    assert _br._default_readyz_url().startswith("http://127.0.0.1:9099")
