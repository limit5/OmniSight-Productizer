"""U6-0 G6.3a — resolve a SHADOW-only CanonicalizationContext for future guard telemetry.

runner_sdk roots come from the frozen shadow_root_registry; specialist roots
from the active-workspace ContextVar (mirroring the live fallback); chat/a2a
(and any adapter with no file canonicalizer) return None. SHADOW SCOPE ONLY:
the synthesized workspace_id is telemetry scope, never a grant identity.

No ordinary Exception escapes: any ordinary failure returns None so the caller
can skip shadow telemetry without crashing live dispatch. BaseException
intentionally propagates, matching the guard's control-flow convention.
"""
from __future__ import annotations

from backend.agents import shadow_root_registry
from backend.agents.action_canonicalize import CanonicalizationContext


def resolve_shadow_context(
    adapter_namespace: str,
    tool_name: str,
    schema_version: str,
) -> "CanonicalizationContext | None":
    """Return a shadow context bound exactly to the dispatch key, or None."""
    try:
        if (
            type(adapter_namespace) is not str
            or type(tool_name) is not str
            or type(schema_version) is not str
        ):
            return None
        root: str | None = None
        if adapter_namespace == "runner_sdk":
            root = shadow_root_registry.resolve_shadow_root(
                adapter_namespace,
                tool_name,
                schema_version,
            )
        elif adapter_namespace == "specialist":
            # Lazy import contains dependency/cycle risk from heavy tools.py.
            from backend.agents.tools import get_active_workspace

            # Mirror the live ContextVar -> unresolved WORKSPACE_ROOT fallback.
            root = str(get_active_workspace())
        if not root or type(root) is not str:
            return None
        return CanonicalizationContext(
            workspace_id=(
                f"shadow:{adapter_namespace}:{tool_name}@{schema_version}"
            ),
            workspace_root=root,
            adapter_namespace=adapter_namespace,
            tool_name=tool_name,
            schema_version=schema_version,
        )
    except Exception:  # noqa: BLE001 — telemetry resolution never breaks dispatch
        return None
