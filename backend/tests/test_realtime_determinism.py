"""C12 — L4-CORE-12 Real-time / determinism track tests (#226).

Covers:
  - RT profile config loading + parsing (PREEMPT_RT, FreeRTOS, Zephyr)
  - Cyclictest config loading + parsing
  - Trace tool config loading
  - Latency tier config loading
  - Data model serialization round-trips
  - Percentile computation from latency samples
  - Histogram generation
  - Cyclictest harness execution (with synthetic samples)
  - Scheduler trace capture (with synthetic events)
  - Threshold gate (pass + fail + tier-based + custom budget)
  - Kernel config fragment generation (PREEMPT_RT)
  - RTOS config header generation (FreeRTOS, Zephyr)
  - Latency report generation
  - Doc suite generator integration (get_rt_certs)
  - Audit log integration
  - Edge cases (unknown profile, empty samples, unknown config)
  - REST endpoint smoke tests
  - Acceptance: synthetic trace → gate pass/fail pipeline
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.realtime_determinism import (
    BuildType,
    CyclictestResult,
    GateFinding,
    GateVerdict,
    HistogramBucket,
    LatencyPercentiles,
    LatencySample,
    RTOSType,
    SchedulerPolicy,
    RunStatus,
    ThresholdGateResult,
    TraceCapture,
    TraceToolType,
    build_histogram,
    capture_scheduler_trace,
    clear_rt_certs,
    compute_percentiles,
    generate_kernel_config_fragment,
    generate_latency_report,
    generate_rtos_config_header,
    get_cyclictest_config,
    get_latency_tier,
    get_rt_certs,
    get_rt_profile,
    get_trace_tool,
    list_cyclictest_configs,
    list_latency_tiers,
    list_rt_profiles,
    list_trace_tools,
    log_cyclictest_result,
    log_gate_result,
    log_trace_capture,
    register_rt_cert,
    reload_rt_config_for_tests,
    run_cyclictest,
    threshold_gate,
)


# -- Fixtures --

@pytest.fixture(autouse=True)
def _reload_config():
    reload_rt_config_for_tests()
    clear_rt_certs()
    yield
    reload_rt_config_for_tests()
    clear_rt_certs()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  1. Config loading & parsing — RT profiles
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRTProfileConfig:
    def test_list_rt_profiles_returns_all(self):
        profiles = list_rt_profiles()
        assert len(profiles) == 4
        ids = {p.profile_id for p in profiles}
        assert ids == {"preempt_rt", "preempt_rt_relaxed", "freertos", "zephyr"}

    def test_preempt_rt_profile_attributes(self):
        p = get_rt_profile("preempt_rt")
        assert p is not None
        assert p.name == "PREEMPT_RT Linux"
        assert p.build_type == "linux"
        assert p.kernel_configs.get("CONFIG_PREEMPT_RT") == "y"
        assert p.kernel_configs.get("CONFIG_HZ_1000") == "y"
        assert p.kernel_configs.get("CONFIG_CPU_FREQ") == "n"
        assert p.default_p99_budget_us == 100.0
        assert len(p.recommended_boot_params) >= 4

    def test_preempt_rt_relaxed_profile(self):
        p = get_rt_profile("preempt_rt_relaxed")
        assert p is not None
        assert p.build_type == "linux"
        assert p.kernel_configs.get("CONFIG_HZ_250") == "y"
        assert p.default_p99_budget_us == 500.0

    def test_freertos_profile_attributes(self):
        p = get_rt_profile("freertos")
        assert p is not None
        assert p.build_type == "rtos"
        assert p.rtos_type == "freertos"
        assert p.rtos_configs.get("configUSE_PREEMPTION") == 1
        assert p.rtos_configs.get("configTICK_RATE_HZ") == 1000
        assert p.rtos_configs.get("configMAX_PRIORITIES") == 32
        assert p.default_p99_budget_us == 50.0

    def test_zephyr_profile_attributes(self):
        p = get_rt_profile("zephyr")
        assert p is not None
        assert p.build_type == "rtos"
        assert p.rtos_type == "zephyr"
        assert p.rtos_configs.get("CONFIG_SYS_CLOCK_TICKS_PER_SEC") == 1000
        assert p.rtos_configs.get("CONFIG_SCHED_DEADLINE") == "y"
        assert p.default_p99_budget_us == 30.0

    def test_unknown_profile_returns_none(self):
        assert get_rt_profile("nonexistent") is None

    def test_profile_to_dict_roundtrip(self):
        p = get_rt_profile("preempt_rt")
        d = p.to_dict()
        assert d["profile_id"] == "preempt_rt"
        assert d["build_type"] == "linux"
        assert isinstance(d["kernel_configs"], dict)
        assert isinstance(d["recommended_boot_params"], list)

    def test_linux_profiles_have_kernel_configs(self):
        for p in list_rt_profiles():
            if p.build_type == "linux":
                assert len(p.kernel_configs) > 0
                assert "CONFIG_PREEMPT_RT" in p.kernel_configs

    def test_rtos_profiles_have_rtos_configs(self):
        for p in list_rt_profiles():
            if p.build_type == "rtos":
                assert len(p.rtos_configs) > 0
                assert p.rtos_type in ("freertos", "zephyr")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2. Config loading — cyclictest configs
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCyclictestConfig:
    def test_list_cyclictest_configs_returns_all(self):
        configs = list_cyclictest_configs()
        assert len(configs) == 3
        ids = {c.config_id for c in configs}
        assert ids == {"default", "stress", "minimal"}

    def test_default_config_values(self):
        c = get_cyclictest_config("default")
        assert c is not None
        assert c.threads == 4
        assert c.priority == 99
        assert c.interval_us == 1000
        assert c.duration_s == 60
        assert c.policy == "fifo"
        assert c.stress_background is False

    def test_stress_config_values(self):
        c = get_cyclictest_config("stress")
        assert c is not None
        assert c.threads == 8
        assert c.duration_s == 300
        assert c.stress_background is True
        assert c.histogram_buckets == 500

    def test_minimal_config_values(self):
        c = get_cyclictest_config("minimal")
        assert c is not None
        assert c.threads == 1
        assert c.duration_s == 10

    def test_unknown_config_returns_none(self):
        assert get_cyclictest_config("nonexistent") is None

    def test_config_to_dict(self):
        c = get_cyclictest_config("default")
        d = c.to_dict()
        assert d["config_id"] == "default"
        assert d["threads"] == 4
        assert d["priority"] == 99


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  3. Config loading — trace tools
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestTraceToolConfig:
    def test_list_trace_tools_returns_all(self):
        tools = list_trace_tools()
        assert len(tools) == 2
        ids = {t.tool_id for t in tools}
        assert ids == {"trace_cmd", "bpftrace"}

    def test_trace_cmd_attributes(self):
        t = get_trace_tool("trace_cmd")
        assert t is not None
        assert t.command == "trace-cmd"
        assert len(t.events) >= 5
        assert "sched:sched_switch" in t.events
        assert t.output_format == "dat"

    def test_bpftrace_attributes(self):
        t = get_trace_tool("bpftrace")
        assert t is not None
        assert t.command == "bpftrace"
        assert len(t.probes) >= 3
        assert t.output_format == "json"

    def test_unknown_tool_returns_none(self):
        assert get_trace_tool("nonexistent") is None

    def test_tool_to_dict(self):
        t = get_trace_tool("trace_cmd")
        d = t.to_dict()
        assert d["tool_id"] == "trace_cmd"
        assert isinstance(d["events"], list)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  4. Config loading — latency tiers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestLatencyTierConfig:
    def test_list_latency_tiers_returns_all(self):
        tiers = list_latency_tiers()
        assert len(tiers) == 4
        ids = {t.tier_id for t in tiers}
        assert ids == {"ultra_strict", "strict", "moderate", "relaxed"}

    def test_ultra_strict_tier(self):
        t = get_latency_tier("ultra_strict")
        assert t is not None
        assert t.p99_budget_us == 50.0
        assert t.p999_budget_us == 100.0
        assert t.max_jitter_us == 20.0

    def test_relaxed_tier(self):
        t = get_latency_tier("relaxed")
        assert t is not None
        assert t.p99_budget_us == 5000.0

    def test_tiers_ordered_by_strictness(self):
        tiers = list_latency_tiers()
        tier_map = {t.tier_id: t for t in tiers}
        assert tier_map["ultra_strict"].p99_budget_us < tier_map["strict"].p99_budget_us
        assert tier_map["strict"].p99_budget_us < tier_map["moderate"].p99_budget_us
        assert tier_map["moderate"].p99_budget_us < tier_map["relaxed"].p99_budget_us

    def test_unknown_tier_returns_none(self):
        assert get_latency_tier("nonexistent") is None

    def test_tier_to_dict(self):
        t = get_latency_tier("strict")
        d = t.to_dict()
        assert d["tier_id"] == "strict"
        assert d["p99_budget_us"] == 150.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  5. Data model serialization
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestDataModels:
    def test_latency_sample_to_dict(self):
        s = LatencySample(timestamp_us=1000.0, latency_us=42.5, thread_id=0, cpu=1)
        d = s.to_dict()
        assert d["latency_us"] == 42.5
        assert d["cpu"] == 1

    def test_latency_percentiles_to_dict(self):
        p = LatencyPercentiles(p50_us=10.0, p99_us=50.0, sample_count=100)
        d = p.to_dict()
        assert d["p50_us"] == 10.0
        assert d["sample_count"] == 100

    def test_histogram_bucket_to_dict(self):
        b = HistogramBucket(lower_us=0.0, upper_us=10.0, count=5, pct=25.0)
        d = b.to_dict()
        assert d["count"] == 5
        assert d["pct"] == 25.0

    def test_cyclictest_result_to_dict(self):
        r = CyclictestResult(
            result_id="ct-123",
            config_id="default",
            profile_id="preempt_rt",
            status="completed",
        )
        d = r.to_dict()
        assert d["result_id"] == "ct-123"
        assert d["config_id"] == "default"
        assert "percentiles" in d
        assert "histogram" in d

    def test_trace_capture_to_dict(self):
        c = TraceCapture(capture_id="trace-123", tool_id="trace_cmd", events_captured=42)
        d = c.to_dict()
        assert d["capture_id"] == "trace-123"
        assert d["events_captured"] == 42

    def test_gate_finding_to_dict(self):
        f = GateFinding(category="latency_budget", metric="p99", actual_us=200.0, budget_us=100.0)
        d = f.to_dict()
        assert d["actual_us"] == 200.0
        assert d["budget_us"] == 100.0

    def test_threshold_gate_result_to_dict(self):
        g = ThresholdGateResult(verdict="passed", tier_id="strict")
        d = g.to_dict()
        assert d["verdict"] == "passed"
        assert d["tier_id"] == "strict"

    def test_enum_values(self):
        assert BuildType.linux.value == "linux"
        assert BuildType.rtos.value == "rtos"
        assert RTOSType.freertos.value == "freertos"
        assert RTOSType.zephyr.value == "zephyr"
        assert SchedulerPolicy.fifo.value == "fifo"
        assert TraceToolType.trace_cmd.value == "trace_cmd"
        assert RunStatus.passed.value == "passed"
        assert GateVerdict.failed.value == "failed"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  6. Percentile computation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestPercentileComputation:
    def test_basic_percentiles(self):
        samples = list(range(1, 101))  # 1..100
        p = compute_percentiles([float(s) for s in samples])
        assert p.p50_us == 50.0
        assert p.p90_us == 90.0
        assert p.p95_us == 95.0
        assert p.p99_us == 99.0
        assert p.min_us == 1.0
        assert p.max_us == 100.0
        assert p.sample_count == 100

    def test_empty_samples_returns_defaults(self):
        p = compute_percentiles([])
        assert p.sample_count == 0
        assert p.p50_us == 0.0
        assert p.p99_us == 0.0

    def test_single_sample(self):
        p = compute_percentiles([42.0])
        assert p.p50_us == 42.0
        assert p.p99_us == 42.0
        assert p.min_us == 42.0
        assert p.max_us == 42.0
        assert p.jitter_us == 0.0
        assert p.stddev_us == 0.0

    def test_jitter_calculation(self):
        samples = [10.0, 20.0, 30.0, 40.0, 50.0]
        p = compute_percentiles(samples)
        assert p.jitter_us == 40.0  # max - min

    def test_stddev_nonzero(self):
        samples = [10.0, 20.0, 30.0, 40.0, 50.0]
        p = compute_percentiles(samples)
        assert p.stddev_us > 0.0
        assert p.avg_us == 30.0

    def test_all_same_values(self):
        samples = [25.0] * 100
        p = compute_percentiles(samples)
        assert p.p50_us == 25.0
        assert p.p99_us == 25.0
        assert p.jitter_us == 0.0
        assert p.stddev_us == 0.0

    def test_large_dataset_percentiles(self):
        samples = [float(i) for i in range(1, 10001)]
        p = compute_percentiles(samples)
        assert p.p50_us == 5000.0
        assert p.p99_us == 9900.0
        assert p.sample_count == 10000


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  7. Histogram generation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestHistogram:
    def test_basic_histogram(self):
        samples = [float(i) for i in range(100)]
        hist = build_histogram(samples, buckets=10)
        assert len(hist) == 10
        total = sum(b.count for b in hist)
        assert total == 100

    def test_empty_samples_returns_empty(self):
        hist = build_histogram([])
        assert len(hist) == 0

    def test_single_value_histogram(self):
        hist = build_histogram([42.0], buckets=10)
        assert len(hist) == 1
        assert hist[0].count == 1
        assert hist[0].pct == 100.0

    def test_all_same_value_histogram(self):
        hist = build_histogram([10.0] * 50, buckets=10)
        assert len(hist) == 1
        assert hist[0].count == 50

    def test_histogram_percentages_sum(self):
        samples = [float(i) for i in range(1000)]
        hist = build_histogram(samples, buckets=20)
        total_pct = sum(b.pct for b in hist)
        assert abs(total_pct - 100.0) < 1.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  8. Cyclictest harness
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCyclictest:
    def test_run_with_samples(self):
        samples = [10.0, 15.0, 20.0, 25.0, 30.0, 35.0, 40.0, 45.0, 50.0, 100.0]
        result = run_cyclictest("default", "preempt_rt", latency_samples=samples)
        assert result.status == "completed"
        assert result.percentiles.sample_count == 10
        assert result.percentiles.min_us == 10.0
        assert result.percentiles.max_us == 100.0
        assert len(result.histogram) > 0

    def test_run_without_samples_returns_pending(self):
        result = run_cyclictest("default", "preempt_rt")
        assert result.status == "pending"
        assert result.percentiles.sample_count == 0

    def test_run_with_unknown_config_returns_error(self):
        result = run_cyclictest("nonexistent", "preempt_rt", latency_samples=[10.0])
        assert result.status == "error"

    def test_result_id_format(self):
        result = run_cyclictest("default", "preempt_rt", latency_samples=[10.0])
        assert result.result_id.startswith("ct-")

    def test_run_with_stress_config(self):
        samples = [float(i) for i in range(1, 51)]
        result = run_cyclictest("stress", "preempt_rt", latency_samples=samples)
        assert result.status == "completed"
        assert result.config_id == "stress"

    def test_run_with_minimal_config(self):
        result = run_cyclictest("minimal", "preempt_rt", latency_samples=[5.0, 10.0])
        assert result.status == "completed"

    def test_samples_assigned_to_threads(self):
        samples = [10.0, 20.0, 30.0, 40.0]
        result = run_cyclictest("default", "preempt_rt", latency_samples=samples)
        thread_ids = {s.thread_id for s in result.samples}
        assert len(thread_ids) == 4  # 4 threads for default config

    def test_empty_samples_list(self):
        result = run_cyclictest("default", "preempt_rt", latency_samples=[])
        assert result.status == "pending"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  9. Scheduler trace capture
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestTraceCapture:
    def test_capture_with_events(self):
        events = [
            {"event": "sched_switch", "timestamp": 1000, "prev_comm": "idle", "next_comm": "rt_task"},
            {"event": "sched_switch", "timestamp": 1010, "prev_comm": "rt_task", "next_comm": "idle"},
            {"event": "irq_handler_entry", "timestamp": 1005, "irq": 42},
            {"event": "irq_handler_exit", "timestamp": 1006, "irq": 42},
            {"event": "sched_wakeup", "timestamp": 1008, "comm": "rt_task"},
        ]
        capture = capture_scheduler_trace("trace_cmd", 5.0, trace_events=events)
        assert capture.status == "completed"
        assert capture.events_captured == 5
        assert capture.summary["sched_switch_count"] == 2
        assert capture.summary["irq_handler_count"] == 2
        assert capture.summary["sched_wakeup_count"] == 1

    def test_capture_without_events_returns_pending(self):
        capture = capture_scheduler_trace("trace_cmd", 5.0)
        assert capture.status == "pending"

    def test_capture_with_bpftrace(self):
        events = [{"event": "sched_switch", "timestamp": 100}]
        capture = capture_scheduler_trace("bpftrace", 2.0, trace_events=events)
        assert capture.status == "completed"
        assert capture.tool_id == "bpftrace"

    def test_capture_unknown_tool_returns_error(self):
        capture = capture_scheduler_trace("nonexistent", 5.0)
        assert capture.status == "error"

    def test_capture_id_format(self):
        capture = capture_scheduler_trace("trace_cmd", 5.0, trace_events=[{"event": "test"}])
        assert capture.capture_id.startswith("trace-")

    def test_output_path_format(self):
        capture = capture_scheduler_trace("trace_cmd", 5.0, trace_events=[{"event": "test"}])
        assert capture.output_path.endswith(".dat")

    def test_bpftrace_output_json(self):
        capture = capture_scheduler_trace("bpftrace", 5.0, trace_events=[{"event": "test"}])
        assert capture.output_path.endswith(".json")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  10. Threshold gate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestThresholdGate:
    def test_gate_pass_with_custom_budget(self):
        samples = [10.0, 15.0, 20.0, 25.0, 30.0]
        result = run_cyclictest("default", "preempt_rt", latency_samples=samples)
        gate = threshold_gate(result, custom_budget_us=100.0)
        assert gate.verdict == "passed"
        assert len(gate.findings) == 0

    def test_gate_fail_with_custom_budget(self):
        samples = [10.0, 15.0, 20.0, 25.0, 200.0]
        result = run_cyclictest("default", "preempt_rt", latency_samples=samples)
        gate = threshold_gate(result, custom_budget_us=50.0)
        assert gate.verdict == "failed"
        assert len(gate.findings) > 0
        assert gate.findings[0].metric == "p99"

    def test_gate_with_tier(self):
        samples = [5.0, 8.0, 10.0, 12.0, 15.0, 18.0, 20.0, 22.0, 25.0, 28.0]
        result = run_cyclictest("default", "preempt_rt", latency_samples=samples)
        gate = threshold_gate(result, tier_id="moderate")
        assert gate.verdict == "passed"
        assert gate.tier_id == "moderate"

    def test_gate_fail_ultra_strict_tier(self):
        samples = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
        result = run_cyclictest("default", "preempt_rt", latency_samples=samples)
        gate = threshold_gate(result, tier_id="ultra_strict")
        assert gate.verdict == "failed"
        assert any(f.metric == "p99" for f in gate.findings)

    def test_gate_uses_profile_default_budget(self):
        samples = [5.0, 10.0, 15.0, 20.0, 25.0]
        result = run_cyclictest("default", "preempt_rt", latency_samples=samples)
        gate = threshold_gate(result)
        assert gate.verdict == "passed"

    def test_gate_error_on_incomplete_result(self):
        result = run_cyclictest("default", "preempt_rt")  # no samples → pending
        gate = threshold_gate(result, custom_budget_us=100.0)
        assert gate.verdict == "error"
        assert any("not completed" in f.message for f in gate.findings)

    def test_gate_finding_details(self):
        samples = [50.0, 100.0, 150.0, 200.0, 250.0]
        result = run_cyclictest("default", "preempt_rt", latency_samples=samples)
        gate = threshold_gate(result, custom_budget_us=100.0)
        if gate.findings:
            f = gate.findings[0]
            assert f.category == "latency_budget"
            assert f.actual_us > 0
            assert f.budget_us == 100.0
            assert "µs" in f.message

    def test_gate_tier_checks_jitter(self):
        samples = [1.0] * 99 + [1000.0]  # extreme jitter
        result = run_cyclictest("default", "preempt_rt", latency_samples=samples)
        gate = threshold_gate(result, tier_id="ultra_strict")
        assert gate.verdict == "failed"
        jitter_findings = [f for f in gate.findings if f.metric == "jitter"]
        assert len(jitter_findings) > 0

    def test_gate_pass_relaxed_tier_wide_samples(self):
        samples = [100.0, 200.0, 300.0, 400.0, 500.0, 600.0, 700.0, 800.0, 900.0, 1000.0]
        result = run_cyclictest("default", "preempt_rt", latency_samples=samples)
        gate = threshold_gate(result, tier_id="relaxed")
        assert gate.verdict == "passed"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  11. Kernel config generation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestKernelConfig:
    def test_preempt_rt_config_fragment(self):
        fragment = generate_kernel_config_fragment("preempt_rt")
        assert "CONFIG_PREEMPT_RT=y" in fragment
        assert "CONFIG_HZ_1000=y" in fragment
        assert "# CONFIG_CPU_FREQ is not set" in fragment
        assert "isolcpus" in fragment

    def test_relaxed_config_fragment(self):
        fragment = generate_kernel_config_fragment("preempt_rt_relaxed")
        assert "CONFIG_PREEMPT_RT=y" in fragment
        assert "CONFIG_HZ_250=y" in fragment

    def test_rtos_profile_returns_empty_kernel_config(self):
        fragment = generate_kernel_config_fragment("freertos")
        assert fragment == ""

    def test_unknown_profile_returns_empty(self):
        fragment = generate_kernel_config_fragment("nonexistent")
        assert fragment == ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  12. RTOS config header generation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRTOSConfig:
    def test_freertos_header(self):
        header = generate_rtos_config_header("freertos")
        assert "#define configUSE_PREEMPTION 1" in header
        assert "#define configTICK_RATE_HZ 1000" in header
        assert "#define configMAX_PRIORITIES 32" in header
        assert "#ifndef OMNISIGHT_RT_CONFIG_H" in header
        assert "#endif" in header

    def test_zephyr_header(self):
        header = generate_rtos_config_header("zephyr")
        assert "#define CONFIG_SYS_CLOCK_TICKS_PER_SEC 1000" in header
        assert '#define CONFIG_SCHED_DEADLINE "y"' in header

    def test_linux_profile_returns_empty_rtos_header(self):
        header = generate_rtos_config_header("preempt_rt")
        assert header == ""

    def test_unknown_profile_returns_empty(self):
        header = generate_rtos_config_header("nonexistent")
        assert header == ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  13. Latency report generation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestLatencyReport:
    def test_report_contains_percentiles(self):
        samples = [10.0, 20.0, 30.0, 40.0, 50.0]
        result = run_cyclictest("default", "preempt_rt", latency_samples=samples)
        report = generate_latency_report(result)
        assert "Cyclictest Latency Report" in report
        assert "P50" in report
        assert "P99" in report
        assert "preempt_rt" in report

    def test_report_markdown_table(self):
        samples = [10.0, 20.0, 30.0]
        result = run_cyclictest("default", "preempt_rt", latency_samples=samples)
        report = generate_latency_report(result)
        assert "| Percentile |" in report
        assert "| Min" in report
        assert "| Max" in report

    def test_report_pending_result(self):
        result = run_cyclictest("default", "preempt_rt")
        report = generate_latency_report(result)
        assert "pending" in report


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  14. Doc suite generator integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestDocSuiteIntegration:
    def test_register_and_get_certs(self):
        register_rt_cert("RT-Linux PREEMPT_RT", status="Passed", cert_id="rt-001")
        certs = get_rt_certs()
        assert len(certs) == 1
        assert certs[0]["standard"] == "RT-Linux PREEMPT_RT"
        assert certs[0]["status"] == "Passed"

    def test_clear_certs(self):
        register_rt_cert("test", status="Pending")
        assert len(get_rt_certs()) == 1
        clear_rt_certs()
        assert len(get_rt_certs()) == 0

    def test_multiple_certs(self):
        register_rt_cert("PREEMPT_RT", status="Passed", cert_id="rt-001")
        register_rt_cert("FreeRTOS Timing", status="Pending", cert_id="rt-002")
        register_rt_cert("Zephyr Determinism", status="Passed", cert_id="rt-003")
        certs = get_rt_certs()
        assert len(certs) == 3


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  15. Audit log integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestAuditIntegration:
    @pytest.mark.asyncio
    async def test_log_cyclictest_result(self):
        result = run_cyclictest("default", "preempt_rt", latency_samples=[10.0, 20.0])
        mock_audit = MagicMock()
        mock_audit.log = AsyncMock(return_value=42)
        with patch.dict("sys.modules", {"backend.audit": mock_audit}):
            await log_cyclictest_result(result)

    @pytest.mark.asyncio
    async def test_log_gate_result(self):
        result = run_cyclictest("default", "preempt_rt", latency_samples=[10.0])
        gate = threshold_gate(result, custom_budget_us=100.0)
        mock_audit = MagicMock()
        mock_audit.log = AsyncMock(return_value=43)
        with patch.dict("sys.modules", {"backend.audit": mock_audit}):
            await log_gate_result(gate)

    @pytest.mark.asyncio
    async def test_log_trace_capture(self):
        capture = capture_scheduler_trace("trace_cmd", 5.0, trace_events=[{"event": "test"}])
        mock_audit = MagicMock()
        mock_audit.log = AsyncMock(return_value=44)
        with patch.dict("sys.modules", {"backend.audit": mock_audit}):
            await log_trace_capture(capture)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  16. Edge cases
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestEdgeCases:
    def test_very_large_latency(self):
        samples = [1.0] * 99 + [1_000_000.0]
        p = compute_percentiles(samples)
        assert p.max_us == 1_000_000.0
        assert p.jitter_us == 999_999.0

    def test_zero_latency_samples(self):
        samples = [0.0] * 10
        p = compute_percentiles(samples)
        assert p.p99_us == 0.0
        assert p.avg_us == 0.0

    def test_negative_latency_handled(self):
        samples = [-5.0, 0.0, 5.0, 10.0]
        p = compute_percentiles(samples)
        assert p.min_us == -5.0

    def test_two_samples(self):
        p = compute_percentiles([10.0, 20.0])
        assert p.sample_count == 2
        assert p.avg_us == 15.0
        assert p.stddev_us > 0

    def test_cyclictest_with_freertos_profile(self):
        result = run_cyclictest("default", "freertos", latency_samples=[5.0, 10.0, 15.0])
        assert result.status == "completed"
        assert result.profile_id == "freertos"

    def test_gate_with_zephyr_profile(self):
        result = run_cyclictest("default", "zephyr", latency_samples=[5.0, 10.0, 15.0, 20.0, 25.0])
        gate = threshold_gate(result)
        assert gate.profile_id == "zephyr"

    def test_histogram_with_two_distinct_values(self):
        samples = [10.0, 100.0]
        hist = build_histogram(samples, buckets=10)
        total = sum(b.count for b in hist)
        assert total == 2


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  17. REST endpoint smoke tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRESTEndpoints:
    @pytest.fixture
    def client(self):
        from unittest.mock import MagicMock
        mock_user = MagicMock()
        mock_user.username = "test-operator"

        from backend.routers.realtime import router
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(router)

        from backend import auth as _au
        original = _au.require_operator

        async def mock_require_operator():
            return mock_user

        app.dependency_overrides[original] = mock_require_operator
        return TestClient(app)

    def test_list_profiles(self, client):
        resp = client.get("/realtime/profiles")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 4

    def test_get_profile(self, client):
        resp = client.get("/realtime/profiles/preempt_rt")
        assert resp.status_code == 200
        assert resp.json()["profile_id"] == "preempt_rt"

    def test_get_profile_404(self, client):
        resp = client.get("/realtime/profiles/nonexistent")
        assert resp.status_code == 404

    def test_list_cyclictest_configs(self, client):
        resp = client.get("/realtime/cyclictest/configs")
        assert resp.status_code == 200
        assert resp.json()["count"] == 3

    def test_list_trace_tools(self, client):
        resp = client.get("/realtime/trace/tools")
        assert resp.status_code == 200
        assert resp.json()["count"] == 2

    def test_list_tiers(self, client):
        resp = client.get("/realtime/tiers")
        assert resp.status_code == 200
        assert resp.json()["count"] == 4

    def test_run_cyclictest_endpoint(self, client):
        resp = client.post("/realtime/cyclictest/run", json={
            "config_id": "default",
            "profile_id": "preempt_rt",
            "latency_samples": [10.0, 20.0, 30.0, 40.0, 50.0],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"

    def test_capture_trace_endpoint(self, client):
        resp = client.post("/realtime/trace/capture", json={
            "tool_id": "trace_cmd",
            "duration_s": 2.0,
            "trace_events": [{"event": "sched_switch"}],
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"

    def test_gate_check_endpoint(self, client):
        resp = client.post("/realtime/gate/check", json={
            "config_id": "default",
            "profile_id": "preempt_rt",
            "latency_samples": [5.0, 10.0, 15.0, 20.0, 25.0],
            "custom_budget_us": 100.0,
        })
        assert resp.status_code == 200
        assert resp.json()["verdict"] == "passed"

    def test_kernel_config_endpoint(self, client):
        resp = client.get("/realtime/profiles/preempt_rt/kernel-config")
        assert resp.status_code == 200
        data = resp.json()
        assert "CONFIG_PREEMPT_RT" in data["kernel_config_fragment"]

    def test_report_endpoint(self, client):
        resp = client.post("/realtime/report", json={
            "config_id": "default",
            "profile_id": "preempt_rt",
            "latency_samples": [10.0, 20.0, 30.0],
        })
        assert resp.status_code == 200
        assert "report_markdown" in resp.json()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  18. Acceptance tests — full pipeline
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestAcceptance:
    def test_full_pipeline_pass(self):
        """Good RT system: run cyclictest → analyze → gate → report."""
        samples = [8.0, 9.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0,
                   18.0, 19.0, 20.0, 21.0, 22.0, 23.0, 24.0, 25.0, 26.0, 27.0]

        result = run_cyclictest("default", "preempt_rt", latency_samples=samples)
        assert result.status == "completed"
        assert result.percentiles.p99_us <= 100.0

        gate = threshold_gate(result, tier_id="moderate")
        assert gate.verdict == "passed"

        report = generate_latency_report(result)
        assert "P99" in report

    def test_full_pipeline_fail(self):
        """Bad RT system: high latency spikes → gate fail."""
        samples = [10.0] * 90 + [500.0] * 5 + [2000.0] * 3 + [5000.0, 10000.0]

        result = run_cyclictest("default", "preempt_rt", latency_samples=samples)
        assert result.status == "completed"

        gate = threshold_gate(result, tier_id="strict")
        assert gate.verdict == "failed"

    def test_full_pipeline_with_trace(self):
        """Run cyclictest + trace capture → analyze both."""
        samples = [15.0, 18.0, 20.0, 22.0, 25.0]
        result = run_cyclictest("minimal", "preempt_rt", latency_samples=samples)

        trace_events = [
            {"event": "sched_switch", "timestamp": 1000},
            {"event": "sched_wakeup", "timestamp": 1001},
            {"event": "irq_handler_entry", "timestamp": 1002},
            {"event": "irq_handler_exit", "timestamp": 1003},
        ]
        capture = capture_scheduler_trace("trace_cmd", 5.0, trace_events=trace_events)
        assert capture.status == "completed"
        assert capture.events_captured == 4

        gate = threshold_gate(result, custom_budget_us=100.0)
        assert gate.verdict == "passed"

    def test_rtos_pipeline(self):
        """FreeRTOS: run cyclictest → check against default budget."""
        samples = [3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 12.0, 15.0]
        result = run_cyclictest("default", "freertos", latency_samples=samples)
        assert result.status == "completed"

        gate = threshold_gate(result)
        assert gate.profile_id == "freertos"

    def test_zephyr_pipeline_ultra_strict(self):
        """Zephyr: tight latency → ultra-strict tier."""
        samples = [2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 12.0]
        result = run_cyclictest("default", "zephyr", latency_samples=samples)
        gate = threshold_gate(result, tier_id="ultra_strict")
        assert gate.profile_id == "zephyr"

    def test_kernel_config_generation_pipeline(self):
        """Profile → kernel config → validate content."""
        for profile in list_rt_profiles():
            if profile.build_type == "linux":
                frag = generate_kernel_config_fragment(profile.profile_id)
                assert len(frag) > 0
                assert "CONFIG_PREEMPT_RT" in frag
            else:
                header = generate_rtos_config_header(profile.profile_id)
                assert len(header) > 0
                assert "#define" in header
