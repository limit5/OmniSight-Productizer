"""U6-0 G6.2a-2 — neutral shadow-root registry (dormant).

Maps a canonicalizer dispatch key (adapter_namespace, tool_name, schema_version) → the trusted ABSOLUTE workspace ROOT
that owns that tool's file operations, for the FUTURE shadow telemetry path (G6.3). NEUTRAL: imports only stdlib, so the
guard (which cannot import tool_dispatcher/runner_handlers — import cycle) can read it. Binders PUSH roots during a
registration epoch (G6.2a-2-wire); a coordinator FREEZES (publishes readiness); the guard resolver READS (G6.3).
Process-local. DORMANT: no registrant/reader yet.

Fail-closed: resolve returns a root ONLY after freeze, ONLY for a cleanly-registered, un-poisoned key. A conflicting
re-registration POISONS the key (permanently unresolvable until reset). No shadow root is a grant identity — G6b threads
a real server-issued identity; this module carries only a filesystem root for telemetry scope.
"""
from __future__ import annotations

import os
import threading

_ShadowKey = tuple[str, str, str]


class ShadowRootRegistryError(Exception):
    """Fail-closed shadow-root registration error (registration path only; resolve never raises)."""


_ROOTS: dict[_ShadowKey, str] = {}
_POISONED: set[_ShadowKey] = set()
_LOCK = threading.RLock()
_frozen: bool = False


def _valid_key(a: object, t: object, s: object) -> bool:
    return type(a) is str and type(t) is str and type(s) is str


def register_shadow_root(
    adapter_namespace: str,
    tool_name: str,
    schema_version: str,
    *,
    workspace_root: str,
) -> None:
    """Record the trusted ABSOLUTE root for (adapter, tool, schema) during the registration epoch.

    Idempotent for the IDENTICAL root (before OR after freeze). A CONFLICTING re-registration (same key, different root)
    POISONS the key (removed; unresolvable until reset) and raises. A genuinely NEW key after freeze raises. Empty /
    non-``str`` / non-absolute roots and non-``str`` / empty keys are rejected. Exact ``type(v) is str``.
    """
    if not _valid_key(adapter_namespace, tool_name, schema_version):
        raise ShadowRootRegistryError("non_str_dispatch_key")
    if not (adapter_namespace and tool_name and schema_version):
        raise ShadowRootRegistryError("empty_dispatch_key")
    if type(workspace_root) is not str or not workspace_root:
        raise ShadowRootRegistryError("invalid_workspace_root")
    if not os.path.isabs(workspace_root):
        raise ShadowRootRegistryError("workspace_root_not_absolute")
    key = (adapter_namespace, tool_name, schema_version)
    target = f"{adapter_namespace}/{tool_name}@{schema_version}"
    with _LOCK:
        if key in _POISONED:
            raise ShadowRootRegistryError(f"poisoned_shadow_root:{target}")
        existing = _ROOTS.get(key)
        if existing is not None and existing != workspace_root:
            _POISONED.add(key)
            _ROOTS.pop(key, None)
            raise ShadowRootRegistryError(f"conflicting_shadow_root:{target}")
        if existing is not None:
            return
        if _frozen:
            raise ShadowRootRegistryError(f"registry_frozen:{target}")
        _ROOTS[key] = workspace_root


def resolve_shadow_root(
    adapter_namespace: object,
    tool_name: object,
    schema_version: object,
) -> "str | None":
    """Return the trusted absolute root for the key, or None. RAISE-FREE and READINESS-GATED: None unless the registry
    is FROZEN (freeze-before-serve — no partial-registry read), and None for a malformed / unregistered / POISONED key.
    The caller (G6.3) skips shadow telemetry on None — never crashes, never gets a stale root.
    """
    if not _valid_key(adapter_namespace, tool_name, schema_version):
        return None
    key = (adapter_namespace, tool_name, schema_version)
    with _LOCK:
        if not _frozen or key in _POISONED:
            return None
        return _ROOTS.get(key)


def freeze_registry() -> None:
    """Publish readiness (idempotent). After freeze: resolve serves; NEW-key register fails; identical re-register no-op."""
    global _frozen
    with _LOCK:
        _frozen = True


def is_frozen() -> bool:
    with _LOCK:
        return _frozen


def reset_for_tests() -> None:
    """Clear roots + poison set + unfreeze. Tests ONLY (mirrors action_canonicalize.reset_for_tests)."""
    global _frozen
    with _LOCK:
        _ROOTS.clear()
        _POISONED.clear()
        _frozen = False
