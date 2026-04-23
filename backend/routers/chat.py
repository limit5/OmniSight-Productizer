"""Chat / Orchestrator endpoint powered by LangGraph agent topology.

Q.3-SUB-6 (#297, 2026-04-24): the pre-Q.3 module-global
``_history: list[OrchestratorMessage]`` has been retired. Chat
history now lives in the ``chat_messages`` PG table (alembic 0021)
and is scoped per-user + per-tenant, surviving restarts and
replica-level uvicorn worker fan-out. Every write emits a
``chat.message`` SSE event (``broadcast_scope='user'``) so a second
device owned by the same user appends the line without waiting for
a ``GET /chat/history`` refetch. Streaming token-by-token is still
bound to the originator HTTP response body — we only emit the
finalised persisted message on the bus, never intermediate tokens.

Module-global audit (SOP Step 1): this router now holds zero
module-level mutable state — persistence went to PG (compliant
answer #2 "PG/Redis coordination"), fan-out goes through the
process-global :class:`backend.events.EventBus` singleton whose
cross-worker delivery is covered by I10 Redis pub/sub (also answer
#2). The old ``_history`` list was the textbook textbook failure
case the audit explicitly called out.
"""

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime

import asyncpg
from fastapi import APIRouter, Depends, Request
from sse_starlette.sse import EventSourceResponse

from backend import auth as _au
from backend.agents.graph import run_graph
from backend.db_pool import get_conn
from backend.events import emit_chat_message, emit_pipeline_phase
from backend.models import (
    AISuggestion,
    ChatRequest,
    ChatResponse,
    MessageRole,
    OrchestratorMessage,
)
from backend.routers.system import add_system_log

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


# Ceiling on how many past messages ``/chat/history`` returns. The
# 30-day retention sweep keeps the table bounded to roughly this
# volume per active user anyway, but we double-cap here so an
# abnormally chatty day doesn't blow up the initial payload.
_HISTORY_LIMIT = 200


def _now_iso() -> str:
    return datetime.now().isoformat()


def _session_id_from_request(request: Request) -> str:
    """Stable session hash from the request context for SSE tagging.

    Falls back to empty string when no session is attached (e.g. open-
    auth mode or api-key bearer). The empty string is a sentinel —
    ``bus.publish`` treats it as "no session binding" and uses the
    caller-supplied ``broadcast_scope`` alone.
    """
    try:
        sess = getattr(request.state, "session", None)
        if sess and getattr(sess, "token", ""):
            return _au.session_id_from_token(sess.token)
    except Exception:
        return ""
    return ""


def _build_suggestion(result) -> AISuggestion | None:
    if not result.actions:
        return None
    action = result.actions[0]
    if action.agent_type and action.agent_type != "general":
        return AISuggestion(
            id=f"sug-{uuid.uuid4().hex[:6]}",
            type="assign",
            title=f"Dispatched to {action.agent_type.title()} Agent",
            description=action.detail or f"Routed to {action.agent_type} specialist",
            agent_type=action.agent_type,
            priority="high" if action.agent_type == "firmware" else "medium",
            status="pending",
        )
    return None


async def _run_pipeline(user_msg: str) -> OrchestratorMessage:
    """Run the LangGraph pipeline. Emits real-time events via the event bus."""
    try:
        emit_pipeline_phase("start", f"Processing: {user_msg[:80]}")
        add_system_log(f"Command received: {user_msg[:60]}", "info")
        result = await run_graph(user_msg)
        add_system_log(f"Routed to {result.routed_to}, {len(result.tool_results)} tool(s)", "info")
        emit_pipeline_phase("complete", f"Routed to {result.routed_to}, {len(result.tool_results)} tool(s) used")
        suggestion = _build_suggestion(result)
        return OrchestratorMessage(
            id=f"msg-{uuid.uuid4().hex[:6]}",
            role=MessageRole.orchestrator,
            content=result.answer,
            timestamp=_now_iso(),
            suggestion=suggestion,
        )
    except Exception as exc:
        logger.exception("Agent pipeline error")
        emit_pipeline_phase("error", str(exc))
        add_system_log(f"Pipeline error: {exc}", "error")
        return OrchestratorMessage(
            id=f"msg-{uuid.uuid4().hex[:6]}",
            role=MessageRole.orchestrator,
            content=f"[ORCHESTRATOR] Pipeline error: {exc}",
            timestamp=_now_iso(),
        )


async def _try_slash_command(
    conn: asyncpg.Connection, message: str,
) -> OrchestratorMessage | None:
    """Intercept /command before LLM pipeline. Returns reply or None.

    ``conn`` is the request-scoped pool connection — propagated to
    slash handlers that need to write (agents ``/spawn``, etc.). Most
    handlers are read-only / stateless and ignore it, but propagation
    is uniform for future-proofing as later SPs port more domains.
    """
    if not message.startswith("/"):
        return None
    parts = message[1:].strip().split(None, 1)
    cmd_name = parts[0].lower() if parts else ""
    cmd_args = parts[1] if len(parts) > 1 else ""
    from backend.slash_commands import handle_slash_command
    result = await handle_slash_command(conn, cmd_name, cmd_args)
    if result is None:
        return None  # Unknown command — fall through to LLM
    return OrchestratorMessage(
        id=f"msg-{uuid.uuid4().hex[:6]}",
        role=MessageRole.orchestrator,
        content=result,
        timestamp=_now_iso(),
    )


