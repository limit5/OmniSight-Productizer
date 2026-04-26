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

    # M1: per-tenant OOM kills inside the sandbox cgroup. Fires when
    # docker reports `State.OOMKilled=true` on container exit. Used by
    # ops to alert on tenants chronically blowing past their memory cap.
    sandbox_oom_total = Counter(
        "omnisight_sandbox_oom_total",
        "Sandbox containers killed by the cgroup OOM-killer",
        labelnames=("tenant_id", "tier"),
        registry=REGISTRY,
    )

    # M4: per-tenant cgroup-derived resource gauges. Scraped every
    # SAMPLE_INTERVAL_S by host_metrics.run_sampling_loop; source of
    # truth for the admin UI per-tenant bar chart + per-tenant AIMD.
    tenant_cpu_percent = Gauge(
        "omnisight_tenant_cpu_percent",
        "Per-tenant CPU usage summed across all running sandbox containers",
        labelnames=("tenant_id",),
        registry=REGISTRY,
    )
    tenant_mem_used_gb = Gauge(
        "omnisight_tenant_mem_used_gb",
        "Per-tenant memory usage (GiB) — sum of cgroup memory.current",
        labelnames=("tenant_id",),
        registry=REGISTRY,
    )
    tenant_disk_used_gb = Gauge(
        "omnisight_tenant_disk_used_gb",
        "Per-tenant on-disk usage (GiB) from tenant_quota.measure_tenant_usage",
        labelnames=("tenant_id",),
        registry=REGISTRY,
    )
    tenant_sandbox_count = Gauge(
        "omnisight_tenant_sandbox_count",
        "Number of currently running sandbox containers per tenant",
        labelnames=("tenant_id",),
        registry=REGISTRY,
    )
    tenant_cpu_seconds_total = Counter(
        "omnisight_tenant_cpu_seconds_total",
        "Per-tenant cumulative CPU-seconds (billing basis)",
        labelnames=("tenant_id",),
        registry=REGISTRY,
    )
    tenant_mem_gb_seconds_total = Counter(
        "omnisight_tenant_mem_gb_seconds_total",
        "Per-tenant cumulative GiB-seconds of memory (billing basis)",
        labelnames=("tenant_id",),
        registry=REGISTRY,
    )
    tenant_derate_total = Counter(
        "omnisight_tenant_derate_total",
        "Per-tenant AIMD derate events by reason",
        labelnames=("tenant_id", "reason"),  # reason: culprit | flat | recover
        registry=REGISTRY,
    )

    # H1: host-level gauges populated every SAMPLE_INTERVAL_S by
    # host_metrics.run_host_sampling_loop — whole-machine view that sits
    # beside the per-tenant cgroup numbers. Gauges (not counters) because
    # they represent an instantaneous sample, not a monotonic total;
    # Prometheus scrapes whatever the most recent tick published.
    host_cpu_percent = Gauge(
        "omnisight_host_cpu_percent",
        "Whole-host CPU utilisation (0-100) from psutil.cpu_percent",
        registry=REGISTRY,
    )
    host_mem_percent = Gauge(
        "omnisight_host_mem_percent",
        "Whole-host memory utilisation (0-100); derived from (total-available)/total",
        registry=REGISTRY,
    )
    host_disk_percent = Gauge(
        "omnisight_host_disk_percent",
        "Root filesystem utilisation (0-100) from psutil.disk_usage('/')",
        registry=REGISTRY,
    )
    host_loadavg_1m = Gauge(
        "omnisight_host_loadavg_1m",
        "1-minute load average (raw, not normalised by core count)",
        registry=REGISTRY,
    )
    host_container_count = Gauge(
        "omnisight_host_container_count",
        "Running Docker container count (SDK primary, CLI fallback) labelled by source",
        labelnames=("source",),  # sdk | cli | unavailable
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

    # B15 #350: Skill Lazy Loading (Progressive Disclosure) ─────
    # `skill_load_total` — every time the prompt layer assembles a
    # skill payload for an agent turn, partitioned by the flag mode
    # that produced it (`eager` inlines a full role body, `lazy`
    # Phase-1 emits the catalog) and the phase that fired
    # (`phase1_catalog`, `phase2_explicit`, `phase2_matched`,
    # `phase2_miss`). The `result` label captures whether the skill
    # body was actually injected (`loaded` | `empty` | `capped`).
    skill_load_total = Counter(
        "omnisight_skill_load_total",
        "Skill-loading events partitioned by feature-flag mode + phase",
        labelnames=("mode", "phase", "result"),
        registry=REGISTRY,
    )
    # `skill_token_saved_total` — rough tokens saved by choosing lazy
    # mode over eager. Increments on every lazy Phase-1 build by
    # (eager_full_role_tokens − catalog_tokens) so operators can size
    # the token budget impact in Grafana. Counter so the rate() over
    # time shows cumulative savings.
    skill_token_saved_total = Counter(
        "omnisight_skill_token_saved_total",
        "Cumulative input tokens saved by skill lazy loading vs eager baseline",
        labelnames=("mode",),  # lazy | eager (eager always adds 0)
        registry=REGISTRY,
    )
    # `skill_load_latency_ms` — wall-clock time each skill-loading
    # call took. Buckets go up to 1s because the most expensive
    # phase (scanning configs/roles/** for the catalog + reading
    # skill bodies) should normally finish in <50ms; a 1s+ bucket
    # is a red flag that filesystem I/O or YAML parsing has stalled.
    skill_load_latency_ms = Histogram(
        "omnisight_skill_load_latency_ms",
        "Wall-clock milliseconds spent assembling a skill payload",
        labelnames=("mode", "phase"),
        buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1000, 5000),
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
    prewarm_paused_total = Counter(
        "omnisight_prewarm_paused_total",
        "Pre-warm new-pool creation paused due to host high pressure",
        labelnames=("reason",),
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

    # Phase 64-C-LOCAL: T3 runner dispatch split (local / bundle /
    # ssh / qemu) — how many t3 tasks are going through which runner
    # class. Surfaces in Ops Summary so the operator can tell at a
    # glance whether the host==target happy path is actually being
    # hit.
    t3_runner_dispatch_total = Counter(
        "omnisight_t3_runner_dispatch_total",
        "T3 task dispatches by runner class",
        labelnames=("runner",),  # local | ssh | qemu | bundle
        registry=REGISTRY,
    )

    # O1 (#264): Redis distributed file-path mutex lock ────────
    dist_lock_wait_seconds = Histogram(
        "omnisight_dist_lock_wait_seconds",
        "Seconds spent in acquire_paths before outcome",
        labelnames=("outcome",),  # acquired | conflict
        buckets=(0.01, 0.05, 0.1, 0.5, 1, 5, 10, 30, 60, 300),
        registry=REGISTRY,
    )
    dist_lock_held_total = Counter(
        "omnisight_dist_lock_held_total",
        "Lock ownership transitions by outcome",
        labelnames=("outcome",),  # acquired | conflict | released | preempted
        registry=REGISTRY,
    )
    dist_lock_deadlock_kills_total = Counter(
        "omnisight_dist_lock_deadlock_kills_total",
        "Tasks force-killed by the deadlock sweep",
        labelnames=("reason",),
        registry=REGISTRY,
    )

    # O2 (#265): Message Queue abstraction layer ────────────────
    queue_depth = Gauge(
        "omnisight_queue_depth",
        "Number of messages in the queue, partitioned by priority + state",
        labelnames=("priority", "state"),  # P0..P3 × Queued/Ready/Claimed/...
        registry=REGISTRY,
    )
    queue_claim_duration_seconds = Histogram(
        "omnisight_queue_claim_duration_seconds",
        "Wall-clock seconds for a worker pull() call to return",
        labelnames=("outcome",),  # hit | empty
        buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5, 10),
        registry=REGISTRY,
    )

    # O3 (#266): Stateless Agent Worker Pool ────────────────────
    worker_active = Gauge(
        "omnisight_worker_active",
        "Number of workers currently registered in workers:active",
        registry=REGISTRY,
    )
    worker_inflight = Gauge(
        "omnisight_worker_inflight",
        "Number of in-flight tasks across this worker process",
        registry=REGISTRY,
    )
    worker_heartbeat_total = Counter(
        "omnisight_worker_heartbeat_total",
        "Heartbeat refresh ticks emitted",
        registry=REGISTRY,
    )
    worker_lifecycle_total = Counter(
        "omnisight_worker_lifecycle_total",
        "Worker lifecycle events (start | stop)",
        labelnames=("event",),
        registry=REGISTRY,
    )
    worker_task_total = Counter(
        "omnisight_worker_task_total",
        "Tasks processed by outcome",
        labelnames=("outcome",),  # acked | nacked | error | locked
        registry=REGISTRY,
    )
    worker_task_seconds = Histogram(
        "omnisight_worker_task_seconds",
        "End-to-end seconds spent per acked task",
        buckets=(0.05, 0.1, 0.5, 1, 5, 10, 30, 60, 300, 1800),
        registry=REGISTRY,
    )

    # O6 (#269): Merger Agent — conflict-resolution +2 votes ────
    merger_plus_two_total = Counter(
        "omnisight_merger_agent_plus_two_total",
        "Merger Agent Code-Review +2 votes cast (scope: conflict correctness)",
        registry=REGISTRY,
    )
    merger_abstain_total = Counter(
        "omnisight_merger_agent_abstain_total",
        "Merger Agent abstentions / refusals, partitioned by reason",
        labelnames=("reason",),
        registry=REGISTRY,
    )
    merger_security_refusal_total = Counter(
        "omnisight_merger_agent_security_refusal_total",
        "Merger Agent hard refusals for security-sensitive files",
        registry=REGISTRY,
    )
    merger_confidence = Histogram(
        "omnisight_merger_agent_confidence",
        "Merger Agent self-reported confidence per resolved conflict",
        buckets=(0.0, 0.1, 0.25, 0.5, 0.75, 0.85, 0.9, 0.95, 0.99, 1.0),
        registry=REGISTRY,
    )

    # Phase 63-E: episodic memory quality decay ────────────────
    memory_decay_total = Counter(
        "omnisight_memory_decay_total",
        "Memory decay events by action: decayed / skipped_recent / restored",
        labelnames=("action",),
        registry=REGISTRY,
    )

    # O9 (#272): orchestration observability ───────────────────
    awaiting_human_pending = Gauge(
        "omnisight_awaiting_human_plus_two_pending",
        "Number of changes for which the Merger has +2'd but no human +2 yet",
        registry=REGISTRY,
    )
    awaiting_human_age_seconds = Gauge(
        "omnisight_awaiting_human_plus_two_age_seconds",
        "Wall-clock age of the oldest still-pending dual-sign change",
        registry=REGISTRY,
    )
    worker_pool_capacity = Gauge(
        "omnisight_worker_pool_capacity",
        "Configured maximum concurrent in-flight tasks for this worker pool",
        registry=REGISTRY,
    )

    # Process up-time
    process_start_time = Gauge(
        "omnisight_process_start_time_seconds",
        "Unix timestamp when this process started",
        registry=REGISTRY,
    )
    process_start_time.set(time.time())

    # R2 (#308): Semantic Entropy Monitor ──────────────────────
    semantic_entropy_score = Gauge(
        "omnisight_semantic_entropy_score",
        "Rolling-window pairwise cosine-similarity mean for an agent's outputs",
        labelnames=("agent_id",),
        registry=REGISTRY,
    )
    cognitive_deadlock_total = Counter(
        "omnisight_cognitive_deadlock_total",
        "Times an agent's entropy crossed the deadlock threshold",
        labelnames=("agent_id",),
        registry=REGISTRY,
    )

    # R3 (#309): Scratchpad Memory Offload + Auto-Continuation ──
    scratchpad_saves_total = Counter(
        "omnisight_scratchpad_saves_total",
        "Times an agent's scratchpad was flushed to disk",
        labelnames=("agent_id", "trigger"),  # trigger: turn_interval|tool_done|subtask_switch|manual
        registry=REGISTRY,
    )
    scratchpad_size_bytes = Gauge(
        "omnisight_scratchpad_size_bytes",
        "Size (bytes, on-disk ciphertext) of the most recent scratchpad write",
        labelnames=("agent_id",),
        registry=REGISTRY,
    )
    token_continuation_total = Counter(
        "omnisight_token_continuation_total",
        "Auto-continuation rounds issued after stop_reason=max_tokens",
        labelnames=("agent_id", "provider"),
        registry=REGISTRY,
    )

    # R0 (#306): PEP Gateway — tool-call intercept decisions ───
    pep_decisions_total = Counter(
        "omnisight_pep_decisions_total",
        "PEP Gateway decisions partitioned by outcome / tier / rule",
        labelnames=("decision", "tier", "rule"),  # decision: auto_allow|hold|deny
        registry=REGISTRY,
    )
    pep_deny_total = Counter(
        "omnisight_pep_deny_total",
        "PEP Gateway hard-deny events (destructive pattern matches)",
        labelnames=("rule",),
        registry=REGISTRY,
    )
    pep_hold_duration_seconds = Histogram(
        "omnisight_pep_hold_duration_seconds",
        "Wall-clock seconds a HELD tool call spent waiting for operator",
        labelnames=("outcome",),  # approved | rejected | timeout
        buckets=(1, 5, 15, 60, 300, 900, 1800, 3600),
        registry=REGISTRY,
    )

    # G7 (HA-07): observability for HA signals ─────────────────
    # `backend_instance_up` is the classic "is this backend alive"
    # gauge. We set it to 1 at boot and flip it to 0 when the
    # lifecycle coordinator begins draining — the reverse-proxy /
    # k8s service can use `instance_up == 1` to decide which pods to
    # send traffic to. The instance_id label lets Prometheus scrape
    # multiple replicas behind the same target.
    backend_instance_up = Gauge(
        "omnisight_backend_instance_up",
        "1 when this backend replica is serving traffic, 0 when draining/down",
        labelnames=("instance_id",),
        registry=REGISTRY,
    )
    # `rolling_deploy_responses_total` is the source-of-truth counter
    # for the 5xx rate. Labels are the HTTP status class (2xx/3xx/
    # 4xx/5xx). PromQL can compute the rate at any horizon as
    # `rate(…{status_class="5xx"}[5m]) / rate(…[5m])`.
    rolling_deploy_responses_total = Counter(
        "omnisight_rolling_deploy_responses_total",
        "HTTP responses served during the rolling deploy window, by status class",
        labelnames=("status_class",),  # 2xx | 3xx | 4xx | 5xx
        registry=REGISTRY,
    )
    # `rolling_deploy_5xx_rate` is a convenience gauge exporting the
    # in-process rolling 5xx share (0.0..1.0) over the last 60s. The
    # gauge lets alert rules that can't / don't want to do PromQL
    # rate-of-rate math fire directly off a single scalar.
    rolling_deploy_5xx_rate = Gauge(
        "omnisight_rolling_deploy_5xx_rate",
        "Rolling-window 5xx response rate (0..1) computed in-process",
        registry=REGISTRY,
    )
    # `replica_lag_seconds` — Postgres streaming replication lag, in
    # seconds. Populated by the pg_ha sampler calling
    # `ha_observability.update_replica_lag()`. The `replica` label
    # carries the standby application_name.
    replica_lag_seconds = Gauge(
        "omnisight_replica_lag_seconds",
        "Streaming replication lag from primary to standby, in seconds",
        labelnames=("replica",),
        registry=REGISTRY,
    )
    # `readyz_latency_seconds` — end-to-end wall-clock latency of the
    # /readyz probe. Buckets tuned for fast probe paths: most replies
    # should be <50ms; a single bucket past 1s catches a stalled DB.
    readyz_latency_seconds = Histogram(
        "omnisight_readyz_latency_seconds",
        "Wall-clock seconds to serve the /readyz probe",
        labelnames=("outcome",),  # ready | not_ready | draining
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
        registry=REGISTRY,
    )
    # H2 audit (2026-04-19): gauge surface for the alembic mismatch
    # detected inside /readyz's migration check. A deploy that forgets
    # `alembic upgrade head` ships code that reads a stale schema; the
    # readyz check does catch it but the signal only surfaces in the
    # JSON payload, invisible to Prometheus. Exposing a numeric gauge
    # lets `OmniSightMigrationMismatch` fire without custom exporters.
    # 0 = aligned, 1 = mismatch (pending migrations).
    readyz_migrations_pending = Gauge(
        "omnisight_readyz_migrations_pending",
        "1 if alembic head on disk > applied revision, 0 if aligned",
        registry=REGISTRY,
    )

    # Y9 #285 row 4 — per-(tenant, project, product_line) billing metrics.
    # Cardinality is bucketed via ``backend.metrics_labels.bucket_*``
    # (tenant ≤ 1000, project ≤ 10000, product_line ≤ 50 per worker;
    # overflow → "other"; None / "" → "unknown"). Counters mirror the
    # ``billing_usage_events`` rows that ``backend.billing_usage`` writes
    # so PromQL ``rate(...)`` queries align with PG ``SUM(...)`` rollups
    # at the same time-window for cross-checking T4 / T6 dashboards.
    billing_llm_calls_total = Counter(
        "omnisight_billing_llm_calls_total",
        "LLM calls fan-outed to billing, by tenant/project/product_line/provider/model",
        labelnames=("tenant_id", "project_id", "product_line", "provider", "model"),
        registry=REGISTRY,
    )
    billing_llm_input_tokens_total = Counter(
        "omnisight_billing_llm_input_tokens_total",
        "LLM input tokens recorded in billing fan-out",
        labelnames=("tenant_id", "project_id", "product_line", "provider", "model"),
        registry=REGISTRY,
    )
    billing_llm_output_tokens_total = Counter(
        "omnisight_billing_llm_output_tokens_total",
        "LLM output tokens recorded in billing fan-out",
        labelnames=("tenant_id", "project_id", "product_line", "provider", "model"),
        registry=REGISTRY,
    )
    billing_llm_cost_usd_total = Counter(
        "omnisight_billing_llm_cost_usd_total",
        "LLM cost (USD) recorded in billing fan-out",
        labelnames=("tenant_id", "project_id", "product_line", "provider", "model"),
        registry=REGISTRY,
    )
    billing_workflow_runs_total = Counter(
        "omnisight_billing_workflow_runs_total",
        "Workflow runs fan-outed to billing, by tenant/project/product_line/kind/status",
        labelnames=(
            "tenant_id", "project_id", "product_line",
            "workflow_kind", "workflow_status",
        ),
        registry=REGISTRY,
    )
    billing_workspace_gb_hours_total = Counter(
        "omnisight_billing_workspace_gb_hours_total",
        "Workspace GB-hours recorded by GC sweep, by tenant/project/product_line",
        labelnames=("tenant_id", "project_id", "product_line"),
        registry=REGISTRY,
    )
    # Cap exhaustion gauge — flips when any of the three caps fills up.
    # Lets operators alert on `omnisight_metrics_label_cap_used > 0.9`
    # before the next new tenant / project starts collapsing into
    # ``other`` and silently breaking the per-slice dashboard.
    metrics_label_cap_used = Gauge(
        "omnisight_metrics_label_cap_used",
        "Fraction (0..1) of the per-worker label cap consumed by tracked values",
        labelnames=("dimension",),  # tenant | project | product_line
        registry=REGISTRY,
    )

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
    sandbox_oom_total = _NoOp()  # type: ignore
    tenant_cpu_percent = _NoOp()  # type: ignore
    tenant_mem_used_gb = _NoOp()  # type: ignore
    tenant_disk_used_gb = _NoOp()  # type: ignore
    tenant_sandbox_count = _NoOp()  # type: ignore
    tenant_cpu_seconds_total = _NoOp()  # type: ignore
    tenant_mem_gb_seconds_total = _NoOp()  # type: ignore
    tenant_derate_total = _NoOp()  # type: ignore
    host_cpu_percent = _NoOp()  # type: ignore
    host_mem_percent = _NoOp()  # type: ignore
    host_disk_percent = _NoOp()  # type: ignore
    host_loadavg_1m = _NoOp()  # type: ignore
    host_container_count = _NoOp()  # type: ignore
    skill_extracted_total = _NoOp()  # type: ignore
    skill_promoted_total = _NoOp()  # type: ignore
    skill_load_total = _NoOp()  # type: ignore
    skill_token_saved_total = _NoOp()  # type: ignore
    skill_load_latency_ms = _NoOp()  # type: ignore
    intelligence_score = _NoOp()  # type: ignore
    intelligence_alert_total = _NoOp()  # type: ignore
    prompt_outcome_total = _NoOp()  # type: ignore
    prompt_rolled_back_total = _NoOp()  # type: ignore
    dag_validation_total = _NoOp()  # type: ignore
    dag_validation_error_total = _NoOp()  # type: ignore
    dag_mutation_total = _NoOp()  # type: ignore
    prewarm_started_total = _NoOp()  # type: ignore
    prewarm_consumed_total = _NoOp()  # type: ignore
    prewarm_paused_total = _NoOp()  # type: ignore
    training_set_rows = _NoOp()  # type: ignore
    finetune_eval_score = _NoOp()  # type: ignore
    prompt_cache_hit_total = _NoOp()  # type: ignore
    prompt_cache_miss_total = _NoOp()  # type: ignore
    intelligence_iq_score = _NoOp()  # type: ignore
    intelligence_iq_regression_total = _NoOp()  # type: ignore
    rag_prefetch_total = _NoOp()  # type: ignore
    memory_decay_total = _NoOp()  # type: ignore
    t3_runner_dispatch_total = _NoOp()  # type: ignore
    dist_lock_wait_seconds = _NoOp()  # type: ignore
    dist_lock_held_total = _NoOp()  # type: ignore
    dist_lock_deadlock_kills_total = _NoOp()  # type: ignore
    queue_depth = _NoOp()  # type: ignore
    queue_claim_duration_seconds = _NoOp()  # type: ignore
    worker_active = _NoOp()  # type: ignore
    worker_inflight = _NoOp()  # type: ignore
    worker_heartbeat_total = _NoOp()  # type: ignore
    worker_lifecycle_total = _NoOp()  # type: ignore
    worker_task_total = _NoOp()  # type: ignore
    worker_task_seconds = _NoOp()  # type: ignore
    merger_plus_two_total = _NoOp()  # type: ignore
    merger_abstain_total = _NoOp()  # type: ignore
    merger_security_refusal_total = _NoOp()  # type: ignore
    merger_confidence = _NoOp()  # type: ignore
    awaiting_human_pending = _NoOp()  # type: ignore
    awaiting_human_age_seconds = _NoOp()  # type: ignore
    worker_pool_capacity = _NoOp()  # type: ignore
    process_start_time = _NoOp()  # type: ignore
    pep_decisions_total = _NoOp()  # type: ignore
    pep_deny_total = _NoOp()  # type: ignore
    pep_hold_duration_seconds = _NoOp()  # type: ignore
    semantic_entropy_score = _NoOp()  # type: ignore
    cognitive_deadlock_total = _NoOp()  # type: ignore
    scratchpad_saves_total = _NoOp()  # type: ignore
    scratchpad_size_bytes = _NoOp()  # type: ignore
    token_continuation_total = _NoOp()  # type: ignore
    backend_instance_up = _NoOp()  # type: ignore
    rolling_deploy_responses_total = _NoOp()  # type: ignore
    rolling_deploy_5xx_rate = _NoOp()  # type: ignore
    replica_lag_seconds = _NoOp()  # type: ignore
    readyz_latency_seconds = _NoOp()  # type: ignore
    readyz_migrations_pending = _NoOp()  # type: ignore
    # Y9 #285 row 4 — billing metrics with (tenant, project, product_line)
    billing_llm_calls_total = _NoOp()  # type: ignore
    billing_llm_input_tokens_total = _NoOp()  # type: ignore
    billing_llm_output_tokens_total = _NoOp()  # type: ignore
    billing_llm_cost_usd_total = _NoOp()  # type: ignore
    billing_workflow_runs_total = _NoOp()  # type: ignore
    billing_workspace_gb_hours_total = _NoOp()  # type: ignore
    metrics_label_cap_used = _NoOp()  # type: ignore
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
    global sandbox_oom_total
    global tenant_cpu_percent, tenant_mem_used_gb, tenant_disk_used_gb
    global tenant_sandbox_count, tenant_cpu_seconds_total
    global tenant_mem_gb_seconds_total, tenant_derate_total
    global host_cpu_percent, host_mem_percent, host_disk_percent
    global host_loadavg_1m, host_container_count
    global skill_extracted_total, skill_promoted_total
    global skill_load_total, skill_token_saved_total, skill_load_latency_ms
    global intelligence_score, intelligence_alert_total
    global prompt_outcome_total, prompt_rolled_back_total
    global dag_validation_total, dag_validation_error_total, dag_mutation_total
    global prewarm_started_total, prewarm_consumed_total, prewarm_paused_total
    global training_set_rows
    global finetune_eval_score
    global prompt_cache_hit_total, prompt_cache_miss_total
    global intelligence_iq_score, intelligence_iq_regression_total
    global rag_prefetch_total
    global memory_decay_total
    global t3_runner_dispatch_total
    global dist_lock_wait_seconds, dist_lock_held_total, dist_lock_deadlock_kills_total
    global queue_depth, queue_claim_duration_seconds
    global worker_active, worker_inflight, worker_heartbeat_total
    global worker_lifecycle_total, worker_task_total, worker_task_seconds
    global merger_plus_two_total, merger_abstain_total
    global merger_security_refusal_total, merger_confidence
    global awaiting_human_pending, awaiting_human_age_seconds
    global worker_pool_capacity
    global process_start_time
    # G7 HA-07 — forward-declared below where rebinding happens; listed
    # here to keep all `reset_for_tests()` rebindings in one place.
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
    sandbox_oom_total = Counter(
        "omnisight_sandbox_oom_total",
        "Sandbox containers killed by the cgroup OOM-killer",
        labelnames=("tenant_id", "tier"), registry=REGISTRY,
    )
    tenant_cpu_percent = Gauge(
        "omnisight_tenant_cpu_percent",
        "Per-tenant CPU usage summed across all running sandbox containers",
        labelnames=("tenant_id",), registry=REGISTRY,
    )
    tenant_mem_used_gb = Gauge(
        "omnisight_tenant_mem_used_gb",
        "Per-tenant memory usage (GiB)",
        labelnames=("tenant_id",), registry=REGISTRY,
    )
    tenant_disk_used_gb = Gauge(
        "omnisight_tenant_disk_used_gb",
        "Per-tenant on-disk usage (GiB)",
        labelnames=("tenant_id",), registry=REGISTRY,
    )
    tenant_sandbox_count = Gauge(
        "omnisight_tenant_sandbox_count",
        "Running sandbox containers per tenant",
        labelnames=("tenant_id",), registry=REGISTRY,
    )
    tenant_cpu_seconds_total = Counter(
        "omnisight_tenant_cpu_seconds_total",
        "Cumulative CPU-seconds per tenant (billing)",
        labelnames=("tenant_id",), registry=REGISTRY,
    )
    tenant_mem_gb_seconds_total = Counter(
        "omnisight_tenant_mem_gb_seconds_total",
        "Cumulative GiB-seconds per tenant (billing)",
        labelnames=("tenant_id",), registry=REGISTRY,
    )
    tenant_derate_total = Counter(
        "omnisight_tenant_derate_total",
        "Per-tenant AIMD derate events",
        labelnames=("tenant_id", "reason"), registry=REGISTRY,
    )
    host_cpu_percent = Gauge(
        "omnisight_host_cpu_percent",
        "Whole-host CPU utilisation (0-100)",
        registry=REGISTRY,
    )
    host_mem_percent = Gauge(
        "omnisight_host_mem_percent",
        "Whole-host memory utilisation (0-100)",
        registry=REGISTRY,
    )
    host_disk_percent = Gauge(
        "omnisight_host_disk_percent",
        "Root filesystem utilisation (0-100)",
        registry=REGISTRY,
    )
    host_loadavg_1m = Gauge(
        "omnisight_host_loadavg_1m",
        "1-minute load average",
        registry=REGISTRY,
    )
    host_container_count = Gauge(
        "omnisight_host_container_count",
        "Running Docker container count labelled by source",
        labelnames=("source",), registry=REGISTRY,
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
    skill_load_total = Counter(
        "omnisight_skill_load_total",
        "Skill-loading events partitioned by mode + phase",
        labelnames=("mode", "phase", "result"), registry=REGISTRY,
    )
    skill_token_saved_total = Counter(
        "omnisight_skill_token_saved_total",
        "Tokens saved by lazy loading vs eager baseline",
        labelnames=("mode",), registry=REGISTRY,
    )
    skill_load_latency_ms = Histogram(
        "omnisight_skill_load_latency_ms",
        "Wall-clock ms spent assembling a skill payload",
        labelnames=("mode", "phase"),
        buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1000, 5000),
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
    prewarm_paused_total = Counter(
        "omnisight_prewarm_paused_total",
        "Pre-warm pool creation paused due to host high pressure",
        labelnames=("reason",), registry=REGISTRY,
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
    t3_runner_dispatch_total = Counter(
        "omnisight_t3_runner_dispatch_total",
        "T3 task dispatches by runner class",
        labelnames=("runner",), registry=REGISTRY,
    )
    dist_lock_wait_seconds = Histogram(
        "omnisight_dist_lock_wait_seconds", "Dist-lock acquire wait",
        labelnames=("outcome",),
        buckets=(0.01, 0.05, 0.1, 0.5, 1, 5, 10, 30, 60, 300),
        registry=REGISTRY,
    )
    dist_lock_held_total = Counter(
        "omnisight_dist_lock_held_total", "Dist-lock ownership transitions",
        labelnames=("outcome",), registry=REGISTRY,
    )
    dist_lock_deadlock_kills_total = Counter(
        "omnisight_dist_lock_deadlock_kills_total", "Deadlock-sweep kills",
        labelnames=("reason",), registry=REGISTRY,
    )
    queue_depth = Gauge(
        "omnisight_queue_depth", "Queue depth by priority/state",
        labelnames=("priority", "state"), registry=REGISTRY,
    )
    queue_claim_duration_seconds = Histogram(
        "omnisight_queue_claim_duration_seconds", "Pull duration",
        labelnames=("outcome",),
        buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5, 10),
        registry=REGISTRY,
    )
    worker_active = Gauge(
        "omnisight_worker_active", "Active worker count",
        registry=REGISTRY,
    )
    worker_inflight = Gauge(
        "omnisight_worker_inflight", "Per-process in-flight tasks",
        registry=REGISTRY,
    )
    worker_heartbeat_total = Counter(
        "omnisight_worker_heartbeat_total", "Heartbeat ticks",
        registry=REGISTRY,
    )
    worker_lifecycle_total = Counter(
        "omnisight_worker_lifecycle_total", "Worker lifecycle events",
        labelnames=("event",), registry=REGISTRY,
    )
    worker_task_total = Counter(
        "omnisight_worker_task_total", "Worker task outcomes",
        labelnames=("outcome",), registry=REGISTRY,
    )
    worker_task_seconds = Histogram(
        "omnisight_worker_task_seconds", "End-to-end task seconds",
        buckets=(0.05, 0.1, 0.5, 1, 5, 10, 30, 60, 300, 1800),
        registry=REGISTRY,
    )
    merger_plus_two_total = Counter(
        "omnisight_merger_agent_plus_two_total",
        "Merger Agent +2 votes", registry=REGISTRY,
    )
    merger_abstain_total = Counter(
        "omnisight_merger_agent_abstain_total",
        "Merger Agent abstentions by reason",
        labelnames=("reason",), registry=REGISTRY,
    )
    merger_security_refusal_total = Counter(
        "omnisight_merger_agent_security_refusal_total",
        "Merger Agent security-file refusals", registry=REGISTRY,
    )
    merger_confidence = Histogram(
        "omnisight_merger_agent_confidence",
        "Merger Agent confidence",
        buckets=(0.0, 0.1, 0.25, 0.5, 0.75, 0.85, 0.9, 0.95, 0.99, 1.0),
        registry=REGISTRY,
    )
    awaiting_human_pending = Gauge(
        "omnisight_awaiting_human_plus_two_pending",
        "Number of changes waiting for the human +2 hard gate",
        registry=REGISTRY,
    )
    awaiting_human_age_seconds = Gauge(
        "omnisight_awaiting_human_plus_two_age_seconds",
        "Wall-clock age of the oldest still-pending dual-sign change",
        registry=REGISTRY,
    )
    worker_pool_capacity = Gauge(
        "omnisight_worker_pool_capacity",
        "Configured maximum concurrent in-flight tasks",
        registry=REGISTRY,
    )
    process_start_time = Gauge(
        "omnisight_process_start_time_seconds", "Process start time",
        registry=REGISTRY,
    )
    process_start_time.set(time.time())
    global pep_decisions_total, pep_deny_total, pep_hold_duration_seconds
    pep_decisions_total = Counter(
        "omnisight_pep_decisions_total", "PEP decisions",
        labelnames=("decision", "tier", "rule"), registry=REGISTRY,
    )
    pep_deny_total = Counter(
        "omnisight_pep_deny_total", "PEP hard-deny events",
        labelnames=("rule",), registry=REGISTRY,
    )
    pep_hold_duration_seconds = Histogram(
        "omnisight_pep_hold_duration_seconds", "PEP hold duration",
        labelnames=("outcome",),
        buckets=(1, 5, 15, 60, 300, 900, 1800, 3600),
        registry=REGISTRY,
    )
    global semantic_entropy_score, cognitive_deadlock_total
    semantic_entropy_score = Gauge(
        "omnisight_semantic_entropy_score",
        "Rolling-window pairwise cosine-similarity mean for an agent's outputs",
        labelnames=("agent_id",), registry=REGISTRY,
    )
    cognitive_deadlock_total = Counter(
        "omnisight_cognitive_deadlock_total",
        "Times an agent's entropy crossed the deadlock threshold",
        labelnames=("agent_id",), registry=REGISTRY,
    )
    global scratchpad_saves_total, scratchpad_size_bytes, token_continuation_total
    scratchpad_saves_total = Counter(
        "omnisight_scratchpad_saves_total",
        "Times an agent's scratchpad was flushed to disk",
        labelnames=("agent_id", "trigger"), registry=REGISTRY,
    )
    scratchpad_size_bytes = Gauge(
        "omnisight_scratchpad_size_bytes",
        "Size (bytes, on-disk ciphertext) of the most recent scratchpad write",
        labelnames=("agent_id",), registry=REGISTRY,
    )
    token_continuation_total = Counter(
        "omnisight_token_continuation_total",
        "Auto-continuation rounds issued after stop_reason=max_tokens",
        labelnames=("agent_id", "provider"), registry=REGISTRY,
    )
    # G7 (HA-07): observability for HA signals ─────────────────
    global backend_instance_up, rolling_deploy_responses_total
    global rolling_deploy_5xx_rate, replica_lag_seconds, readyz_latency_seconds
    global readyz_migrations_pending
    backend_instance_up = Gauge(
        "omnisight_backend_instance_up",
        "1 when this backend replica is serving traffic, 0 when draining/down",
        labelnames=("instance_id",), registry=REGISTRY,
    )
    rolling_deploy_responses_total = Counter(
        "omnisight_rolling_deploy_responses_total",
        "HTTP responses served during the rolling deploy window, by status class",
        labelnames=("status_class",), registry=REGISTRY,
    )
    rolling_deploy_5xx_rate = Gauge(
        "omnisight_rolling_deploy_5xx_rate",
        "Rolling-window 5xx response rate (0..1) computed in-process",
        registry=REGISTRY,
    )
    replica_lag_seconds = Gauge(
        "omnisight_replica_lag_seconds",
        "Streaming replication lag from primary to standby, in seconds",
        labelnames=("replica",), registry=REGISTRY,
    )
    readyz_latency_seconds = Histogram(
        "omnisight_readyz_latency_seconds",
        "Wall-clock seconds to serve the /readyz probe",
        labelnames=("outcome",),
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
        registry=REGISTRY,
    )
    readyz_migrations_pending = Gauge(
        "omnisight_readyz_migrations_pending",
        "1 if alembic head on disk > applied revision, 0 if aligned",
        registry=REGISTRY,
    )
    # Y9 #285 row 4 — per-(tenant, project, product_line) billing metrics
    global billing_llm_calls_total, billing_llm_input_tokens_total
    global billing_llm_output_tokens_total, billing_llm_cost_usd_total
    global billing_workflow_runs_total, billing_workspace_gb_hours_total
    global metrics_label_cap_used
    billing_llm_calls_total = Counter(
        "omnisight_billing_llm_calls_total",
        "LLM calls fan-outed to billing, by tenant/project/product_line/provider/model",
        labelnames=("tenant_id", "project_id", "product_line", "provider", "model"),
        registry=REGISTRY,
    )
    billing_llm_input_tokens_total = Counter(
        "omnisight_billing_llm_input_tokens_total",
        "LLM input tokens recorded in billing fan-out",
        labelnames=("tenant_id", "project_id", "product_line", "provider", "model"),
        registry=REGISTRY,
    )
    billing_llm_output_tokens_total = Counter(
        "omnisight_billing_llm_output_tokens_total",
        "LLM output tokens recorded in billing fan-out",
        labelnames=("tenant_id", "project_id", "product_line", "provider", "model"),
        registry=REGISTRY,
    )
    billing_llm_cost_usd_total = Counter(
        "omnisight_billing_llm_cost_usd_total",
        "LLM cost (USD) recorded in billing fan-out",
        labelnames=("tenant_id", "project_id", "product_line", "provider", "model"),
        registry=REGISTRY,
    )
    billing_workflow_runs_total = Counter(
        "omnisight_billing_workflow_runs_total",
        "Workflow runs fan-outed to billing, by tenant/project/product_line/kind/status",
        labelnames=(
            "tenant_id", "project_id", "product_line",
            "workflow_kind", "workflow_status",
        ),
        registry=REGISTRY,
    )
    billing_workspace_gb_hours_total = Counter(
        "omnisight_billing_workspace_gb_hours_total",
        "Workspace GB-hours recorded by GC sweep, by tenant/project/product_line",
        labelnames=("tenant_id", "project_id", "product_line"),
        registry=REGISTRY,
    )
    metrics_label_cap_used = Gauge(
        "omnisight_metrics_label_cap_used",
        "Fraction (0..1) of the per-worker label cap consumed by tracked values",
        labelnames=("dimension",),
        registry=REGISTRY,
    )
