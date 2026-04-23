"""Q.3-SUB-3 (#297) — Notification read-state cross-device SSE sync.

Before Q.3-SUB-3 ``POST /notifications/{id}/read`` flipped the PG row
silently — a second device showing the bell badge would only drop the
counter on the next ``/notifications/unread-count`` poll. This suite
locks the emit + the cross-device contract:

  * ``test_notification_read_emits_on_mark`` — a successful
    ``mark_notification_read`` publishes exactly one
    ``notification.read`` event with ``id`` + ``user_id`` on the bus.
  * ``test_notification_read_no_emit_on_miss`` — a 404-style "not
    found" mark MUST NOT publish (no spurious decrements on other
    devices when the target was already gone).
  * ``test_notification_read_scope_is_user`` — payload contract lock:
    ``_broadcast_scope='user'`` so Q.4 (#298) can switch from
    advisory to enforced without a payload change.
  * ``test_notification_read_cross_device_decrement`` — two parallel
    ``bus.subscribe()`` listeners (simulating two user sessions);
    the HTTP POST from session A drives a ``notification.read``
    payload onto session B's queue. This is the contract the
    frontend ``use-engine.ts`` dispatcher consumes.
  * ``test_notification_read_emit_failure_does_not_break_mark`` —
    a flaky SSE bus must NEVER fail the mark-read HTTP call.

Audit evidence: ``docs/design/multi-device-state-sync.md`` Path 8.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.events import bus as _bus


async def _drain_for_notification_read(
    queue, notification_id: str, timeout: float = 2.0,
):
    """Drain the SSE queue until a ``notification.read`` event for
    ``notification_id`` arrives, or return None on timeout.

    Filters out heartbeats and other events so ambient chatter from
    sibling fixtures doesn't masquerade as the Q.3-SUB-3 payload.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            msg = await asyncio.wait_for(queue.get(), timeout=0.3)
        except asyncio.TimeoutError:
            continue
        if msg.get("event") != "notification.read":
            continue
        data = json.loads(msg["data"])
        if data.get("id") != notification_id:
            continue
        return data
    return None


@pytest.mark.asyncio
async def test_notification_read_emits_on_mark(client):
    """POST /runtime/notifications/{id}/read on an existing row must
    emit exactly one ``notification.read`` SSE event carrying the
    notification id and the acting user's id.
    """
    from backend import db
    from backend.db_pool import get_pool

    notif_id = "n-emit-1"
    async with get_pool().acquire() as conn:
        await db.insert_notification(conn, {
            "id": notif_id,
            "level": "warning",
            "title": "disk almost full",
            "message": "body",
            "source": "test",
            "timestamp": "2026-04-24T00:00:00",
            "read": False,
            "action_url": None,
            "action_label": None,
            "auto_resolved": False,
        })

    q = _bus.subscribe(tenant_id=None)
    try:
        res = await client.post(f"/api/v1/runtime/notifications/{notif_id}/read")
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "ok"

        data = await _drain_for_notification_read(q, notif_id)
        assert data is not None, (
            "POST /notifications/{id}/read must publish a "
            "notification.read event — cross-device decrement would "
            "otherwise wait for the next unread-count poll."
        )
        assert data["id"] == notif_id
        # The current_user on the client fixture is the synthetic
        # anonymous-admin (``_ANON_ADMIN``) whose id is
        # ``"anonymous"`` — assert presence, not a specific literal,
        # so the test doesn't lock to the dev auth mode.
        assert isinstance(data.get("user_id"), str) and data["user_id"]
    finally:
        _bus.unsubscribe(q)
        async with get_pool().acquire() as conn:
            await conn.execute("DELETE FROM notifications WHERE id = $1",
                               notif_id)


@pytest.mark.asyncio
async def test_notification_read_no_emit_on_miss(client):
    """A mark-read hitting a non-existent notification must NOT emit.

    Without this guard, a spurious ``notification.read`` could drive
    other devices' unread counters negative (we clamp at 0 on the
    frontend, but the server should not broadcast phantom reads).
    """
    q = _bus.subscribe(tenant_id=None)
    try:
        res = await client.post(
            "/api/v1/runtime/notifications/does-not-exist/read",
        )
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "not_found"

        # Bounded wait — nothing should arrive.
        data = await _drain_for_notification_read(
            q, "does-not-exist", timeout=0.5,
        )
        assert data is None, (
            "Mark-read against a missing row must not publish — other "
            "devices would otherwise over-decrement the bell badge."
        )
    finally:
        _bus.unsubscribe(q)


