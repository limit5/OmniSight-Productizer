"""LLM pricing lookup (Z.3 #292).

Reads `config/llm_pricing.yaml` (the singular `config/` dir established by
Z.3 checkbox 1, distinct from the plural `configs/` used by forecast.py and
context_limits.py) and exposes a single `get_pricing(provider, model)`
entry-point used by `backend/routers/system.py::track_tokens` to convert
token counts into USD without hard-coding rates in Python.

Lookup chain (checkbox 3 layers throttled warnings onto the fallback arms):
    1. Exact `providers[<provider>][<model>]` hit.
    2. Provider known, model unknown → `providers[<provider>]._default`
       (emits `WARNING` once per provider per 24h within this worker).
    3. Provider unknown OR not provided → global `defaults` (emits
       `WARNING` once per provider-key per 24h within this worker).
    4. YAML missing / unreadable → `_HARD_CODED_FALLBACK` per-model dict
       (bit-identical to the pre-Z.3 dict at system.py:1094-1103) so a
       corrupt/missing YAML at boot never crashes billing — Z.3 checkbox 6
       test will exercise this path.

Provider auto-detect: callers like `track_tokens(model, ...)` only know the
model. Passing `provider=None` triggers a scan across all known provider
tables for an exact model-id match; first hit wins. Unknown model under
`None` provider falls through to global defaults.

Module-global state audit (per SOP Step 1):
    - `_PRICING_CACHE` is a module-level dict. Each uvicorn worker derives
      the same value from the same on-disk YAML at first call → matches the
      "derived from a shared static source" escape hatch (answer 1 of the
      three valid answers). The YAML is bundled in the production image and
      read-only at runtime.
    - `reload()` clears this worker's local cache. Z.3 checkbox 4 wires
      `POST /runtime/pricing/reload` to call `reload()` locally, then
      `publish_cross_worker(PRICING_RELOAD_EVENT, ...)` so peer workers'
      registered callback (`_on_pricing_reload_event` below) re-reads the
      same YAML — matches answer 2 of the three valid audit answers
      ("Redis-coordinated"). When Redis is absent the broadcast no-ops
      and only the calling worker reloads (degraded but safe; operator
      runbook covers the manual rolling restart fallback).
    - There is a small reload-propagation window where worker A has
      already re-read the YAML and worker B has not yet processed the
      pubsub message (bounded by `pubsub.get_message(timeout=1.0)` in
      `shared_state.start_pubsub_listener`). Token-tracking calls landing
      on B during that window will bill at the old rates. This is
      acceptable per Z.3 checkbox 7 — "既有 token usage 紀錄不重算成本，
      保留當時計費，只影響未來計價" — the old vs new rates are both
      valid "future" prices in those sub-second windows.
    - `_FALLBACK_WARN_TS` (Z.3 checkbox 3) is a module-level dict mapping
      a throttle key (`fallback_arm:<provider_key>`) to the unix timestamp
      of its last `WARNING` emit. **Intentionally per-worker** — answer 3
      of the three valid audit answers ("故意每 worker 獨立"). Routing
      log-throttle through Redis/SharedKV would add an RTT to every price
      lookup for zero observability benefit: at 2 workers × 2 replicas =
      4 aggregate workers, the ceiling is 4 warning lines per day per
      fallback arm per provider — still far below "log spam" by any
      operational standard, and rolling restarts reset cleanly per worker.
      A lock guards the dict so concurrent async callers in the same
      worker do not race on check-then-update.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PRICING_PATH = _PROJECT_ROOT / "config" / "llm_pricing.yaml"

# Boot-resilience: bit-identical to the pre-Z.3 hard-coded dict that lived
# at backend/routers/system.py:1094-1103. Used when the YAML is missing or
# unparseable so a broken config file cannot zero out billing or crash the
# worker. Z.3 checkbox 6 will assert this contract via a test.
_HARD_CODED_FALLBACK: dict[str, tuple[float, float]] = {
    "claude-opus-4-7": (5.0, 25.0),
    "claude-sonnet-4-20250514": (3.0, 15.0),
    "claude-opus-4-20250514": (15.0, 75.0),
    "gpt-4o": (5.0, 15.0),
    "gemini-1.5-pro": (0.5, 1.5),
    "grok-3-mini": (2.0, 10.0),
    "llama-3.3-70b-versatile": (0.6, 0.6),
    "deepseek-chat": (0.14, 0.28),
}
_HARD_CODED_GLOBAL_DEFAULT: tuple[float, float] = (1.0, 3.0)

_DEFAULT_KEY = "_default"

_PRICING_CACHE: dict[str, Any] | None = None

# Z.3 checkbox 3 (#292): per-worker log throttle for fallback arm hits.
# Map `fallback_arm:<provider_key>` → unix ts of last WARNING emit. See
# module docstring "Module-global state audit" for the per-worker rationale.
_WARN_THROTTLE_SECONDS = 86400.0  # 1 warning per key per 24 h per worker
_FALLBACK_WARN_TS: dict[str, float] = {}
_FALLBACK_WARN_LOCK = threading.Lock()


def _maybe_warn_fallback(throttle_key: str, fmt: str, *args: Any) -> None:
    """Emit a fallback `WARNING` once per `throttle_key` per 24 h.

    `throttle_key` conventionally encodes both the fallback arm and the
    provider, e.g. `provider_default:anthropic` or `global_default:openai`,
    so two different arms on the same provider — and the same arm on two
    different providers — each get their own 24 h clock.
    """
    now = time.time()
    with _FALLBACK_WARN_LOCK:
        last = _FALLBACK_WARN_TS.get(throttle_key, 0.0)
        if now - last < _WARN_THROTTLE_SECONDS:
            return
        _FALLBACK_WARN_TS[throttle_key] = now
    logger.warning(fmt, *args)


def _coerce_rate_pair(raw: object) -> tuple[float, float] | None:
    """Pull `(input, output)` out of a YAML mapping; return None if invalid.

    Tolerates the two natural shapes — `{input: X, output: Y}` (the YAML
    convention) and a 2-tuple/list — so future hand-edits stay forgiving.
    """
    if isinstance(raw, dict):
        try:
            return float(raw["input"]), float(raw["output"])
        except (KeyError, TypeError, ValueError):
            return None
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        try:
            return float(raw[0]), float(raw[1])
        except (TypeError, ValueError):
            return None
    return None


def _load_pricing() -> dict[str, Any]:
    """Read the YAML once and cache parsed structure.

    Returned dict shape:
        {
            "providers": {
                "<provider>": {"<model>": (in, out), "_default": (in, out)?},
                ...
            },
            "defaults": (in, out),
            "metadata": {...},  # passthrough for GET /runtime/pricing
        }
    On any load/parse failure we cache an empty providers map plus the
    hard-coded global default so subsequent lookups still return numbers.
    """
    global _PRICING_CACHE
    if _PRICING_CACHE is not None:
        return _PRICING_CACHE

    parsed: dict[str, Any] = {
        "providers": {},
        "defaults": _HARD_CODED_GLOBAL_DEFAULT,
        "metadata": {},
        "_loaded_from_yaml": False,
    }
    try:
        if _PRICING_PATH.exists():
            data = yaml.safe_load(_PRICING_PATH.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                providers_raw = data.get("providers") or {}
                if isinstance(providers_raw, dict):
                    providers: dict[str, dict[str, tuple[float, float]]] = {}
                    for provider, models in providers_raw.items():
                        if not isinstance(models, dict):
                            continue
                        table: dict[str, tuple[float, float]] = {}
                        for model, raw in models.items():
                            pair = _coerce_rate_pair(raw)
                            if pair is not None:
                                table[str(model)] = pair
                        providers[str(provider).lower()] = table
                    parsed["providers"] = providers

                defaults_pair = _coerce_rate_pair(data.get("defaults"))
                if defaults_pair is not None:
                    parsed["defaults"] = defaults_pair

                meta = data.get("metadata")
                if isinstance(meta, dict):
                    parsed["metadata"] = meta

                parsed["_loaded_from_yaml"] = True
        else:
            logger.warning(
                "llm_pricing.yaml not found at %s — billing falls back to "
                "hard-coded rates", _PRICING_PATH,
            )
    except Exception as exc:
        # Catch broadly: a corrupt YAML must not crash the worker. The
        # hard-coded fallback table keeps billing alive.
        logger.warning("llm_pricing.yaml load failed (%s); using hard-coded fallback", exc)

    _PRICING_CACHE = parsed
    return _PRICING_CACHE


def _scan_providers_for_model(model: str) -> tuple[float, float] | None:
    """First-hit-wins scan across providers when caller did not supply one."""
    if not model:
        return None
    cache = _load_pricing()
    for table in cache["providers"].values():
        if model in table:
            return table[model]
    return None


def get_pricing(provider: str | None, model: str) -> tuple[float, float]:
    """Return `(input_per_mtok, output_per_mtok)` USD for the given model.

    Lookup order:
        provider+model exact → provider `_default` → global `defaults`
        → hard-coded boot fallback. `provider=None` triggers a model-only
        scan across providers, then falls through to global defaults.

    Each fallback arm emits a `WARNING` the first time it is hit for a
    given provider-key within a 24 h window (per-worker throttle, see
    module docstring). Exact hits and `provider=None` scan hits are not
    considered fallbacks and never log.

    Returns a tuple of floats — never raises. Unknown inputs always resolve
    to the global default rather than 0.0/0.0 so dashboards surface
    "expensive unknown" rather than silently mis-billing as free.
    """
    cache = _load_pricing()
    providers: dict[str, dict[str, tuple[float, float]]] = cache["providers"]

    if model:
        if provider:
            provider_key = provider.strip().lower()
            table = providers.get(provider_key)
            if table is not None:
                exact = table.get(model)
                if exact is not None:
                    return exact
                provider_default = table.get(_DEFAULT_KEY)
                if provider_default is not None:
                    _maybe_warn_fallback(
                        f"provider_default:{provider_key}",
                        "llm_pricing fallback: provider=%r known but model=%r "
                        "unknown; billing at providers[%s]._default=%s "
                        "(throttled 1/day/provider per worker)",
                        provider_key, model, provider_key, provider_default,
                    )
                    return provider_default
        else:
            scanned = _scan_providers_for_model(model)
            if scanned is not None:
                return scanned

    # Both-unknown arm: either provider was None and the model-scan missed,
    # or provider is not in the YAML, or provider is in the YAML but has no
    # _default and no exact match. All three collapse to global defaults.
    provider_label = (provider or "").strip().lower() or "<unknown>"
    if cache["_loaded_from_yaml"]:
        _maybe_warn_fallback(
            f"global_default:{provider_label}",
            "llm_pricing fallback: no provider/model match for "
            "provider=%r model=%r; billing at global defaults=%s "
            "(throttled 1/day/provider per worker)",
            provider, model, cache["defaults"],
        )
        return cache["defaults"]

    # YAML never loaded — try the per-model hard-coded fallback before
    # giving up to the global default. Keeps the eight pre-Z.3 models
    # billing at exactly their historical rate when the YAML is broken.
    if model and model in _HARD_CODED_FALLBACK:
        return _HARD_CODED_FALLBACK[model]
    _maybe_warn_fallback(
        f"hardcoded_global:{provider_label}",
        "llm_pricing fallback: YAML unavailable and model=%r has no "
        "hard-coded rate for provider=%r; billing at boot-safety "
        "default=%s (throttled 1/day/provider per worker)",
        model, provider, _HARD_CODED_GLOBAL_DEFAULT,
    )
    return _HARD_CODED_GLOBAL_DEFAULT


def reload() -> dict[str, Any]:
    """Re-read the YAML on the next lookup; return a small status report.

    This clears the local-process cache only. Cross-worker fan-out (so all
    uvicorn workers re-read together) is wired in Z.3 checkbox 4 along
    with the `POST /runtime/pricing/reload` endpoint that broadcasts a
    SharedKV reload signal.
    """
    global _PRICING_CACHE
    _PRICING_CACHE = None
    fresh = _load_pricing()
    return {
        "loaded_from_yaml": fresh["_loaded_from_yaml"],
        "providers": sorted(fresh["providers"].keys()),
        "metadata": fresh.get("metadata", {}),
    }


def get_metadata() -> dict[str, Any]:
    """Expose the YAML's `metadata` block (for GET /runtime/pricing in checkbox 5)."""
    return dict(_load_pricing().get("metadata", {}))


