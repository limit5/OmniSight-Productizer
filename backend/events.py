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

# Late import to avoid circular — resolved at first use.
# Lock prevents two early concurrent emits from each importing the system
# module and racing on the assignment.
_log_fn = None
import threading as _threading_log
_log_fn_lock = _threading_log.Lock()


def _log(message: str, level: str = "info") -> None:
    """Write to the system log buffer (REPORTER VORTEX)."""
    global _log_fn
    fn = _log_fn
    if fn is None:
        with _log_fn_lock:
            if _log_fn is None:
                from backend.routers.system import add_system_log
                _log_fn = add_system_log
            fn = _log_fn
    fn(message, level)


# Event types worth persisting to DB (skip high-frequency transient events)
# debug_finding excluded: has its own dedicated persistence in emit_debug_finding()
# notification excluded: already persisted by notify() → db.insert_notification()
_PERSIST_EVENT_TYPES = frozenset({
    "agent_update", "task_update", "simulation", "invoke",
    # Phase 47: persist audit-relevant decision events
    "decision_pending", "decision_resolved", "decision_auto_executed",
    "decision_undone", "mode_changed",
})


class EventBus:
    """Pub/sub for SSE events with optional persistence."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._dropped_events: int = 0  # backpressure telemetry

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        # set.discard is O(1) and safe for missing elements
        self._subscribers.discard(q)

    def publish(self, event: str, data: dict[str, Any]) -> None:
        data.setdefault("timestamp", datetime.now().isoformat())
        data_json = json.dumps(data)
        msg = {"event": event, "data": data_json}
        dead: list[asyncio.Queue] = []
        # Snapshot to allow safe mutation during iteration
        for q in list(self._subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                # Backpressure: drop slowest subscriber rather than block
                # publishers. Surface count via subscriber_dropped for telemetry.
                self._dropped_events += 1
                dead.append(q)
                logger.warning(
                    "EventBus: dropping subscriber (queue full, event=%s, total_dropped=%d)",
                    event, self._dropped_events,
                )
        for q in dead:
            self._subscribers.discard(q)

        # Persist important events asynchronously
        if event in _PERSIST_EVENT_TYPES:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return  # No running loop (sync context) — skip persistence
            loop.create_task(_persist_event(event, data_json))

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def subscriber_dropped(self) -> int:
        return self._dropped_events


async def _persist_event(event_type: str, data_json: str) -> None:
    """Write event to DB (best-effort, non-blocking).

    Failures are logged at debug level — DB unavailability must not break
    SSE delivery, but silent failure also shouldn't hide chronic outages.
    """
    try:
        from backend import db
        await db.insert_event(event_type, data_json)
    except Exception as exc:  # pragma: no cover — DB-dependent
        logger.debug("event persist failed (%s): %s", event_type, exc)


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
        "timestamp": datetime.now().isoformat(),
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
        "timestamp": datetime.now().isoformat(),
        **extra,
    })
    level_label = "error" if action == "result" and extra.get("status") == "fail" else "info"
    _log(f"[SIM] {sim_id} {action}: {detail}", level=level_label)


def emit_debug_finding(
    task_id: str, agent_id: str, finding_type: str, severity: str, message: str,
    context: dict | None = None, **extra: Any,
) -> None:
    """Debug discovery events: stuck loops, repeated errors, loop breaker triggers.

    Publishes SSE event AND persists to DB asynchronously.
    """
    import json as _json
    import uuid as _uuid

    now = datetime.now().isoformat()
    finding_id = f"dbg-{_uuid.uuid4().hex[:8]}"
    context_json = _json.dumps(context or {})

    # SSE event for real-time frontend display
    bus.publish("debug_finding", {
        "id": finding_id,
        "task_id": task_id,
        "agent_id": agent_id,
        "finding_type": finding_type,
        "severity": severity,
        "message": message,
        "timestamp": now,
        **extra,
    })

    # Persist to DB asynchronously (fire-and-forget)
    import asyncio
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_persist_debug_finding({
            "id": finding_id, "task_id": task_id, "agent_id": agent_id,
            "finding_type": finding_type, "severity": severity,
            "content": message, "context": context_json,
            "status": "open", "created_at": now,
        }))
    except RuntimeError:
        pass  # No running loop — skip DB persistence (e.g., in sync tests)

    level_label = "error" if severity in ("error", "critical") else "warn" if severity == "warn" else "info"
    _log(f"[DEBUG] {finding_type.upper()} ({agent_id}): {message}", level=level_label)


async def _persist_debug_finding(data: dict) -> None:
    """Write debug finding to DB (best-effort, non-blocking)."""
    try:
        from backend import db
        await db.insert_debug_finding(data)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Failed to persist debug finding: %s", exc)
