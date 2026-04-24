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
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from backend import auth as _au
from backend.agents.graph import run_graph
from backend.db_pool import get_conn
from backend.events import emit_chat_message, emit_pipeline_phase, emit_session_titled
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

    ZZ.B2 #304-2 checkbox 1: also upserts the ``chat_sessions`` row
    and schedules the auto-title background task when the session hits
    3 user turns (no auto_title yet). Both are best-effort — a failure
    here must not block the chat response.
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
    # ZZ.B2 #304-2 checkbox 1 — keep the chat_sessions row fresh and
    # fire the 3-user-turn auto-title trigger. Best-effort: a failure
    # here must never bubble up and break the chat response.
    if session_id:
        try:
            await _db.upsert_chat_session(
                conn, session_id=session_id, user_id=user_id,
            )
        except Exception as exc:
            logger.debug("upsert_chat_session failed: %s", exc)
        if payload["role"] == "user":
            try:
                await _maybe_schedule_auto_title(
                    conn, session_id=session_id, user_id=user_id,
                )
            except Exception as exc:
                logger.debug("auto-title trigger check failed: %s", exc)


# ZZ.B2 #304-2 checkbox 1: auto-title generation.
#
# Module-global audit (SOP Step 1, 2026-04-21 rule): the only module-
# global state here is ``_auto_title_inflight`` — a per-worker set of
# ``(user_id, session_id)`` pairs the current process has already
# scheduled for title generation. The reason this is a per-worker set
# and not a cross-worker lock:
#
#   1. PG is the single source of truth. ``set_session_auto_title``
#      uses a conditional UPDATE (``NOT (metadata ? 'auto_title')``)
#      so concurrent 3-turn triggers from two uvicorn workers
#      converge to one winner. The loser's UPDATE affects 0 rows
#      and :func:`emit_session_titled` is only called for the winner.
#   2. The in-process set is a cheap dedupe — prevents the same
#      worker from firing 3 overlapping background tasks because of
#      3 chat writes hitting the 3-user-turn threshold near-simultaneously
#      (e.g. if the user pastes 3 messages back-to-back). Losing
#      this set is acceptable (next 3 writes would still race to the
#      conditional UPDATE and PG picks the winner) — falls under SOP
#      "故意每 worker 獨立" (acceptable answer #3).
#
# Net effect: at-most-once auto-title per session, enforced by PG;
# the per-worker set is a latency-optimisation, not a correctness
# guarantee.

_auto_title_inflight: set[tuple[str, str]] = set()
_AUTO_TITLE_TURN_THRESHOLD = 3


async def _maybe_schedule_auto_title(
    conn: asyncpg.Connection,
    *,
    session_id: str,
    user_id: str,
) -> None:
    """Kick off auto-title generation when the session hits 3 user turns.

    Skips when:
      * ``metadata.auto_title`` is already set for this session (the
        LLM has already titled it — don't re-title on every further
        turn, wastes tokens).
      * The same ``(user_id, session_id)`` is already in-flight in
        this worker process.
      * Fewer than 3 user turns have been persisted yet.
    """
    from backend import db as _db
    meta = await _db.get_chat_session_metadata(
        conn, session_id=session_id, user_id=user_id,
    )
    if meta is not None and "auto_title" in meta:
        return
    key = (user_id, session_id)
    if key in _auto_title_inflight:
        return
    count = await _db.count_user_turns_in_session(
        conn, session_id=session_id, user_id=user_id,
    )
    if count < _AUTO_TITLE_TURN_THRESHOLD:
        return
    _auto_title_inflight.add(key)
    from backend.db_context import current_tenant_id
    tenant_id = current_tenant_id() or "t-default"
    asyncio.create_task(
        _generate_auto_title(
            session_id=session_id, user_id=user_id, tenant_id=tenant_id,
        ),
    )