def reset_cache_for_tests() -> None:
    """Clear the YAML cache + fallback-warn throttle so tests start fresh.

    Production code path is `reload()`; this exists strictly for the test
    suite, mirroring the same hook in `backend/context_limits.py`. The
    throttle map is cleared too so each test gets a pristine "first emit"
    window regardless of what earlier tests in the same process did.
    """
    global _PRICING_CACHE
    _PRICING_CACHE = None
    with _FALLBACK_WARN_LOCK:
        _FALLBACK_WARN_TS.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Z.3 checkbox 4 (#292) — cross-worker reload fan-out
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# `POST /runtime/pricing/reload` (in backend/routers/system.py) reloads
# the YAML on the worker that received the request, then publishes a
# `PRICING_RELOAD_EVENT` over Redis pub/sub. Every worker (including the
# originator) registers `_on_pricing_reload_event` below at import time
# so peers re-read the same YAML without an explicit per-worker hit.
# Origin filtering is intentionally absent: invalidating a stale cache
# costs ~one disk read on the next get_pricing() call, much cheaper than
# the bookkeeping required to suppress one self-message.

PRICING_RELOAD_EVENT = "pricing_reload"


def _on_pricing_reload_event(event: str, data: dict) -> None:
    """Cross-worker callback: clear this worker's pricing cache.

    Invoked by `shared_state.start_pubsub_listener` when any worker
    publishes `PRICING_RELOAD_EVENT` via `publish_cross_worker`. Body
    is intentionally tiny — the next `get_pricing()` call re-loads
    the YAML lazily, so this callback only has to invalidate.
    """
    if event != PRICING_RELOAD_EVENT:
        return
    global _PRICING_CACHE
    _PRICING_CACHE = None
    logger.info(
        "llm_pricing cross-worker reload received (origin=%s); "
        "next lookup will re-read YAML",
        data.get("origin_worker", "<unknown>") if isinstance(data, dict) else "<unknown>",
    )


# Register at import time — mirrors the `events.py` precedent. Wrapped in
# try/except so the import order never blocks on a circular dep or a
# missing shared_state module during early bootstrap (matches the same
# defensive pattern in backend/events.py:265-269).
try:
    from backend.shared_state import register_cross_worker_callback as _register_cb
    _register_cb(_on_pricing_reload_event)
except Exception:  # pragma: no cover — defensive, like events.py
    pass
