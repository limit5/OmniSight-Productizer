"""BP.A2A.2 — inbound Agent-to-Agent discovery and invocation routes.

Module-global state audit (SOP Step 1): this router keeps only immutable
constants at module scope. AgentCard data is rebuilt from request-derived
public base URLs, and invocation state is run-scoped per request; no
cross-worker cache or singleton coordination is required.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sse_starlette.sse import EventSourceResponse

from backend import auth as _auth
from backend import pep_gateway as _pep
from backend.a2a.agent_card import (
    DEFAULT_DISCOVERY_PATH,
    DEFAULT_STREAM_EVENTS,
    AgentCard,
    build_agent_card,
    build_capability_descriptors,
)
from backend.agents.graph import run_graph
from backend.agents.state import GraphState
from backend.sandbox_tier import Guild


router = APIRouter(tags=["a2a"])

A2A_INVOKE_PEP_TOOL = "a2a_invoke"
A2A_INVOKE_PEP_TIER = "t1"
A2A_PEP_HOLD_TIMEOUT_S = 600.0
_A2A_GUILD_NAMES = frozenset(guild.value for guild in Guild)


class A2AInvokeRequest(BaseModel):
    """Minimal A2A invoke payload accepted by OmniSight inbound routes."""

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    input: str | dict[str, Any] | None = None
    message: str | None = None
    command: str | None = None
    task: str | None = None
    stream: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


def _derive_public_base_url(request: Request) -> str:
    """Build the externally-facing origin for AgentCard endpoint URLs."""

    fwd_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    fwd_host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    if fwd_proto and fwd_host:
        return f"{fwd_proto}://{fwd_host}"
    return str(request.base_url or "").rstrip("/")


def _known_agent_names(public_base_url: str) -> frozenset[str]:
    return frozenset(
        descriptor.agent_name
        for descriptor in build_capability_descriptors(public_base_url)
    )


def _coerce_command(body: A2AInvokeRequest) -> str:
    for value in (body.command, body.message, body.task):
        if value:
            return value
    if isinstance(body.input, str) and body.input:
        return body.input
    if isinstance(body.input, dict):
        for key in ("command", "message", "task", "prompt", "text"):
            value = body.input.get(key)
            if value:
                return str(value)
    raise HTTPException(
        status_code=422,
        detail="A2A invoke payload requires command, message, task, or input text",
    )


def _graph_to_a2a_payload(
    *,
    invocation_id: str,
    agent_name: str,
    graph: GraphState,
    pep_decision: _pep.PepDecision,
) -> dict[str, Any]:
    status = "failed" if graph.last_error else "completed"
    return {
        "invocation_id": invocation_id,
        "agent_name": agent_name,
        "status": status,
        "routed_to": graph.routed_to,
        "answer": graph.answer,
        "last_error": graph.last_error,
        "pep_decision_id": pep_decision.id,
        "pep_action": pep_decision.action.value,
        "tool_results": [
            result.model_dump()
            for result in graph.tool_results
        ],
        "actions": [
            action.model_dump()
            for action in graph.actions
        ],
    }


async def _run_a2a_graph(
    *,
    command: str,
    agent_name: str,
    invocation_id: str,
) -> GraphState:
    try:
        return await run_graph(
            command,
            agent_sub_type=agent_name,
            task_id=invocation_id,
        )
    except Exception as exc:  # noqa: BLE001 — A2A returns task_failed
        return GraphState(
            user_command=command,
            routed_to=agent_name,
            answer="",
            last_error=f"{exc.__class__.__name__}: {exc}",
            task_id=invocation_id,
        )


async def _authorize_a2a_invoke(
    *,
    agent_name: str,
    command: str,
    user: _auth.User,
) -> _pep.PepDecision:
    decision = await _pep.evaluate(
        tool=A2A_INVOKE_PEP_TOOL,
        arguments={
            "agent_name": agent_name,
            "command": command,
            "tenant_id": user.tenant_id,
            "caller": user.email,
        },
        agent_id=f"a2a:{user.email}",
        tier=A2A_INVOKE_PEP_TIER,
        guild_id=agent_name if agent_name in _A2A_GUILD_NAMES else None,
        hold_timeout_s=A2A_PEP_HOLD_TIMEOUT_S,
    )
    if decision.action is not _pep.PepAction.auto_allow:
        raise HTTPException(
            status_code=403,
            detail={
                "reason": "pep_denied",
                "pep_action": decision.action.value,
                "pep_rule": decision.rule,
                "pep_reason": decision.reason,
            },
        )
    return decision


@router.get(DEFAULT_DISCOVERY_PATH, response_model=AgentCard)
async def discover_agent_card(
    request: Request,
    _user: _auth.User = Depends(_auth.require_viewer),
) -> AgentCard:
    """Publish OmniSight's public A2A AgentCard."""

    return build_agent_card(_derive_public_base_url(request))


@router.post("/a2a/invoke/{agent_name}")
async def invoke_agent(
    agent_name: str,
    body: A2AInvokeRequest,
    request: Request,
    stream: bool = False,
    user: _auth.User = Depends(_auth.require_operator),
):
    """Invoke an OmniSight specialist via sync JSON or streaming SSE."""

    base_url = _derive_public_base_url(request)
    if agent_name not in _known_agent_names(base_url):
        raise HTTPException(status_code=404, detail=f"unknown A2A agent {agent_name!r}")

    command = _coerce_command(body)
    invocation_id = f"a2a-{uuid.uuid4().hex[:12]}"
    pep_decision = await _authorize_a2a_invoke(
        agent_name=agent_name,
        command=command,
        user=user,
    )
    wants_stream = stream or body.stream

    if wants_stream:
        async def event_generator():
            yield {
                "event": DEFAULT_STREAM_EVENTS[0],
                "data": json.dumps({
                    "invocation_id": invocation_id,
                    "agent_name": agent_name,
                }),
            }
            yield {
                "event": DEFAULT_STREAM_EVENTS[1],
                "data": json.dumps({
                    "invocation_id": invocation_id,
                    "pep_decision_id": pep_decision.id,
                }),
            }
            graph = await _run_a2a_graph(
                command=command,
                agent_name=agent_name,
                invocation_id=invocation_id,
            )
            payload = _graph_to_a2a_payload(
                invocation_id=invocation_id,
                agent_name=agent_name,
                graph=graph,
                pep_decision=pep_decision,
            )
            yield {
                "event": DEFAULT_STREAM_EVENTS[2],
                "data": json.dumps({
                    "invocation_id": invocation_id,
                    "answer": graph.answer,
                    "routed_to": graph.routed_to,
                }),
            }
            event_name = (
                DEFAULT_STREAM_EVENTS[4]
                if graph.last_error
                else DEFAULT_STREAM_EVENTS[3]
            )
            yield {"event": event_name, "data": json.dumps(payload)}

        return EventSourceResponse(event_generator())

    graph = await _run_a2a_graph(
        command=command,
        agent_name=agent_name,
        invocation_id=invocation_id,
    )
    return JSONResponse(
        content=_graph_to_a2a_payload(
            invocation_id=invocation_id,
            agent_name=agent_name,
            graph=graph,
            pep_decision=pep_decision,
        ),
    )
