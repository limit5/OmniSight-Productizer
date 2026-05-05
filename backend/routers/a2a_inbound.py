"""BP.A2A.2 — inbound Agent-to-Agent discovery and invocation routes.

Module-global state audit (SOP Step 1): this router keeps only immutable
constants/classes at module scope. AgentCard data is rebuilt from
request-derived public base URLs, invocation state is per request, the
rate gate delegates to Redis-backed ``backend.rate_limit`` when
configured (documented per-worker fallback otherwise), and audit-chain
serialization is handled by PG advisory locks in ``backend.audit``.
"""

from __future__ import annotations

import json
import hashlib
import time
import uuid
from dataclasses import dataclass
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
from backend.db_context import current_tenant_id, set_tenant_id
from backend.sandbox_tier import Guild


router = APIRouter(tags=["a2a"])

A2A_INVOKE_PEP_TOOL = "a2a_invoke"
A2A_INVOKE_PEP_TIER = "t1"
A2A_PEP_HOLD_TIMEOUT_S = 600.0
DEFAULT_A2A_AGENT_RATE_CAPACITY = 60
DEFAULT_A2A_AGENT_RATE_WINDOW_SECONDS = 60.0
_A2A_GUILD_NAMES = frozenset(guild.value for guild in Guild)


@dataclass(frozen=True)
class A2ARateLimitConfig:
    """Per-tenant/per-AgentCard token bucket settings."""

    capacity: int = DEFAULT_A2A_AGENT_RATE_CAPACITY
    window_seconds: float = DEFAULT_A2A_AGENT_RATE_WINDOW_SECONDS


class A2ARateLimited(Exception):
    """Raised when a tenant exceeds one A2A AgentCard bucket."""

    def __init__(self, tenant_id: str, agent_name: str, retry_after_seconds: float) -> None:
        self.tenant_id = tenant_id
        self.agent_name = agent_name
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            f"Tenant {tenant_id} A2A agent {agent_name} rate limit exceeded; "
            f"retry in {retry_after_seconds:.2f}s"
        )


class A2ARateGate:
    """Per-tenant/per-AgentCard rate gate.

    Delegates to ``backend.rate_limit``: Redis coordinates prod workers;
    the documented in-memory fallback remains intentionally per-worker
    for local dev and tests.
    """

    def __init__(
        self,
        config: A2ARateLimitConfig | None = None,
        *,
        key_prefix: str = "a2a:agent",
    ) -> None:
        self.config = config or A2ARateLimitConfig()
        self.key_prefix = key_prefix

    def check(self, tenant_id: str, agent_name: str) -> None:
        tid = tenant_id or "t-default"
        agent = agent_name or "agent-card"
        from backend.rate_limit import get_limiter

        allowed, retry_after = get_limiter().allow(
            f"{self.key_prefix}:{tid}:{agent}",
            self.config.capacity,
            self.config.window_seconds,
        )
        if not allowed:
            raise A2ARateLimited(tid, agent, retry_after)


class A2AInvokeRequest(BaseModel):
    """Minimal A2A invoke payload accepted by OmniSight inbound routes."""

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    input: str | dict[str, Any] | None = None
    message: str | None = None
    command: str | None = None
    task: str | None = None
    stream: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


def _hash_jsonable(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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


async def _audit_a2a_event(
    *,
    action: str,
    entity_id: str,
    tenant_id: str,
    actor: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> None:
    saved = current_tenant_id()
    try:
        set_tenant_id(tenant_id or "t-default")
        try:
            from backend import audit as _audit
            await _audit.log(
                action=action,
                entity_kind="a2a_agent_card",
                entity_id=entity_id,
                before=before,
                after=after,
                actor=actor,
                session_id=session_id,
            )
        except Exception:
            pass
    finally:
        set_tenant_id(saved)


def _session_id(request: Request) -> str | None:
    sess = getattr(getattr(request, "state", None), "session", None)
    return sess.token if sess else None


def _raise_rate_limited(exc: A2ARateLimited) -> None:
    retry = max(1, int(exc.retry_after_seconds) + 1)
    raise HTTPException(
        status_code=429,
        detail={
            "reason": "a2a_rate_limited",
            "tenant_id": exc.tenant_id,
            "agent_name": exc.agent_name,
        },
        headers={"Retry-After": str(retry)},
    ) from exc


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
    user: _auth.User = Depends(_auth.require_viewer),
) -> AgentCard:
    """Publish OmniSight's public A2A AgentCard."""

    try:
        A2ARateGate().check(user.tenant_id, "agent-card")
    except A2ARateLimited as exc:
        _raise_rate_limited(exc)
    card = build_agent_card(_derive_public_base_url(request))
    await _audit_a2a_event(
        action="a2a_agent_card_discovered",
        entity_id="agent-card",
        tenant_id=user.tenant_id,
        actor=user.email,
        after={
            "tenant_id": user.tenant_id,
            "capability_count": len(card.capabilities),
            "card_hash": _hash_jsonable(card.model_dump()),
        },
        session_id=_session_id(request),
    )
    return card


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
    try:
        A2ARateGate().check(user.tenant_id, agent_name)
    except A2ARateLimited as exc:
        _raise_rate_limited(exc)
    wants_stream = stream or body.stream
    started_at = time.perf_counter()
    request_hash = _hash_jsonable(body.model_dump(mode="json"))

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
            await _audit_a2a_event(
                action="a2a_agent_invoked",
                entity_id=invocation_id,
                tenant_id=user.tenant_id,
                actor=user.email,
                before={
                    "tenant_id": user.tenant_id,
                    "agent_name": agent_name,
                    "request_hash": request_hash,
                    "stream": True,
                },
                after={
                    "tenant_id": user.tenant_id,
                    "agent_name": agent_name,
                    "response_hash": _hash_jsonable(payload),
                    "latency_ms": int((time.perf_counter() - started_at) * 1000),
                    "status": payload["status"],
                    "pep_decision_id": pep_decision.id,
                },
                session_id=_session_id(request),
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
    payload = _graph_to_a2a_payload(
        invocation_id=invocation_id,
        agent_name=agent_name,
        graph=graph,
        pep_decision=pep_decision,
    )
    await _audit_a2a_event(
        action="a2a_agent_invoked",
        entity_id=invocation_id,
        tenant_id=user.tenant_id,
        actor=user.email,
        before={
            "tenant_id": user.tenant_id,
            "agent_name": agent_name,
            "request_hash": request_hash,
            "stream": False,
        },
        after={
            "tenant_id": user.tenant_id,
            "agent_name": agent_name,
            "response_hash": _hash_jsonable(payload),
            "latency_ms": int((time.perf_counter() - started_at) * 1000),
            "status": payload["status"],
            "pep_decision_id": pep_decision.id,
        },
        session_id=_session_id(request),
    )
    return JSONResponse(
        content=payload,
    )
