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
async def test_healthz_reports_ok_with_db_up(client):
    r = await client.get("/api/v1/healthz")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["db"]["ok"] is True
    assert data["version"]
    assert "auth_mode" in data
    assert "profile" in data
    assert "sse" in data
    assert "watchdog" in data


@pytest.mark.asyncio
async def test_healthz_returns_503_when_db_probe_fails(client, monkeypatch):
    from backend.routers import observability as obs

    async def _fail():
        return {"ok": False, "latency_ms": 1, "error": "boom"}

    monkeypatch.setattr(obs, "_probe_db", _fail)
    r = await client.get("/api/v1/healthz")
    assert r.status_code == 503
    assert r.json()["ok"] is False


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
async def test_dlq_retry_marks_exhausted_as_dead(monkeypatch):
    from backend import db, notifications as n
    from backend.config import settings

    nid = f"notif-dlq1-{uuid.uuid4().hex[:6]}"
    await db.init()
    try:
        await db.insert_notification({
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
            nid, "failed",
            attempts=settings.notification_max_retries,
            error="simulated",
        )
        result = await n.retry_failed_notifications()
        assert result["dead"] >= 1

        rows = await db.list_failed_notifications()
        assert all(r["id"] != nid for r in rows)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_dlq_retry_redispatches_when_attempts_remain(monkeypatch):
    from backend import db, notifications as n
    from backend.config import settings

    nid = f"notif-dlq2-{uuid.uuid4().hex[:6]}"
    await db.init()
    try:
        await db.insert_notification({
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
            nid, "failed", attempts=0, error="first attempt",
        )
        # No webhooks configured → dispatch resolves as 'skipped'
        monkeypatch.setattr(settings, "notification_slack_webhook", "", raising=False)
        monkeypatch.setattr(settings, "notification_jira_url", "", raising=False)
        monkeypatch.setattr(settings, "notification_pagerduty_key", "", raising=False)

        result = await n.retry_failed_notifications()
        assert result["retried"] >= 1
    finally:
        await db.close()
