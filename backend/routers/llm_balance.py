"""Z.2 (#291) — ``/runtime/providers/*/balance`` endpoint.

Surface
───────
* ``GET /runtime/providers/{provider}/balance`` — read the cached
  balance for a single LLM provider. Prefers
  :class:`~backend.shared_state.SharedKV` (written by the background
  refresher in :mod:`backend.llm_balance_refresher`); when the cache
  slot is empty we trigger **one** live fetch, write the result, and
  return it so the first dashboard load after a cold start does not
  render "unknown" for 10 minutes.

Response envelope
─────────────────
Three top-level shapes, keyed on ``status``:

* ``{"status": "ok", "provider": ..., "currency": ...,
  "balance_remaining": ..., "granted_total": ..., "usage_total": ...,
  "last_refreshed_at": ..., "source": "cache"|"live", "raw": {...}}``
* ``{"status": "unsupported", "provider": ..., "reason":
  "provider does not expose a public balance API with API-key
  authentication"}`` — provider absent from
  :data:`backend.llm_balance.SUPPORTED_BALANCE_PROVIDERS`.
* ``{"status": "error", "provider": ..., "message": ...}`` — no cached
  snapshot AND the on-demand fetch failed (missing key / auth fail /
  transport / 5xx). The cache is intentionally **not** written on
  failure — the operator may be mid-rotation, and we want the next
  refresh (scheduled or on-demand) to pick the new key up cleanly.

The ``stale_since`` marker for the "5xx with a prior cached snapshot"
case is the next Z.2 checkbox (邊界). This endpoint only serves the
cached-or-trigger contract spelled out in the Z.2 spec.

Module-global audit (SOP Step 1, 2026-04-21 rule)
─────────────────────────────────────────────────
No module-globals introduced here. Every call goes through
:func:`resolve_balance` with injected ``kv`` / ``fetcher`` /
``key_resolver`` hooks; production defaults import the module-const
registry and a fresh :class:`SharedKV` handle (which is itself a thin
wrapper around the process-wide Redis client or its in-memory
fallback — cross-worker consistency is the SharedKV layer's
responsibility, not this router's). Qualified answer #2 ("via Redis")
for the cache; qualified answer #3 ("intentionally per-worker") for
the on-demand fetch path — N workers will each issue one vendor
request on cold-start, which for 2-worker × 2-provider is still well
under every documented rate limit.

Read-after-write audit
──────────────────────
Single writer site is ``kv.set(provider, _serialise_balance(info))``
inside :func:`resolve_balance`. ``SharedKV.set`` is atomic (Redis HSET
/ in-memory lock); concurrent readers either see the old snapshot or
the new one, never a torn write.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field

from backend import auth as _auth
from backend.llm_balance import (
    BalanceFetchError,
    BalanceInfo,
    SUPPORTED_BALANCE_PROVIDERS,
    is_balance_supported,
)
from backend.llm_balance_refresher import (
    BALANCE_NAMESPACE,
    _resolve_api_key,
    _serialise_balance,
)
from backend.shared_state import SharedKV

logger = logging.getLogger(__name__)


# Provider-name validation matches the service layer registry so a
# typo'd path parameter resolves to 400 rather than silently hitting
# the "unsupported" branch (which would mislead callers into thinking
# the provider exists but lacks a balance API).
_VALID_PROVIDER_NAMES: frozenset[str] = frozenset({
    "anthropic", "google", "openai", "xai", "groq",
    "deepseek", "together", "openrouter", "ollama",
})

# Static reason string the UI surfaces under the "unsupported" pill.
# Locked by the Z.2 spec — keep verbatim so downstream tests / UI
# copy don't drift.
_UNSUPPORTED_REASON = (
    "provider does not expose a public balance API "
    "with API-key authentication"
)


router = APIRouter(
    prefix="/runtime/providers",
    tags=["runtime-providers"],
    dependencies=[Depends(_auth.require_admin)],
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Service layer — pure, injectable
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _unsupported_envelope(provider: str) -> dict[str, Any]:
    return {
        "status": "unsupported",
        "provider": provider,
        "reason": _UNSUPPORTED_REASON,
    }


def _ok_envelope(
    provider: str, info: BalanceInfo, *, source: str,
) -> dict[str, Any]:
    """Shape an ``ok`` response from a ``BalanceInfo``.

    ``source`` is ``"cache"`` when served from SharedKV, ``"live"``
    when the endpoint triggered a fresh fetch this request. The
    dashboard uses this hint to render a subtle "just-fetched" vs
    "cached N min ago" distinction.
    """
    return {
        "status": "ok",
        "provider": provider,
        "currency": info.get("currency", ""),
        "balance_remaining": info.get("balance_remaining"),
        "granted_total": info.get("granted_total"),
        "usage_total": info.get("usage_total"),
        "last_refreshed_at": info.get("last_refreshed_at"),
        "source": source,
        "raw": info.get("raw") or {},
    }


def _error_envelope(provider: str, message: str) -> dict[str, Any]:
    return {
        "status": "error",
        "provider": provider,
        "message": message,
    }


def _read_cache(
    kv: SharedKV, provider: str,
) -> BalanceInfo | None:
    """Fetch the ``provider`` slot from ``kv`` and JSON-decode it.

    Returns ``None`` when the slot is empty OR the payload is not
    round-trippable (should not happen — the refresher writes via
    :func:`_serialise_balance` — but defence-in-depth keeps a single
    bad write from permanently serving 500s to the dashboard).
    """
    raw = kv.get(provider, "")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning(
            "llm_balance: SharedKV slot for %s is not valid JSON "
            "(ignoring): %r",
            provider, raw[:200],
        )
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed  # type: ignore[return-value]


async def resolve_balance(
    provider: str,
    *,
    kv: SharedKV | None = None,
    fetchers: dict[str, Callable[..., Awaitable[BalanceInfo | None]]] | None = None,
    key_resolver: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """Serve one provider's balance envelope.

    Contract mirrors the Z.2 checkbox spec:

    1. Unsupported provider → ``unsupported`` envelope (no fetch, no
       cache touch).
    2. Cache hit → ``ok`` envelope with ``source="cache"``.
    3. Cache miss → call the fetcher once.
       * Fetcher returns a ``BalanceInfo`` → write to cache + ``ok``
         envelope with ``source="live"``.
       * Fetcher returns ``None`` (auth failure / no key) → ``error``
         envelope, **no** cache write.
       * Fetcher raises :class:`BalanceFetchError` or an unexpected
         exception → ``error`` envelope, **no** cache write.

    Injection hooks exist so tests can exercise every branch without
    touching Redis, HTTP, or the system clock; production callers leave
    everything ``None``.
    """
    if not is_balance_supported(provider):
        return _unsupported_envelope(provider)

    _fetchers = (
        fetchers if fetchers is not None else SUPPORTED_BALANCE_PROVIDERS
    )
    _resolve = key_resolver if key_resolver is not None else _resolve_api_key
    _store = kv if kv is not None else SharedKV(BALANCE_NAMESPACE)

    cached = _read_cache(_store, provider)
    if cached is not None:
        return _ok_envelope(provider, cached, source="cache")

    # Cache miss — trigger one live fetch. Resolving the key inside the
    # miss branch (rather than earlier) keeps keyless "unsupported"
    # providers from accidentally paying the ``Settings`` attribute
    # lookup on the happy path.
    api_key = _resolve(provider)
    if not api_key:
        return _error_envelope(
            provider,
            "no API key configured for this provider",
        )

    fetcher = _fetchers.get(provider)
    if fetcher is None:
        # Defence-in-depth — ``is_balance_supported`` already gated,
        # but a caller who injected a narrower ``fetchers`` dict might
        # not include this provider. Treat as unsupported-in-scope.
        return _unsupported_envelope(provider)

    try:
        info = await fetcher(api_key)
    except BalanceFetchError as exc:
        return _error_envelope(provider, f"fetch failed: {exc.reason}")
    except Exception as exc:  # noqa: BLE001 — outer boundary
        logger.warning(
            "llm_balance on-demand fetch unexpected error for %s: %s",
            provider, exc,
        )
        return _error_envelope(
            provider, f"unexpected error: {type(exc).__name__}",
        )

    if info is None:
        return _error_envelope(
            provider,
            "authentication failed — key may be missing or revoked",
        )

    try:
        _store.set(provider, _serialise_balance(info))
    except Exception as exc:
        # Cache write failure does not fail the request — operator
        # sees the live value, and the next refresher tick will retry.
        logger.warning(
            "llm_balance: SharedKV write failed for %s: %s",
            provider, exc,
        )

    return _ok_envelope(provider, info, source="live")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP surface
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.get("/{provider}/balance")
async def get_provider_balance(provider: str) -> dict[str, Any]:
    """Return the cached (or freshly fetched) LLM provider balance.

    Rejects provider names that aren't in the service-layer registry
    so a typo surfaces as HTTP 400 rather than a confusing
    ``unsupported`` envelope.
    """
    if provider not in _VALID_PROVIDER_NAMES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown provider {provider!r}; expected one of "
                f"{sorted(_VALID_PROVIDER_NAMES)}"
            ),
        )
    return await resolve_balance(provider)
