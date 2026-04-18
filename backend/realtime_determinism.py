"""C12 — L4-CORE-12 Real-time / determinism track (#226).

RT-linux build profile (PREEMPT_RT), RTOS build profile (FreeRTOS / Zephyr),
cyclictest harness + percentile latency report, scheduler trace capture
(trace-cmd / bpftrace), and threshold gate (fails build if P99 > budget).

Public API:
    profiles   = list_rt_profiles()
    profile    = get_rt_profile(profile_id)
    configs    = list_cyclictest_configs()
    result     = run_cyclictest(config_id, latency_samples)
    trace      = capture_scheduler_trace(tool_id, duration_s)
    report     = analyze_latency(result)
    gate       = threshold_gate(result, tier_id_or_budget)
"""

from __future__ import annotations

import logging
import math
import statistics
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_RT_PROFILES_PATH = _PROJECT_ROOT / "configs" / "realtime_profiles.yaml"


# -- Enums --

class BuildType(str, Enum):
    linux = "linux"
    rtos = "rtos"


class RTOSType(str, Enum):
    freertos = "freertos"
    zephyr = "zephyr"


class SchedulerPolicy(str, Enum):
    fifo = "fifo"
    rr = "rr"
    deadline = "deadline"


class TraceToolType(str, Enum):
    trace_cmd = "trace_cmd"
    bpftrace = "bpftrace"


class RunStatus(str, Enum):
    passed = "passed"
    failed = "failed"
    pending = "pending"
    error = "error"
    running = "running"
    completed = "completed"


class GateVerdict(str, Enum):
    passed = "passed"
    failed = "failed"
    error = "error"


# -- Data models --

@dataclass
class RTProfileDef:
    profile_id: str
    name: str
    description: str = ""
    build_type: str = "linux"
    rtos_type: str = ""
    kernel_configs: dict[str, str] = field(default_factory=dict)
    rtos_configs: dict[str, Any] = field(default_factory=dict)
    recommended_boot_params: list[str] = field(default_factory=list)
    default_p99_budget_us: float = 100.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "name": self.name,
            "description": self.description,
            "build_type": self.build_type,
            "rtos_type": self.rtos_type,
            "kernel_configs": dict(self.kernel_configs),
            "rtos_configs": dict(self.rtos_configs),
            "recommended_boot_params": list(self.recommended_boot_params),
            "default_p99_budget_us": self.default_p99_budget_us,
        }


@dataclass
class CyclictestConfig:
    config_id: str
    name: str
    description: str = ""
    threads: int = 4
    priority: int = 99
    interval_us: int = 1000
    duration_s: int = 60
    histogram_buckets: int = 200
    affinity_cpus: list[int] = field(default_factory=list)
    policy: str = "fifo"
    stress_background: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_id": self.config_id,
            "name": self.name,
            "description": self.description,
            "threads": self.threads,
            "priority": self.priority,
            "interval_us": self.interval_us,
            "duration_s": self.duration_s,
            "histogram_buckets": self.histogram_buckets,
            "affinity_cpus": list(self.affinity_cpus),
            "policy": self.policy,
            "stress_background": self.stress_background,
        }


@dataclass
class TraceToolDef:
    tool_id: str
    name: str
    description: str = ""
    command: str = ""
    events: list[str] = field(default_factory=list)
    probes: list[str] = field(default_factory=list)
    output_format: str = "dat"

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "name": self.name,
            "description": self.description,
            "command": self.command,
            "events": list(self.events),
            "probes": list(self.probes),
            "output_format": self.output_format,
        }


@dataclass
class LatencyTierDef:
    tier_id: str
    name: str
    description: str = ""
    p50_budget_us: float = 100.0
    p95_budget_us: float = 250.0
    p99_budget_us: float = 500.0
    p999_budget_us: float = 2000.0
    max_jitter_us: float = 200.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier_id": self.tier_id,
            "name": self.name,
            "description": self.description,
            "p50_budget_us": self.p50_budget_us,
            "p95_budget_us": self.p95_budget_us,
            "p99_budget_us": self.p99_budget_us,
            "p999_budget_us": self.p999_budget_us,
            "max_jitter_us": self.max_jitter_us,
        }