@pytest.mark.asyncio
async def test_notification_read_scope_is_user(client):
    """Lock the ``broadcast_scope='user'`` payload contract — the
    frontend filter (and the eventual Q.4 #298 server enforcement)
    both rely on this label being present on every emit.
    """
    from backend import db
    from backend.db_pool import get_pool

    notif_id = "n-scope-1"
    async with get_pool().acquire() as conn:
        await db.insert_notification(conn, {
            "id": notif_id,
            "level": "warning",
            "title": "scope test",
            "message": "",
            "source": "test",
            "timestamp": "2026-04-24T00:00:00",
            "read": False,
            "action_url": None,
            "action_label": None,
            "auto_resolved": False,
        })

    q = _bus.subscribe(tenant_id=None)
    try:
        res = await client.post(
            f"/api/v1/runtime/notifications/{notif_id}/read",
        )
        assert res.status_code == 200, res.text

        data = await _drain_for_notification_read(q, notif_id)
        assert data is not None
        assert data["_broadcast_scope"] == "user", (
            "notification.read must carry broadcast_scope=user so Q.4 "
            "(#298) can enforce per-user delivery without changing the "
            "payload shape."
        )
        assert data["id"] == notif_id
        assert isinstance(data.get("user_id"), str) and data["user_id"]
    finally:
        _bus.unsubscribe(q)
        async with get_pool().acquire() as conn:
            await conn.execute("DELETE FROM notifications WHERE id = $1",
                               notif_id)


@pytest.mark.asyncio
async def test_notification_read_cross_device_decrement(client):
    """Two bus subscribers (simulating two user sessions on the same
    user account); the HTTP POST from session A must reach session B's
    queue with the id + user_id the frontend dispatcher uses to
    decrement its unread counter and flip the list row.
    """
    from backend import db
    from backend.db_pool import get_pool

    notif_id = "n-cross-1"
    async with get_pool().acquire() as conn:
        await db.insert_notification(conn, {
            "id": notif_id,
            "level": "warning",
            "title": "cross device",
            "message": "",
            "source": "test",
            "timestamp": "2026-04-24T00:00:00",
            "read": False,
            "action_url": None,
            "action_label": None,
            "auto_resolved": False,
        })

    # Session A — the one firing the mutation. Session B — simulated
    # second device; we assert primarily on B to prove fan-out.
    q_a = _bus.subscribe(tenant_id=None)
    q_b = _bus.subscribe(tenant_id=None)
    try:
        res = await client.post(
            f"/api/v1/runtime/notifications/{notif_id}/read",
        )
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "ok"

        data_b = await _drain_for_notification_read(q_b, notif_id)
        assert data_b is not None, (
            "session B must receive the mark-read notification.read "
            "event"
        )
        assert data_b["id"] == notif_id
        assert isinstance(data_b.get("user_id"), str) and data_b["user_id"]

        data_a = await _drain_for_notification_read(q_a, notif_id)
        assert data_a is not None, (
            "originator must also see its own event — the dispatcher "
            "is an idempotent flip so double-apply is safe"
        )
        assert data_a["id"] == notif_id

        # DB row is actually flipped — the emit must run AFTER the
        # UPDATE commits so consumers that trust the event can
        # optimistically skip the follow-up REST read.
        async with get_pool().acquire() as conn:
            rows = await db.list_notifications(conn)
        rec = next(r for r in rows if r["id"] == notif_id)
        assert rec["read"] is True
    finally:
        _bus.unsubscribe(q_a)
        _bus.unsubscribe(q_b)
        async with get_pool().acquire() as conn:
            await conn.execute("DELETE FROM notifications WHERE id = $1",
                               notif_id)


@pytest.mark.asyncio
async def test_notification_read_emit_failure_does_not_break_mark(
    client, monkeypatch,
):
    """A flaky SSE bus / Redis outage must NEVER fail the mark-read
    HTTP call — the truth is in PG, the emit is latency-optimisation.
    """
    from backend import db
    from backend import events as _events
    from backend.db_pool import get_pool

    notif_id = "n-boom-1"
    async with get_pool().acquire() as conn:
        await db.insert_notification(conn, {
            "id": notif_id,
            "level": "warning",
            "title": "boom",
            "message": "",
            "source": "test",
            "timestamp": "2026-04-24T00:00:00",
            "read": False,
            "action_url": None,
            "action_label": None,
            "auto_resolved": False,
        })

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated SSE bus outage")

    monkeypatch.setattr(_events, "emit_notification_read", _boom)
    try:
        res = await client.post(
            f"/api/v1/runtime/notifications/{notif_id}/read",
        )
        # 200 must still return; the PG row must still flip.
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "ok"

        async with get_pool().acquire() as conn:
            rows = await db.list_notifications(conn)
        rec = next(r for r in rows if r["id"] == notif_id)
        assert rec["read"] is True
    finally:
        async with get_pool().acquire() as conn:
            await conn.execute("DELETE FROM notifications WHERE id = $1",
                               notif_id)
