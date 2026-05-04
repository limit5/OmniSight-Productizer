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
    InMemoryProxyAuditMetadataStore,
    InMemoryProxyHeartbeatStore,
    ProxyAuditMetadata,
    ProxyHeartbeat,
    get_proxy_health,
    list_audit_metadata,
    record_audit_metadata,
    record_heartbeat,
    set_proxy_audit_metadata_store_for_tests,
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


def test_record_audit_metadata_stores_only_metadata() -> None:
    store = InMemoryProxyAuditMetadataStore()

    record_audit_metadata(
        ProxyAuditMetadata(
            proxy_id="proxy-a",
            tenant_id="tenant-a",
            provider="openai",
            method="POST",
            path="/v1/chat/completions",
            status_code=200,
            model="gpt-4.1",
            token_count=12,
            prompt_tokens=7,
            completion_tokens=5,
            total_tokens=12,
            recorded_at="2026-05-04T00:00:00Z",
        ),
        store=store,
        now=1_000.0,
    )

    entries = list_audit_metadata("proxy-a", store=store)
    assert len(entries) == 1
    assert entries[0].model == "gpt-4.1"
    assert entries[0].token_count == 12
    assert not hasattr(entries[0], "prompt")
    assert not hasattr(entries[0], "response")


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


@pytest.mark.asyncio
async def test_proxy_audit_endpoint_records_metadata_only(client) -> None:
    store = InMemoryProxyAuditMetadataStore()
    set_proxy_audit_metadata_store_for_tests(store)
    try:
        response = await client.post(
            "/api/v1/byog/proxies/proxy-a/audit",
            json={
                "proxy_id": "proxy-a",
                "tenant_id": "tenant-a",
                "provider": "openai",
                "method": "POST",
                "path": "/v1/chat/completions",
                "status_code": 200,
                "model": "gpt-4.1",
                "token_count": 12,
                "prompt_tokens": 7,
                "completion_tokens": 5,
                "total_tokens": 12,
                "recorded_at": "2026-05-04T00:00:00Z",
            },
        )
        assert response.status_code == 200
        assert response.json()["token_count"] == 12

        entries = list_audit_metadata("proxy-a", store=store)
        assert len(entries) == 1
        assert entries[0].provider == "openai"
        assert entries[0].model == "gpt-4.1"
        assert entries[0].token_count == 12
    finally:
        set_proxy_audit_metadata_store_for_tests(None)


@pytest.mark.asyncio
async def test_proxy_audit_endpoint_rejects_prompt_payload(client) -> None:
    response = await client.post(
        "/api/v1/byog/proxies/proxy-a/audit",
        json={
            "proxy_id": "proxy-a",
            "model": "gpt-4.1",
            "token_count": 12,
            "prompt": "must stay customer-side",
        },
    )

    assert response.status_code == 422