@dataclass
class LatencySample:
    timestamp_us: float = 0.0
    latency_us: float = 0.0
    thread_id: int = 0
    cpu: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp_us": self.timestamp_us,
            "latency_us": self.latency_us,
            "thread_id": self.thread_id,
            "cpu": self.cpu,
        }


@dataclass
class LatencyPercentiles:
    p50_us: float = 0.0
    p90_us: float = 0.0
    p95_us: float = 0.0
    p99_us: float = 0.0
    p999_us: float = 0.0
    min_us: float = 0.0
    max_us: float = 0.0
    avg_us: float = 0.0
    stddev_us: float = 0.0
    jitter_us: float = 0.0
    sample_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "p50_us": self.p50_us,
            "p90_us": self.p90_us,
            "p95_us": self.p95_us,
            "p99_us": self.p99_us,
            "p999_us": self.p999_us,
            "min_us": self.min_us,
            "max_us": self.max_us,
            "avg_us": self.avg_us,
            "stddev_us": self.stddev_us,
            "jitter_us": self.jitter_us,
            "sample_count": self.sample_count,
        }


@dataclass
class HistogramBucket:
    lower_us: float = 0.0
    upper_us: float = 0.0
    count: int = 0
    pct: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "lower_us": self.lower_us,
            "upper_us": self.upper_us,
            "count": self.count,
            "pct": self.pct,
        }


@dataclass
class CyclictestResult:
    result_id: str = ""
    config_id: str = ""
    profile_id: str = ""
    status: str = "pending"
    percentiles: LatencyPercentiles = field(default_factory=LatencyPercentiles)
    histogram: list[HistogramBucket] = field(default_factory=list)
    samples: list[LatencySample] = field(default_factory=list)
    started_at: float = 0.0
    completed_at: float = 0.0
    duration_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "result_id": self.result_id,
            "config_id": self.config_id,
            "profile_id": self.profile_id,
            "status": self.status,
            "percentiles": self.percentiles.to_dict(),
            "histogram": [b.to_dict() for b in self.histogram],
            "sample_count": len(self.samples),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_s": self.duration_s,
        }


@dataclass
class TraceCapture:
    capture_id: str = ""
    tool_id: str = ""
    status: str = "pending"
    events_captured: int = 0
    duration_s: float = 0.0
    output_path: str = ""
    trace_events: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "capture_id": self.capture_id,
            "tool_id": self.tool_id,
            "status": self.status,
            "events_captured": self.events_captured,
            "duration_s": self.duration_s,
            "output_path": self.output_path,
            "summary": dict(self.summary),
        }


@dataclass
class GateFinding:
    category: str = ""
    metric: str = ""
    actual_us: float = 0.0
    budget_us: float = 0.0
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "metric": self.metric,
            "actual_us": self.actual_us,
            "budget_us": self.budget_us,
            "message": self.message,
        }


@dataclass
class ThresholdGateResult:
    verdict: str = "passed"
    tier_id: str = ""
    profile_id: str = ""
    findings: list[GateFinding] = field(default_factory=list)
    percentiles: LatencyPercentiles = field(default_factory=LatencyPercentiles)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "tier_id": self.tier_id,
            "profile_id": self.profile_id,
            "findings": [f.to_dict() for f in self.findings],
            "percentiles": self.percentiles.to_dict(),
        }


# -- Config loading --

_CONFIG: dict[str, Any] = {}


def _load_config() -> dict[str, Any]:
    global _CONFIG
    if _CONFIG:
        return _CONFIG
    if _RT_PROFILES_PATH.exists():
        with open(_RT_PROFILES_PATH, "r") as f:
            _CONFIG = yaml.safe_load(f) or {}
    else:
        logger.warning("RT profiles config not found: %s", _RT_PROFILES_PATH)
        _CONFIG = {}
    return _CONFIG


