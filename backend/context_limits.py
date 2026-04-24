"""Provider / model context-window limit lookup (ZZ.A2 #303-2).

Sibling to the pricing loader in `backend/forecast.py::_load_pricing()` — both
read a hand-authored YAML under `configs/` mapping (provider → key → scalar).
This one reads `configs/context_window_limits.yaml` and exposes a single
public entry point used by the SSE `turn_metrics` emitter and the UI to
compute the context-usage percentage bar rendered on the TokenUsageStats
card (ZZ.A2 checkboxes 3 + 4).

Lookup contract (documented in the YAML header):
    1. Exact model id under the provider (e.g. "claude-opus-4-7").
    2. Provider's `default` entry.
    3. Return None if `default` is null or the provider is unknown — the UI
       must render "—" instead of fabricating a percentage against a wrong
       limit.

Ollama exception: local `num_ctx` is operator-configurable and the YAML's
per-model numbers are upstream maxima. The env var
`OMNISIGHT_OLLAMA_CONTEXT_LIMIT` overrides every ollama lookup, for
deployments running tighter than upstream (e.g. 8k instead of 128k to fit on
smaller GPUs).

Module-global state audit (per SOP Step 1): `_LIMITS_CACHE` is filled once
from the on-disk YAML. Each uvicorn worker recomputes the same value from
the same file, so cross-worker divergence is impossible — matches the
"derived from a shared static source" escape hatch. The YAML is bundled in
the production image, not mutated at runtime.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LIMITS_PATH = _PROJECT_ROOT / "configs" / "context_window_limits.yaml"

_OLLAMA_OVERRIDE_ENV = "OMNISIGHT_OLLAMA_CONTEXT_LIMIT"

_LimitValue = int | None
_ProviderTable = dict[str, _LimitValue]
_LIMITS_CACHE: dict[str, _ProviderTable] | None = None


def _coerce_limit(raw: object) -> _LimitValue:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, float):
        ivalue = int(raw)
        return ivalue if ivalue > 0 else None
    try:
        ivalue = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return ivalue if ivalue > 0 else None


def _load_limits() -> dict[str, _ProviderTable]:
    global _LIMITS_CACHE
    if _LIMITS_CACHE is not None:
        return _LIMITS_CACHE
    table: dict[str, _ProviderTable] = {}
    try:
        if _LIMITS_PATH.exists():
            data = yaml.safe_load(_LIMITS_PATH.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                for provider, models in data.items():
                    if not isinstance(models, dict):
                        continue
                    table[str(provider).lower()] = {
                        str(model): _coerce_limit(raw) for model, raw in models.items()
                    }
    except Exception as exc:
        logger.warning("context_window_limits.yaml load failed: %s", exc)
    _LIMITS_CACHE = table
    return _LIMITS_CACHE


def _ollama_env_override() -> _LimitValue:
    raw = os.environ.get(_OLLAMA_OVERRIDE_ENV, "").strip()
    if not raw:
        return None
    return _coerce_limit(raw)


def get_context_limit(provider: str | None, model: str | None) -> int | None:
    """Return the max context-window size (tokens) for the given provider+model.

    Returns None when the provider is unknown, the provider's `default` is
    `null` in the YAML (Ollama / OpenRouter pass-through routes), or inputs
    are blank — callers must treat None as "no data" (render "—"), not as
    zero. Ollama honours `OMNISIGHT_OLLAMA_CONTEXT_LIMIT` as a per-deployment
    override regardless of model.
    """
    provider_key = (provider or "").strip().lower()
    if not provider_key:
        return None

    if provider_key == "ollama":
        override = _ollama_env_override()
        if override is not None:
            return override

    table = _load_limits().get(provider_key)
    if not table:
        return None

    model_key = (model or "").strip()
    if model_key and model_key in table:
        limit = table[model_key]
        if limit is not None:
            return limit

    return table.get("default")


def reset_cache_for_tests() -> None:
    """Clear the YAML cache so a test can rewrite the file and re-load.

    Production code must not call this — it exists for the test suite only.
    """
    global _LIMITS_CACHE
    _LIMITS_CACHE = None
