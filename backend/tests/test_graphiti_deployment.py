"""OP-901 — Graphiti MCP deployment and ingestion contracts."""
from __future__ import annotations

import importlib.util
import json
import sys
import urllib.error
from pathlib import Path
from typing import Any

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from backend.agents import mcp_integration as mcp
from backend.integrations import jira_to_graphiti_webhook as jira_graphiti
from backend.routers import webhooks

REPO_ROOT = Path(__file__).resolve().parents[2]


class _Response:
    def __init__(self, body: dict[str, Any] | None = None) -> None:
        self.body = body or {"status": "ok"}

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.body).encode()


def _load_bridge_module():
    path = REPO_ROOT / "scripts" / "bridge_gerrit_to_graphiti.py"
    spec = importlib.util.spec_from_file_location("bridge_gerrit_to_graphiti", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_compose_declares_graphiti_container_healthcheck_and_auth_env() -> None:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    service = compose["services"]["graphiti"]

    assert service["image"] == "getzep/graphiti:latest"
    assert service["profiles"] == ["graphiti"]
    assert "${OMNISIGHT_GRAPHITI_PORT:-8003}:8000" in service["ports"]
    assert any(
        item.startswith("OMNISIGHT_MCP_GRAPHITI_TOKEN=")
        for item in service["environment"]
    )
    healthcheck = " ".join(service["healthcheck"]["test"])
    assert "http://localhost:8000/api/v1/health" in healthcheck


def test_caddy_snippet_routes_dns_name_to_graphiti_with_tls() -> None:
    text = (REPO_ROOT / "deploy/caddy/mcp-graphiti.caddy").read_text()

    assert "mcp-graphiti.sora.services" in text
    assert "reverse_proxy {$OMNISIGHT_GRAPHITI_UPSTREAM:127.0.0.1:8003}" in text
    assert "/api/v1/health" in text
    assert "Strict-Transport-Security" in text


def test_graphiti_mcp_env_registration_and_read_only_contract() -> None:
    env = {"OMNISIGHT_MCP_GRAPHITI_TOKEN": "token-123"}
    registry = mcp.build_registry_from_env(env=env)
    servers = list(registry.list_all(enabled_only=True))

    assert len(servers) == 1
    assert servers[0].name == "mcp_graphiti"
    assert servers[0].url == "https://mcp-graphiti.local"
    assert mcp.is_graphiti_mcp_read_only_tool("mcp__mcp_graphiti__getTicketTimeline")
    assert mcp.is_graphiti_mcp_read_only_tool("mcp__mcp_graphiti__queryTimeline")
    assert not mcp.is_graphiti_mcp_read_only_tool("mcp__mcp_graphiti__createEpisode")
    assert not mcp.is_graphiti_mcp_read_only_tool("mcp__mcp_graphiti__updateNode")
    assert not mcp.is_graphiti_mcp_read_only_tool("mcp__mcp_graphiti__deleteEpisode")


def test_jira_webhook_forwards_normalized_payload_with_graphiti_bearer() -> None:
    captured: list[Any] = []

    def opener(request: Any, timeout: float) -> _Response:
        captured.append((request, timeout))
        return _Response({"entity_id": "jira-OP-901"})

    payload = {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "id": "10001",
            "key": "OP-901",
            "fields": {
                "summary": "Graphiti MCP",
                "status": {"name": "In Progress"},
                "updated": "2026-05-11T10:00:00.000+0800",
            },
        },
        "user": {"accountId": "acct-1", "displayName": "Codex"},
        "changelog": {"id": "20002", "items": [{"field": "status"}]},
    }
    config = jira_graphiti.GraphitiWebhookConfig(
        graphiti_base_url="https://mcp-graphiti.sora.services",
        graphiti_token="graphiti-token",
        jira_webhook_secret="jira-secret",
    )

    result = jira_graphiti.handle_jira_webhook(
        payload,
        authorization="Bearer jira-secret",
        config=config,
        opener=opener,
    )

    assert result["status"] == "forwarded"
    request, timeout = captured[0]
    assert timeout == 10.0
    assert request.full_url == "https://mcp-graphiti.sora.services/ingest/jira"
    assert request.headers["Authorization"] == "Bearer graphiti-token"
    body = json.loads(request.data.decode())
    assert body["issue_key"] == "OP-901"
    assert body["status"] == "In Progress"
    assert body["changelog"]["items"] == [{"field": "status"}]