async def _generate_auto_title(
    *,
    session_id: str,
    user_id: str,
    tenant_id: str,
) -> None:
    """Background task: condense the first 3 user turns → LLM → persist title.

    Uses its own pool connection (the request-scoped ``conn`` has
    returned by the time this runs). Failures are logged + swallowed —
    the session just stays titled-by-hash until the next chance.
    """
    from backend import db as _db
    from backend.db_pool import get_pool
    from backend.db_context import set_tenant_id, current_tenant_id
    title_for_emit: str = ""
    # contextvars are task-local — an ``asyncio.create_task`` spawn
    # gets its own copy so restoring isn't strictly required here, but
    # we capture + restore anyway so the pattern survives a future
    # refactor that reuses a shared task.
    prior_tenant = current_tenant_id()
    try:
        set_tenant_id(tenant_id)
        pool = get_pool()
        async with pool.acquire() as conn:
            rows = await _db.list_chat_messages(conn, user_id, limit=50)
            user_turns = [
                r["content"] for r in rows if r.get("role") == "user"
                and r.get("session_id") == session_id
            ][:_AUTO_TITLE_TURN_THRESHOLD]
            if len(user_turns) < _AUTO_TITLE_TURN_THRESHOLD:
                return
            title = await _compose_title_via_llm(user_turns)
            if not title:
                return
            updated = await _db.set_session_auto_title(
                conn, session_id=session_id, user_id=user_id, title=title,
            )
            if not updated:
                return
        title_for_emit = title
    except Exception as exc:
        logger.warning("auto-title generation failed: %s", exc)
    finally:
        set_tenant_id(prior_tenant)
        _auto_title_inflight.discard((user_id, session_id))
    if title_for_emit:
        try:
            emit_session_titled(
                session_id=session_id,
                user_id=user_id,
                title=title_for_emit,
                source="auto",
                broadcast_scope="user",
                tenant_id=tenant_id,
            )
        except Exception as exc:
            logger.debug("emit_session_titled failed: %s", exc)


# Hard cap on each condensed turn so a runaway 10k-char pasted log
# doesn't drive up the title-generation prompt.
_AUTO_TITLE_CONDENSE_CHARS = 240


def _condense_turn(text: str) -> str:
    """Compact a single user turn for the title-prompt input.

    Keeps the first line and the first ``_AUTO_TITLE_CONDENSE_CHARS``
    chars — sufficient for the LLM to infer intent, cheap on tokens.
    """
    s = (text or "").strip()
    if not s:
        return ""
    # Collapse multi-line paste: first non-empty line carries most intent.
    first_line = next((ln for ln in s.splitlines() if ln.strip()), s)
    return first_line.strip()[:_AUTO_TITLE_CONDENSE_CHARS]


async def _compose_title_via_llm(user_turns: list[str]) -> str:
    """Call an LLM to produce a <= 8-word descriptive title.

    Uses the configured primary provider via ``get_llm()`` — ZZ.B2
    checkbox 3 is the follow-up that swaps in a ``get_cheapest_model()``
    helper to avoid burning the Opus quota on 10-char titles.
    Intentionally scoped to one LLM call; any error is returned as
    an empty string so the caller skips the SSE emit.
    """
    from backend.agents.llm import get_llm
    condensed = [_condense_turn(t) for t in user_turns if t]
    condensed = [c for c in condensed if c]
    if not condensed:
        return ""
    joined = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(condensed))
    prompt = (
        "Summarize the following chat conversation as a short, "
        "descriptive title (max 8 words, no quotes, no trailing "
        "punctuation, no prefixes like 'Chat:'). Return ONLY the "
        "title text.\n\n" + joined
    )
    try:
        llm = get_llm()
        if llm is None:
            return ""
        response = await asyncio.wait_for(llm.ainvoke(prompt), timeout=15.0)
        content = getattr(response, "content", response)
        if isinstance(content, list):
            # Structured LLM output — flatten like turn.complete does.
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text") or block.get("content") or ""
                    if text:
                        parts.append(str(text))
                else:
                    parts.append(str(block))
            content = "\n".join(parts)
        return _sanitize_title(str(content))
    except Exception as exc:
        logger.debug("LLM title generation failed: %s", exc)
        return ""


