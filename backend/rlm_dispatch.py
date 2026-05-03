"""BP.A.5b — RLM-pattern decomposition decision branch.

Decision rule (ADR R10 + Appendix C, 2026-04-25):

    IF context_tokens > CONTEXT_TOKENS_THRESHOLD        (100_000)
       AND task_type ∈ RLM_TASK_TYPES                   {analysis, audit, forensics}
       AND task_type ∉ SIMPLE_TASK_TYPES                 {crud, retrieval, simple_lookup}
    THEN mode = "partition_map_summarize"   (depth=1 hard cap)
    ELSE mode = "standard"

Fail-open design: any exception in the decision or partition path returns a
"standard" plan so callers always get a usable result rather than a raised
exception (heuristic failure regresses to agent dispatch, never blocks work).

Borrows the RLM «partition → map → summarize» pattern from
arXiv:2512.24601 (Recursive Language Models, MIT OASYS lab) but does NOT
install the ``rlms`` PyPI package — depth > 1 causes 96× latency blow-up
(R10 reproduction caveat, Appendix C.3).  Hard depth cap = DEPTH_CAP = 1.

The "partition" step splits the payload into equal-sized chunks (by char
count as a token proxy).  The "map" step (one call per chunk) and
"summarize" step (one call over all chunk results) are the responsibility
of the BP.A.6 dispatcher that invokes this module — not implemented here.

Module-global state: only immutable frozensets and int / float constants.
Every worker derives the same values from the same source — cross-worker
safe under ``uvicorn --workers N`` by construction (SOP Step 1 answer #1:
不共享，因為每 worker 從同樣來源推導出同樣的值).

Cross-references:
  BP.A.5  ``backend/cognitive_load.py``      — structural load gate (upstream)
  BP.A.6  ``backend/template_validator.py``  — will call plan_dispatch before
                                               forwarding to Coder or LLM
  BP.A.7  ``backend/tests/test_templates.py`` — unified suite (folds this in)
  ADR R10 + Appendix C  docs/design/blueprint-v2-implementation-plan.md
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ── Decision thresholds ───────────────────────────────────────────────────────

# Minimum context-token count that may trigger RLM mode (strictly greater-than).
CONTEXT_TOKENS_THRESHOLD: int = 100_000

# Task types that are eligible for RLM partition-map-summarize mode.
RLM_TASK_TYPES: frozenset[str] = frozenset({"analysis", "audit", "forensics"})

# Task types that are always routed to standard dispatch even if context is large.
SIMPLE_TASK_TYPES: frozenset[str] = frozenset({"crud", "retrieval", "simple_lookup"})

# ── Partition constants ───────────────────────────────────────────────────────

# Target token budget per partition chunk.  Chunks are sized by character
# count using a 1-char ≈ 0.25-token proxy (inverse of the 4-char/token rule).
PARTITION_SIZE_TOKENS: int = 50_000

# Hard upper bound on the number of partition chunks regardless of context size.
MAX_PARTITIONS: int = 8

# RLM recursion depth hard cap — partitions themselves are NOT further
# partitioned (depth=1 means we go exactly one level deep and stop).
DEPTH_CAP: int = 1

# ── Types ─────────────────────────────────────────────────────────────────────

DispatchMode = Literal["partition_map_summarize", "standard"]


class RlmDispatchPlan(BaseModel):
    """Immutable result of the RLM-pattern dispatch decision.

    Produced by ``plan_dispatch``; consumed by BP.A.6 template validator
    to route long-context analysis/audit/forensics tasks through the
    partition-map-summarize pipeline.
    """

    model_config = ConfigDict(frozen=True)

    mode: DispatchMode = Field(
        ...,
        description=(
            "'partition_map_summarize' when RLM conditions are met; "
            "'standard' otherwise (including all fail-open fallbacks)."
        ),
    )
    partitions: tuple[str, ...] = Field(
        ...,
        description=(
            "Payload chunks for the map phase (non-empty only in "
            "partition_map_summarize mode). Empty tuple in standard mode."
        ),
    )
    depth_cap: int = Field(
        ...,
        ge=0,
        description=(
            "Hard recursion depth cap: 1 in partition_map_summarize mode "
            "(partitions are never further subdivided); 0 in standard mode."
        ),
    )
    context_tokens: int = Field(
        ...,
        ge=0,
        description="Caller-supplied context token count (reflected for traceability).",
    )
    task_type: str = Field(
        ...,
        description="Caller-supplied task type string (reflected for traceability).",
    )


# ── Core decision logic ───────────────────────────────────────────────────────


def decide_dispatch_mode(context_tokens: int, task_type: str) -> DispatchMode:
    """Return 'partition_map_summarize' or 'standard' for the given context.

    Fail-open: any exception returns 'standard' rather than propagating.
    """
    try:
        if (
            context_tokens > CONTEXT_TOKENS_THRESHOLD
            and task_type in RLM_TASK_TYPES
            and task_type not in SIMPLE_TASK_TYPES
        ):
            return "partition_map_summarize"
        return "standard"
    except Exception:  # noqa: BLE001  — fail-open per ADR R10
        return "standard"


def partition_text(text: str, context_tokens: int) -> tuple[str, ...]:
    """Split *text* into equal-sized chunks for the RLM map phase.

    Chunk count = min(max(2, ceil(context_tokens / PARTITION_SIZE_TOKENS)),
                      MAX_PARTITIONS).

    Lossless split: ``"".join(partition_text(t, n)) == t`` for all inputs.
    Actual chunk count may be less than the target when *text* is shorter
    than one character per requested partition.
    """
    num_partitions: int = min(
        max(2, math.ceil(context_tokens / PARTITION_SIZE_TOKENS)),
        MAX_PARTITIONS,
    )
    if not text:
        return ("",) * num_partitions
    chars: int = len(text)
    chunk_size: int = max(1, math.ceil(chars / num_partitions))
    return tuple(text[i : i + chunk_size] for i in range(0, chars, chunk_size))


# ── Main entry point ──────────────────────────────────────────────────────────


def plan_dispatch(
    context_tokens: int,
    task_type: str,
    payload: str = "",
) -> RlmDispatchPlan:
    """Decide dispatch mode and build the partition plan.

    Fail-open: any exception in the heuristic or partition path yields a
    "standard" plan with empty partitions and depth_cap=0, so the caller
    always receives a usable ``RlmDispatchPlan`` rather than a raised
    exception.

    Args:
        context_tokens: Estimated token count of the agent context window.
        task_type:      Task classification string (e.g. "analysis", "crud").
        payload:        The content to partition in RLM mode; ignored in
                        standard mode.  Defaults to "" (decision-only call).

    Returns:
        Frozen ``RlmDispatchPlan`` with mode, partitions, depth_cap,
        context_tokens, and task_type populated.
    """
    try:
        mode = decide_dispatch_mode(context_tokens, task_type)
        if mode == "partition_map_summarize":
            partitions = partition_text(payload, context_tokens)
            depth_cap = DEPTH_CAP
        else:
            partitions = ()
            depth_cap = 0
    except Exception:  # noqa: BLE001  — fail-open per ADR R10
        mode = "standard"
        partitions = ()
        depth_cap = 0
    return RlmDispatchPlan(
        mode=mode,
        partitions=partitions,
        depth_cap=depth_cap,
        context_tokens=context_tokens,
        task_type=task_type,
    )


__all__ = [
    "CONTEXT_TOKENS_THRESHOLD",
    "DEPTH_CAP",
    "MAX_PARTITIONS",
    "PARTITION_SIZE_TOKENS",
    "RLM_TASK_TYPES",
    "SIMPLE_TASK_TYPES",
    "DispatchMode",
    "RlmDispatchPlan",
    "decide_dispatch_mode",
    "partition_text",
    "plan_dispatch",
]
