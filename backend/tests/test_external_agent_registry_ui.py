"""BP.A2A.6 -- external A2A agent operator UI/API contract."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend import auth
from backend.agents.external_agent_registry import ExternalAgentRegistry


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _user(role: str) -> auth.User:
    return auth.User(
        id=f"u-{role}",
        email=f"{role}@example.com",
        name=role,
        role=role,
        tenant_id="t-default",
    )


def _request(registry: ExternalAgentRegistry):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        external_agent_registry=registry,
    )))


def test_router_roles_are_read_all_write_operator() -> None:
    from backend.routers import external_agents

    list_src = inspect.getsource(external_agents.list_external_agents)
    post_src = inspect.getsource(external_agents.register_external_agent)
    patch_src = inspect.getsource(external_agents.patch_external_agent)

    assert "Depends(auth.require_viewer)" in list_src
    assert "Depends(auth.require_operator)" in post_src
    assert "Depends(auth.require_operator)" in patch_src


@pytest.mark.asyncio
async def test_viewer_can_inspect_but_response_is_read_only() -> None:
    from backend.routers import external_agents

    registry = ExternalAgentRegistry()
    req = _request(registry)
    await external_agents.register_external_agent(
        external_agents.RegisterExternalAgentRequest(
            agent_id="threat-intel",
            display_name="Threat Intel",
            base_url="https://agent.example.com",
            agent_name="orchestrator",
        ),
        req,  # type: ignore[arg-type]
        _user("operator"),
    )

    res = await external_agents.list_external_agents(
        req,  # type: ignore[arg-type]
        actor=_user("viewer"),
    )
    body = json.loads(res.body)

    assert body["can_register"] is False
    assert body["external_agents"][0]["agent_id"] == "threat-intel"
    assert body["external_agents"][0]["agent_card_url"].endswith(
        "/.well-known/agent.json"
    )


@pytest.mark.asyncio
async def test_operator_registers_and_toggles_endpoint() -> None:
    from backend.routers import external_agents

    registry = ExternalAgentRegistry()
    req = _request(registry)

    created = await external_agents.register_external_agent(
        external_agents.RegisterExternalAgentRequest(
            agent_id="partner-bsp",
            display_name="Partner BSP",
            base_url="https://partner.example.com/a2a/",
            agent_name="bsp",
            auth_mode="bearer",
            token_ref="secret:partner-bsp",
            tags=["bsp", "partner"],
            capabilities=["device_tree"],
        ),
        req,  # type: ignore[arg-type]
        _user("operator"),
    )
    created_body = json.loads(created.body)

    assert created_body["external_agent"]["base_url"] == "https://partner.example.com/a2a"
    assert created_body["external_agent"]["token_ref"] == "secret:partner-bsp"
    assert created_body["external_agent"]["enabled"] is True

    patched = await external_agents.patch_external_agent(
        "partner-bsp",
        external_agents.PatchExternalAgentRequest(enabled=False),
        req,  # type: ignore[arg-type]
        _user("operator"),
    )
    patched_body = json.loads(patched.body)

    assert patched_body["external_agent"]["enabled"] is False


def test_main_includes_external_agents_router() -> None:
    src = (PROJECT_ROOT / "backend/main.py").read_text()
    assert "external_agents as _external_agents_router" in src
    assert "app.include_router(_external_agents_router.router" in src


def test_frontend_api_exports_external_agent_wrappers() -> None:
    src = (PROJECT_ROOT / "lib/api.ts").read_text()
    for token in [
        "ExternalAgentRow",
        "listExternalAgents",
        "registerExternalAgent",
        "patchExternalAgent",
        '"/external-agents"',
    ]:
        assert token in src


def test_operations_console_page_wires_registration_controls() -> None:
    src = (PROJECT_ROOT / "app/admin/external-agents/page.tsx").read_text()
    for token in [
        "data-testid=\"admin-external-agents-page\"",
        "Register External Agent",
        "listExternalAgents",
        "registerExternalAgent",
        "patchExternalAgent",
        "roleAtLeast(user?.role, \"operator\")",
    ]:
        assert token in src
