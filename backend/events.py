"""Global event bus for real-time SSE push to frontend.

Every emit_* call does TWO things:
  1. Pushes an SSE event to connected clients (real-time UI updates)
  2. Writes to the system log buffer (REPORTER VORTEX display)
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# Late import to avoid circular — resolved at first use
_log_fn = None


def _log(message: str, level: str = "info") -> None:
    """Write to the system log buffer (REPORTER VORTEX)."""
    global _log_fn
    if _log_fn is None:
        from backend.routers.system import add_system_log
        _log_fn = add_system_log
    _log_fn(message, level)


class EventBus:
    """Simple pub/sub for SSE events."""

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers = [s for s in self._subscribers if s is not q]

    def publish(self, event: str, data: dict[str, Any]) -> None:
        data.setdefault("timestamp", datetime.now().isoformat())
        msg = {"event": event, "data": json.dumps(data)}
        dead: list[asyncio.Queue] = []
        for q in self._subscribers:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._subscribers.remove(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


# Singleton
bus = EventBus()


# ─── Convenience publishers (each one also writes to REPORTER VORTEX log) ───

def emit_agent_update(agent_id: str, status: str, thought_chain: str = "", **extra: Any) -> None:
    bus.publish("agent_update", {
        "agent_id": agent_id,
        "status": status,
        "thought_chain": thought_chain,
        **extra,
    })
    level = "error" if status == "error" else "warn" if status == "warning" else "info"
    _log(f"[AGENT] {agent_id} → {status.upper()}" + (f": {thought_chain[:80]}" if thought_chain else ""), level)


def emit_task_update(task_id: str, status: str, assigned_agent_id: str | None = None, **extra: Any) -> None:
    bus.publish("task_update", {
        "task_id": task_id,
        "status": status,
        "assigned_agent_id": assigned_agent_id,
        **extra,
    })
    _log(f"[TASK] {task_id} → {status.upper()}" + (f" (agent: {assigned_agent_id})" if assigned_agent_id else ""))


def emit_tool_progress(tool_name: str, phase: str, output: str = "", **extra: Any) -> None:
    """phase: 'start' | 'done' | 'error'"""
    bus.publish("tool_progress", {
        "tool_name": tool_name,
        "phase": phase,
        "output": output[:1000],
        **extra,
    })
    if phase == "start":
        _log(f"[TOOL] ⟳ {tool_name} executing...")
    elif phase == "done":
        preview = output[:60].replace("\n", " ")
        _log(f"[TOOL] ✓ {tool_name}: {preview}")
    elif phase == "error":
        _log(f"[TOOL] ✗ {tool_name}: {output[:80]}", "error")


def emit_pipeline_phase(phase: str, detail: str = "", **extra: Any) -> None:
    bus.publish("pipeline", {
        "phase": phase,
        "detail": detail,
        **extra,
    })
    level = "error" if "error" in phase else "warn" if "warning" in phase else "info"
    _log(f"[PIPELINE] {phase}: {detail}", level)


def emit_workspace(agent_id: str, action: str, detail: str = "", **extra: Any) -> None:
    """Workspace lifecycle events."""
    bus.publish("workspace", {
        "agent_id": agent_id,
        "action": action,
        "detail": detail,
        **extra,
    })
    _log(f"[WORKSPACE] {agent_id} {action}: {detail}")


def emit_container(agent_id: str, action: str, detail: str = "", **extra: Any) -> None:
    """Docker container events."""
    bus.publish("container", {
        "agent_id": agent_id,
        "action": action,
        "detail": detail,
        **extra,
    })
    _log(f"[DOCKER] {agent_id} {action}: {detail}")


def emit_invoke(action_type: str, detail: str = "", **extra: Any) -> None:
    """INVOKE action events."""
    bus.publish("invoke", {
        "action_type": action_type,
        "detail": detail,
        **extra,
    })
    _log(f"[INVOKE] {action_type}: {detail}")


def emit_token_warning(level: str, message: str, usage: float = 0, budget: float = 0, **extra: Any) -> None:
    """Token budget warning events.

    Levels: ``warn`` (80%), ``downgrade`` (90%), ``frozen`` (100%), ``reset``, ``all_providers_failed``.
    """
    bus.publish("token_warning", {
        "level": level,
        "message": message,
        "usage": usage,
        "budget": budget,
        **extra,
    })
    level_label = {"warn": "warn", "downgrade": "warn", "frozen": "error", "reset": "info"}.get(level, "warn")
    _log(f"[TOKEN] {level.upper()}: {message}", level=level_label)


def emit_simulation(sim_id: str, action: str, detail: str = "", **extra: Any) -> None:
    """Simulation lifecycle events: start, progress, result."""
    bus.publish("simulation", {
        "sim_id": sim_id,
        "action": action,
        "detail": detail,
        **extra,
    })
    level_label = "error" if action == "result" and extra.get("status") == "fail" else "info"
    _log(f"[SIM] {sim_id} {action}: {detail}", level=level_label)
