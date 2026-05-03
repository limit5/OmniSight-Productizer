"""BP.A.5 — Cognitive Load Scanner (fan-in / fan-out / mock-limit quantizer).

Measures three structural complexity metrics from a ``TaskTemplate`` and
produces a ``CognitiveLoadReport``. The report's ``estimated_tokens`` is
compared against ``TaskTemplate.max_cognitive_load_tokens``; when
``exceeds_ceiling`` is ``True`` the caller (BP.A.6 middleware) should
bounce the task back to PM Guild for re-decomposition rather than
dispatching it to a Coder.

Three metrics:

  fan_in    — ``len(task.allowed_dependencies)``. Each upstream dependency
              the Coder is permitted to import adds structural coupling and
              requires additional context tokens to reason about.

  fan_out   — Estimated downstream dispatches if this task were decomposed
              further. Derived from the size class baseline (S → 1, M → 3,
              XL → 6) plus one additional dispatch for every two fan-in
              dependencies: ``FAN_OUT_BASE[size] + fan_in // 2``.

  mock_limit — Maximum allowed mocked/patched dependencies in the task's
               test suite. Capped at ``min(ceil(fan_in * 0.5), 5)`` so at
               most half the dependencies may be mocked, and never more than
               5 absolute, keeping tests close to production fidelity.

Estimated-token formula::

    estimated_tokens = BASE_TOKENS[size]
                     + FAN_IN_WEIGHT * fan_in
                     + FAN_OUT_WEIGHT * fan_out

Constants are module-level immutable values. Cross-worker safety (SOP
Step 1 強制問題): no module-level mutable state, no singletons, no
in-memory cache. Every worker derives the same values from the same source —
SOP Step 1 acceptable answer #1 ("不共享，因為每 worker 從同樣來源推導出
同樣的值"). Safe under ``uvicorn --workers N`` by construction.

Cross-references:
  - BP.A.2 ``backend/templates/task.py``     — TaskTemplate (input)
  - BP.A.6 ``backend/template_validator.py`` — consumes this report to emit
    cognitive-penalty prompts when ``exceeds_ceiling`` is True.
  - BP.A.7 ``backend/tests/test_templates.py`` — unified test suite
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.templates.task import TaskTemplate

# ── Immutable tuning constants ────────────────────────────────────────────────

# Base token budget per size class (rough structural token floor before
# accounting for explicit dependency coupling).
_BASE_TOKENS: dict[str, int] = {"S": 500, "M": 2_000, "XL": 8_000}

# Tokens added per upstream dependency (each dep needs reading + integrating).
_FAN_IN_WEIGHT: int = 150

# Baseline estimated downstream dispatches by size class.
_FAN_OUT_BASE: dict[str, int] = {"S": 1, "M": 3, "XL": 6}

# Tokens added per estimated downstream dispatch.
_FAN_OUT_WEIGHT: int = 250

# fan_out increments by 1 for every 2 allowed_dependencies.
_FAN_OUT_DEP_STEP: int = 2

# Fraction of fan_in that may be replaced by mocks in tests.
_MOCK_FRACTION: float = 0.5

# Absolute upper bound on mocked dependencies regardless of fan_in.
_MOCK_MAX: int = 5


# ── Report model ─────────────────────────────────────────────────────────────


class CognitiveLoadReport(BaseModel):
    """Immutable snapshot of a task's structural complexity metrics.

    Produced by ``scan_cognitive_load``; consumed by BP.A.6 middleware.
    All fields are read-only after construction.
    """

    model_config = ConfigDict(frozen=True)

    fan_in: int = Field(
        ...,
        ge=0,
        description=(
            "Number of upstream dependencies (len(allowed_dependencies)). "
            "Each dependency the Coder may read adds coupling and context "
            "tokens to the cognitive budget."
        ),
    )
    fan_out: int = Field(
        ...,
        ge=1,
        description=(
            "Estimated downstream dispatches if this task were split "
            "further. Baseline from size class plus one per two fan-in "
            "dependencies."
        ),
    )
    mock_limit: int = Field(
        ...,
        ge=0,
        description=(
            "Maximum dependencies that may be mocked/patched in the "
            "task's test suite: min(ceil(fan_in * 0.5), 5). Keeps tests "
            "close to production fidelity."
        ),
    )
    estimated_tokens: int = Field(
        ...,
        gt=0,
        description=(
            "Cognitive load estimate in tokens: BASE_TOKENS[size] + "
            "FAN_IN_WEIGHT * fan_in + FAN_OUT_WEIGHT * fan_out."
        ),
    )
    ceiling: int = Field(
        ...,
        gt=0,
        description="Task's max_cognitive_load_tokens ceiling (reflected for context).",
    )
    exceeds_ceiling: bool = Field(
        ...,
        description=(
            "True when estimated_tokens > ceiling. Caller should bounce "
            "the task back to PM Guild for re-decomposition."
        ),
    )
    size: Literal["S", "M", "XL"] = Field(
        ...,
        description="Size class reflected from the input TaskTemplate.",
    )


# ── Public API ────────────────────────────────────────────────────────────────


def scan_cognitive_load(task: TaskTemplate) -> CognitiveLoadReport:
    """Quantify the structural cognitive load of *task*.

    Returns a frozen ``CognitiveLoadReport``. If ``report.exceeds_ceiling``
    is ``True``, the task should be rejected and returned to PM Guild for
    finer decomposition before dispatching to a Coder.

    Args:
        task: A validated ``TaskTemplate`` instance (BP.A.2).

    Returns:
        ``CognitiveLoadReport`` with fan_in, fan_out, mock_limit,
        estimated_tokens, ceiling, exceeds_ceiling, and size.
    """
    fan_in: int = len(task.allowed_dependencies)
    fan_out: int = _FAN_OUT_BASE[task.size] + (fan_in // _FAN_OUT_DEP_STEP)
    mock_limit: int = min(math.ceil(fan_in * _MOCK_FRACTION), _MOCK_MAX)
    estimated_tokens: int = (
        _BASE_TOKENS[task.size]
        + _FAN_IN_WEIGHT * fan_in
        + _FAN_OUT_WEIGHT * fan_out
    )
    return CognitiveLoadReport(
        fan_in=fan_in,
        fan_out=fan_out,
        mock_limit=mock_limit,
        estimated_tokens=estimated_tokens,
        ceiling=task.max_cognitive_load_tokens,
        exceeds_ceiling=estimated_tokens > task.max_cognitive_load_tokens,
        size=task.size,
    )


__all__ = [
    "CognitiveLoadReport",
    "scan_cognitive_load",
    "_BASE_TOKENS",
    "_FAN_IN_WEIGHT",
    "_FAN_OUT_BASE",
    "_FAN_OUT_WEIGHT",
    "_FAN_OUT_DEP_STEP",
    "_MOCK_FRACTION",
    "_MOCK_MAX",
]