def reload_rt_config_for_tests() -> None:
    global _CONFIG
    _CONFIG = {}
    _load_config()


# -- RT profiles --

def list_rt_profiles() -> list[RTProfileDef]:
    cfg = _load_config()
    profiles = cfg.get("rt_profiles", {})
    result = []
    for pid, data in profiles.items():
        result.append(RTProfileDef(
            profile_id=pid,
            name=data.get("name", pid),
            description=data.get("description", ""),
            build_type=data.get("build_type", "linux"),
            rtos_type=data.get("rtos_type", ""),
            kernel_configs=data.get("kernel_configs", {}),
            rtos_configs=data.get("rtos_configs", {}),
            recommended_boot_params=data.get("recommended_boot_params", []),
            default_p99_budget_us=data.get("default_p99_budget_us", 100.0),
        ))
    return result


def get_rt_profile(profile_id: str) -> RTProfileDef | None:
    for p in list_rt_profiles():
        if p.profile_id == profile_id:
            return p
    return None


# -- Cyclictest configs --

def list_cyclictest_configs() -> list[CyclictestConfig]:
    cfg = _load_config()
    configs = cfg.get("cyclictest_configs", {})
    result = []
    for cid, data in configs.items():
        result.append(CyclictestConfig(
            config_id=cid,
            name=data.get("name", cid),
            description=data.get("description", ""),
            threads=data.get("threads", 4),
            priority=data.get("priority", 99),
            interval_us=data.get("interval_us", 1000),
            duration_s=data.get("duration_s", 60),
            histogram_buckets=data.get("histogram_buckets", 200),
            affinity_cpus=data.get("affinity_cpus", []),
            policy=data.get("policy", "fifo"),
            stress_background=data.get("stress_background", False),
        ))
    return result


def get_cyclictest_config(config_id: str) -> CyclictestConfig | None:
    for c in list_cyclictest_configs():
        if c.config_id == config_id:
            return c
    return None


# -- Trace tools --

def list_trace_tools() -> list[TraceToolDef]:
    cfg = _load_config()
    tools = cfg.get("trace_tools", {})
    result = []
    for tid, data in tools.items():
        result.append(TraceToolDef(
            tool_id=tid,
            name=data.get("name", tid),
            description=data.get("description", ""),
            command=data.get("command", ""),
            events=data.get("events", []),
            probes=data.get("probes", []),
            output_format=data.get("output_format", "dat"),
        ))
    return result


def get_trace_tool(tool_id: str) -> TraceToolDef | None:
    for t in list_trace_tools():
        if t.tool_id == tool_id:
            return t
    return None


# -- Latency tiers --

def list_latency_tiers() -> list[LatencyTierDef]:
    cfg = _load_config()
    tiers = cfg.get("latency_tiers", {})
    result = []
    for tid, data in tiers.items():
        result.append(LatencyTierDef(
            tier_id=tid,
            name=data.get("name", tid),
            description=data.get("description", ""),
            p50_budget_us=data.get("p50_budget_us", 100.0),
            p95_budget_us=data.get("p95_budget_us", 250.0),
            p99_budget_us=data.get("p99_budget_us", 500.0),
            p999_budget_us=data.get("p999_budget_us", 2000.0),
            max_jitter_us=data.get("max_jitter_us", 200.0),
        ))
    return result


def get_latency_tier(tier_id: str) -> LatencyTierDef | None:
    for t in list_latency_tiers():
        if t.tier_id == tier_id:
            return t
    return None


# -- Latency analysis --

def compute_percentiles(latencies_us: list[float]) -> LatencyPercentiles:
    if not latencies_us:
        return LatencyPercentiles()

    sorted_lat = sorted(latencies_us)
    n = len(sorted_lat)

    def _pctl(pct: float) -> float:
        idx = int(math.ceil(pct / 100.0 * n)) - 1
        return sorted_lat[max(0, min(idx, n - 1))]

    avg = statistics.mean(sorted_lat)
    stddev = statistics.stdev(sorted_lat) if n > 1 else 0.0

    return LatencyPercentiles(
        p50_us=_pctl(50),
        p90_us=_pctl(90),
        p95_us=_pctl(95),
        p99_us=_pctl(99),
        p999_us=_pctl(99.9),
        min_us=sorted_lat[0],
        max_us=sorted_lat[-1],
        avg_us=avg,
        stddev_us=stddev,
        jitter_us=sorted_lat[-1] - sorted_lat[0],
        sample_count=n,
    )


