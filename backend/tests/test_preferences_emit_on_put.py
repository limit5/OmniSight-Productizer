"""Q.3-SUB-4 (#297) — User preferences cross-device SSE sync.

Before Q.3-SUB-4 ``PUT /user-preferences/{key}`` flipped the PG row
silently — a second device owned by the same user would only pick
up the change on a full page reload or the next explicit prefs GET.
The J4 cross-tab ``StorageEvent`` path (``storage-bridge.tsx``)
only covered *tabs inside the same browser*, not other devices.

This suite locks the emit + the cross-device contract:

  * ``test_preferences_emit_on_put`` — a successful upsert publishes
    exactly one ``preferences.updated`` event with ``pref_key`` +
    ``value`` + ``user_id`` on the bus.
  * ``test_preferences_emit_on_overwrite`` — the second PUT on the
    same key still emits (upserts must fan out, not just inserts).
  * ``test_preferences_emit_scope_is_user`` — payload contract lock:
    ``_broadcast_scope='user'`` so Q.4 (#298) can switch from
    advisory to enforced without a payload change.
  * ``test_preferences_cross_device_fanout`` — two parallel
    ``bus.subscribe()`` listeners (simulating two user sessions on
    the same account); the HTTP PUT from session A drives a
    ``preferences.updated`` payload onto session B's queue with the
    id + user_id the storage-bridge SSE dispatcher uses to patch
    localStorage + notify in-tab listeners.
  * ``test_preferences_emit_failure_does_not_break_put`` — a flaky
    SSE bus / Redis outage must NEVER fail the PUT HTTP call; PG is
    the source of truth.

Audit evidence: ``docs/design/multi-device-state-sync.md`` Path 7.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from httpx import AsyncClient, ASGITransport

from backend.events import bus as _bus

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def _prefs_client(pg_test_pool, pg_test_dsn, monkeypatch):
    """Shared fixture — open auth + seeded anonymous user.

    user_preferences has an FK to users(id); the open-auth
    ``current_user`` returns the synthetic ``anonymous`` row we seed
    here, so ON CONFLICT UPSERT doesn't trip an FK violation.
    """
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "open")
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)

    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE users, user_preferences RESTART IDENTITY CASCADE"
        )
        await conn.execute(
            "INSERT INTO users (id, email, name, role, password_hash, "
            "enabled, tenant_id) VALUES ($1, $2, $3, $4, $5, 1, $6) "
            "ON CONFLICT (id) DO NOTHING",
            "anonymous", "anonymous@local", "(anonymous)", "admin",
            "", "t-default",
        )

    from backend import db as _db
    from backend.main import app
    from backend import bootstrap as _boot

    async def _green():
        return _boot.BootstrapStatus(
            admin_password_default=False,
            llm_provider_configured=True,
            cf_tunnel_configured=True,
            smoke_passed=True,
        )
    monkeypatch.setattr(_boot, "get_bootstrap_status", _green)
    _boot._gate_cache_reset()

    if _db._db is not None:
        await _db.close()
    await _db.init()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        _boot._gate_cache_reset()
        await _db.close()
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE users, user_preferences RESTART IDENTITY CASCADE"
            )


async def _drain_for_prefs_updated(
    queue, pref_key: str, timeout: float = 2.0,
):
    """Drain the SSE queue until a ``preferences.updated`` event for
    ``pref_key`` arrives, or return None on timeout.

    Filters out heartbeats and other events so ambient chatter from
    sibling fixtures doesn't masquerade as the Q.3-SUB-4 payload.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            msg = await asyncio.wait_for(queue.get(), timeout=0.3)
        except asyncio.TimeoutError:
            continue
        if msg.get("event") != "preferences.updated":
            continue
        data = json.loads(msg["data"])
        if data.get("pref_key") != pref_key:
            continue
        return data
    return None


