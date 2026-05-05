"""BP.A2A.6 -- external A2A agent registry contract tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.agents.external_agent_registry import (
    ExternalAgentDisabledError,
    ExternalAgentEndpoint,
    ExternalAgentNotFoundError,
    ExternalAgentRegistry,
    InMemoryExternalAgentRegistryStore,
)


def _endpoint(**overrides) -> ExternalAgentEndpoint:
    values = {
        "agent_id": "threat-intel",
        "display_name": "Threat Intel",
        "base_url": "https://agent.example.com/",
        "agent_name": "orchestrator",
        "description": "partner A2A endpoint",
        "tags": ("secops", "intel", "secops"),
        "capabilities": ("ioc_enrichment", "cve_triage"),
    }
    values.update(overrides)
    return ExternalAgentEndpoint(**values)


def test_endpoint_normalises_base_url_and_derives_agent_card_url() -> None:
    endpoint = _endpoint()

    assert endpoint.base_url == "https://agent.example.com"
    assert endpoint.agent_card_url == "https://agent.example.com/.well-known/agent.json"
    assert endpoint.tags == ("intel", "secops")


def test_endpoint_rejects_non_slug_agent_id() -> None:
    with pytest.raises(ValueError, match="agent_id must be a lowercase slug"):
        _endpoint(agent_id="ThreatIntel")


def test_endpoint_rejects_relative_base_url() -> None:
    with pytest.raises(ValueError, match="absolute http\\(s\\) URL"):
        _endpoint(base_url="/internal")


def test_endpoint_requires_token_ref_for_bearer_auth() -> None:
    with pytest.raises(ValueError, match="token_ref is required"):
        _endpoint(auth_mode="bearer", token_ref="")


def test_endpoint_rejects_token_ref_for_no_auth() -> None:
    with pytest.raises(ValueError, match="token_ref must be empty"):
        _endpoint(auth_mode="none", token_ref="secret:a2a")


@pytest.mark.asyncio
async def test_registry_register_upserts_without_losing_registered_at() -> None:
    registry = ExternalAgentRegistry()
    first = await registry.register_endpoint(_endpoint(display_name="Threat Intel"))
    second = await registry.register_endpoint(_endpoint(display_name="Threat Intel v2"))

    assert second.registered_at == first.registered_at
    assert second.updated_at is not None
    assert second.display_name == "Threat Intel v2"


@pytest.mark.asyncio
async def test_registry_list_sorted_and_enabled_filter() -> None:
    registry = ExternalAgentRegistry()
    await registry.register_endpoint(_endpoint(agent_id="z-agent", display_name="Zed"))
    await registry.register_endpoint(
        _endpoint(agent_id="a-agent", display_name="Alpha", enabled=False)
    )

    all_rows = await registry.list_endpoints()
    enabled_rows = await registry.list_endpoints(enabled_only=True)

    assert [row.agent_id for row in all_rows] == ["a-agent", "z-agent"]
    assert [row.agent_id for row in enabled_rows] == ["z-agent"]


@pytest.mark.asyncio
async def test_registry_set_enabled_and_missing_errors() -> None:
    registry = ExternalAgentRegistry()
    await registry.register_endpoint(_endpoint())

    disabled = await registry.set_enabled("threat-intel", False)
    assert disabled.enabled is False

    with pytest.raises(ExternalAgentNotFoundError):
        await registry.get_endpoint("missing")
    with pytest.raises(ExternalAgentDisabledError):
        await registry.get_endpoint("threat-intel", require_enabled=True)


@pytest.mark.asyncio
async def test_registry_set_health_writes_to_store() -> None:
    store = InMemoryExternalAgentRegistryStore()
    registry = ExternalAgentRegistry(store=store)
    await registry.register_endpoint(_endpoint())
    checked_at = datetime.now(timezone.utc)

    await registry.set_health("threat-intel", "healthy", checked_at)
    endpoint = await store.get_endpoint("threat-intel")

    assert endpoint is not None
    assert endpoint.health_status == "healthy"
    assert endpoint.last_health_check == checked_at


@pytest.mark.asyncio
async def test_registry_build_client_uses_endpoint_base_url() -> None:
    registry = ExternalAgentRegistry()
    await registry.register_endpoint(_endpoint(base_url="https://agent.example.com/root/"))

    client = await registry.build_client(
        "threat-intel",
        tenant_id="t-a2a",
        bearer_token="tok",
    )

    assert client.base_url == "https://agent.example.com/root"
    assert client.tenant_id == "t-a2a"
    assert client.bearer_token == "tok"