def build_histogram(
    latencies_us: list[float],
    buckets: int = 200,
) -> list[HistogramBucket]:
    if not latencies_us:
        return []

    min_val = min(latencies_us)
    max_val = max(latencies_us)

    if max_val == min_val:
        return [HistogramBucket(
            lower_us=min_val,
            upper_us=max_val,
            count=len(latencies_us),
            pct=100.0,
        )]

    bucket_width = (max_val - min_val) / buckets
    result = []
    n = len(latencies_us)

    for i in range(buckets):
        lower = min_val + i * bucket_width
        upper = lower + bucket_width
        if i == buckets - 1:
            count = sum(1 for v in latencies_us if lower <= v <= upper)
        else:
            count = sum(1 for v in latencies_us if lower <= v < upper)
        result.append(HistogramBucket(
            lower_us=round(lower, 3),
            upper_us=round(upper, 3),
            count=count,
            pct=round(count / n * 100.0, 3) if n > 0 else 0.0,
        ))

    return result


# -- Cyclictest harness --

def run_cyclictest(
    config_id: str = "default",
    profile_id: str = "preempt_rt",
    latency_samples: list[float] | None = None,
) -> CyclictestResult:
    config = get_cyclictest_config(config_id)
    if config is None:
        return CyclictestResult(
            result_id=f"ct-err-{int(time.time())}",
            config_id=config_id,
            profile_id=profile_id,
            status=RunStatus.error.value,
        )

    get_rt_profile(profile_id)
    started = time.time()

    if latency_samples is not None and len(latency_samples) > 0:
        samples = [
            LatencySample(
                timestamp_us=i * config.interval_us,
                latency_us=lat,
                thread_id=i % max(config.threads, 1),
                cpu=i % max(config.threads, 1),
            )
            for i, lat in enumerate(latency_samples)
        ]
        pctls = compute_percentiles(latency_samples)
        hist = build_histogram(latency_samples, config.histogram_buckets)
        status = RunStatus.completed.value
    else:
        samples = []
        pctls = LatencyPercentiles()
        hist = []
        status = RunStatus.pending.value

    completed = time.time()

    return CyclictestResult(
        result_id=f"ct-{int(started)}",
        config_id=config_id,
        profile_id=profile_id,
        status=status,
        percentiles=pctls,
        histogram=hist,
        samples=samples,
        started_at=started,
        completed_at=completed,
        duration_s=completed - started,
    )


# -- Scheduler trace capture --

def capture_scheduler_trace(
    tool_id: str = "trace_cmd",
    duration_s: float = 5.0,
    trace_events: list[dict[str, Any]] | None = None,
) -> TraceCapture:
    tool = get_trace_tool(tool_id)
    if tool is None:
        return TraceCapture(
            capture_id=f"trace-err-{int(time.time())}",
            tool_id=tool_id,
            status=RunStatus.error.value,
        )

    started = time.time()

    if trace_events is not None and len(trace_events) > 0:
        sched_switch_count = sum(
            1 for e in trace_events if e.get("event", "") == "sched_switch"
        )
        irq_count = sum(
            1 for e in trace_events
            if e.get("event", "").startswith("irq_handler")
        )
        wakeup_count = sum(
            1 for e in trace_events if e.get("event", "") == "sched_wakeup"
        )

        summary = {
            "total_events": len(trace_events),
            "sched_switch_count": sched_switch_count,
            "irq_handler_count": irq_count,
            "sched_wakeup_count": wakeup_count,
            "duration_s": duration_s,
            "tool": tool.name,
        }

        return TraceCapture(
            capture_id=f"trace-{int(started)}",
            tool_id=tool_id,
            status=RunStatus.completed.value,
            events_captured=len(trace_events),
            duration_s=duration_s,
            output_path=f"/tmp/omnisight-trace-{int(started)}.{tool.output_format}",
            trace_events=trace_events,
            summary=summary,
        )
    else:
        return TraceCapture(
            capture_id=f"trace-{int(started)}",
            tool_id=tool_id,
            status=RunStatus.pending.value,
            duration_s=duration_s,
            summary={"tool": tool.name, "note": "Hardware trace pending — no real kernel available"},
        )


