"""RPG.W13 -- MP.W17.7 ``tool_invocation`` telemetry → tool proficiency rollup.

ADR-0008 §"MCP/A2A tool proficiency (W13)" wires the W17.7 telemetry
stream (OP-117, already live) into the W13 proficiency table. This
module is the consumer side of that pipe: it normalises the event
payload, calls
:func:`backend.agents.tool_proficiency.record_tool_invocation`, and
returns the resulting transition so the dispatcher / SSE bus can fan
out ``tool:level_up`` events.

The consumer is **not** the data source — every event must already
exist in the W17.7 stream. The rebuild script
(``scripts/rpg_rebuild_tool_proficiency.py``) replays the same shape
to re-derive ``agent_tool_proficiency`` from history; if you find
yourself wanting to invent new event shapes here, that is the bug.

Shape contract (subset; extra keys are tolerated)::

    {
      "tool_name": "Read",       # required — maps to ``tool_id``
      "agent_id":  "agent-a1",   # required for per-agent rollup
      "success":   true,         # required — boolean outcome from W17.7
      "timestamp": "2026-05-11T12:34:56Z"   # optional; defaults to now
    }

Events missing ``agent_id`` are dropped (W17.7 emits them when the
runner has not yet stamped an agent identity — e.g. boot-time
schema probes). Events with malformed timestamps fall back to "now".
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from backend.agents.tool_proficiency import (
    ToolInvocationRecorded,
    ToolProficiencyStore,
    record_tool_invocation,
)


LOG = logging.getLogger(__name__)
INVOCATION_LOG_PREFIX = "events.tool_invocation "


@dataclass(frozen=True)
class TelemetryConsumerStats:
    """Roll-up returned by :func:`consume_batch` for operator dashboards."""

    received: int
    applied: int
    dropped_missing_agent: int
    dropped_missing_tool: int
    level_ups: int


def _parse_timestamp(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, str) and raw:
        try:
            cleaned = raw.replace("Z", "+00:00")
            dt = datetime.fromisoformat(cleaned)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            LOG.debug("mp_w17 telemetry timestamp %r unparseable; using now()", raw)
    return datetime.now(timezone.utc)


def _coerce_outcome(payload: Mapping[str, Any]) -> str:
    """Map W17.7 outcome shapes to a W13 outcome string."""
    success = payload.get("success")
    if isinstance(success, bool):
        return "success" if success else "fail"
    outcome = payload.get("outcome")
    if isinstance(outcome, Mapping):
        nested = _coerce_outcome(outcome)
        if nested == "success":
            return "success"
        is_error = outcome.get("is_error")
        if isinstance(is_error, bool):
            return "fail" if is_error else "success"
    if isinstance(outcome, str) and outcome.strip().lower() in {
        "success", "ok", "passed", "done"
    }:
        return "success"
    # Some upstream producers send free-form ``status`` instead — be lenient.
    status = payload.get("status")
    if isinstance(status, str) and status.strip().lower() in {
        "success", "ok", "passed", "done"
    }:
        return "success"
    is_error = payload.get("is_error")
    if isinstance(is_error, bool):
        return "fail" if is_error else "success"
    return "fail"


def payload_from_invocation_log(line: str) -> dict[str, Any] | None:
    """Extract the JSON payload from one ``events.tool_invocation`` log line."""
    if not isinstance(line, str):
        return None
    raw = line.strip()
    if INVOCATION_LOG_PREFIX in raw:
        raw = raw.split(INVOCATION_LOG_PREFIX, 1)[1].strip()
    if not raw.startswith("{"):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


async def consume_event(
    store: ToolProficiencyStore,
    payload: Mapping[str, Any],
) -> ToolInvocationRecorded | None:
    """Apply one W17.7 ``tool_invocation`` event to the W13 store.

    Returns the :class:`ToolInvocationRecorded` transition when the
    event was applied, or ``None`` when the event was dropped (missing
    ``agent_id`` / ``tool_name``). Callers should treat ``None`` as a
    non-error skip, not a failure.
    """
    invocation = payload.get("invocation")
    invocation_payload = invocation if isinstance(invocation, Mapping) else {}
    tool_id = (
        payload.get("tool_name")
        or payload.get("tool_id")
        or invocation_payload.get("tool_name")
        or invocation_payload.get("tool_id")
    )
    agent_id = payload.get("agent_id") or invocation_payload.get("agent_id")
    if not isinstance(tool_id, str) or not tool_id.strip():
        LOG.debug("mp_w17 telemetry event missing tool_name; dropping")
        return None
    if not isinstance(agent_id, str) or not agent_id.strip():
        LOG.debug("mp_w17 telemetry event missing agent_id; dropping")
        return None

    when = _parse_timestamp(
        payload.get("timestamp") or invocation_payload.get("timestamp")
    )
    outcome = _coerce_outcome(payload)
    return await record_tool_invocation(
        store, agent_id, tool_id, outcome, now=when
    )


async def consume_invocation_log_line(
    store: ToolProficiencyStore,
    line: str,
) -> ToolInvocationRecorded | None:
    """Apply one logged ``events.tool_invocation`` JSON payload if present."""
    payload = payload_from_invocation_log(line)
    if payload is None:
        return None
    return await consume_event(store, payload)


async def consume_batch(
    store: ToolProficiencyStore,
    payloads: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
) -> TelemetryConsumerStats:
    """Consume a batch of events, returning aggregate counters."""
    received = len(payloads)
    applied = 0
    dropped_missing_agent = 0
    dropped_missing_tool = 0
    level_ups = 0
    for payload in payloads:
        invocation = payload.get("invocation")
        invocation_payload = invocation if isinstance(invocation, Mapping) else {}
        tool_id = (
            payload.get("tool_name")
            or payload.get("tool_id")
            or invocation_payload.get("tool_name")
            or invocation_payload.get("tool_id")
        )
        agent_id = payload.get("agent_id") or invocation_payload.get("agent_id")
        if not isinstance(tool_id, str) or not tool_id.strip():
            dropped_missing_tool += 1
            continue
        if not isinstance(agent_id, str) or not agent_id.strip():
            dropped_missing_agent += 1
            continue
        recorded = await consume_event(store, payload)
        if recorded is None:
            continue
        applied += 1
        if recorded.new_level > recorded.previous_level:
            level_ups += 1
    return TelemetryConsumerStats(
        received=received,
        applied=applied,
        dropped_missing_agent=dropped_missing_agent,
        dropped_missing_tool=dropped_missing_tool,
        level_ups=level_ups,
    )


__all__ = [
    "TelemetryConsumerStats",
    "consume_batch",
    "consume_event",
    "consume_invocation_log_line",
    "payload_from_invocation_log",
]
