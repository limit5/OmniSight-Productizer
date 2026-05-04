"""Z.2 (#291) — LLM provider balance background refresher.

Lifespan-scoped async task. Every ``DEFAULT_INTERVAL_S`` (600 s = 10
min) the loop iterates the providers in
:data:`backend.llm_balance.SUPPORTED_BALANCE_PROVIDERS`, resolves each
provider's API key from :mod:`backend.config`'s ``Settings`` scalar
fields (``{provider}_api_key``), calls the matching fetcher, and
writes the normalised ``BalanceInfo`` into
``SharedKV("provider_balance")`` keyed by provider name. The Z.2
endpoint checkboxes (``GET /runtime/providers/{provider}/balance``
and the batch variant) will read from this same namespace.

Per-provider backoff state
──────────────────────────
Each supported provider has its own
:class:`_ProviderBackoff` record:

* Successful fetch → ``next_attempt_at = now + base_interval``
  (the normal 10-minute cadence).
* Transport / 5xx / malformed body (raises
  :class:`~backend.llm_balance.BalanceFetchError`) →
  ``consecutive_failures`` bumps by one, the next attempt is delayed
  ``min(base_interval × 2^failures, MAX_BACKOFF_S)`` with
  ``MAX_BACKOFF_S = 3600`` (one hour cap per the Z.2 checkbox spec).
* Auth failure (fetcher returns ``None``) gets the same exponential
  backoff as a transport error, because hammering a revoked key is
  as bad for the vendor relationship as hammering a dead server.
  The cached snapshot is *not* overwritten — the operator may rotate
  the key between ticks, and we want the next successful refresh to
  land cleanly without a stale "auth_failed" envelope.
* No key configured → skip silently. No HTTP call, no backoff, no
  cache touch. Next tick re-evaluates; if a key gets set later it
  picks up immediately.

Module-global audit (SOP Step 1, 2026-04-21 rule)
─────────────────────────────────────────────────
Two module-globals:

1. ``_LOOP_RUNNING`` — singleton flag matching the Phase-52 DLQ /
   Phase-63-E memory-decay convention. Each uvicorn worker runs its
   own copy of the loop; the flag prevents a double-start if
   ``run_refresh_loop()`` is called twice in the same worker (e.g.
   tests that re-enter lifespan). Qualified answer #1 — each worker
   derives the same value (``True`` while its loop is running).
2. Per-provider backoff state is held **inside** ``run_refresh_loop``
   (local ``state`` dict) or passed explicitly to
   :func:`refresh_once`. **Not** module-global on purpose: with N
   workers each worker maintains independent backoff. Vendor API
   traffic scales linearly with worker count (N × 6 req/hour/provider
   = ~12 req/h at 2 workers × 2 supported providers — well under
   every documented provider rate limit). The SharedKV write is the
   cross-worker coordination point: whichever worker writes last
   wins; readers of the future ``/runtime/providers/*/balance``
   endpoints see a single latest snapshot regardless of which worker
   fetched it. Qualified answer #3 — "intentionally per-worker" and
   documented here.

Read-after-write audit
──────────────────────
The only write is ``SharedKV.set(provider, json.dumps(info))``.
``SharedKV`` itself composes under concurrency — the Redis hash write
is atomic, the in-memory fallback uses ``threading.Lock``. Readers
always see a complete envelope; there is no two-phase write that a
concurrent reader could catch mid-flight.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from backend.llm_balance import (
    BalanceFetchError,
    BalanceInfo,
    SUPPORTED_BALANCE_PROVIDERS,
)
from backend.shared_state import SharedKV

logger = logging.getLogger(__name__)


# Base cadence: 10 minutes. Providers refresh this often in steady state.
DEFAULT_INTERVAL_S = 600.0

# Exponential backoff cap: one hour. Per the Z.2 checkbox spec —
# any delay longer than this is worse than showing an "unavailable"
# state to the operator, so we stop extending past 3600 s.
MAX_BACKOFF_S = 3600.0

# SharedKV namespace name. Consumers (upcoming Z.2 endpoints) must
# read from this exact namespace. Exported so the endpoint module can
# import the constant rather than hard-coding the string twice.
BALANCE_NAMESPACE = "provider_balance"

# Z.2 boundary contract (2026-04-24): separate namespace holding
# per-provider "last failure timestamp" markers so the endpoint can
# render ``stale_since`` next to a cached value when the provider's API
# is currently 5xx-ing / unreachable / returning malformed bodies.
# Written on :class:`BalanceFetchError` + unexpected-exception paths,
# cleared on successful fetch. Auth-fail (fetcher returns ``None``)
# intentionally does NOT touch this marker — auth errors are
# operator-side (key revoked / rotated), not "provider is having
# trouble", so the "stale because server is down" semantic does not
# apply.
STALE_NAMESPACE = "provider_balance_stale"


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
        return v if v > 0 else default
    except ValueError:
        return default


@dataclass
class _ProviderBackoff:
    """Per-provider failure tracker owned by :func:`run_refresh_loop`.

    Not a module-global — see file docstring for why.
    """

    consecutive_failures: int = 0
    next_attempt_at: float = 0.0

    def reset(self, *, now: float, base_interval_s: float) -> None:
        self.consecutive_failures = 0
        self.next_attempt_at = now + base_interval_s

    def record_failure(
        self, *, now: float, base_interval_s: float,
    ) -> float:
        """Bump ``consecutive_failures`` by one, compute the delay
        ``min(base × 2^failures, MAX_BACKOFF_S)``, and shift
        ``next_attempt_at`` forward by that delay. Returns the delay
        in seconds so the caller can log it."""
        self.consecutive_failures += 1
        delay = min(
            base_interval_s * (2 ** self.consecutive_failures),
            MAX_BACKOFF_S,
        )
        self.next_attempt_at = now + delay
        return delay


def _kv() -> SharedKV:
    return SharedKV(BALANCE_NAMESPACE)


def _stale_kv() -> SharedKV:
    return SharedKV(STALE_NAMESPACE)


def _write_stale_marker(
    stale_kv: SharedKV, provider: str, now: float,
) -> None:
    """Record a provider-side failure timestamp.

    Best-effort: a SharedKV write failure is logged by the caller
    context (``refresh_once`` wraps writes in ``except Exception``)
    rather than here — we keep this helper contract-simple so the
    refresher + endpoint can share the exact same write semantics
    without a thin wrapper drifting.
    """
    stale_kv.set(provider, f"{now:.6f}")


def _clear_stale_marker(stale_kv: SharedKV, provider: str) -> None:
    """Drop the stale marker for a provider.

    Called after a successful fetch so the endpoint stops rendering
    ``stale_since`` on the next cache read.
    """
    stale_kv.delete(provider)


def _read_stale_marker(
    stale_kv: SharedKV, provider: str,
) -> float | None:
    """Return the recorded failure epoch, or ``None`` when the slot is
    empty / unparseable.

    Unparseable entries are self-healed — we delete and return
    ``None`` so a corrupted slot does not permanently mask a provider
    behind a "stale" render that never clears on the next success.
    """
    raw = stale_kv.get(provider, "")
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        try:
            stale_kv.delete(provider)
        except Exception:
            pass
        return None


def _serialise_balance(info: BalanceInfo) -> str:
    """Serialise a ``BalanceInfo`` for the SharedKV hash slot.

    ``json.dumps`` with ``default=str`` so any stray non-JSON type the
    vendor may sneak into ``raw`` (e.g. ``datetime``) degrades to a
    string rather than crashing the entire refresh. ``None`` numeric
    fields are preserved (not dropped) — the dashboard distinguishes
    "provider did not report usage" from "refresher has not run yet".
    """
    return json.dumps(dict(info), default=str)


def _resolve_api_key(provider: str) -> str | None:
    """Read the system-level API key for this provider from
    ``Settings``.

    The refresher runs in system scope (no tenant contextvar, no
    pool-acquire needed for every tick), so the Settings scalar — which
    Phase-5b's auto-migration keeps in lock-step with the
    ``llm_credentials`` table for the platform-default tenant — gives
    us the canonical key without the deprecation-warn noise a
    :func:`get_llm_credential_sync` call would generate once per
    worker. Empty / unset → ``None`` so the loop can short-circuit to
    the "no_key" outcome without a fetch attempt.
    """
    from backend.config import settings

    attr = f"{provider}_api_key"
    key = (getattr(settings, attr, "") or "").strip()
    return key or None


async def refresh_once(
    *,
    state: dict[str, _ProviderBackoff],
    base_interval_s: float = DEFAULT_INTERVAL_S,
    now: float | None = None,
    fetchers: dict[str, Callable[..., Awaitable[BalanceInfo | None]]] | None = None,
    key_resolver: Callable[[str], str | None] | None = None,
    kv: SharedKV | None = None,
    stale_kv: SharedKV | None = None,
) -> dict[str, str]:
    """Run one refresh pass over all supported providers.

    Returns a ``{provider: outcome}`` map where outcome is one of:

    * ``"ok"`` — fetched + cached, backoff reset, stale marker cleared.
    * ``"auth_fail"`` — fetcher returned ``None`` (401/403 / bad key).
      Cache untouched, backoff advanced. Stale marker **not** touched —
      the key being revoked is an operator-side concern, not a
      "provider is down" signal that should change the cached-snapshot
      freshness contract.
    * ``"fetch_error"`` — :class:`BalanceFetchError` or unexpected
      exception. Cache untouched, backoff advanced, stale marker
      written so any prior cached snapshot served by the endpoint
      next carries ``stale_since=<now>``.
    * ``"no_key"`` — no key configured in Settings; no HTTP call,
      no backoff, no cache write, no stale marker change.
    * ``"backoff"`` — provider is currently within its backoff window;
      skipped this tick (no fetch attempt, no cache/stale write).

    Injection points (``fetchers``, ``key_resolver``, ``kv``,
    ``stale_kv``, ``now``) exist so unit tests can drive the pure
    logic without touching HTTP, Redis, or the real clock. Production
    callers pass nothing and the defaults do the right thing.
    """
    _now = now if now is not None else time.time()
    _fetchers = (
        fetchers if fetchers is not None else SUPPORTED_BALANCE_PROVIDERS
    )
    _resolve = key_resolver if key_resolver is not None else _resolve_api_key
    _store = kv if kv is not None else _kv()
    _stale = stale_kv if stale_kv is not None else _stale_kv()

    outcomes: dict[str, str] = {}
    for provider, fetcher in _fetchers.items():
        bo = state.setdefault(provider, _ProviderBackoff())
        if bo.next_attempt_at > _now:
            outcomes[provider] = "backoff"
            continue

        api_key = _resolve(provider)
        if not api_key:
            outcomes[provider] = "no_key"
            continue

        try:
            info = await fetcher(api_key, now=_now)
        except BalanceFetchError as exc:
            delay = bo.record_failure(
                now=_now, base_interval_s=base_interval_s,
            )
            logger.warning(
                "llm_balance_refresher: %s fetch failed (%s); "
                "backing off %.0fs",
                provider, exc.reason, delay,
            )
            try:
                _write_stale_marker(_stale, provider, _now)
            except Exception as stale_exc:
                logger.warning(
                    "llm_balance_refresher: %s stale marker write "
                    "failed: %s",
                    provider, stale_exc,
                )
            outcomes[provider] = "fetch_error"
            continue
        except Exception as exc:
            # Any unexpected error — network, JSON, whatever — treat as
            # a transient fetch error. Back off so we don't hammer the
            # vendor and log loudly so the operator notices. Same
            # stale-marker semantics as ``BalanceFetchError`` since from
            # the endpoint's perspective "the refresh failed for a
            # reason that is not the operator's fault" is one concept.
            delay = bo.record_failure(
                now=_now, base_interval_s=base_interval_s,
            )
            logger.warning(
                "llm_balance_refresher: %s unexpected %s (%s); "
                "backing off %.0fs",
                provider, type(exc).__name__, exc, delay,
            )
            try:
                _write_stale_marker(_stale, provider, _now)
            except Exception as stale_exc:
                logger.warning(
                    "llm_balance_refresher: %s stale marker write "
                    "failed: %s",
                    provider, stale_exc,
                )
            outcomes[provider] = "fetch_error"
            continue

        if info is None:
            delay = bo.record_failure(
                now=_now, base_interval_s=base_interval_s,
            )
            logger.warning(
                "llm_balance_refresher: %s auth failure; "
                "backing off %.0fs",
                provider, delay,
            )
            outcomes[provider] = "auth_fail"
            continue

        try:
            _store.set(provider, _serialise_balance(info))
        except Exception as exc:
            # SharedKV already falls back to in-memory on Redis errors,
            # so reaching here usually means JSON serialisation blew up
            # on an exotic ``raw`` payload. Log but don't kill the loop.
            logger.warning(
                "llm_balance_refresher: %s SharedKV write failed: %s",
                provider, exc,
            )
        # Success → clear any previously-recorded stale marker so the
        # endpoint stops rendering ``stale_since`` on the next read.
        # Wrapped defensively so a flaky SharedKV here cannot cascade
        # into an uncaught exception that skips the backoff reset.
        try:
            _clear_stale_marker(_stale, provider)
        except Exception as stale_exc:
            logger.warning(
                "llm_balance_refresher: %s stale marker clear "
                "failed: %s",
                provider, stale_exc,
            )
        bo.reset(now=_now, base_interval_s=base_interval_s)
        outcomes[provider] = "ok"

    return outcomes


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Background loop
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_LOOP_RUNNING = False


async def run_refresh_loop(*, interval_s: float | None = None) -> None:
    """Singleton background coroutine.

    Mirrors :func:`backend.memory_decay.run_decay_loop` and
    :func:`backend.notifications.run_dlq_loop`: single guard flag,
    cancellation-aware ``asyncio.sleep``, ``finally`` resets the flag
    so a re-entered lifespan (tests) starts cleanly.

    Boot policy: runs one refresh immediately so the dashboard has
    fresh data at app-up time; otherwise the first tick would be 10 min
    out and every dashboard load in the interim would render
    "unknown". The initial tick is best-effort — any exception is
    logged and swallowed so a broken provider doesn't crash startup.
    """
    global _LOOP_RUNNING
    if _LOOP_RUNNING:
        return
    _LOOP_RUNNING = True

    interval = interval_s if interval_s is not None else _env_float(
        "OMNISIGHT_LLM_BALANCE_INTERVAL_S", DEFAULT_INTERVAL_S,
    )
    state: dict[str, _ProviderBackoff] = {}

    try:
        # Immediate first tick — see docstring rationale.
        try:
            await refresh_once(state=state, base_interval_s=interval)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "llm_balance_refresher initial tick failed: %s", exc,
            )

        while True:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            try:
                await refresh_once(state=state, base_interval_s=interval)
            except Exception as exc:
                logger.warning(
                    "llm_balance_refresher tick failed: %s", exc,
                )
    except asyncio.CancelledError:
        pass
    finally:
        _LOOP_RUNNING = False
