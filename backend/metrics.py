"""Phase 52 — Prometheus metrics registry.

Exposes process-wide Counter / Histogram / Gauge instances that the
rest of the codebase imports + bumps. The companion `/metrics`
endpoint in `routers/system.py` (well, a new `metrics_router`) renders
them in Prometheus exposition format.

Naming follows the `omnisight_<domain>_<name>_<unit>` convention so
they sort cleanly in Grafana.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from prometheus_client import (
        Counter,
        Gauge,
        Histogram,
        CollectorRegistry,
        generate_latest,
        CONTENT_TYPE_LATEST,
    )
    _AVAILABLE = True
except ImportError:  # pragma: no cover
    _AVAILABLE = False
    Counter = Gauge = Histogram = CollectorRegistry = None  # type: ignore
    CONTENT_TYPE_LATEST = "text/plain; charset=utf-8"

    def generate_latest(*_a, **_kw) -> bytes:  # type: ignore
        return b"# prometheus_client not installed\n"


def is_available() -> bool:
    return _AVAILABLE


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Registry + metrics
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

if _AVAILABLE:
    REGISTRY = CollectorRegistry()

    # Decisions ─────────────────────────────────────────────────
    decision_total = Counter(
        "omnisight_decision_total",
        "Decisions registered by kind / severity / status",
        labelnames=("kind", "severity", "status"),
        registry=REGISTRY,
    )
    decision_resolve_seconds = Histogram(
        "omnisight_decision_resolve_seconds",
        "Wall-clock seconds from propose to resolve / auto-execute",
        labelnames=("kind", "severity", "resolver"),
        buckets=(0.1, 0.5, 1, 5, 10, 30, 60, 300, 600, 1800),
        registry=REGISTRY,
    )

    # Pipeline ──────────────────────────────────────────────────
    pipeline_step_seconds = Histogram(
        "omnisight_pipeline_step_seconds",
        "Pipeline step wall-clock duration",
        labelnames=("phase", "step", "outcome"),
        buckets=(1, 5, 30, 60, 300, 900, 1800, 3600, 7200),
        registry=REGISTRY,
    )

    # Provider ──────────────────────────────────────────────────
    provider_failure_total = Counter(
        "omnisight_provider_failure_total",
        "LLM provider failures by reason",
        labelnames=("provider", "reason"),
        registry=REGISTRY,
    )
    provider_latency_seconds = Histogram(
        "omnisight_provider_latency_seconds",
        "LLM provider request latency",
        labelnames=("provider", "model"),
        buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120),
        registry=REGISTRY,
    )

    # SSE ───────────────────────────────────────────────────────
    sse_subscribers = Gauge(
        "omnisight_sse_subscribers",
        "Number of currently connected SSE subscribers",
        registry=REGISTRY,
    )
    sse_dropped_total = Counter(
        "omnisight_sse_dropped_total",
        "Subscribers dropped due to backpressure (queue full)",
        registry=REGISTRY,
    )

    # Workflow (Phase 56) ───────────────────────────────────────
    workflow_step_total = Counter(
        "omnisight_workflow_step_total",
        "Durable workflow steps recorded by outcome",
        labelnames=("kind", "outcome"),
        registry=REGISTRY,
    )

    # Auth (Phase 54) ───────────────────────────────────────────
    auth_login_total = Counter(
        "omnisight_auth_login_total",
        "Login attempts by outcome",
        labelnames=("outcome",),
        registry=REGISTRY,
    )

    # Fix-B B6: non-fatal persistence / dispatch failures that used to
    # be `except Exception: pass`. Now logged + incremented so Grafana
    # can alert when a normally-silent write starts failing repeatedly.
    persist_failure_total = Counter(
        "omnisight_persist_failure_total",
        "Non-fatal persistence/dispatch failures that were swallowed",
        labelnames=("module",),
        registry=REGISTRY,
    )

    # Fix-A S6: orphaned CI subprocess tracker ─────────────────
    subprocess_orphan_total = Counter(
        "omnisight_subprocess_orphan_total",
        "CI subprocess kill() failed after timeout — likely zombie",
        labelnames=("target",),
        registry=REGISTRY,
    )

    # Phase 64-A S3: image digest allow-list rejections ─────────
    sandbox_image_rejected_total = Counter(
        "omnisight_sandbox_image_rejected_total",
        "Container launches refused because image digest not in trust list",
        labelnames=("image",),
        registry=REGISTRY,
    )

    # Phase 64-A S4: lifetime-cap killswitch fires ──────────────
    sandbox_lifetime_killed_total = Counter(
        "omnisight_sandbox_lifetime_killed_total",
        "Containers SIGKILLed for exceeding the wall-clock lifetime cap",
        labelnames=("tier",),
        registry=REGISTRY,
    )

    # Phase 64-A S5: every sandbox launch attempt ───────────────
    sandbox_launch_total = Counter(
        "omnisight_sandbox_launch_total",
        "Sandbox launch attempts by tier, runtime, and outcome",
        labelnames=("tier", "runtime", "result"),  # result: success | error
        registry=REGISTRY,
    )

    # Phase 64-D D3: per-exec output truncation events ──────────
    sandbox_output_truncated_total = Counter(
        "omnisight_sandbox_output_truncated_total",
        "exec_in_container outputs that exceeded sandbox_max_output_bytes",
        labelnames=("tier",),
        registry=REGISTRY,
    )

    # Phase 62: skills extraction (Knowledge Generation L1) ─────
    skill_extracted_total = Counter(
        "omnisight_skill_extracted_total",
        "Skill extraction events from completed workflow_runs",
        labelnames=("status",),  # written | skipped_threshold | skipped_unsafe
        registry=REGISTRY,
    )
    skill_promoted_total = Counter(
        "omnisight_skill_promoted_total",
        "Skill candidates moved from _pending into live skills/",
        registry=REGISTRY,
    )

    # Phase 63-A: Intelligence Immune System signal layer ───────
    intelligence_score = Gauge(
        "omnisight_intelligence_score",
        "Current per-agent score in {code_pass, compliance, consistency, entropy}",
        labelnames=("agent_id", "dim"),
        registry=REGISTRY,
    )
    intelligence_alert_total = Counter(
        "omnisight_intelligence_alert_total",
        "Alerts emitted by IIS signal layer (no mitigation triggered here)",
        labelnames=("agent_id", "dim", "level"),
        registry=REGISTRY,
    )

    # Phase 63-C: Prompt registry + canary ──────────────────────
    prompt_outcome_total = Counter(
        "omnisight_prompt_outcome_total",
        "Per-prompt-version outcomes recorded by IIS feedback",
        labelnames=("role", "outcome"),
        registry=REGISTRY,
    )
    prompt_rolled_back_total = Counter(
        "omnisight_prompt_rolled_back_total",
        "Canary prompts auto-rolled-back because they regressed vs active",
        labelnames=("path",),
        registry=REGISTRY,
    )

    # Phase 56-DAG-A: planner validation outcomes ───────────────
    dag_validation_total = Counter(
        "omnisight_dag_validation_total",
        "DAG plans submitted to the validator",
        labelnames=("result",),  # passed | failed
        registry=REGISTRY,
    )
    dag_validation_error_total = Counter(
        "omnisight_dag_validation_error_total",
        "Per-rule validator errors (cycle, tier_violation, mece, ...)",
        labelnames=("rule",),
        registry=REGISTRY,
    )

    # Phase 56-DAG-C: mutation loop outcomes ────────────────────
    dag_mutation_total = Counter(
        "omnisight_dag_mutation_total",
        "DAG mutation loop outcomes: recovered / exhausted",
        labelnames=("result",),
        registry=REGISTRY,
    )

    # Phase 67-C: speculative container pre-warm ────────────────
    prewarm_started_total = Counter(
        "omnisight_prewarm_started_total",
        "Containers launched speculatively for in-degree-0 DAG tasks",
        registry=REGISTRY,
    )
    prewarm_consumed_total = Counter(
        "omnisight_prewarm_consumed_total",
        "Pre-warmed container outcomes: hit / miss / cancelled / start_error",
        labelnames=("result",),
        registry=REGISTRY,
    )

    # Phase 65: training set export funnel ──────────────────────
    training_set_rows = Counter(
        "omnisight_training_set_rows_total",
        "Training-set rows: result=written or skip:<reason>",
        labelnames=("result",),
        registry=REGISTRY,
    )

    # Phase 65 S2: hold-out evaluation gauge ────────────────────
    finetune_eval_score = Gauge(
        "omnisight_finetune_eval_score",
        "Latest hold-out weighted score (0..1) per model evaluated",
        labelnames=("model",),
        registry=REGISTRY,
    )

    # Phase 67-A: prompt cache hit/miss in input tokens ─────────
    prompt_cache_hit_total = Counter(
        "omnisight_prompt_cache_hit_total",
        "Input tokens served from the provider's prompt cache",
        labelnames=("provider",),
        registry=REGISTRY,
    )
    prompt_cache_miss_total = Counter(
        "omnisight_prompt_cache_miss_total",
        "Input tokens that were NOT served from cache (newly billed)",
        labelnames=("provider",),
        registry=REGISTRY,
    )

    # Phase 63-D D3: daily IQ benchmark score per model ─────────
    intelligence_iq_score = Gauge(
        "omnisight_intelligence_iq_score",
        "Latest nightly IQ benchmark weighted score (0..1) per model",
        labelnames=("model",),
        registry=REGISTRY,
    )
    intelligence_iq_regression_total = Counter(
        "omnisight_intelligence_iq_regression_total",
        "IQ regression events (sustained score < baseline - threshold)",
        labelnames=("model",),
        registry=REGISTRY,
    )

    # Phase 67-D: RAG pre-fetch outcomes on step error ──────────
    rag_prefetch_total = Counter(
        "omnisight_rag_prefetch_total",
        "RAG pre-fetch outcomes: injected / no_hit / below_confidence / search_error",
        labelnames=("result",),
        registry=REGISTRY,
    )

    # Phase 63-E: episodic memory quality decay ────────────────
    memory_decay_total = Counter(
        "omnisight_memory_decay_total",
        "Memory decay events by action: decayed / skipped_recent / restored",
        labelnames=("action",),
        registry=REGISTRY,
    )

    # Process up-time
    process_start_time = Gauge(
        "omnisight_process_start_time_seconds",
        "Unix timestamp when this process started",
        registry=REGISTRY,
    )
    process_start_time.set(time.time())

else:
    # No-op stubs so callers don't have to guard every increment.
    class _NoOp:
        def labels(self, *_a, **_kw): return self
        def inc(self, *_a, **_kw): pass
        def dec(self, *_a, **_kw): pass
        def set(self, *_a, **_kw): pass
        def observe(self, *_a, **_kw): pass

    decision_total = decision_resolve_seconds = _NoOp()  # type: ignore
    pipeline_step_seconds = _NoOp()  # type: ignore
    provider_failure_total = provider_latency_seconds = _NoOp()  # type: ignore
    sse_subscribers = sse_dropped_total = _NoOp()  # type: ignore
    workflow_step_total = _NoOp()  # type: ignore
    auth_login_total = _NoOp()  # type: ignore
    persist_failure_total = _NoOp()  # type: ignore
    subprocess_orphan_total = _NoOp()  # type: ignore
    sandbox_image_rejected_total = _NoOp()  # type: ignore
    sandbox_lifetime_killed_total = _NoOp()  # type: ignore
    sandbox_launch_total = _NoOp()  # type: ignore
    sandbox_output_truncated_total = _NoOp()  # type: ignore
    skill_extracted_total = _NoOp()  # type: ignore
    skill_promoted_total = _NoOp()  # type: ignore
    intelligence_score = _NoOp()  # type: ignore
    intelligence_alert_total = _NoOp()  # type: ignore
    prompt_outcome_total = _NoOp()  # type: ignore
    prompt_rolled_back_total = _NoOp()  # type: ignore
    dag_validation_total = _NoOp()  # type: ignore
    dag_validation_error_total = _NoOp()  # type: ignore
    dag_mutation_total = _NoOp()  # type: ignore
    prewarm_started_total = _NoOp()  # type: ignore
    prewarm_consumed_total = _NoOp()  # type: ignore
    training_set_rows = _NoOp()  # type: ignore
    finetune_eval_score = _NoOp()  # type: ignore
    prompt_cache_hit_total = _NoOp()  # type: ignore
    prompt_cache_miss_total = _NoOp()  # type: ignore
    intelligence_iq_score = _NoOp()  # type: ignore
    intelligence_iq_regression_total = _NoOp()  # type: ignore
    rag_prefetch_total = _NoOp()  # type: ignore
    memory_decay_total = _NoOp()  # type: ignore
    process_start_time = _NoOp()  # type: ignore
    REGISTRY = None  # type: ignore


def render_exposition() -> tuple[bytes, str]:
    """Return (body, content_type) for the /metrics endpoint."""
    if not _AVAILABLE:
        return (b"# prometheus_client not installed\n", "text/plain; charset=utf-8")
    return (generate_latest(REGISTRY), CONTENT_TYPE_LATEST)


def reset_for_tests() -> None:
    """Re-create REGISTRY so tests start from clean counters."""
    if not _AVAILABLE:
        return
    global REGISTRY, decision_total, decision_resolve_seconds
    global pipeline_step_seconds, provider_failure_total, provider_latency_seconds
    global sse_subscribers, sse_dropped_total, workflow_step_total
    global auth_login_total, subprocess_orphan_total, persist_failure_total
    global sandbox_image_rejected_total, sandbox_lifetime_killed_total
    global sandbox_launch_total, sandbox_output_truncated_total
    global skill_extracted_total, skill_promoted_total
    global intelligence_score, intelligence_alert_total
    global prompt_outcome_total, prompt_rolled_back_total
    global dag_validation_total, dag_validation_error_total, dag_mutation_total
    global prewarm_started_total, prewarm_consumed_total, training_set_rows
    global finetune_eval_score
    global prompt_cache_hit_total, prompt_cache_miss_total
    global intelligence_iq_score, intelligence_iq_regression_total
    global rag_prefetch_total
    global memory_decay_total
    global process_start_time
    REGISTRY = CollectorRegistry()
    decision_total = Counter(
        "omnisight_decision_total", "Decisions registered",
        labelnames=("kind", "severity", "status"), registry=REGISTRY,
    )
    decision_resolve_seconds = Histogram(
        "omnisight_decision_resolve_seconds", "Resolve duration",
        labelnames=("kind", "severity", "resolver"),
        buckets=(0.1, 0.5, 1, 5, 10, 30, 60, 300, 600, 1800),
        registry=REGISTRY,
    )
    pipeline_step_seconds = Histogram(
        "omnisight_pipeline_step_seconds", "Pipeline step duration",
        labelnames=("phase", "step", "outcome"),
        buckets=(1, 5, 30, 60, 300, 900, 1800, 3600, 7200),
        registry=REGISTRY,
    )
    provider_failure_total = Counter(
        "omnisight_provider_failure_total", "Provider failures",
        labelnames=("provider", "reason"), registry=REGISTRY,
    )
    provider_latency_seconds = Histogram(
        "omnisight_provider_latency_seconds", "Provider latency",
        labelnames=("provider", "model"),
        buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120),
        registry=REGISTRY,
    )
    sse_subscribers = Gauge(
        "omnisight_sse_subscribers", "SSE subscribers", registry=REGISTRY,
    )
    sse_dropped_total = Counter(
        "omnisight_sse_dropped_total", "SSE drops", registry=REGISTRY,
    )
    workflow_step_total = Counter(
        "omnisight_workflow_step_total", "Workflow steps",
        labelnames=("kind", "outcome"), registry=REGISTRY,
    )
    auth_login_total = Counter(
        "omnisight_auth_login_total", "Login attempts",
        labelnames=("outcome",), registry=REGISTRY,
    )
    subprocess_orphan_total = Counter(
        "omnisight_subprocess_orphan_total", "CI subprocess kill failed",
        labelnames=("target",), registry=REGISTRY,
    )
    persist_failure_total = Counter(
        "omnisight_persist_failure_total", "Swallowed persistence failures",
        labelnames=("module",), registry=REGISTRY,
    )
    sandbox_image_rejected_total = Counter(
        "omnisight_sandbox_image_rejected_total",
        "Sandbox launches refused due to untrusted image digest",
        labelnames=("image",), registry=REGISTRY,
    )
    sandbox_lifetime_killed_total = Counter(
        "omnisight_sandbox_lifetime_killed_total",
        "Containers SIGKILLed by the lifetime-cap watchdog",
        labelnames=("tier",), registry=REGISTRY,
    )
    sandbox_launch_total = Counter(
        "omnisight_sandbox_launch_total",
        "Sandbox launch attempts by tier/runtime/result",
        labelnames=("tier", "runtime", "result"), registry=REGISTRY,
    )
    sandbox_output_truncated_total = Counter(
        "omnisight_sandbox_output_truncated_total",
        "exec_in_container outputs that exceeded sandbox_max_output_bytes",
        labelnames=("tier",), registry=REGISTRY,
    )
    skill_extracted_total = Counter(
        "omnisight_skill_extracted_total", "Skill extraction events",
        labelnames=("status",), registry=REGISTRY,
    )
    skill_promoted_total = Counter(
        "omnisight_skill_promoted_total",
        "Skill candidates promoted into live skills/",
        registry=REGISTRY,
    )
    intelligence_score = Gauge(
        "omnisight_intelligence_score",
        "Per-agent IIS score across 4 dims",
        labelnames=("agent_id", "dim"), registry=REGISTRY,
    )
    intelligence_alert_total = Counter(
        "omnisight_intelligence_alert_total",
        "IIS alerts emitted (signal-only)",
        labelnames=("agent_id", "dim", "level"), registry=REGISTRY,
    )
    prompt_outcome_total = Counter(
        "omnisight_prompt_outcome_total", "Prompt-version outcomes",
        labelnames=("role", "outcome"), registry=REGISTRY,
    )
    prompt_rolled_back_total = Counter(
        "omnisight_prompt_rolled_back_total", "Canary auto-rollbacks",
        labelnames=("path",), registry=REGISTRY,
    )
    dag_validation_total = Counter(
        "omnisight_dag_validation_total", "DAG plans validated",
        labelnames=("result",), registry=REGISTRY,
    )
    dag_validation_error_total = Counter(
        "omnisight_dag_validation_error_total", "DAG validation errors",
        labelnames=("rule",), registry=REGISTRY,
    )
    dag_mutation_total = Counter(
        "omnisight_dag_mutation_total", "DAG mutation outcomes",
        labelnames=("result",), registry=REGISTRY,
    )
    prewarm_started_total = Counter(
        "omnisight_prewarm_started_total", "Pre-warmed containers started",
        registry=REGISTRY,
    )
    prewarm_consumed_total = Counter(
        "omnisight_prewarm_consumed_total", "Pre-warm outcomes",
        labelnames=("result",), registry=REGISTRY,
    )
    training_set_rows = Counter(
        "omnisight_training_set_rows_total", "Training set rows",
        labelnames=("result",), registry=REGISTRY,
    )
    finetune_eval_score = Gauge(
        "omnisight_finetune_eval_score", "Hold-out weighted score per model",
        labelnames=("model",), registry=REGISTRY,
    )
    prompt_cache_hit_total = Counter(
        "omnisight_prompt_cache_hit_total", "Cached input tokens",
        labelnames=("provider",), registry=REGISTRY,
    )
    prompt_cache_miss_total = Counter(
        "omnisight_prompt_cache_miss_total", "Uncached input tokens",
        labelnames=("provider",), registry=REGISTRY,
    )
    intelligence_iq_score = Gauge(
        "omnisight_intelligence_iq_score", "Latest IQ weighted score",
        labelnames=("model",), registry=REGISTRY,
    )
    intelligence_iq_regression_total = Counter(
        "omnisight_intelligence_iq_regression_total",
        "IQ regression events",
        labelnames=("model",), registry=REGISTRY,
    )
    rag_prefetch_total = Counter(
        "omnisight_rag_prefetch_total",
        "RAG pre-fetch outcomes",
        labelnames=("result",), registry=REGISTRY,
    )
    memory_decay_total = Counter(
        "omnisight_memory_decay_total",
        "Memory decay events by action",
        labelnames=("action",), registry=REGISTRY,
    )
    process_start_time = Gauge(
        "omnisight_process_start_time_seconds", "Process start time",
        registry=REGISTRY,
    )
    process_start_time.set(time.time())