def _sanitize_title(raw: str) -> str:
    """Trim LLM output to a single-line title, bounded length.

    Strips leading/trailing quotes, dangling punctuation, and caps at
    ~80 chars. Matches the common 'llm returns "Thing title"' pattern
    so the sidebar never renders the outer quotes.
    """
    s = (raw or "").strip()
    if not s:
        return ""
    # Take only the first non-empty line.
    s = next((ln for ln in s.splitlines() if ln.strip()), s).strip()
    # Strip surrounding quotes a chatty model sometimes adds.
    for pair in ('""', "''", "``"):
        if len(s) >= 2 and s[0] == pair[0] and s[-1] == pair[1]:
            s = s[1:-1].strip()
    # Drop "Title:" / "Chat:" style prefixes.
    low = s.lower()
    for prefix in ("title:", "chat:", "session:", "conversation:"):
        if low.startswith(prefix):
            s = s[len(prefix):].strip()
            low = s.lower()
    return s[:80].rstrip(" .,;:")


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


@router.get("/sessions")
async def list_sessions(
    user: _au.User = Depends(_au.require_operator),
    conn: asyncpg.Connection = Depends(get_conn),
    limit: int = 50,
):
    """ZZ.B2 #304-2 checkbox 1: list the user's recent chat sessions.

    Drives the left-sidebar workflow/chat list. Each row returns the
    session hash + ``metadata`` so the UI can pick between
    ``user_title`` / ``auto_title`` / hash per the checkbox-2 fallback
    chain (implemented on the frontend, not here — this endpoint is
    purely a projection).

    Tenant-scoped via the request's tenant contextvar. ``limit`` is
    clamped to [1, 200] so a malicious caller can't OOM by asking
    for a huge page.
    """
    from backend import db as _db
    bounded = max(1, min(int(limit), 200))
    items = await _db.list_chat_sessions_for_user(
        conn, user.id, limit=bounded,
    )
    return {"items": items, "count": len(items)}


class SessionTitleBody(BaseModel):
    """Body for ``PATCH /chat/sessions/{session_id}/title``.

    ``title`` is the operator-authored override. ``None`` or empty/
    whitespace clears the override (reverts to ``auto_title`` or hash
    per the frontend fallback chain). 120-char defensive cap mirrors
    the one ``set_session_auto_title`` applies to LLM output so the
    sidebar never has to truncate on render.
    """
    title: str | None = Field(default=None, max_length=120)


@router.patch("/sessions/{session_id}/title")
async def rename_session(
    session_id: str,
    body: SessionTitleBody,
    request: Request,
    user: _au.User = Depends(_au.require_operator),
    conn: asyncpg.Connection = Depends(get_conn),
):
    """ZZ.B2 #304-2 checkbox 2: operator-authored session rename.

    Sets (or clears when ``title`` is empty/``None``) the
    ``metadata.user_title`` field on ``chat_sessions`` for the current
    user + tenant. When set, the frontend fallback chain prefers the
    ``user_title`` over ``auto_title`` / hash. When cleared, the row
    drops back to whichever auto/hash label the rest of the chain
    yields.

    Emits ``session.titled`` with ``source="user"`` so other devices
    of the same operator relabel the sidebar row in-place; the SSE
    path mirrors the auto-title path (checkbox 1) so the frontend
    reducer branch doesn't need a new event type.

    A 404 is returned when no row matched — typically means the
    session was never persisted (no chat_messages write yet) or it
    belongs to a different tenant.
    """
    from backend import db as _db
    updated = await _db.set_session_user_title(
        conn, session_id=session_id, user_id=user.id, title=body.title,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="session not found")
    meta = await _db.get_chat_session_metadata(
        conn, session_id=session_id, user_id=user.id,
    ) or {}
    # Pick the effective title to broadcast: the cleaned input on set,
    # or the surviving auto_title (if any) on clear. Empty broadcast
    # title is the signal to the sidebar that the user override went
    # away — it then falls back to auto / hash via resolveSessionTitle.
    cleaned = (body.title or "").strip()
    if cleaned:
        broadcast_title = cleaned[:120]
    else:
        broadcast_title = str(meta.get("auto_title", ""))
    try:
        from backend.db_context import current_tenant_id
        emit_session_titled(
            session_id=session_id,
            user_id=user.id,
            title=broadcast_title,
            source="user",
            broadcast_scope="user",
            tenant_id=current_tenant_id(),
        )
    except Exception as exc:  # pragma: no cover — best-effort fan-out
        logger.debug("emit_session_titled (user) failed: %s", exc)
    return {
        "session_id": session_id,
        "metadata": meta,
    }
