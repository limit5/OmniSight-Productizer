"""U6-0 G6b-1 — resolve the SERVER-derived authoritative (workspace_id, workspace_root) for the FUTURE enforce path
(authorize_canonical). DORMANT: no caller (G6b-2 wires it). The ROOT is the REAL server-owned root (reused from
resolve_shadow_context — runner_sdk's frozen registry root / specialist's active workspace — then RESOLVED to match the
executor's resolved root); the workspace_id is derived from the trusted execution_context (server identity) — NEVER the
shadow synthetic id. No ordinary Exception escapes: any failure ⇒ None ⇒ the enforce branch handles it (never crashes)."""
from __future__ import annotations

import hashlib
from pathlib import Path

from backend.agents.shadow_context_resolver import resolve_shadow_context

_AUTHORITATIVE_ADAPTERS = frozenset({"runner_sdk", "specialist"})


def resolve_authoritative_workspace(
    execution_context,
    adapter_namespace: str,
    tool_name: str,
    schema_version: str,
) -> "tuple[str, str] | None":
    """Return (workspace_id, workspace_root) or None. workspace_root = the REAL server-owned root, RESOLVED (== executor
    root); workspace_id = ``ws:{tenant}:{adapter}:{sha256(root)}`` — server-derived, NON-shadow, stable per
    (tenant, adapter, resolved-root). None ⇒ no authoritative workspace (chat/a2a, empty/malformed dispatch, unfrozen
    registry, unbound/empty tenant, or a bad root). No ordinary Exception escapes."""
    try:
        if (
            type(adapter_namespace) is not str
            or type(tool_name) is not str
            or type(schema_version) is not str
        ):
            return None
        if not (adapter_namespace and tool_name and schema_version):     # codex BLOCKER-1: reject EMPTY components
            return None
        if adapter_namespace not in _AUTHORITATIVE_ADAPTERS:             # explicit: only the file-canonicalizer adapters
            return None
        shadow = resolve_shadow_context(adapter_namespace, tool_name, schema_version)  # REUSE the real root (cycle-safe)
        if shadow is None:
            return None
        raw_root = shadow.workspace_root
        if type(raw_root) is not str or not raw_root:
            return None
        root = str(Path(raw_root).resolve())                            # codex BLOCKER-2: byte-stable == executor's resolved root
        tenant = getattr(execution_context, "tenant_id", None)
        if type(tenant) is not str or not tenant:                       # unbound/empty-tenant ⇒ no authoritative id
            return None
        digest = hashlib.sha256(root.encode("utf-8")).hexdigest()       # FULL 256-bit — workspace_id IS hashed into the grant digest (no truncation)
        return (f"ws:{tenant}:{adapter_namespace}:{digest}", root)
    except Exception:  # noqa: BLE001 — authoritative resolution never breaks the guard; None ⇒ the enforce branch handles it
        return None
