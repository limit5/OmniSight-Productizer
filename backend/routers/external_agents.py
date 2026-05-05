"""BP.A2A.6 -- operator UI API for external A2A agent endpoints.

GET   /external-agents              -- operator-visible endpoint list
POST  /external-agents              -- register or update one endpoint
PATCH /external-agents/{agent_id}   -- operator kill-switch

Module-global state audit
-------------------------
The default registry uses the BP.A2A.6 in-memory store and is therefore
per-worker dev/test state by design. The route also checks
``request.app.state.external_agent_registry`` first, so production can
inject a durable store without changing the HTTP contract.

Read-after-write timing audit
-----------------------------
The in-memory store writes before building the response, so the caller
sees its own write inside one worker. Cross-worker visibility requires
the future durable store injection noted above; no PG/Redis timing
semantics are changed in this row.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend import auth
from backend.agents.external_agent_registry import (
    ExternalAgentEndpoint,
    ExternalAgentNotFoundError,
    ExternalAgentRegistry,
)


router = APIRouter(prefix="/external-agents", tags=["external-agents"])

ExternalAgentAuthModeLiteral = Literal["none", "bearer", "oauth2"]

_DEFAULT_REGISTRY = ExternalAgentRegistry()


class RegisterExternalAgentRequest(BaseModel):
    agent_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    description: str = ""
    auth_mode: ExternalAgentAuthModeLiteral = "none"
    token_ref: str = ""
    enabled: bool = True
    tags: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)


class PatchExternalAgentRequest(BaseModel):
    enabled: bool = Field(description="Operator kill-switch state.")


def _registry(request: Request) -> ExternalAgentRegistry:
    return getattr(request.app.state, "external_agent_registry", _DEFAULT_REGISTRY)


def _endpoint_payload(endpoint: ExternalAgentEndpoint) -> dict[str, Any]:
    return {
        "agent_id": endpoint.agent_id,
        "display_name": endpoint.display_name,
        "base_url": endpoint.base_url,
        "agent_card_url": endpoint.agent_card_url,
        "agent_name": endpoint.agent_name,
        "description": endpoint.description,
        "auth_mode": endpoint.auth_mode,
        "token_ref": endpoint.token_ref,
        "enabled": endpoint.enabled,
        "tags": list(endpoint.tags),
        "capabilities": list(endpoint.capabilities),
        "health_status": endpoint.health_status,
        "last_health_check": (
            None
            if endpoint.last_health_check is None
            else endpoint.last_health_check.isoformat()
        ),
        "registered_at": (
            None
            if endpoint.registered_at is None
            else endpoint.registered_at.isoformat()
        ),
        "updated_at": (
            None
            if endpoint.updated_at is None
            else endpoint.updated_at.isoformat()
        ),
        "config": endpoint.config,
    }


@router.get("")
async def list_external_agents(
    request: Request,
    actor: auth.User = Depends(auth.require_viewer),
) -> JSONResponse:
    """Return external A2A agent endpoints for Operations Console display."""
    endpoints = await _registry(request).list_endpoints()
    return JSONResponse(
        status_code=200,
        content={
            "external_agents": [_endpoint_payload(endpoint) for endpoint in endpoints],
            "can_register": auth.role_at_least(actor.role, "operator"),
        },
    )


@router.post("")
async def register_external_agent(
    req: RegisterExternalAgentRequest,
    request: Request,
    _actor: auth.User = Depends(auth.require_operator),
) -> JSONResponse:
    """Register or update one external A2A agent endpoint."""
    try:
        endpoint = ExternalAgentEndpoint(
            agent_id=req.agent_id,
            display_name=req.display_name,
            base_url=req.base_url,
            agent_name=req.agent_name,
            description=req.description,
            auth_mode=req.auth_mode,
            token_ref=req.token_ref,
            enabled=req.enabled,
            tags=tuple(req.tags),
            capabilities=tuple(req.capabilities),
            config=req.config,
        )
        saved = await _registry(request).register_endpoint(endpoint)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(status_code=200, content={"external_agent": _endpoint_payload(saved)})


@router.patch("/{agent_id:path}")
async def patch_external_agent(
    agent_id: str,
    req: PatchExternalAgentRequest,
    request: Request,
    _actor: auth.User = Depends(auth.require_operator),
) -> JSONResponse:
    """Set the operator kill-switch for one external A2A agent endpoint."""
    try:
        endpoint = await _registry(request).set_enabled(agent_id, req.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ExternalAgentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse(status_code=200, content={"external_agent": _endpoint_payload(endpoint)})
