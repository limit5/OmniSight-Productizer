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
import logging
import math
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any

from backend.pricing import get_pricing

logger = logging.getLogger(__name__)


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
DRIFT_WARN_THRESHOLD = 0.50
CALIBRATION_ALPHA = 0.30


@dataclass(frozen=True)
class TenantCalibration:
    """Per-tenant estimator multipliers derived from observed outcomes."""

    tenant_id: str
    token_multiplier: float = 1.0
    wall_time_multiplier: float = 1.0
    cost_multiplier: float = 1.0
    sample_count: int = 0
    last_token_drift: float = 0.0
    last_wall_time_drift: float = 0.0
    last_cost_drift: float = 0.0


@dataclass(frozen=True)
class CostPrediction:
    """Snapshot used to compare a pre-dispatch prediction with actual usage."""

    tenant_id: str
    provider_id: str
    input_tokens: int
    output_tokens: int
    wall_time_seconds: float
    cost_usd: float


@dataclass(frozen=True)
class CostActual:
    """Observed post-dispatch cost signals used for tenant calibration."""

    input_tokens: int
    output_tokens: int
    wall_time_seconds: float
    cost_usd: float


def predict_token_count(
    task_spec: Any,
    tenant_calibration: TenantCalibration | None = None,
) -> int:
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
    return _apply_int_multiplier(
        max(by_chars, by_words) + envelope,
        tenant_calibration.token_multiplier if tenant_calibration else 1.0,
    )


def predict_wall_time(
    task: Any,
    provider: Any,
    tenant_calibration: TenantCalibration | None = None,
) -> float:
    """Return predicted wall-clock seconds for dispatching ``task``."""
    provider_id = _provider_id(provider)
    total_tokens = (
        predict_token_count(task, tenant_calibration)
        + _predict_output_tokens(task, tenant_calibration)
    )
    base = PROVIDER_BASE_SECONDS.get(provider_id, DEFAULT_BASE_SECONDS)
    per_1k = PROVIDER_SECONDS_PER_1K_TOKENS.get(
        provider_id, DEFAULT_SECONDS_PER_1K_TOKENS
    )
    predicted = base + (total_tokens / 1_000.0) * per_1k
    if tenant_calibration:
        predicted *= tenant_calibration.wall_time_multiplier
    return round(predicted, 3)


def predict_cost(
    task: Any,
    provider: Any,
    tenant_calibration: TenantCalibration | None = None,
) -> float:
    """Return predicted provider cost in USD for dispatching ``task``."""
    provider_id = _provider_id(provider)
    provider_key = _pricing_provider(provider_id)
    model = _model_id(task, provider_id)
    input_rate, output_rate = get_pricing(provider_key, model)
    input_tokens = predict_token_count(task, tenant_calibration)
    output_tokens = _predict_output_tokens(task, tenant_calibration)
    cost = (
        input_tokens * input_rate / 1_000_000.0
        + output_tokens * output_rate / 1_000_000.0
    )
    if tenant_calibration:
        cost *= tenant_calibration.cost_multiplier
    return round(cost, 6)


def predict(
    task: Any,
    provider: Any,
    *,
    tenant_id: str = "t-default",
    tenant_calibration: TenantCalibration | None = None,
) -> CostPrediction:
    """Return the comparable prediction snapshot for one task/provider."""
    provider_id = _provider_id(provider)
    return CostPrediction(
        tenant_id=tenant_id,
        provider_id=provider_id,
        input_tokens=predict_token_count(task, tenant_calibration),
        output_tokens=_predict_output_tokens(task, tenant_calibration),
        wall_time_seconds=predict_wall_time(task, provider, tenant_calibration),
        cost_usd=predict_cost(task, provider, tenant_calibration),
    )


def update_tenant_calibration(
    prediction: CostPrediction,
    actual: CostActual,
    current: TenantCalibration | None = None,
    *,
    drift_warn_threshold: float = DRIFT_WARN_THRESHOLD,
) -> TenantCalibration:
    """Return updated per-tenant multipliers and warn on large drift."""
    current = current or TenantCalibration(tenant_id=prediction.tenant_id)
    token_drift = _drift_ratio(
        prediction.input_tokens + prediction.output_tokens,
        actual.input_tokens + actual.output_tokens,
    )
    wall_time_drift = _drift_ratio(
        prediction.wall_time_seconds,
        actual.wall_time_seconds,
    )
    cost_drift = _drift_ratio(prediction.cost_usd, actual.cost_usd)
    _log_large_drift(
        prediction.tenant_id,
        prediction.provider_id,
        token_drift=token_drift,
        wall_time_drift=wall_time_drift,
        cost_drift=cost_drift,
        threshold=drift_warn_threshold,
    )
    return TenantCalibration(
        tenant_id=current.tenant_id,
        token_multiplier=_blend_multiplier(
            current.token_multiplier, token_drift, current.sample_count
        ),
        wall_time_multiplier=_blend_multiplier(
            current.wall_time_multiplier, wall_time_drift, current.sample_count
        ),
        cost_multiplier=_blend_multiplier(
            current.cost_multiplier, cost_drift, current.sample_count
        ),
        sample_count=current.sample_count + 1,
        last_token_drift=token_drift,
        last_wall_time_drift=wall_time_drift,
        last_cost_drift=cost_drift,
    )


def _predict_output_tokens(
    task: Any,
    tenant_calibration: TenantCalibration | None = None,
) -> int:
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
        return _apply_int_multiplier(
            max(explicit, 0),
            tenant_calibration.token_multiplier if tenant_calibration else 1.0,
        )
    predicted = math.ceil(predict_token_count(task) * DEFAULT_OUTPUT_TOKEN_RATIO)
    clamped = min(max(predicted, MIN_OUTPUT_TOKENS), MAX_OUTPUT_TOKENS)
    return _apply_int_multiplier(
        clamped,
        tenant_calibration.token_multiplier if tenant_calibration else 1.0,
    )


def _drift_ratio(predicted: float, actual: float) -> float:
    if predicted == 0 and actual == 0:
        return 0.0
    denominator = max(abs(predicted), 1e-9)
    return (actual - predicted) / denominator


def _blend_multiplier(current: float, drift: float, sample_count: int) -> float:
    target = max(current * (1.0 + drift), 0.0)
    if sample_count <= 0:
        return target
    return current * (1.0 - CALIBRATION_ALPHA) + target * CALIBRATION_ALPHA


def _log_large_drift(
    tenant_id: str,
    provider_id: str,
    *,
    token_drift: float,
    wall_time_drift: float,
    cost_drift: float,
    threshold: float,
) -> None:
    drifts = {
        "tokens": token_drift,
        "wall_time": wall_time_drift,
        "cost": cost_drift,
    }
    largest_axis, largest_drift = max(
        drifts.items(), key=lambda item: abs(item[1])
    )
    if abs(largest_drift) <= threshold:
        return
    logger.warning(
        "cost estimator drift exceeded threshold tenant=%s provider=%s "
        "axis=%s drift_pct=%.3f threshold=%.3f",
        tenant_id,
        provider_id,
        largest_axis,
        largest_drift,
        threshold,
    )


def _apply_int_multiplier(value: int, multiplier: float) -> int:
    return max(0, math.ceil(value * max(multiplier, 0.0)))


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
    "CostActual",
    "CostPrediction",
    "TenantCalibration",
    "predict",
    "predict_cost",
    "predict_token_count",
    "predict_wall_time",
    "update_tenant_calibration",
]