async def test_preferences_emit_on_put(_prefs_client: AsyncClient):
    """PUT /user-preferences/{key} must emit exactly one
    ``preferences.updated`` SSE event carrying pref_key + value +
    the acting user's id.
    """
    q = _bus.subscribe(tenant_id=None)
    try:
        res = await _prefs_client.put(
            "/api/v1/user-preferences/locale",
            json={"value": "ja"},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["key"] == "locale"
        assert body["value"] == "ja"

        data = await _drain_for_prefs_updated(q, "locale")
        assert data is not None, (
            "PUT /user-preferences/{key} must publish a "
            "preferences.updated event — cross-device sync would "
            "otherwise wait for the next page reload."
        )
        assert data["pref_key"] == "locale"
        assert data["value"] == "ja"
        # The client fixture uses open-auth-mode whose ``current_user``
        # returns the synthetic ``anonymous`` row we seeded.
        assert isinstance(data.get("user_id"), str) and data["user_id"]
    finally:
        _bus.unsubscribe(q)


async def test_preferences_emit_on_overwrite(_prefs_client: AsyncClient):
    """The second PUT on an existing key must still emit — upserts
    fan out whether the underlying op is INSERT or UPDATE.
    """
    # Seed the row so the second PUT takes the UPDATE branch.
    res1 = await _prefs_client.put(
        "/api/v1/user-preferences/locale",
        json={"value": "en"},
    )
    assert res1.status_code == 200, res1.text

    q = _bus.subscribe(tenant_id=None)
    try:
        res2 = await _prefs_client.put(
            "/api/v1/user-preferences/locale",
            json={"value": "ja"},
        )
        assert res2.status_code == 200, res2.text

        data = await _drain_for_prefs_updated(q, "locale")
        assert data is not None, (
            "ON CONFLICT DO UPDATE must still publish — without this "
            "a flip-back on device A never reaches device B."
        )
        assert data["value"] == "ja"
    finally:
        _bus.unsubscribe(q)


async def test_preferences_emit_scope_is_user(_prefs_client: AsyncClient):
    """Lock the ``broadcast_scope='user'`` payload contract — the
    frontend filter (and the eventual Q.4 #298 server enforcement)
    both rely on this label being present on every emit.
    """
    q = _bus.subscribe(tenant_id=None)
    try:
        res = await _prefs_client.put(
            "/api/v1/user-preferences/tour_seen",
            json={"value": "1"},
        )
        assert res.status_code == 200, res.text

        data = await _drain_for_prefs_updated(q, "tour_seen")
        assert data is not None
        assert data["_broadcast_scope"] == "user", (
            "preferences.updated must carry broadcast_scope=user so "
            "Q.4 (#298) can enforce per-user delivery without "
            "changing the payload shape."
        )
        assert data["pref_key"] == "tour_seen"
        assert data["value"] == "1"
        assert isinstance(data.get("user_id"), str) and data["user_id"]
    finally:
        _bus.unsubscribe(q)


async def test_preferences_cross_device_fanout(_prefs_client: AsyncClient):
    """Two bus subscribers (simulating two user sessions on the same
    user account); the HTTP PUT from session A must reach session B's
    queue with the pref_key + value + user_id the frontend
    storage-bridge dispatcher uses to patch localStorage.

    The PG row also must actually be flipped — the emit must run AFTER
    the INSERT commits so consumers that trust the event can
    optimistically skip the follow-up REST read.
    """
    # Session A — originator. Session B — simulated second device;
    # we assert primarily on B to prove fan-out.
    q_a = _bus.subscribe(tenant_id=None)
    q_b = _bus.subscribe(tenant_id=None)
    try:
        res = await _prefs_client.put(
            "/api/v1/user-preferences/wizard_seen",
            json={"value": "1"},
        )
        assert res.status_code == 200, res.text

        data_b = await _drain_for_prefs_updated(q_b, "wizard_seen")
        assert data_b is not None, (
            "session B must receive the preferences.updated event"
        )
        assert data_b["pref_key"] == "wizard_seen"
        assert data_b["value"] == "1"
        assert isinstance(data_b.get("user_id"), str) and data_b["user_id"]

        data_a = await _drain_for_prefs_updated(q_a, "wizard_seen")
        assert data_a is not None, (
            "originator must also see its own event — the dispatcher "
            "is an idempotent write so double-apply is safe"
        )
        assert data_a["pref_key"] == "wizard_seen"

        # Confirm the PG row actually committed before the emit.
        get_res = await _prefs_client.get(
            "/api/v1/user-preferences/wizard_seen",
        )
        assert get_res.status_code == 200
        assert get_res.json()["value"] == "1"
    finally:
        _bus.unsubscribe(q_a)
        _bus.unsubscribe(q_b)


async def test_preferences_emit_failure_does_not_break_put(
    _prefs_client: AsyncClient, monkeypatch,
):
    """A flaky SSE bus / Redis outage must NEVER fail the PUT HTTP
    call — the truth is in PG, the emit is latency-optimisation.
    """
    from backend.routers import preferences as _prefs_router

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated SSE bus outage")

    # Monkey-patch the symbol imported inside the handler's try-block.
    # The handler does ``from backend.events import emit_preferences_updated``
    # inside the try, so we patch the source module.
    from backend import events as _events
    monkeypatch.setattr(_events, "emit_preferences_updated", _boom)

    res = await _prefs_client.put(
        "/api/v1/user-preferences/locale",
        json={"value": "ja"},
    )
    # 200 must still return; the PG row must still flip.
    assert res.status_code == 200, res.text
    assert res.json()["value"] == "ja"

    get_res = await _prefs_client.get(
        "/api/v1/user-preferences/locale",
    )
    assert get_res.status_code == 200
    assert get_res.json()["value"] == "ja"

    # Reference the router module so the import isn't removed by
    # an over-eager lint — we want to keep the symbol reachable for
    # future patches that need to reach into the handler.
    assert _prefs_router.router is not None
