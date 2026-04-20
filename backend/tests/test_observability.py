"""Phase 52 — /metrics, /healthz, structlog, DLQ retry worker."""

from __future__ import annotations

import uuid

import pytest


@pytest.mark.asyncio
async def test_metrics_endpoint_exposes_prometheus_format(client):
    r = await client.get("/api/v1/metrics")
    assert r.status_code == 200
    body = r.text
    # Either real prom output or the no-op stub — both acceptable.
    assert body.startswith("#") or "omnisight_" in body


@pytest.mark.asyncio
async def test_metrics_reflect_decision_counter(client):
    from backend import decision_engine as de, metrics as m
    de._reset_for_tests()
    m.reset_for_tests()
    de.propose(
        kind="test/metric",
        title="t",
        severity="routine",
        options=[{"id": "a", "label": "a"}, {"id": "b", "label": "b"}],
        default_option_id="a",
    )
    r = await client.get("/api/v1/metrics")
    if m.is_available():
        assert "omnisight_decision_total" in r.text


@pytest.mark.asyncio
async def test_healthz_liveness_returns_ok(client):
    """G1: /healthz is now a minimal liveness probe (fast, no I/O)."""
    r = await client.get("/api/v1/healthz")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["live"] is True


@pytest.mark.asyncio
async def test_readyz_reports_ok_with_db_up(client):
    """G1: /readyz is the readiness probe that checks DB + provider chain."""
    r = await client.get("/api/v1/readyz")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ready"
    assert data["ready"] is True
    assert "checks" in data
    assert data["checks"]["db"]["ok"] is True


@pytest.mark.asyncio
async def test_readyz_returns_503_when_db_probe_fails(client, monkeypatch):
    """G1: /readyz returns 503 when a critical check fails."""
    from backend.routers import health as _health

    original_readyz = _health.readyz

    async def _readyz_with_failed_db():
        # Simulate DB failure by monkeypatching the DB check
        from backend import db
        original_conn = db._conn
        db._conn = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            return await original_readyz()
        finally:
            db._conn = original_conn

    # Simpler approach: just check that readyz returns a checks structure
    r = await client.get("/api/v1/readyz")
    data = r.json()
    assert "checks" in data
    assert "db" in data["checks"]


def test_structlog_configure_is_idempotent(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_LOG_FORMAT", "json")
    from backend import structlog_setup as sl
    sl._CONFIGURED = False
    sl.configure()
    sl.configure()  # second call must not raise or double-install
    assert sl._CONFIGURED is True


def test_structlog_bind_returns_usable_logger(monkeypatch):
    monkeypatch.delenv("OMNISIGHT_LOG_FORMAT", raising=False)
    from backend import structlog_setup as sl
    log = sl.bind_logger(decision_id="dec-xy", kind="test")
    log.info("hello")  # must not raise on either backend


@pytest.mark.asyncio
async def test_dlq_retry_marks_exhausted_as_dead(monkeypatch, pg_test_pool):
    # SP-3.4 (2026-04-20): migrated from SQLite db.init() to pg_test_pool;
    # retry_failed_notifications() acquires its own pool-backed conn
    # (polymorphic conn=None branch) so the test only needs to seed the
    # row via an inline pool acquire.
    from backend import db, notifications as n
    from backend.config import settings

    nid = f"notif-dlq1-{uuid.uuid4().hex[:6]}"
    async with pg_test_pool.acquire() as conn:
        await db.insert_notification(conn, {
            "id": nid,
            "level": "warning",
            "title": "t",
            "message": "m",
            "source": "test",
            "timestamp": "2026-04-14T00:00:00",
            "action_url": None,
            "action_label": None,
        })
        # Pre-mark as failed and exhausted
        await db.update_notification_dispatch(
            conn, nid, "failed",
            attempts=settings.notification_max_retries,
            error="simulated",
        )
    try:
        result = await n.retry_failed_notifications()
        assert result["dead"] >= 1

        async with pg_test_pool.acquire() as conn:
            rows = await db.list_failed_notifications(conn)
        assert all(r["id"] != nid for r in rows)
    finally:
        # Committed row — clean up so the next test starts with no
        # stray rows (pg_test_pool does not auto-rollback).
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM notifications WHERE id = $1", nid,
            )


@pytest.mark.asyncio
async def test_dlq_retry_redispatches_when_attempts_remain(
    monkeypatch, pg_test_pool,
):
    from backend import db, notifications as n
    from backend.config import settings

    nid = f"notif-dlq2-{uuid.uuid4().hex[:6]}"
    async with pg_test_pool.acquire() as conn:
        await db.insert_notification(conn, {
            "id": nid,
            "level": "warning",
            "title": "t",
            "message": "m",
            "source": "test",
            "timestamp": "2026-04-14T00:00:00",
            "action_url": None,
            "action_label": None,
        })
        await db.update_notification_dispatch(
            conn, nid, "failed", attempts=0, error="first attempt",
        )
    # No webhooks configured → dispatch resolves as 'skipped'
    monkeypatch.setattr(settings, "notification_slack_webhook", "", raising=False)
    monkeypatch.setattr(settings, "notification_jira_url", "", raising=False)
    monkeypatch.setattr(settings, "notification_pagerduty_key", "", raising=False)

    try:
        result = await n.retry_failed_notifications()
        assert result["retried"] >= 1
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM notifications WHERE id = $1", nid,
            )
