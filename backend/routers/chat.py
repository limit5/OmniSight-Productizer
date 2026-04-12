"""Chat / Orchestrator endpoint powered by LangGraph agent topology."""

import asyncio
import json
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from backend.agents.graph import run_graph
from backend.events import emit_pipeline_phase
from backend.routers.system import add_system_log
from backend.models import (
    AISuggestion,
    ChatRequest,
    ChatResponse,
    MessageRole,
    OrchestratorMessage,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])

_history: list[OrchestratorMessage] = []


def _now() -> str:
    return datetime.now().isoformat()


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
            timestamp=_now(),
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
            timestamp=_now(),
        )


async def _try_slash_command(message: str) -> OrchestratorMessage | None:
    """Intercept /command before LLM pipeline. Returns reply or None."""
    if not message.startswith("/"):
        return None
    parts = message[1:].strip().split(None, 1)
    cmd_name = parts[0].lower() if parts else ""
    cmd_args = parts[1] if len(parts) > 1 else ""
    from backend.slash_commands import handle_slash_command
    result = await handle_slash_command(cmd_name, cmd_args)
    if result is None:
        return None  # Unknown command — fall through to LLM
    return OrchestratorMessage(
        id=f"msg-{uuid.uuid4().hex[:6]}",
        role=MessageRole.orchestrator,
        content=result,
        timestamp=_now(),
    )


@router.post("", response_model=ChatResponse)
async def chat(body: ChatRequest):
    user_message = OrchestratorMessage(
        id=f"msg-{uuid.uuid4().hex[:6]}",
        role=MessageRole.user,
        content=body.message,
        timestamp=_now(),
    )
    _history.append(user_message)
    # Slash command interception — skip LLM if handled
    slash_reply = await _try_slash_command(body.message)
    if slash_reply:
        _history.append(slash_reply)
        return ChatResponse(message=slash_reply)
    reply = await _run_pipeline(body.message)
    _history.append(reply)
    return ChatResponse(message=reply)


@router.post("/stream")
async def chat_stream(body: ChatRequest):
    """SSE streaming — pipeline runs with real-time events pushed via event bus,
    then the final answer is streamed token-by-token here."""
    # Slash command interception
    slash_reply = await _try_slash_command(body.message)
    reply = slash_reply if slash_reply else await _run_pipeline(body.message)

    _history.append(OrchestratorMessage(
        id=f"msg-{uuid.uuid4().hex[:6]}",
        role=MessageRole.user,
        content=body.message,
        timestamp=_now(),
    ))
    _history.append(reply)

    async def event_generator():
        words = reply.content.split()
        for i, word in enumerate(words):
            yield {"event": "token", "data": json.dumps({"token": word, "index": i})}
            await asyncio.sleep(0.04)
        yield {"event": "done", "data": json.dumps(reply.model_dump())}

    return EventSourceResponse(event_generator())


@router.get("/history", response_model=list[OrchestratorMessage])
async def get_history():
    return _history


@router.delete("/history", status_code=204)
async def clear_history():
    _history.clear()
