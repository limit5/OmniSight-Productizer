"""BP.A2A.8 -- SecOps Intel uses outbound A2A for third-party agents."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from backend import circuit_breaker
from backend.a2a.client import A2AClient
from backend.agents.external_agent_registry import ExternalAgentEndpoint
from backend.secops_intel_hooks import (
    architect_pre_blueprint_a2a_hook,
    integration_engineer_pre_install_a2a_hook,
)


BASE_URL = "https://threat-agent.example.com"
TENANT_ID = "tenant-secops-a2a"
NOW = datetime(2026, 5, 6, 8, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _reset_breaker_state():
    circuit_breaker._reset_for_tests()
    yield
    circuit_breaker._reset_for_tests()


def _client_factory(handler):  # noqa: ANN001
    def _factory(**kwargs):  # noqa: ANN003
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    return _factory


async def _no_sleep(_delay: float) -> None:
    return None


class _A2AThreatIntelRegistry:
    def __init__(self, handler, *, agent_name: str = "intel") -> None:  # noqa: ANN001
        self.endpoint = ExternalAgentEndpoint(
            agent_id="threat-intel",
            display_name="Threat Intel",
            base_url=BASE_URL,
            agent_name=agent_name,
            capabilities=("cve", "zero_day", "best_practice"),
        )
        self.handler = handler
        self.client_builds: list[tuple[str, str, str]] = []
        self.endpoint_reads: list[tuple[str, bool]] = []

    async def get_endpoint(self, agent_id: str, *, require_enabled: bool = False):
        self.endpoint_reads.append((agent_id, require_enabled))
        return self.endpoint

    async def build_client(
        self,
        agent_id: str,
        *,
        tenant_id: str,
        bearer_token: str = "",
    ) -> A2AClient:
        self.client_builds.append((agent_id, tenant_id, bearer_token))
        return A2AClient(
            BASE_URL,
            tenant_id=tenant_id,
            bearer_token=bearer_token,
            client_factory=_client_factory(self.handler),
            sleep=_no_sleep,
        )


def _reports_payload() -> dict:
    return {
        "status": "completed",
        "reports": [
            {
                "kind": "cve",
                "query": "Acme Camera vite-plugin-camera",
                "source": "partner-a2a",
                "fetched_at": NOW.isoformat(),
                "total_items": 1,
                "items": [
                    {
                        "id": "CVE-2026-8001",
                        "title": "Partner observed install target RCE",
                        "source": "partner-a2a",
                        "url": "https://partner.example.test/CVE-2026-8001",
                        "severity": "CRITICAL",
                        "published_at": "2026-05-06T00:00:00+00:00",
                        "updated_at": "2026-05-06T01:00:00+00:00",
                        "summary": "Exploit telemetry from the registered agent.",
                        "affected": ["vite-plugin-camera"],
                        "references": [],
                        "tags": ["a2a", "cve"],
                    }
                ],
                "error": "",
            },
            {
                "kind": "zero_day",
                "query": "Acme Camera vite-plugin-camera",
                "source": "partner-a2a",
                "fetched_at": NOW.isoformat(),
                "total_items": 0,
                "items": [],
                "error": "",
            },
        ],
    }


@pytest.mark.asyncio
async def test_pre_install_hook_invokes_registered_threat_intel_agent_over_a2a():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["tenant"] = request.headers["x-omnisight-tenant-id"]
        captured["auth"] = request.headers["authorization"]
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(200, json=_reports_payload())

    registry = _A2AThreatIntelRegistry(handler)

    result = await integration_engineer_pre_install_a2a_hook(
        registry=registry,
        tenant_id=TENANT_ID,
        bearer_token="tok-secops",
        product_name="Acme Camera",
        install_targets=["vite-plugin-camera"],
        now=NOW,
    )

    assert registry.endpoint_reads == [("threat-intel", True)]
    assert registry.client_builds == [("threat-intel", TENANT_ID, "tok-secops")]
    assert captured["method"] == "POST"
    assert captured["path"] == "/a2a/invoke/intel"
    assert captured["tenant"] == TENANT_ID
    assert captured["auth"] == "Bearer tok-secops"
    assert captured["payload"] == {
        "hook": "integration_engineer_pre_install",
        "guild": "intel",
        "product_name": "Acme Camera",
        "query": "Acme Camera vite-plugin-camera",
        "best_practice_topic": "dependency install Acme Camera",
        "requested_reports": ["cve", "zero_day", "best_practice"],
        "limit": 5,
        "context": {"install_targets": ["vite-plugin-camera"]},
    }
    assert result["hook"] == "integration_engineer_pre_install"
    assert result["status"] == "findings"
    assert result["reports"][0]["source"] == "partner-a2a"
    assert "CVE-2026-8001" in result["brief"]


@pytest.mark.asyncio
async def test_architect_hook_sends_blueprint_context_to_a2a_agent():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"status": "completed", "result": {"reports": []}})

    registry = _A2AThreatIntelRegistry(handler, agent_name="secops")

    result = await architect_pre_blueprint_a2a_hook(
        registry=registry,
        tenant_id=TENANT_ID,
        product_name="Acme Camera",
        blueprint_keywords=["qt", "linux"],
        limit=250,
        now=NOW,
    )

    assert captured["payload"] == {
        "hook": "architect_pre_blueprint",
        "guild": "intel",
        "product_name": "Acme Camera",
        "query": "Acme Camera qt linux",
        "best_practice_topic": "secure architecture Acme Camera qt linux",
        "requested_reports": ["cve", "zero_day", "best_practice"],
        "limit": 100,
        "context": {"blueprint_keywords": ["qt", "linux"]},
    }
    assert result["hook"] == "architect_pre_blueprint"
    assert result["status"] == "clean"
    assert result["reports"] == []


@pytest.mark.asyncio
async def test_a2a_threat_intel_failure_returns_passive_error_brief():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "failed", "last_error": "partner feed unavailable"},
        )

    registry = _A2AThreatIntelRegistry(handler)

    result = await integration_engineer_pre_install_a2a_hook(
        registry=registry,
        tenant_id=TENANT_ID,
        product_name="Acme Camera",
        now=NOW,
    )

    assert result["status"] == "clean"
    assert result["blocking"] is False
    assert result["reports"] == [
        {
            "kind": "a2a",
            "query": "Acme Camera",
            "source": "a2a:threat-intel",
            "fetched_at": "2026-05-06T08:30:00+00:00",
            "total_items": 0,
            "items": [],
            "error": "partner feed unavailable",
        }
    ]
    assert "partner feed unavailable" in result["brief"]
