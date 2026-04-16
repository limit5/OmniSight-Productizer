"""M3 — Per-tenant-per-provider circuit breaker.

Background
==========

Phase 25 introduced a global ``_provider_failures`` dict in
``backend.agents.llm`` keyed only by provider name (e.g. ``"openai"``).
A 5-minute cooldown skipped a provider after one failure.  The blast
radius was the entire deployment: tenant A's bad OpenAI key would
trip the circuit for tenant B too.

This module replaces that scheme with circuit state keyed by the
triple ``(tenant_id, provider, api_key_fingerprint)``.  Each tenant /
key pair has its own open/closed state and cooldown clock, so a single
tenant's broken key cannot push other tenants onto the failover chain.

Public API
----------

  ``record_failure(tenant_id, provider, fingerprint, *, reason)``
      Open the circuit for the key.  Audits ``circuit.open`` (new
      transition only) and emits an SSE ``circuit_state`` event.

  ``record_success(tenant_id, provider, fingerprint)``
      Close an open circuit.  Audits ``circuit.close`` and emits SSE.

  ``is_open(tenant_id, provider, fingerprint) -> bool``
      Cheap check used in the ``get_llm`` failover loop.

  ``cooldown_remaining(tenant_id, provider, fingerprint) -> int``
      Seconds until the open circuit times out (0 when closed).

  ``snapshot(tenant_id=None, provider=None) -> list[dict]``
      Read state for the health endpoint and UI.  Filters by tenant
      and/or provider when supplied; returns every key when omitted.

  ``reset(tenant_id=None, provider=None, fingerprint=None)``
      Test helper / operator override.

Audit & SSE
-----------

Open and close transitions both:

  * call ``audit.log_sync`` with action ``circuit.open`` or
    ``circuit.close`` and ``entity_id = "<provider>/<fingerprint>"``.
    The current tenant context is honoured by ``tenant_insert_value``.
  * publish an SSE ``circuit_state`` event so Settings → LLM Providers
    refreshes without polling.

Memory bound
------------

The ``_state`` dict is capped at ``_MAX_KEYS`` entries (LRU-pruned by
``last_seen`` timestamp) so a steady stream of new tenants or rotated
keys cannot grow the dict without bound.  Mirrors the existing safety
in ``backend.agents.llm._record_provider_failure``.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)


# 5-minute cooldown — matches the legacy global PROVIDER_COOLDOWN so
# operator expectations don't shift between releases.
COOLDOWN_SECONDS: int = 300

# Bound on the per-key dict.  When exceeded, oldest-by-last_seen entries
# are evicted in batches.
_MAX_KEYS: int = 1024
_EVICT_TARGET: int = 768  # shrink to this size after a prune


# Sentinel used when no API key is available (e.g. provider has no
# configured key yet but is being probed).  Keeps the (tenant, provider,
# fp) triple well-formed without leaking blank strings to the audit log.
NO_KEY_FINGERPRINT: str = "no-key"


_lock = threading.Lock()
# key: (tenant_id, provider, fingerprint) -> state dict
_state: dict[tuple[str, str, str], dict[str, Any]] = {}


def _key(tenant_id: str | None, provider: str, fingerprint: str | None) -> tuple[str, str, str]:
    tid = tenant_id or "t-default"
    fp = fingerprint or NO_KEY_FINGERPRINT
    return (tid, provider, fp)


def _now() -> float:
    return time.time()


def _evict_if_needed_locked() -> None:
    """Caller must hold ``_lock``."""
    if len(_state) <= _MAX_KEYS:
        return
    items = sorted(_state.items(), key=lambda kv: kv[1].get("last_seen", 0))
    to_drop = len(_state) - _EVICT_TARGET
    for k, _v in items[:to_drop]:
        _state.pop(k, None)


def _emit_state_event(tenant_id: str, provider: str, fingerprint: str,
                      transition: str, reason: str | None) -> None:
    """Best-effort SSE + audit emission. Never raises into the caller."""
    try:
        from backend.events import bus
        bus.publish(
            "circuit_state",
            {
                "tenant_id": tenant_id,
                "provider": provider,
                "fingerprint": fingerprint,
                "transition": transition,  # "open" | "close"
                "reason": reason or "",
                "ts": _now(),
            },
            broadcast_scope="tenant",
            tenant_id=tenant_id,
        )
    except Exception as exc:
        logger.debug("circuit_state SSE publish skipped: %s", exc)

    try:
        from backend import audit
        from backend.db_context import set_tenant_id, current_tenant_id
        prior = current_tenant_id()
        # Audit chain is per-tenant; ensure we write under the tenant
        # whose circuit moved, even when the caller is on a system
        # context (e.g. background sweep).
        try:
            set_tenant_id(tenant_id)
            audit.log_sync(
                action=f"circuit.{transition}",
                entity_kind="circuit",
                entity_id=f"{provider}/{fingerprint}",
                after={"reason": reason} if reason else None,
                actor="system",
            )
        finally:
            set_tenant_id(prior)
    except Exception as exc:
        logger.debug("circuit_state audit skipped: %s", exc)


def record_failure(tenant_id: str | None, provider: str,
                   fingerprint: str | None, *, reason: str | None = None) -> None:
    """Mark the (tenant, provider, key) circuit as open.

    Only the *first* failure of an existing closed circuit triggers the
    audit + SSE side-effects; repeated failures while open just refresh
    the timestamp so the cooldown rolls forward.
    """
    if not provider:
        return
    k = _key(tenant_id, provider, fingerprint)
    now = _now()
    transitioned = False
    with _lock:
        cur = _state.get(k)
        if cur is None or not cur.get("open"):
            _state[k] = {
                "open": True,
                "opened_at": now,
                "last_failure": now,
                "last_seen": now,
                "failure_count": (cur.get("failure_count", 0) + 1) if cur else 1,
                "reason": reason,
            }
            transitioned = True
        else:
            cur["last_failure"] = now
            cur["last_seen"] = now
            cur["failure_count"] = cur.get("failure_count", 0) + 1
            if reason:
                cur["reason"] = reason
        _evict_if_needed_locked()
    if transitioned:
        tid, prov, fp = k
        _emit_state_event(tid, prov, fp, "open", reason)


def record_success(tenant_id: str | None, provider: str,
                   fingerprint: str | None) -> None:
    """Mark the circuit as closed.

    A no-op if it was already closed (or never seen), so happy-path
    success calls don't spam audit / SSE.
    """
    if not provider:
        return
    k = _key(tenant_id, provider, fingerprint)
    transitioned = False
    with _lock:
        cur = _state.get(k)
        if cur and cur.get("open"):
            cur["open"] = False
            cur["closed_at"] = _now()
            cur["last_seen"] = _now()
            cur["failure_count"] = 0
            transitioned = True
        elif cur:
            cur["last_seen"] = _now()
    if transitioned:
        tid, prov, fp = k
        _emit_state_event(tid, prov, fp, "close", reason=None)


def is_open(tenant_id: str | None, provider: str,
            fingerprint: str | None) -> bool:
    """Return True if the circuit is currently open AND inside its cooldown.

    Once the cooldown has elapsed the breaker auto-half-opens (returns
    False) so the next call has a chance to ride through and either
    close it via ``record_success`` or refresh ``last_failure`` via
    ``record_failure``.  This mirrors the existing global behaviour.
    """
    if not provider:
        return False
    k = _key(tenant_id, provider, fingerprint)
    with _lock:
        cur = _state.get(k)
        if not cur or not cur.get("open"):
            return False
        elapsed = _now() - cur.get("last_failure", 0)
        return elapsed < COOLDOWN_SECONDS


def cooldown_remaining(tenant_id: str | None, provider: str,
                       fingerprint: str | None) -> int:
    if not provider:
        return 0
    k = _key(tenant_id, provider, fingerprint)
    with _lock:
        cur = _state.get(k)
        if not cur or not cur.get("open"):
            return 0
        remaining = COOLDOWN_SECONDS - (_now() - cur.get("last_failure", 0))
        return max(0, int(remaining))


def snapshot(tenant_id: str | None = None,
             provider: str | None = None) -> list[dict[str, Any]]:
    """Return circuit state rows for the health endpoint / UI."""
    out: list[dict[str, Any]] = []
    now = _now()
    with _lock:
        for (tid, prov, fp), st in _state.items():
            if tenant_id is not None and tid != tenant_id:
                continue
            if provider is not None and prov != provider:
                continue
            open_now = bool(st.get("open"))
            cooldown = 0
            if open_now:
                cooldown = max(0, int(COOLDOWN_SECONDS - (now - st.get("last_failure", 0))))
                if cooldown == 0:
                    # Auto half-open after cooldown elapses
                    open_now = False
            out.append({
                "tenant_id": tid,
                "provider": prov,
                "fingerprint": fp,
                "open": open_now,
                "cooldown_remaining": cooldown,
                "failure_count": int(st.get("failure_count", 0)),
                "last_failure": st.get("last_failure"),
                "opened_at": st.get("opened_at"),
                "closed_at": st.get("closed_at"),
                "reason": st.get("reason"),
            })
    out.sort(key=lambda r: (r["tenant_id"], r["provider"], r["fingerprint"]))
    return out


def reset(tenant_id: str | None = None, provider: str | None = None,
          fingerprint: str | None = None) -> int:
    """Operator / test helper. Returns the number of cleared entries."""
    cleared = 0
    with _lock:
        for k in list(_state.keys()):
            tid, prov, fp = k
            if tenant_id is not None and tid != tenant_id:
                continue
            if provider is not None and prov != provider:
                continue
            if fingerprint is not None and fp != fingerprint:
                continue
            _state.pop(k, None)
            cleared += 1
    return cleared


def _resolve_active_fingerprint(provider: str) -> str:
    """Best-effort fingerprint for the *currently configured* key of
    ``provider`` in this process.

    M3 keeps using the global per-process API key (per-tenant secret
    integration is the next milestone), so the fingerprint is derived
    from ``settings.<provider>_api_key`` when present.  Returning the
    sentinel ``no-key`` keeps the key triple stable when no key is
    configured (e.g. Ollama or unconfigured providers).
    """
    try:
        from backend.config import settings
        from backend.secret_store import fingerprint as _fp
        attr = f"{provider}_api_key"
        val = getattr(settings, attr, "") or ""
        if val:
            return _fp(val)
    except Exception as exc:
        logger.debug("active fingerprint resolution failed for %s: %s", provider, exc)
    return NO_KEY_FINGERPRINT


def active_fingerprint(provider: str) -> str:
    """Public alias used by the failover path."""
    return _resolve_active_fingerprint(provider)


def _reset_for_tests() -> None:
    """Wipe all state (used by pytest fixtures)."""
    with _lock:
        _state.clear()