# -- Threshold gate --

def threshold_gate(
    result: CyclictestResult,
    tier_id: str | None = None,
    custom_budget_us: float | None = None,
) -> ThresholdGateResult:
    if result.status != RunStatus.completed.value:
        return ThresholdGateResult(
            verdict=GateVerdict.error.value,
            tier_id=tier_id or "",
            profile_id=result.profile_id,
            findings=[GateFinding(
                category="status",
                metric="test_status",
                message=f"Cyclictest not completed (status={result.status})",
            )],
            percentiles=result.percentiles,
        )

    tier: LatencyTierDef | None = None
    if tier_id:
        tier = get_latency_tier(tier_id)

    if tier is None and custom_budget_us is None:
        profile = get_rt_profile(result.profile_id)
        custom_budget_us = profile.default_p99_budget_us if profile else 100.0

    findings: list[GateFinding] = []
    pctls = result.percentiles

    if tier:
        checks = [
            ("p50", pctls.p50_us, tier.p50_budget_us),
            ("p95", pctls.p95_us, tier.p95_budget_us),
            ("p99", pctls.p99_us, tier.p99_budget_us),
            ("p999", pctls.p999_us, tier.p999_budget_us),
            ("jitter", pctls.jitter_us, tier.max_jitter_us),
        ]
        for metric, actual, budget in checks:
            if actual > budget:
                findings.append(GateFinding(
                    category="latency_budget",
                    metric=metric,
                    actual_us=actual,
                    budget_us=budget,
                    message=f"{metric} latency {actual:.1f}µs exceeds budget {budget:.1f}µs",
                ))
    elif custom_budget_us is not None:
        if pctls.p99_us > custom_budget_us:
            findings.append(GateFinding(
                category="latency_budget",
                metric="p99",
                actual_us=pctls.p99_us,
                budget_us=custom_budget_us,
                message=f"P99 latency {pctls.p99_us:.1f}µs exceeds budget {custom_budget_us:.1f}µs",
            ))

    verdict = GateVerdict.failed.value if findings else GateVerdict.passed.value

    return ThresholdGateResult(
        verdict=verdict,
        tier_id=tier_id or "",
        profile_id=result.profile_id,
        findings=findings,
        percentiles=pctls,
    )


# -- Kernel config generation --

def generate_kernel_config_fragment(profile_id: str) -> str:
    profile = get_rt_profile(profile_id)
    if profile is None:
        return ""

    if profile.build_type != BuildType.linux.value:
        return ""

    lines = [
        f"# Auto-generated RT kernel config for {profile.name}",
        f"# Profile: {profile_id}",
        "",
    ]

    for key, value in sorted(profile.kernel_configs.items()):
        if value == "n":
            lines.append(f"# {key} is not set")
        elif value == "m":
            lines.append(f"{key}=m")
        else:
            lines.append(f"{key}={value}")

    if profile.recommended_boot_params:
        lines.append("")
        lines.append("# Recommended boot parameters:")
        for param in profile.recommended_boot_params:
            lines.append(f"#   {param}")

    return "\n".join(lines) + "\n"


