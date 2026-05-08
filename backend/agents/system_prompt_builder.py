"""System prompt helpers for agent boot-time context."""

from __future__ import annotations

from backend.agents.tool_dispatcher import get_tool_summary

_TOOL_CATALOG_PREFIX = "Available tools:"


def inject_tool_catalog(system_prompt: str, tool_names: list[str]) -> str:
    """Append a compact tool catalog to ``system_prompt``.

    The catalog is intentionally a single line so it gives the model enough
    affordance to choose a tool without consuming the full JSON schema budget.
    """
    if not tool_names or _TOOL_CATALOG_PREFIX in system_prompt:
        return system_prompt

    entries = [
        f"{name} ({get_tool_summary(name)})"
        for name in dict.fromkeys(tool_names)
    ]
    catalog = f"{_TOOL_CATALOG_PREFIX} {', '.join(entries)}"

    if not system_prompt:
        return catalog
    return f"{system_prompt.rstrip()}\n\n{catalog}"
