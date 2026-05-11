"""OP-853 - Graphiti MCP query guard and silent-degrade wrapper.

Graphiti is a sister remote MCP to OP-813's JIRA MCP. The runner should use
it only for temporal context queries; when the MCP server is unavailable the
runner proceeds without temporal context instead of failing ticket pickup.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from backend.agents.mcp_integration import is_graphiti_mcp_read_only_tool

logger = logging.getLogger(__name__)


class GraphitiMCPUnavailable(Exception):
    """Graphiti MCP server is down or timed out."""


class TemporalQueryNoMatch(Exception):
    """Graphiti query succeeded but found no relevant historical match."""


class GraphitiWriteRefused(Exception):
    """A non-read Graphiti MCP tool was attempted and refused locally."""


GraphitiDispatcher = Callable[[str, dict[str, Any]], Any]


def dispatch_graphiti_temporal_query(
    tool_name: str,
    payload: dict[str, Any],
    *,
    dispatcher: GraphitiDispatcher,
) -> Any | None:
    """Dispatch one read-only Graphiti MCP query.

    Returns ``None`` for server outage or no-match conditions so caller-side
    context injection can degrade to "no temporal context". Write-shaped tool
    names are refused before dispatch.
    """
    if not is_graphiti_mcp_read_only_tool(tool_name):
        raise GraphitiWriteRefused(tool_name)
    try:
        return dispatcher(tool_name, payload)
    except TemporalQueryNoMatch:
        logger.info("Graphiti temporal query returned no match: %s", tool_name)
        return None
    except (GraphitiMCPUnavailable, TimeoutError, ConnectionError, OSError) as exc:
        logger.info("Graphiti MCP unavailable; continuing without temporal context: %s", exc)
        return None