def generate_rtos_config_header(profile_id: str) -> str:
    profile = get_rt_profile(profile_id)
    if profile is None:
        return ""

    if profile.build_type != BuildType.rtos.value:
        return ""

    lines = [
        f"/* Auto-generated RTOS config for {profile.name} */",
        f"/* Profile: {profile_id}, RTOS: {profile.rtos_type} */",
        "",
        "#ifndef OMNISIGHT_RT_CONFIG_H",
        "#define OMNISIGHT_RT_CONFIG_H",
        "",
    ]

    for key, value in sorted(profile.rtos_configs.items()):
        if isinstance(value, str):
            lines.append(f'#define {key} "{value}"')
        else:
            lines.append(f"#define {key} {value}")

    lines.extend(["", "#endif /* OMNISIGHT_RT_CONFIG_H */", ""])

    return "\n".join(lines)


# -- Latency report generation --

def generate_latency_report(result: CyclictestResult) -> str:
    pctls = result.percentiles
    lines = [
        "# Cyclictest Latency Report",
        "",
        f"**Profile:** {result.profile_id}",
        f"**Config:** {result.config_id}",
        f"**Status:** {result.status}",
        f"**Duration:** {result.duration_s:.2f}s",
        f"**Samples:** {pctls.sample_count}",
        "",
        "## Percentile Breakdown",
        "",
        "| Percentile | Latency (µs) |",
        "|-----------|-------------|",
        f"| Min       | {pctls.min_us:.1f} |",
        f"| P50       | {pctls.p50_us:.1f} |",
        f"| P90       | {pctls.p90_us:.1f} |",
        f"| P95       | {pctls.p95_us:.1f} |",
        f"| P99       | {pctls.p99_us:.1f} |",
        f"| P99.9     | {pctls.p999_us:.1f} |",
        f"| Max       | {pctls.max_us:.1f} |",
        f"| Avg       | {pctls.avg_us:.1f} |",
        f"| Stddev    | {pctls.stddev_us:.1f} |",
        f"| Jitter    | {pctls.jitter_us:.1f} |",
        "",
    ]

    return "\n".join(lines) + "\n"


# -- Doc suite generator integration --

_ACTIVE_RT_CERTS: list[dict[str, Any]] = []


def register_rt_cert(
    standard: str,
    status: str = "Pending",
    cert_id: str = "",
    details: dict[str, Any] | None = None,
) -> None:
    _ACTIVE_RT_CERTS.append({
        "standard": standard,
        "status": status,
        "cert_id": cert_id,
        "details": details or {},
    })


def get_rt_certs() -> list[dict[str, Any]]:
    return list(_ACTIVE_RT_CERTS)


def clear_rt_certs() -> None:
    _ACTIVE_RT_CERTS.clear()


# -- Audit log integration --

async def log_cyclictest_result(result: CyclictestResult) -> Optional[int]:
    try:
        from backend import audit
        return await audit.log(
            action="cyclictest_run",
            entity_kind="cyclictest_result",
            entity_id=result.result_id,
            before=None,
            after=result.to_dict(),
            actor="realtime_determinism",
        )
    except Exception as exc:
        logger.warning("Failed to log cyclictest result to audit: %s", exc)
        return None


def log_cyclictest_result_sync(result: CyclictestResult) -> None:
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("log_cyclictest_result_sync skipped (no running loop)")
        return
    loop.create_task(log_cyclictest_result(result))


async def log_gate_result(gate: ThresholdGateResult) -> Optional[int]:
    try:
        from backend import audit
        return await audit.log(
            action="rt_threshold_gate",
            entity_kind="gate_result",
            entity_id=f"gate-{int(time.time())}",
            before=None,
            after=gate.to_dict(),
            actor="realtime_determinism",
        )
    except Exception as exc:
        logger.warning("Failed to log gate result to audit: %s", exc)
        return None


async def log_trace_capture(capture: TraceCapture) -> Optional[int]:
    try:
        from backend import audit
        return await audit.log(
            action="scheduler_trace_capture",
            entity_kind="trace_capture",
            entity_id=capture.capture_id,
            before=None,
            after=capture.to_dict(),
            actor="realtime_determinism",
        )
    except Exception as exc:
        logger.warning("Failed to log trace capture to audit: %s", exc)
        return None
