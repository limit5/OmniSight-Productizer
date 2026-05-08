"""Dynamic tool-reference support for the docs-site build.

OP-789 moves ``docs/agents/tool-reference.md`` away from the checked-in
generated-output model. The docs site should call this module at build
time and render the reference from ``backend.agents.tool_schemas``, which
is the registry of truth.

Module-global state audit (SOP 2026-04-21 rule)
------------------------------------------------
Only immutable string constants live at module scope. Each build derives
fresh Markdown from the imported tool schema registry; there is no
cross-worker state to coordinate.
"""
from __future__ import annotations

from backend.agents.tool_schemas import generate_markdown_reference

DOCS_SITE_TOOL_REFERENCE_URL = "/docs/agents/tool-reference/"


def build_tool_reference() -> str:
    """Render the tool reference Markdown for docs-site consumption."""
    return generate_markdown_reference()