async def _persist_and_emit(
    conn: asyncpg.Connection,
    msg: OrchestratorMessage,
    *,
    user_id: str,
    session_id: str,
) -> None:
    """Write the message to ``chat_messages`` + fan it out on the bus.

    Per-write retention sweep: ``prune_chat_messages`` keeps the user's
    row count bounded to the last 30 days without needing a dedicated
    cron. Emit failures are swallowed — PG is the source of truth and
    the bus is latency-optimisation.
    """
    from backend import db as _db
    payload = {
        "id": msg.id,
        "user_id": user_id,
        "session_id": session_id,
        "role": msg.role.value if hasattr(msg.role, "value") else str(msg.role),
        "content": msg.content,
        "timestamp": time.time(),
    }
    try:
        await _db.insert_chat_message(conn, payload)
    except Exception as exc:
        # A write failure is real — surface it so the caller does NOT
        # double-emit on the bus (would mislead other devices that a
        # row landed when it did not). We log + re-raise so the client
        # sees the HTTP error instead of a silently dropped message.
        logger.exception("insert_chat_message failed for %s: %s", msg.id, exc)
        raise
    try:
        await _db.prune_chat_messages(conn, user_id)
    except Exception as exc:
        logger.debug("prune_chat_messages failed for user=%s: %s", user_id, exc)
    try:
        sugg_dict = msg.suggestion.model_dump() if msg.suggestion else None
        emit_chat_message(
            message_id=msg.id,
            user_id=user_id,
            role=payload["role"],
            content=msg.content,
            timestamp=msg.timestamp,
            session_id=session_id or None,
            suggestion=sugg_dict,
        )
    except Exception as exc:
        logger.debug("emit_chat_message failed for %s: %s", msg.id, exc)


@router.post("", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    request: Request,
    user: _au.User = Depends(_au.require_operator),
    _quota=Depends(_au.check_llm_quota),  # M4 per-user LLM rate limit
    conn: asyncpg.Connection = Depends(get_conn),
):
    session_id = _session_id_from_request(request)
    user_message = OrchestratorMessage(
        id=f"msg-{uuid.uuid4().hex[:6]}",
        role=MessageRole.user,
        content=body.message,
        timestamp=_now_iso(),
    )
    await _persist_and_emit(conn, user_message, user_id=user.id, session_id=session_id)
    # Slash command interception — skip LLM if handled
    slash_reply = await _try_slash_command(conn, body.message)
    if slash_reply:
        await _persist_and_emit(conn, slash_reply, user_id=user.id, session_id=session_id)
        return ChatResponse(message=slash_reply)
    reply = await _run_pipeline(body.message)
    await _persist_and_emit(conn, reply, user_id=user.id, session_id=session_id)
    return ChatResponse(message=reply)


@router.post("/stream")
async def chat_stream(
    body: ChatRequest,
    request: Request,
    user: _au.User = Depends(_au.require_operator),
    _quota=Depends(_au.check_llm_quota),  # M4 per-user LLM rate limit
    conn: asyncpg.Connection = Depends(get_conn),
):
    """SSE streaming — pipeline runs with real-time events pushed via event bus,
    then the final answer is streamed token-by-token here.

    The token-by-token chunks ride this HTTP response body only and
    stay bound to the originator session — we never ``bus.publish``
    intermediate chunks (would flood other devices with unreadable
    partial state). The finalised user + orchestrator messages are
    persisted + fanned out via ``chat.message`` once before the stream
    starts, so a second device sees them appear atomically.
    """
    session_id = _session_id_from_request(request)
    # Slash command interception
    slash_reply = await _try_slash_command(conn, body.message)
    reply = slash_reply if slash_reply else await _run_pipeline(body.message)

    user_msg = OrchestratorMessage(
        id=f"msg-{uuid.uuid4().hex[:6]}",
        role=MessageRole.user,
        content=body.message,
        timestamp=_now_iso(),
    )
    await _persist_and_emit(conn, user_msg, user_id=user.id, session_id=session_id)
    await _persist_and_emit(conn, reply, user_id=user.id, session_id=session_id)

    async def event_generator():
        words = reply.content.split()
        for i, word in enumerate(words):
            yield {"event": "token", "data": json.dumps({"token": word, "index": i})}
            await asyncio.sleep(0.04)
        yield {"event": "done", "data": json.dumps(reply.model_dump())}

    return EventSourceResponse(event_generator())


@router.get("/history", response_model=list[OrchestratorMessage])
async def get_history(
    user: _au.User = Depends(_au.require_operator),
    conn: asyncpg.Connection = Depends(get_conn),
):
    """Return the current user's chat history, oldest-first.

    Bounded to the most recent ``_HISTORY_LIMIT`` messages. The PG
    retention sweep keeps the table itself bounded to 30 days per
    user; this LIMIT is a second line of defence against pathological
    single-day chat volumes.
    """
    from backend import db as _db
    rows = await _db.list_chat_messages(conn, user.id, limit=_HISTORY_LIMIT)
    out: list[OrchestratorMessage] = []
    for r in rows:
        try:
            role = MessageRole(r["role"])
        except ValueError:
            # Unknown role from an old row — fall back to system so the
            # UI still renders something readable.
            role = MessageRole.system
        ts = r["timestamp"]
        ts_iso = (
            datetime.fromtimestamp(float(ts)).isoformat()
            if isinstance(ts, (int, float))
            else str(ts)
        )
        out.append(OrchestratorMessage(
            id=r["id"],
            role=role,
            content=r["content"],
            timestamp=ts_iso,
        ))
    return out


@router.delete("/history", status_code=204)
async def clear_history(
    user: _au.User = Depends(_au.require_admin),
    conn: asyncpg.Connection = Depends(get_conn),
):
    """Wipe the current admin's chat history.

    Tenant-scoped — even an admin can only clear their own tenant's
    rows for their own user_id. Cross-tenant or cross-user deletes
    are explicitly out of scope for this endpoint.
    """
    from backend import db as _db
    await _db.clear_chat_messages(conn, user.id)
