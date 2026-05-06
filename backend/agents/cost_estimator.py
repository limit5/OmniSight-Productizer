"""MP.W2.1 -- provider-neutral task cost estimator.

This module provides cheap pre-dispatch estimates for subscription-provider
tasks.  It is intentionally side-effect free: no registry mutation, no database
access, and no provider health checks.  Callers pass the task payload plus the
candidate provider and receive deterministic token, latency, and USD estimates.

Module-global state audit (per project SOP)
-------------------------------------------
This module defines immutable constants only.  Pricing is read through
``backend.pricing.get_pricing()``, which owns its own cache and reload
contract.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, is_dataclass
from typing import Any

from backend.pricing import get_pricing


DEFAULT_MODEL_BY_PROVIDER: dict[str, str] = {
    "anthropic": "claude-sonnet-4-20250514",
    "anthropic-subscription": "claude-sonnet-4-20250514",
    "openai": "gpt-4o",
    "openai-subscription": "gpt-4o",
}

PROVIDER_SECONDS_PER_1K_TOKENS: dict[str, float] = {
    "anthropic": 2.4,
    "anthropic-subscription": 2.4,
    "openai": 1.8,
    "openai-subscription": 1.8,
}

PROVIDER_BASE_SECONDS: dict[str, float] = {
    "anthropic": 8.0,
    "anthropic-subscription": 8.0,
    "openai": 6.0,
    "openai-subscription": 6.0,
}

DEFAULT_SECONDS_PER_1K_TOKENS = 2.5
DEFAULT_BASE_SECONDS = 8.0
DEFAULT_OUTPUT_TOKEN_RATIO = 0.35
MIN_OUTPUT_TOKENS = 256
MAX_OUTPUT_TOKENS = 8_192


def predict_token_count(task_spec: Any) -> int:
    """Return a conservative input-token estimate for a task payload.

    The estimator mirrors the adapter boundary: ``TaskSpec.prompt`` carries
    most task bytes, with ``agent_class``, ``tier``, ``area``, and correlation
    metadata adding a small routing envelope.  For dicts or dataclasses, the
    payload is serialised with stable key order before estimating.
    """
    text = _task_text(task_spec)
    if not text:
        return 0

    by_chars = math.ceil(len(text) / 4.0)
    by_words = math.ceil(len(text.split()) * 1.35)
    envelope = 16 if len(text) > 0 else 0
    return max(by_chars, by_words) + envelope


def predict_wall_time(task: Any, provider: Any) -> float:
    """Return predicted wall-clock seconds for dispatching ``task``."""
    provider_id = _provider_id(provider)
    total_tokens = predict_token_count(task) + _predict_output_tokens(task)
    base = PROVIDER_BASE_SECONDS.get(provider_id, DEFAULT_BASE_SECONDS)
    per_1k = PROVIDER_SECONDS_PER_1K_TOKENS.get(
        provider_id, DEFAULT_SECONDS_PER_1K_TOKENS
    )
    return round(base + (total_tokens / 1_000.0) * per_1k, 3)


def predict_cost(task: Any, provider: Any) -> float:
    """Return predicted provider cost in USD for dispatching ``task``."""
    provider_id = _provider_id(provider)
    provider_key = _pricing_provider(provider_id)
    model = _model_id(task, provider_id)
    input_rate, output_rate = get_pricing(provider_key, model)
    input_tokens = predict_token_count(task)
    output_tokens = _predict_output_tokens(task)
    cost = (
        input_tokens * input_rate / 1_000_000.0
        + output_tokens * output_rate / 1_000_000.0
    )
    return round(cost, 6)


def _predict_output_tokens(task: Any) -> int:
    explicit = _first_int_attr(
        task,
        (
            "output_tokens_estimated",
            "estimated_output_tokens",
            "max_output_tokens",
            "max_tokens",
        ),
    )
    if explicit is not None:
        return max(explicit, 0)
    predicted = math.ceil(predict_token_count(task) * DEFAULT_OUTPUT_TOKEN_RATIO)
    return min(max(predicted, MIN_OUTPUT_TOKENS), MAX_OUTPUT_TOKENS)


def _task_text(task_spec: Any) -> str:
    if task_spec is None:
        return ""
    if isinstance(task_spec, str):
        return task_spec
    if isinstance(task_spec, bytes):
        return task_spec.decode("utf-8", errors="replace")
    if is_dataclass(task_spec) and not isinstance(task_spec, type):
        return _stable_json(asdict(task_spec))
    if isinstance(task_spec, dict):
        return _stable_json(task_spec)

    prompt = getattr(task_spec, "prompt", None)
    if isinstance(prompt, str):
        envelope: dict[str, Any] = {"prompt": prompt}
        for attr in ("agent_class", "tier", "area", "correlation_id"):
            value = getattr(task_spec, attr, None)
            if value not in (None, "", [], ()):
                envelope[attr] = value
        return _stable_json(envelope)

    return str(task_spec)


def _stable_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    except TypeError:
        return str(value)


def _provider_id(provider: Any) -> str:
    if provider is None:
        return ""
    if isinstance(provider, str):
        return provider.strip().lower()
    provider_id = getattr(provider, "provider_id", None)
    if callable(provider_id):
        return str(provider_id()).strip().lower()
    if isinstance(provider_id, str):
        return provider_id.strip().lower()
    return str(provider).strip().lower()


def _pricing_provider(provider_id: str) -> str | None:
    if provider_id.endswith("-subscription"):
        provider_id = provider_id.removesuffix("-subscription")
    return provider_id or None


def _model_id(task: Any, provider_id: str) -> str:
    for attr in ("model", "model_id", "provider_model"):
        value = getattr(task, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if isinstance(task, dict):
        for key in ("model", "model_id", "provider_model"):
            value = task.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return DEFAULT_MODEL_BY_PROVIDER.get(provider_id, "")


def _first_int_attr(task: Any, attrs: tuple[str, ...]) -> int | None:
    for attr in attrs:
        if isinstance(task, dict):
            value = task.get(attr)
        else:
            value = getattr(task, attr, None)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value)
    return None


__all__ = [
    "predict_cost",
    "predict_token_count",
    "predict_wall_time",
]
