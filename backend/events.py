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
    """Pub/sub for SSE events with optional persistence.

    I10: cross-worker delivery via Redis Pub/Sub.  When Redis is available,
    ``publish()`` sends events to all workers; each worker's pub/sub listener
    calls ``_deliver_local()`` to fan out to that worker's SSE subscribers.
    """

    def __init__(self) -> None:
        self._subscribers: dict[asyncio.Queue, str | None] = {}
        self._dropped_events: int = 0  # backpressure telemetry
        self._worker_id = f"w-{id(self):x}"

    def subscribe(self, tenant_id: str | None = None) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers[q] = tenant_id
        try:
            from backend import metrics as _m
            _m.sse_subscribers.set(len(self._subscribers))
        except Exception as exc:
            logger.debug("sse_subscribers gauge set failed: %s", exc)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.pop(q, None)
        try:
            from backend import metrics as _m
            _m.sse_subscribers.set(len(self._subscribers))
        except Exception as exc:
            logger.debug("sse_subscribers gauge set failed: %s", exc)

    def _deliver_local(self, event: str, data_json: str,
                       broadcast_scope: str = "global",
                       tenant_id: str | None = None) -> None:
        """Fan out a pre-serialised event to this worker's SSE subscribers."""
        msg = {"event": event, "data": data_json}
        dead: list[asyncio.Queue] = []
        for q, sub_tenant in list(self._subscribers.items()):
            if broadcast_scope == "tenant" and tenant_id and sub_tenant and sub_tenant != tenant_id:
                continue
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                self._dropped_events += 1
                dead.append(q)
                logger.warning(
                    "EventBus: dropping subscriber (queue full, event=%s, total_dropped=%d)",
                    event, self._dropped_events,
                )
                try:
                    from backend import metrics as _m
                    _m.sse_dropped_total.inc()
                except Exception as exc:
                    logger.debug("sse_dropped metric bump failed: %s", exc)
        for q in dead:
            self._subscribers.pop(q, None)

    def publish(self, event: str, data: dict[str, Any],
                session_id: str | None = None,
                broadcast_scope: str = "global",
                tenant_id: str | None = None) -> None:
        data.setdefault("timestamp", datetime.now().isoformat())
        data["_session_id"] = session_id or ""
        data["_broadcast_scope"] = broadcast_scope
        data["_tenant_id"] = tenant_id or ""
        data_json = json.dumps(data)

        # I10: try cross-worker delivery via Redis Pub/Sub
        cross_worker = False
        try:
            from backend.shared_state import publish_cross_worker
            cross_worker = publish_cross_worker("sse", {
                "event": event,
                "data_json": data_json,
                "broadcast_scope": broadcast_scope,
                "tenant_id": tenant_id or "",
                "origin_worker": self._worker_id,
            })
        except Exception:
            pass

        if not cross_worker:
            self._deliver_local(event, data_json, broadcast_scope, tenant_id)

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

    SP-3.10 (2026-04-20): runs as ``asyncio.create_task`` from the
    event bus — no request conn. Acquire pool conn inline.
    """
    try:
        from backend import db
        from backend.db_pool import get_pool
        async with get_pool().acquire() as _conn:
            await db.insert_event(_conn, event_type, data_json)
    except Exception as exc:  # pragma: no cover — DB-dependent
        logger.debug("event persist failed (%s): %s", event_type, exc)


# Singleton
bus = EventBus()


# I10: register cross-worker callback so events from other workers
# get delivered to this worker's local SSE subscribers.
def _on_cross_worker_event(event: str, data: dict) -> None:
    if event != "sse":
        return
    origin = data.get("origin_worker", "")
    if origin == bus._worker_id:
        return
    bus._deliver_local(
        data.get("event", ""),
        data.get("data_json", "{}"),
        data.get("broadcast_scope", "global"),
        data.get("tenant_id") or None,
    )


try:
    from backend.shared_state import register_cross_worker_callback
    register_cross_worker_callback(_on_cross_worker_event)
except Exception:
    pass


# ─── Convenience publishers (each one also writes to REPORTER VORTEX log) ───

def _auto_tenant(tenant_id: str | None) -> str | None:
    """Return explicit tenant_id, or read from request context if available."""
    if tenant_id is not None:
        return tenant_id
    try:
        from backend.db_context import current_tenant_id
        return current_tenant_id()
    except Exception:
        return None


def emit_agent_update(agent_id: str, status: str, thought_chain: str = "",
                      session_id: str | None = None,
                      broadcast_scope: str = "global",
                      tenant_id: str | None = None, **extra: Any) -> None:
    bus.publish("agent_update", {
        "agent_id": agent_id,
        "status": status,
        "thought_chain": thought_chain,
        **extra,
    }, session_id=session_id, broadcast_scope=broadcast_scope,
       tenant_id=_auto_tenant(tenant_id))
    level = "error" if status == "error" else "warn" if status == "warning" else "info"
    _log(f"[AGENT] {agent_id} → {status.upper()}" + (f": {thought_chain[:80]}" if thought_chain else ""), level)

    # R2 (#308): feed thought_chain into the semantic-entropy monitor so
    # rephrased-but-identical reasoning is caught before the retry /
    # wall-clock stuck-detector rules fire. Best-effort — an embedder
    # failure must never block agent_update.
    if thought_chain and status in {"running", "warning", "error"}:
        try:
            from backend.semantic_entropy import record_output
            record_output(agent_id, thought_chain, task_id=extra.get("task_id"))
        except Exception:
            pass


def emit_task_update(task_id: str, status: str, assigned_agent_id: str | None = None,
                     session_id: str | None = None,
                     broadcast_scope: str = "global",
                     tenant_id: str | None = None, **extra: Any) -> None:
    bus.publish("task_update", {
        "task_id": task_id,
        "status": status,
        "assigned_agent_id": assigned_agent_id,
        **extra,
    }, session_id=session_id, broadcast_scope=broadcast_scope,
       tenant_id=_auto_tenant(tenant_id))
    _log(f"[TASK] {task_id} → {status.upper()}" + (f" (agent: {assigned_agent_id})" if assigned_agent_id else ""))


def emit_tool_progress(tool_name: str, phase: str, output: str = "",
                       session_id: str | None = None,
                       broadcast_scope: str = "global",
                       tenant_id: str | None = None, **extra: Any) -> None:
    """phase: 'start' | 'done' | 'error'"""
    bus.publish("tool_progress", {
        "tool_name": tool_name,
        "phase": phase,
        "output": output[:1000],
        **extra,
    }, session_id=session_id, broadcast_scope=broadcast_scope,
       tenant_id=_auto_tenant(tenant_id))
    if phase == "start":
        _log(f"[TOOL] ⟳ {tool_name} executing...")
    elif phase == "done":
        preview = output[:60].replace("\n", " ")
        _log(f"[TOOL] ✓ {tool_name}: {preview}")
    elif phase == "error":
        _log(f"[TOOL] ✗ {tool_name}: {output[:80]}", "error")


def emit_pipeline_phase(phase: str, detail: str = "",
                        session_id: str | None = None,
                        broadcast_scope: str = "global",
                        tenant_id: str | None = None, **extra: Any) -> None:
    bus.publish("pipeline", {
        "phase": phase,
        "detail": detail,
        **extra,
    }, session_id=session_id, broadcast_scope=broadcast_scope,
       tenant_id=_auto_tenant(tenant_id))
    level = "error" if "error" in phase else "warn" if "warning" in phase else "info"
    _log(f"[PIPELINE] {phase}: {detail}", level)


def emit_workspace(agent_id: str, action: str, detail: str = "",
                   session_id: str | None = None,
                   broadcast_scope: str = "global",
                   tenant_id: str | None = None, **extra: Any) -> None:
    """Workspace lifecycle events."""
    bus.publish("workspace", {
        "agent_id": agent_id,
        "action": action,
        "detail": detail,
        **extra,
    }, session_id=session_id, broadcast_scope=broadcast_scope,
       tenant_id=_auto_tenant(tenant_id))
    _log(f"[WORKSPACE] {agent_id} {action}: {detail}")


def emit_container(agent_id: str, action: str, detail: str = "",
                   session_id: str | None = None,
                   broadcast_scope: str = "global",
                   tenant_id: str | None = None, **extra: Any) -> None:
    """Docker container events."""
    bus.publish("container", {
        "agent_id": agent_id,
        "action": action,
        "detail": detail,
        **extra,
    }, session_id=session_id, broadcast_scope=broadcast_scope,
       tenant_id=_auto_tenant(tenant_id))
    _log(f"[DOCKER] {agent_id} {action}: {detail}")


def emit_invoke(action_type: str, detail: str = "",
                session_id: str | None = None,
                broadcast_scope: str = "global",
                tenant_id: str | None = None, **extra: Any) -> None:
    """INVOKE action events."""
    bus.publish("invoke", {
        "action_type": action_type,
        "detail": detail,
        "timestamp": datetime.now().isoformat(),
        **extra,
    }, session_id=session_id, broadcast_scope=broadcast_scope,
       tenant_id=_auto_tenant(tenant_id))
    _log(f"[INVOKE] {action_type}: {detail}")


def emit_token_warning(level: str, message: str, usage: float = 0, budget: float = 0,
                       session_id: str | None = None,
                       broadcast_scope: str = "user",
                       tenant_id: str | None = None, **extra: Any) -> None:
    """Token budget warning events.

    Levels: ``warn`` (80%), ``downgrade`` (90%), ``frozen`` (100%), ``reset``, ``all_providers_failed``.
    """
    bus.publish("token_warning", {
        "level": level,
        "message": message,
        "usage": usage,
        "budget": budget,
        **extra,
    }, session_id=session_id, broadcast_scope=broadcast_scope,
       tenant_id=_auto_tenant(tenant_id))
    level_label = {"warn": "warn", "downgrade": "warn", "frozen": "error", "reset": "info"}.get(level, "warn")
    _log(f"[TOKEN] {level.upper()}: {message}", level=level_label)


def emit_simulation(sim_id: str, action: str, detail: str = "",
                    session_id: str | None = None,
                    broadcast_scope: str = "global",
                    tenant_id: str | None = None, **extra: Any) -> None:
    """Simulation lifecycle events: start, progress, result."""
    bus.publish("simulation", {
        "sim_id": sim_id,
        "action": action,
        "detail": detail,
        "timestamp": datetime.now().isoformat(),
        **extra,
    }, session_id=session_id, broadcast_scope=broadcast_scope,
       tenant_id=_auto_tenant(tenant_id))
    level_label = "error" if action == "result" and extra.get("status") == "fail" else "info"
    _log(f"[SIM] {sim_id} {action}: {detail}", level=level_label)


def emit_agent_entropy(agent_id: str, entropy_score: float,
                       verdict: str,
                       threshold_warn: float = 0.5,
                       threshold_deadlock: float = 0.7,
                       window_size: int = 0,
                       round_idx: int | None = None,
                       task_id: str | None = None,
                       session_id: str | None = None,
                       broadcast_scope: str = "global",
                       tenant_id: str | None = None, **extra: Any) -> None:
    """R2 (#308): per-agent semantic-entropy measurement.

    ``verdict`` is ``"ok" | "warning" | "deadlock"``. Deadlock verdicts
    should also trigger an ``emit_debug_finding`` of type
    ``cognitive_deadlock``; the entropy module already does that.
    """
    # Caller may forward the raw monitor payload which uses ``round`` as
    # the key — accept both names.
    if round_idx is None:
        round_idx = int(extra.pop("round", 0) or 0)
    else:
        extra.pop("round", None)
    extra.pop("threshold", None)
    try:
        score_4 = float(f"{entropy_score:.4f}")
    except Exception:
        score_4 = entropy_score
    bus.publish("agent.entropy", {
        "agent_id": agent_id,
        "task_id": task_id,
        "entropy_score": score_4,
        "threshold_warn": threshold_warn,
        "threshold_deadlock": threshold_deadlock,
        "verdict": verdict,
        "window_size": window_size,
        "round": round_idx,
        **extra,
    }, session_id=session_id, broadcast_scope=broadcast_scope,
       tenant_id=_auto_tenant(tenant_id))
    level = "warn" if verdict == "warning" else "error" if verdict == "deadlock" else "info"
    _log(f"[ENTROPY] {agent_id} score={entropy_score:.3f} → {verdict.upper()}", level)


def emit_agent_scratchpad_saved(
    agent_id: str,
    turn: int,
    size_bytes: int,
    sections_count: int,
    *,
    trigger: str = "turn_interval",
    task_id: str | None = None,
    session_id: str | None = None,
    broadcast_scope: str = "global",
    tenant_id: str | None = None, **extra: Any,
) -> None:
    """R3 (#309): scratchpad flush event.

    ``trigger`` explains why this write happened so the UI can label it
    (``turn_interval`` | ``tool_done`` | ``subtask_switch`` | ``manual`` |
    ``continuation_flush`` | ``crash_recovery``). ``size_bytes`` is the
    ciphertext length on disk, not the plaintext size — callers should
    not try to derive plaintext memory pressure from it.
    """
    bus.publish("agent.scratchpad.saved", {
        "agent_id": agent_id,
        "task_id": task_id,
        "turn": turn,
        "size_bytes": size_bytes,
        "sections_count": sections_count,
        "trigger": trigger,
        "timestamp": datetime.now().isoformat(),
        **extra,
    }, session_id=session_id, broadcast_scope=broadcast_scope,
       tenant_id=_auto_tenant(tenant_id))
    _log(
        f"[SCRATCHPAD] {agent_id} turn={turn} trigger={trigger} "
        f"size={size_bytes}B sections={sections_count}",
    )


def emit_agent_token_continuation(
    agent_id: str,
    *,
    task_id: str | None = None,
    provider: str = "unknown",
    continuation_round: int = 1,
    total_rounds: int = 1,
    appended_chars: int = 0,
    session_id: str | None = None,
    broadcast_scope: str = "global",
    tenant_id: str | None = None, **extra: Any,
) -> None:
    """R3 (#309): emitted when the adapter auto-continues after max_tokens.

    The UI uses this to attach an "↩ auto-continued" tag to the message
    in the agent stream. ``continuation_round`` is 1-based and counts
    only the continuations (the original truncated turn is not round 0).
    """
    bus.publish("agent.token_continuation", {
        "agent_id": agent_id,
        "task_id": task_id,
        "provider": provider,
        "continuation_round": continuation_round,
        "total_rounds": total_rounds,
        "appended_chars": appended_chars,
        "timestamp": datetime.now().isoformat(),
        **extra,
    }, session_id=session_id, broadcast_scope=broadcast_scope,
       tenant_id=_auto_tenant(tenant_id))
    _log(
        f"[CONTINUE] {agent_id} round={continuation_round}/{total_rounds} "
        f"appended={appended_chars}c provider={provider}",
    )


def emit_debug_finding(
    task_id: str, agent_id: str, finding_type: str, severity: str, message: str,
    context: dict | None = None,
    session_id: str | None = None,
    broadcast_scope: str = "global",
    tenant_id: str | None = None, **extra: Any,
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
    }, session_id=session_id, broadcast_scope=broadcast_scope,
       tenant_id=_auto_tenant(tenant_id))

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

    # B1 #209: cross-agent observations route to the Decision Engine
    if finding_type == "cross_agent/observation":
        from backend.cross_agent_router import route_cross_agent_finding
        route_cross_agent_finding(
            finding_id=finding_id,
            task_id=task_id,
            reporter_agent_id=agent_id,
            target_agent_id=(context or {}).get("target_agent_id"),
            message=message,
            context=context,
            blocking=bool((context or {}).get("blocking")),
        )

    level_label = "error" if severity in ("error", "critical") else "warn" if severity == "warn" else "info"
    _log(f"[DEBUG] {finding_type.upper()} ({agent_id}): {message}", level=level_label)


async def _persist_debug_finding(data: dict) -> None:
    """Write debug finding to DB (best-effort, non-blocking).

    SP-3.9 (2026-04-20): runs as ``asyncio.create_task`` from the
    event bus worker — no request conn. Acquire from pool per call;
    the DB write is single-statement so no transaction needed.
    """
    try:
        from backend import db
        from backend.db_pool import get_pool
        async with get_pool().acquire() as _conn:
            await db.insert_debug_finding(_conn, data)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Failed to persist debug finding: %s", exc)


def emit_workflow_updated(
    run_id: str,
    status: str,
    version: int,
    *,
    kind: str | None = None,
    session_id: str | None = None,
    broadcast_scope: str = "user",
    tenant_id: str | None = None,
) -> None:
    """Q.3-SUB-1 (#297): broadcast a workflow_run state change to the user's UIs.

    Fires after every successful workflow_runs INSERT / UPDATE so a
    device watching the RunHistory panel sees status+version changes
    without waiting for the 15 s poll tick. Mirrors the user-scope
    pattern of :func:`emit_new_device_login`: ``broadcast_scope='user'``
    is advisory — the EventBus only enforces the ``tenant`` scope today
    (Q.4 #298 will tighten this), so the frontend is expected to
    additionally self-filter on ``data._session_id`` / user identity
    before applying the patch.
    """
    bus.publish("workflow_updated", {
        "run_id": run_id,
        "status": status,
        "version": version,
        "kind": kind,
    }, session_id=session_id, broadcast_scope=broadcast_scope,
       tenant_id=_auto_tenant(tenant_id))
    _log(
        f"[WORKFLOW] {run_id} → {status.upper()} (v{version})",
    )


def emit_notification_read(
    notification_id: str,
    user_id: str,
    *,
    session_id: str | None = None,
    broadcast_scope: str = "user",
    tenant_id: str | None = None,
) -> None:
    """Q.3-SUB-3 (#297): broadcast a notification read-state flip to the user's UIs.

    Fires after ``db.mark_notification_read`` returns True so that a
    second device showing the bell badge can decrement its unread
    counter and drop the notification from its local list without
    waiting for the next ``/notifications/unread-count`` poll.

    ``broadcast_scope='user'`` is advisory — the EventBus only enforces
    the ``tenant`` scope today (Q.4 #298 will tighten this), so the
    frontend must additionally self-filter on ``data.user_id`` before
    applying the patch. Mirrors the user-scope pattern of
    :func:`emit_new_device_login` / :func:`emit_workflow_updated`.
    """
    bus.publish("notification.read", {
        "id": notification_id,
        "user_id": user_id,
    }, session_id=session_id, broadcast_scope=broadcast_scope,
       tenant_id=_auto_tenant(tenant_id))
    _log(f"[NOTIFY] {notification_id} → READ (user={user_id})")


def emit_preferences_updated(
    pref_key: str,
    value: str,
    user_id: str,
    *,
    session_id: str | None = None,
    broadcast_scope: str = "user",
    tenant_id: str | None = None,
) -> None:
    """Q.3-SUB-4 (#297): broadcast a user-preferences change to the user's UIs.

    Fires after ``PUT /user-preferences/{key}`` writes the PG row so a
    second device (different browser / phone) can patch its cached
    prefs without waiting for the next poll or full-page reload. The
    same-browser cross-tab path (``storage-bridge.tsx`` + J4
    ``StorageEvent``) continues to work in parallel — the SSE handler
    dispatches a synthetic ``StorageEvent`` so tabs in the originator's
    browser don't need to double-subscribe.

    ``broadcast_scope='user'`` is advisory — :class:`EventBus` only
    enforces the ``tenant`` scope today (Q.4 #298 will tighten this),
    so the frontend must additionally self-filter on ``data.user_id``
    before applying the patch. Mirrors the user-scope pattern of
    :func:`emit_new_device_login` / :func:`emit_notification_read`.
    """
    bus.publish("preferences.updated", {
        "pref_key": pref_key,
        "value": value,
        "user_id": user_id,
    }, session_id=session_id, broadcast_scope=broadcast_scope,
       tenant_id=_auto_tenant(tenant_id))
    _log(f"[PREFS] {pref_key}={value[:40]} (user={user_id})")


def emit_integration_settings_updated(
    fields_changed: list[str],
    *,
    scope: str = "user",
    session_id: str | None = None,
    broadcast_scope: str | None = None,
    tenant_id: str | None = None,
) -> None:
    """Q.3-SUB-5 (#297): broadcast a non-LLM integration-settings change.

    Fires from ``PUT /runtime/settings`` after the SharedKV mirror write
    whenever the updated field set contains *any* key outside the LLM
    family (Gerrit / JIRA / GitHub / GitLab / Slack / PagerDuty /
    webhooks / CI / Docker). The LLM subset already owns a dedicated
    ``invoke('provider_switch')`` emit at the same call site — this
    helper covers the remaining integrations so the SYSTEM INTEGRATIONS
    modal on a second device stops waiting for a modal-open refetch to
    discover the change.

    ``scope`` is the caller-facing alias; keep the payload key
    ``_broadcast_scope='user'`` in lock-step with the rest of the Q.3
    emit family so Q.4 (#298) can flip enforcement without a payload
    rewrite. ``fields_changed`` is the raw applied-key list — the
    frontend matches it against the non-LLM prefix set itself rather
    than having the backend second-guess which tab to repaint.
    """
    if broadcast_scope is None:
        broadcast_scope = scope
    bus.publish("integration.settings.updated", {
        "fields_changed": list(fields_changed),
    }, session_id=session_id, broadcast_scope=broadcast_scope,
       tenant_id=_auto_tenant(tenant_id))
    _log(
        f"[INTEGRATION] settings updated: {','.join(fields_changed)[:80]}"
    )


def emit_chat_message(
    message_id: str,
    user_id: str,
    role: str,
    content: str,
    timestamp: str,
    *,
    session_id: str | None = None,
    broadcast_scope: str = "user",
    tenant_id: str | None = None,
    suggestion: dict | None = None,
) -> None:
    """Q.3-SUB-6 (#297): broadcast a persisted chat message to the user's UIs.

    Fires from ``backend.routers.chat`` after every successful
    ``chat_messages`` INSERT so a second device logged into the same
    user account can append the line without waiting for a manual
    ``/chat/history`` refetch. Token-by-token streaming to the
    originator is a separate concern (owned by the ``chat/stream``
    SSE response body); this helper only publishes the **finalised**
    message payload, not partial chunks.

    ``broadcast_scope='user'`` is advisory — :class:`EventBus` only
    enforces the ``tenant`` scope today (Q.4 #298 will tighten this),
    so the frontend must additionally self-filter on ``data.user_id``
    before appending. Mirrors the user-scope pattern of the other
    Q.3 sub-tasks (workflow / notification.read / preferences /
    integration.settings).

    ``suggestion`` is the optional AISuggestion attached to orchestrator
    replies (dispatch hints, etc.); when present we pass it through to
    the payload so the target device renders the same affordance the
    originator sees.
    """
    payload: dict[str, Any] = {
        "id": message_id,
        "user_id": user_id,
        "role": role,
        "content": content,
        "ts": timestamp,
    }
    if suggestion:
        payload["suggestion"] = suggestion
    bus.publish("chat.message", payload,
                session_id=session_id, broadcast_scope=broadcast_scope,
                tenant_id=_auto_tenant(tenant_id))
    _log(f"[CHAT] {role} {message_id} (user={user_id})")


def emit_new_device_login(
    user_id: str,
    token_hint: str,
    ip: str,
    user_agent: str,
    *,
    session_id: str | None = None,
    tenant_id: str | None = None,
) -> None:
    """Q.2 (#296): broadcast a new-device-login alert to the user's UIs.

    The event is tagged ``broadcast_scope="user"`` and carries ``user_id``
    in the payload — the EventBus only enforces ``tenant`` scope today
    (Q.4 #298 will tighten this), so the frontend must additionally
    filter on ``data.user_id == currentUser.id`` before showing the
    toast. We pass ``token_hint`` (mask of the new session's token) so
    the "這不是我 → 踢掉" button can target ``DELETE /auth/sessions/
    {token_hint}`` without ever exposing the raw session cookie to the
    rendered UI.
    """
    bus.publish("security.new_device_login", {
        "user_id": user_id,
        "token_hint": token_hint,
        "ip": ip,
        "user_agent": user_agent,
    }, session_id=session_id, broadcast_scope="user",
       tenant_id=_auto_tenant(tenant_id))
    _log(
        f"[SECURITY] new device login user={user_id} ip={ip} ua={user_agent[:60]}",
        "warn",
    )
