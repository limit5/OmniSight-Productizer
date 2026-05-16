"""Tool invocation telemetry for agent tool calls."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger("events.tool_invocation")


def emit_tool_invocation(
    tool_name: str,
    duration_ms: float,
    success: bool,
    args_size_bytes: int,
    *,
    agent_id: str | None = None,
) -> None:
    """Emit one best-effort telemetry event for a completed tool call."""
    payload = {
        "tool_name": tool_name,
        "duration_ms": round(float(duration_ms), 3),
        "success": bool(success),
        "args_size_bytes": int(args_size_bytes),
        "timestamp": datetime.now().isoformat(),
    }
    if isinstance(agent_id, str) and agent_id.strip():
        payload["agent_id"] = agent_id.strip()
    try:
        from backend.events import bus

        bus.publish(
            "tool_invocation",
            payload,
            broadcast_scope="global",
        )
    except Exception as exc:  # pragma: no cover - telemetry must not break tools
        logger.debug("tool_invocation SSE emit failed: %s", exc)

    try:
        logger.info("events.tool_invocation %s", json.dumps(payload, sort_keys=True))
    except Exception as exc:  # pragma: no cover - logging is best-effort
        logger.debug("tool_invocation log emit failed: %s", exc)


def args_size_bytes(args: dict[str, Any]) -> int:
    """Return the UTF-8 byte size of the serialized tool arguments."""
    try:
        encoded = json.dumps(args, ensure_ascii=False, default=str).encode("utf-8")
    except (TypeError, ValueError):
        encoded = str(args).encode("utf-8", errors="replace")
    return len(encoded)


__all__ = ["args_size_bytes", "emit_tool_invocation"]
