"""KS.3.5 -- BYOG proxy heartbeat contract tests.

Locks the narrow heartbeat row: proxy posts every 30 seconds, SaaS marks
the proxy disconnected when no heartbeat has arrived for 60 seconds. No
PG schema is touched here; KS.3.12 owns historical health-check tables.
"""

from __future__ import annotations

import pytest

from backend.proxy_health import (
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    DEFAULT_STALE_THRESHOLD_SECONDS,
    InMemoryProxyHeartbeatStore,
    ProxyHeartbeat,
    get_proxy_health,
    record_heartbeat,
    set_proxy_heartbeat_store_for_tests,
)


def test_recorded_heartbeat_is_connected_inside_sixty_seconds() -> None:
    store = InMemoryProxyHeartbeatStore()

    record_heartbeat(
        ProxyHeartbeat(proxy_id="proxy-a", tenant_id="tenant-a"),
        store=store,
        now=1_000.0,
    )
    health = get_proxy_health("proxy-a", store=store, now=1_030.0)

    assert health.connected is True
    assert health.stale is False
    assert health.stale_threshold_seconds == DEFAULT_STALE_THRESHOLD_SECONDS
    assert health.last_heartbeat_age_seconds == 30.0
    assert health.heartbeat is not None
    assert health.heartbeat.heartbeat_interval_seconds == DEFAULT_HEARTBEAT_INTERVAL_SECONDS


def test_missing_heartbeat_for_more_than_sixty_seconds_is_disconnected() -> None:
    store = InMemoryProxyHeartbeatStore()

    record_heartbeat(
        ProxyHeartbeat(proxy_id="proxy-a", tenant_id="tenant-a"),
        store=store,
        now=1_000.0,
    )
    health = get_proxy_health("proxy-a", store=store, now=1_061.0)

    assert health.connected is False
    assert health.stale is True
    assert health.last_heartbeat_age_seconds == 61.0


@pytest.mark.asyncio
async def test_proxy_heartbeat_endpoint_records_and_reports_health(client) -> None:
    store = InMemoryProxyHeartbeatStore()
    set_proxy_heartbeat_store_for_tests(store)
    try:
        heartbeat = await client.post(
            "/api/v1/byog/proxies/proxy-a/heartbeat",
            json={
                "proxy_id": "proxy-a",
                "tenant_id": "tenant-a",
                "status": "ok",
                "service": "omnisight-proxy",
                "provider_count": 2,
                "heartbeat_interval_seconds": 30,
            },
        )
        assert heartbeat.status_code == 200
        assert heartbeat.json()["stale_threshold_seconds"] == 60

        health = await client.get("/api/v1/byog/proxies/proxy-a/health")
        assert health.status_code == 200
        body = health.json()
        assert body["connected"] is True
        assert body["stale"] is False
        assert body["heartbeat"]["tenant_id"] == "tenant-a"
        assert body["heartbeat"]["provider_count"] == 2
        assert body["heartbeat"]["heartbeat_interval_seconds"] == 30
    finally:
        set_proxy_heartbeat_store_for_tests(None)


@pytest.mark.asyncio
async def test_proxy_heartbeat_endpoint_rejects_proxy_id_mismatch(client) -> None:
    response = await client.post(
        "/api/v1/byog/proxies/proxy-a/heartbeat",
        json={"proxy_id": "proxy-b"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "body proxy_id must match path proxy_id"