def test_jira_webhook_rejects_bad_inbound_token_before_forwarding() -> None:
    config = jira_graphiti.GraphitiWebhookConfig(
        graphiti_base_url="https://mcp-graphiti.sora.services",
        graphiti_token="graphiti-token",
        jira_webhook_secret="jira-secret",
    )

    result = jira_graphiti.handle_jira_webhook(
        {},
        authorization="Bearer wrong",
        config=config,
        opener=lambda *_args, **_kwargs: pytest.fail("should not forward"),
    )

    assert result == {"status": "rejected", "reason": "invalid_jira_webhook_token"}


@pytest.mark.asyncio
async def test_fastapi_jira_graphiti_route_delegates_to_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import FastAPI

    calls: list[dict[str, Any]] = []

    def fake_handle(payload: dict[str, Any], *, authorization: str, config: Any) -> dict[str, Any]:
        calls.append({
            "payload": payload,
            "authorization": authorization,
            "config": config,
        })
        return {"status": "forwarded", "graphiti": {"status": "ok"}}

    monkeypatch.setattr(
        webhooks.GraphitiWebhookConfig,
        "from_env",
        classmethod(lambda cls: cls("https://mcp-graphiti.sora.services", "token", "jira")),
    )
    monkeypatch.setattr(webhooks, "handle_jira_webhook", fake_handle)

    app = FastAPI()
    app.include_router(webhooks.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/webhooks/jira/graphiti",
            headers={"Authorization": "Bearer jira"},
            json={"issue": {"key": "OP-901"}},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "forwarded"
    assert calls[0]["payload"] == {"issue": {"key": "OP-901"}}
    assert calls[0]["authorization"] == "Bearer jira"


def test_gerrit_bridge_filters_and_forwards_required_events() -> None:
    bridge = _load_bridge_module()
    captured: list[Any] = []

    def opener(request: Any, timeout: float) -> _Response:
        captured.append((request, timeout))
        return _Response()

    config = bridge.GraphitiBridgeConfig(
        graphiti_base_url="https://mcp-graphiti.sora.services",
        graphiti_token="graphiti-token",
        gerrit_host="sora.services",
    )
    lines = [
        json.dumps({"type": "patchset-created", "change": {"id": "I1"}}),
        json.dumps({"type": "change-merged", "change": {"id": "I2"}}),
        json.dumps({
            "type": "comment-added",
            "change": {"id": "I3"},
            "approvals": [{"type": "Code-Review", "value": "2"}],
        }),
        json.dumps({
            "type": "comment-added",
            "change": {"id": "I4"},
            "approvals": [{"type": "Code-Review", "value": "1"}],
        }),
    ]

    assert bridge.forward_stream(lines, config, opener=opener) == 3
    assert [item[0].full_url for item in captured] == [
        "https://mcp-graphiti.sora.services/ingest/gerrit",
        "https://mcp-graphiti.sora.services/ingest/gerrit",
        "https://mcp-graphiti.sora.services/ingest/gerrit",
    ]
    assert captured[0][0].headers["Authorization"] == "Bearer graphiti-token"


def test_gerrit_bridge_raises_auth_rejected_on_401() -> None:
    bridge = _load_bridge_module()
    config = bridge.GraphitiBridgeConfig(
        graphiti_base_url="https://mcp-graphiti.sora.services",
        graphiti_token="bad-token",
        gerrit_host="sora.services",
    )

    def opener(request: Any, timeout: float) -> _Response:
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            "unauthorized",
            hdrs={},
            fp=None,
        )

    with pytest.raises(bridge.GraphitiAuthRejected):
        bridge.post_gerrit_event_to_graphiti(
            {"type": "patchset-created"},
            config,
            opener=opener,
        )


def test_runbook_documents_token_rotation_and_healthcheck() -> None:
    text = (REPO_ROOT / "docs/operations/graphiti-mcp-runbook.md").read_text()

    assert "OMNISIGHT_MCP_GRAPHITI_TOKEN" in text
    assert "Rotation procedure" in text
    assert "curl -fsS https://mcp-graphiti.sora.services/api/v1/health" in text
    assert "create*" in text and "delete*" in text
