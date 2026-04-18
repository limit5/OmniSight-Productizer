"""C11 — L4-CORE-11 Power / battery profiling tests (#225).

Covers:
  - Power profile config loading + parsing (sleep states, domains, ADCs)
  - Sleep-state transition detection from current traces
  - Current profiling sampler (raw sample processing)
  - Battery lifetime model (capacity × avg draw × duty cycle)
  - Feature power budget (mAh/day per feature toggle)
  - Doc suite generator integration (get_power_certs)
  - Audit log integration
  - Edge cases (unknown ADC, empty trace, zero current)
  - REST endpoint smoke tests
  - Synthetic current trace → correct lifetime estimate (acceptance test)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.power_profiling import (
    ADCConfig,
    ADCInterface,
    BatterySpec,
    CurrentSample,
    DutyCycleProfile,
    FeaturePowerBudgetItem,
    FeatureToggleDef,
    PowerDomainDef,
    ProfilingSession,
    ProfilingStatus,
    SleepState,
    SleepStateDef,
    TransitionDirection,
    clear_power_certs,
    compute_feature_power_budget,
    detect_sleep_transitions,
    estimate_battery_lifetime,
    get_adc_config,
    get_battery_chemistry,
    get_feature_toggle,
    get_power_certs,
    get_power_domain,
    get_sleep_state,
    list_adc_configs,
    list_battery_chemistries,
    list_feature_toggles,
    list_power_domains,
    list_sleep_states,
    log_lifetime_estimate,
    log_profiling_result,
    log_profiling_result_sync,
    register_power_cert,
    reload_power_profiles_for_tests,
    sample_current,
)


# -- Fixtures --

@pytest.fixture(autouse=True)
def _reload_config():
    reload_power_profiles_for_tests()
    clear_power_certs()
    yield
    reload_power_profiles_for_tests()
    clear_power_certs()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  1. Config loading & parsing
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestConfigLoading:
    def test_list_sleep_states_returns_six(self):
        states = list_sleep_states()
        assert len(states) == 6
        ids = {s.state_id for s in states}
        assert ids == {"s0_active", "s1_idle", "s2_standby", "s3_suspend", "s4_hibernate", "s5_off"}

    def test_sleep_states_sorted_by_order(self):
        states = list_sleep_states()
        orders = [s.order for s in states]
        assert orders == sorted(orders)

    def test_get_sleep_state_s0(self):
        s = get_sleep_state("s0_active")
        assert s is not None
        assert s.name == "S0 Active"
        assert s.typical_draw_pct == 100
        assert s.wake_latency_ms == 0

    def test_get_sleep_state_s3(self):
        s = get_sleep_state("s3_suspend")
        assert s is not None
        assert s.name == "S3 Suspend to RAM"
        assert s.typical_draw_pct == 3
        assert s.wake_latency_ms == 500

    def test_get_sleep_state_unknown(self):
        assert get_sleep_state("s99_nonexistent") is None

    def test_list_power_domains(self):
        domains = list_power_domains()
        assert len(domains) == 10
        ids = {d.domain_id for d in domains}
        assert "cpu" in ids
        assert "wifi" in ids
        assert "sensor" in ids

    def test_get_power_domain_cpu(self):
        d = get_power_domain("cpu")
        assert d is not None
        assert d.name == "CPU Core"
        assert d.typical_active_ma == 350
        assert d.typical_sleep_ma == 2

    def test_get_power_domain_unknown(self):
        assert get_power_domain("quantum_core") is None

    def test_list_adc_configs(self):
        configs = list_adc_configs()
        assert len(configs) == 4
        ids = {c.adc_id for c in configs}
        assert ids == {"ina219", "ina226", "ads1115", "internal_adc"}

    def test_get_adc_config_ina226(self):
        c = get_adc_config("ina226")
        assert c is not None
        assert c.name == "INA226"
        assert c.resolution_bits == 16
        assert c.sample_rate_hz == 1000
        assert c.shunt_resistor_ohm == 0.01

    def test_adc_lsb_current(self):
        c = get_adc_config("ina219")
        assert c is not None
        expected_lsb = 3.2 / (2 ** 12)
        assert abs(c.lsb_current_a - expected_lsb) < 1e-9

    def test_get_adc_config_unknown(self):
        assert get_adc_config("nonexistent_adc") is None

    def test_list_feature_toggles(self):
        toggles = list_feature_toggles()
        assert len(toggles) == 8
        ids = {t.toggle_id for t in toggles}
        assert "wifi_always_on" in ids
        assert "camera_streaming" in ids

    def test_get_feature_toggle(self):
        t = get_feature_toggle("camera_streaming")
        assert t is not None
        assert t.extra_draw_ma == 500
        assert "sensor" in t.domains_affected

    def test_get_feature_toggle_unknown(self):
        assert get_feature_toggle("holographic_display") is None

    def test_list_battery_chemistries(self):
        chems = list_battery_chemistries()
        assert len(chems) == 4
        ids = {c["chemistry_id"] for c in chems}
        assert ids == {"li_ion", "li_po", "lifepo4", "nimh"}

    def test_get_battery_chemistry_li_ion(self):
        c = get_battery_chemistry("li_ion")
        assert c is not None
        assert c["nominal_voltage_v"] == 3.7
        assert c["cycle_degradation_pct_per_100"] == 2.0

    def test_get_battery_chemistry_unknown(self):
        assert get_battery_chemistry("nuclear") is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2. Data model tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDataModels:
    def test_sleep_state_def_to_dict(self):
        s = SleepStateDef(state_id="s0", name="Active", typical_draw_pct=100, order=0)
        d = s.to_dict()
        assert d["state_id"] == "s0"
        assert d["typical_draw_pct"] == 100

    def test_power_domain_def_to_dict(self):
        d = PowerDomainDef(domain_id="cpu", name="CPU", typical_active_ma=350, typical_sleep_ma=2)
        result = d.to_dict()
        assert result["typical_active_ma"] == 350

    def test_adc_config_to_dict(self):
        c = ADCConfig(adc_id="test", name="Test", resolution_bits=12, max_current_a=3.2)
        d = c.to_dict()
        assert "lsb_current_a" in d

    def test_battery_spec_effective_capacity_new(self):
        b = BatterySpec(capacity_mah=3000, cycle_count=0)
        assert b.effective_capacity_mah == 3000.0

    def test_battery_spec_effective_capacity_degraded(self):
        b = BatterySpec(capacity_mah=3000, cycle_count=200, degradation_pct_per_100_cycles=2.0)
        expected = 3000 * (1.0 - 4.0 / 100.0)
        assert abs(b.effective_capacity_mah - expected) < 0.01

    def test_battery_spec_degradation_capped(self):
        b = BatterySpec(capacity_mah=3000, cycle_count=10000, degradation_pct_per_100_cycles=2.0)
        assert abs(b.effective_capacity_mah - 3000 * 0.2) < 0.01

    def test_duty_cycle_avg_current(self):
        dc = DutyCycleProfile(
            active_pct=20, idle_pct=30, sleep_pct=50,
            active_current_ma=500, idle_current_ma=50, sleep_current_ma=2,
        )
        expected = 500 * 0.2 + 50 * 0.3 + 2 * 0.5
        assert abs(dc.avg_current_ma - expected) < 0.001

    def test_current_sample_to_dict(self):
        s = CurrentSample(timestamp_s=1.0, current_ma=100, voltage_v=3.7, power_mw=370)
        d = s.to_dict()
        assert d["current_ma"] == 100

    def test_profiling_session_to_dict(self):
        s = ProfilingSession(session_id="test", adc_id="ina219", status=ProfilingStatus.completed)
        d = s.to_dict()
        assert d["status"] == "completed"

    def test_feature_toggle_def_to_dict(self):
        t = FeatureToggleDef(toggle_id="wifi", name="WiFi", extra_draw_ma=180)
        d = t.to_dict()
        assert d["extra_draw_ma"] == 180


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  3. Sleep-state transition detection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSleepTransitions:
    def test_empty_trace(self):
        assert detect_sleep_transitions([]) == []

    def test_single_sample(self):
        assert detect_sleep_transitions([{"timestamp_s": 0, "current_ma": 500}]) == []

    def test_no_transition_stable_high(self):
        domains = list_power_domains()
        total = sum(d.typical_active_ma for d in domains)
        trace = [
            {"timestamp_s": 0, "current_ma": total * 0.98},
            {"timestamp_s": 1, "current_ma": total * 0.95},
            {"timestamp_s": 2, "current_ma": total * 0.99},
        ]
        events = detect_sleep_transitions(trace)
        assert len(events) == 0

    def test_active_to_sleep(self):
        domains = list_power_domains()
        total = sum(d.typical_active_ma for d in domains)
        trace = [
            {"timestamp_s": 0, "current_ma": total * 1.0},
            {"timestamp_s": 1, "current_ma": total * 0.95},
            {"timestamp_s": 5, "current_ma": total * 0.03},
        ]
        events = detect_sleep_transitions(trace)
        assert len(events) >= 1
        assert events[0].direction == "entry"

    def test_sleep_to_active(self):
        domains = list_power_domains()
        total = sum(d.typical_active_ma for d in domains)
        trace = [
            {"timestamp_s": 0, "current_ma": total * 0.03},
            {"timestamp_s": 5, "current_ma": total * 0.95},
        ]
        events = detect_sleep_transitions(trace)
        assert len(events) >= 1
        assert events[0].direction == "exit"

    def test_multiple_transitions(self):
        domains = list_power_domains()
        total = sum(d.typical_active_ma for d in domains)
        trace = [
            {"timestamp_s": 0, "current_ma": total},
            {"timestamp_s": 10, "current_ma": total * 0.6},
            {"timestamp_s": 20, "current_ma": total * 0.03},
            {"timestamp_s": 30, "current_ma": total * 0.95},
        ]
        events = detect_sleep_transitions(trace)
        assert len(events) >= 2

    def test_transition_event_fields(self):
        domains = list_power_domains()
        total = sum(d.typical_active_ma for d in domains)
        trace = [
            {"timestamp_s": 0, "current_ma": total},
            {"timestamp_s": 5, "current_ma": total * 0.03},
        ]
        events = detect_sleep_transitions(trace)
        assert len(events) >= 1
        ev = events[0]
        assert ev.timestamp_s == 5
        assert ev.current_ma_before == total
        assert ev.current_ma_after == total * 0.03
        d = ev.to_dict()
        assert "from_state" in d
        assert "to_state" in d


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  4. Current profiling sampler
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCurrentSampler:
    def test_stub_returns_pending(self):
        session = sample_current("ina219", 10.0)
        assert session.status == ProfilingStatus.pending
        assert session.adc_id == "ina219"
        assert "Stub" in session.message

    def test_unknown_adc_returns_error(self):
        session = sample_current("nonexistent", 5.0)
        assert session.status == ProfilingStatus.error
        assert "Unknown ADC" in session.message

    def test_raw_samples_processed(self):
        raw = [
            {"timestamp_s": 0.0, "current_ma": 100, "voltage_v": 3.7},
            {"timestamp_s": 0.5, "current_ma": 200, "voltage_v": 3.7},
            {"timestamp_s": 1.0, "current_ma": 150, "voltage_v": 3.7},
        ]
        session = sample_current("ina226", 1.0, raw_samples=raw)
        assert session.status == ProfilingStatus.completed
        assert session.sample_count == 3
        assert session.avg_current_ma == 150.0
        assert session.peak_current_ma == 200.0
        assert session.min_current_ma == 100.0

    def test_total_charge_calculation(self):
        raw = [
            {"timestamp_s": 0.0, "current_ma": 100, "voltage_v": 3.7},
            {"timestamp_s": 3600.0, "current_ma": 100, "voltage_v": 3.7},
        ]
        session = sample_current("ina219", 3600.0, raw_samples=raw)
        assert abs(session.total_charge_mah - 100.0) < 0.1

    def test_empty_raw_samples(self):
        session = sample_current("ina219", 1.0, raw_samples=[])
        assert session.status == ProfilingStatus.error

    def test_power_calculation(self):
        raw = [
            {"timestamp_s": 0.0, "current_ma": 100, "voltage_v": 3.7},
        ]
        session = sample_current("ina219", 1.0, raw_samples=raw)
        assert session.status == ProfilingStatus.completed
        assert session.samples[0].power_mw == 370.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  5. Battery lifetime model
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBatteryLifetime:
    def test_basic_lifetime(self):
        battery = BatterySpec(capacity_mah=3000, cycle_count=0)
        duty = DutyCycleProfile(
            active_pct=100, idle_pct=0, sleep_pct=0,
            active_current_ma=500, idle_current_ma=0, sleep_current_ma=0,
        )
        est = estimate_battery_lifetime(battery, duty)
        assert abs(est.lifetime_hours - 6.0) < 0.01
        assert abs(est.lifetime_days - 0.25) < 0.01

    def test_duty_cycle_extends_lifetime(self):
        battery = BatterySpec(capacity_mah=3000, cycle_count=0)
        duty = DutyCycleProfile(
            active_pct=20, idle_pct=30, sleep_pct=50,
            active_current_ma=500, idle_current_ma=50, sleep_current_ma=2,
        )
        est = estimate_battery_lifetime(battery, duty)
        avg = 500 * 0.2 + 50 * 0.3 + 2 * 0.5
        expected_h = 3000 / avg
        assert abs(est.lifetime_hours - expected_h) < 0.01

    def test_degraded_battery(self):
        battery = BatterySpec(capacity_mah=3000, cycle_count=200, degradation_pct_per_100_cycles=2.0)
        duty = DutyCycleProfile(
            active_pct=100, idle_pct=0, sleep_pct=0,
            active_current_ma=500, idle_current_ma=0, sleep_current_ma=0,
        )
        est = estimate_battery_lifetime(battery, duty)
        effective = 3000 * (1 - 4.0 / 100)
        expected_h = effective / 500
        assert abs(est.lifetime_hours - expected_h) < 0.01

    def test_zero_current_infinite_lifetime(self):
        battery = BatterySpec(capacity_mah=3000)
        duty = DutyCycleProfile(
            active_pct=0, idle_pct=0, sleep_pct=100,
            active_current_ma=0, idle_current_ma=0, sleep_current_ma=0,
        )
        est = estimate_battery_lifetime(battery, duty)
        assert est.lifetime_hours == float("inf")

    def test_dict_input_battery(self):
        est = estimate_battery_lifetime(
            {"capacity_mah": 2000, "chemistry": "li_po"},
            {"active_pct": 100, "idle_pct": 0, "sleep_pct": 0,
             "active_current_ma": 400, "idle_current_ma": 0, "sleep_current_ma": 0},
        )
        assert abs(est.lifetime_hours - 5.0) < 0.01

    def test_mah_per_day(self):
        battery = BatterySpec(capacity_mah=3000)
        duty = DutyCycleProfile(
            active_pct=100, idle_pct=0, sleep_pct=0,
            active_current_ma=100, idle_current_ma=0, sleep_current_ma=0,
        )
        est = estimate_battery_lifetime(battery, duty)
        assert abs(est.mah_per_day - 2400.0) < 0.1

    def test_lifetime_estimate_to_dict(self):
        battery = BatterySpec(capacity_mah=3000)
        duty = DutyCycleProfile(active_pct=100, active_current_ma=500)
        est = estimate_battery_lifetime(battery, duty)
        d = est.to_dict()
        assert "lifetime_hours" in d
        assert "mah_per_day" in d
        assert "battery" in d
        assert "duty_cycle" in d


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  6. Feature power budget
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestFeatureBudget:
    def test_no_features_enabled(self):
        battery = BatterySpec(capacity_mah=3000)
        budget = compute_feature_power_budget([], battery)
        assert budget.total_avg_current_ma == budget.base_avg_current_ma
        for item in budget.items:
            assert not item.enabled
            assert item.extra_draw_ma == 0

    def test_wifi_enabled(self):
        battery = BatterySpec(capacity_mah=3000)
        budget = compute_feature_power_budget(["wifi_always_on"], battery)
        wifi_item = next(i for i in budget.items if i.toggle_id == "wifi_always_on")
        assert wifi_item.enabled
        assert wifi_item.extra_draw_ma == 180
        assert wifi_item.mah_per_day == 180 * 24

    def test_multiple_features_sum(self):
        battery = BatterySpec(capacity_mah=3000)
        features = ["wifi_always_on", "camera_streaming"]
        budget = compute_feature_power_budget(features, battery)
        wifi = next(i for i in budget.items if i.toggle_id == "wifi_always_on")
        cam = next(i for i in budget.items if i.toggle_id == "camera_streaming")
        assert wifi.enabled
        assert cam.enabled
        total_extra = wifi.extra_draw_ma + cam.extra_draw_ma
        assert abs(budget.total_avg_current_ma - budget.base_avg_current_ma - total_extra) < 0.01

    def test_lifetime_decreases_with_features(self):
        battery = BatterySpec(capacity_mah=3000)
        budget = compute_feature_power_budget(["camera_streaming"], battery)
        assert budget.adjusted_lifetime_hours < budget.base_lifetime_hours

    def test_budget_items_count_matches_toggles(self):
        battery = BatterySpec(capacity_mah=3000)
        budget = compute_feature_power_budget([], battery)
        toggles = list_feature_toggles()
        assert len(budget.items) == len(toggles)

    def test_budget_to_dict(self):
        battery = BatterySpec(capacity_mah=3000)
        budget = compute_feature_power_budget(["bt_advertising"], battery)
        d = budget.to_dict()
        assert "items" in d
        assert "total_mah_per_day" in d
        assert "adjusted_lifetime_hours" in d

    def test_dict_battery_input(self):
        budget = compute_feature_power_budget(
            ["wifi_always_on"],
            {"capacity_mah": 5000, "chemistry": "lifepo4"},
        )
        assert budget.battery.capacity_mah == 5000

    def test_lifetime_impact_positive_when_enabled(self):
        battery = BatterySpec(capacity_mah=3000)
        budget = compute_feature_power_budget(["ai_inference"], battery)
        ai = next(i for i in budget.items if i.toggle_id == "ai_inference")
        assert ai.lifetime_impact_hours > 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  7. Doc suite generator integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDocSuiteIntegration:
    def test_register_and_get_certs(self):
        register_power_cert("IEC 62368-1", "Passed", "CERT-001")
        certs = get_power_certs()
        assert len(certs) == 1
        assert certs[0]["standard"] == "IEC 62368-1"
        assert certs[0]["status"] == "Passed"

    def test_clear_certs(self):
        register_power_cert("UL 60950", "Pending")
        clear_power_certs()
        assert get_power_certs() == []

    def test_multiple_certs(self):
        register_power_cert("IEC 62368-1", "Passed")
        register_power_cert("EN 62311", "Pending")
        certs = get_power_certs()
        assert len(certs) == 2


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  8. Audit log integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestAudit:
    @pytest.mark.asyncio
    async def test_log_profiling_result(self):
        session = ProfilingSession(
            session_id="test-123",
            adc_id="ina219",
            status=ProfilingStatus.completed,
            avg_current_ma=150,
        )
        mock_audit = MagicMock()
        mock_audit.log = AsyncMock(return_value=42)
        with patch.dict("sys.modules", {"backend.audit": mock_audit}):
            result = await log_profiling_result(session)
            assert result == 42
            mock_audit.log.assert_called_once()

    @pytest.mark.asyncio
    async def test_log_lifetime_estimate(self):
        battery = BatterySpec(capacity_mah=3000)
        duty = DutyCycleProfile(active_pct=100, active_current_ma=500)
        est = estimate_battery_lifetime(battery, duty)
        mock_audit = MagicMock()
        mock_audit.log = AsyncMock(return_value=43)
        with patch.dict("sys.modules", {"backend.audit": mock_audit}):
            result = await log_lifetime_estimate(est)
            assert result == 43

    def test_log_profiling_result_sync_no_loop(self):
        session = ProfilingSession(session_id="x", adc_id="y", status=ProfilingStatus.pending)
        log_profiling_result_sync(session)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  9. Edge cases
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestEdgeCases:
    def test_sleep_state_enum_values(self):
        assert SleepState.s0_active.value == "s0_active"
        assert SleepState.s5_off.value == "s5_off"

    def test_transition_direction_enum(self):
        assert TransitionDirection.entry.value == "entry"
        assert TransitionDirection.exit.value == "exit"

    def test_profiling_status_enum(self):
        assert ProfilingStatus.completed.value == "completed"
        assert ProfilingStatus.error.value == "error"

    def test_adc_interface_enum(self):
        assert ADCInterface.i2c.value == "i2c"
        assert ADCInterface.internal.value == "internal"

    def test_battery_spec_to_dict(self):
        b = BatterySpec(capacity_mah=5000, chemistry="lifepo4")
        d = b.to_dict()
        assert d["capacity_mah"] == 5000
        assert "effective_capacity_mah" in d

    def test_duty_cycle_to_dict(self):
        dc = DutyCycleProfile()
        d = dc.to_dict()
        assert "avg_current_ma" in d

    def test_feature_budget_item_to_dict(self):
        item = FeaturePowerBudgetItem(
            toggle_id="test", name="Test", enabled=True,
            extra_draw_ma=100, mah_per_day=2400, lifetime_impact_hours=5,
        )
        d = item.to_dict()
        assert d["enabled"] is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  10. REST endpoint smoke tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRESTEndpoints:
    @pytest.fixture
    def client(self):
        from backend import auth as _au
        from backend.routers.power import router
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(router)

        mock_user = MagicMock()
        mock_user.role = "admin"

        async def _fake_dep():
            return mock_user

        app.dependency_overrides[_au.require_operator] = _fake_dep
        app.dependency_overrides[_au.require_admin] = _fake_dep
        yield TestClient(app)
        app.dependency_overrides.clear()

    def test_get_sleep_states(self, client):
        resp = client.get("/power/sleep-states")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 6

    def test_get_domains(self, client):
        resp = client.get("/power/domains")
        assert resp.status_code == 200
        assert resp.json()["count"] == 10

    def test_get_adc(self, client):
        resp = client.get("/power/adc")
        assert resp.status_code == 200
        assert resp.json()["count"] == 4

    def test_get_features(self, client):
        resp = client.get("/power/features")
        assert resp.status_code == 200
        assert resp.json()["count"] == 8

    def test_get_chemistries(self, client):
        resp = client.get("/power/chemistries")
        assert resp.status_code == 200
        assert resp.json()["count"] == 4

    def test_post_lifetime(self, client):
        resp = client.post("/power/lifetime", json={
            "battery": {"capacity_mah": 3000, "chemistry": "li_ion"},
            "duty_cycle": {
                "active_pct": 20, "idle_pct": 30, "sleep_pct": 50,
                "active_current_ma": 500, "idle_current_ma": 50, "sleep_current_ma": 2,
            },
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["lifetime_hours"] > 0

    def test_post_budget(self, client):
        resp = client.post("/power/budget", json={
            "enabled_features": ["wifi_always_on"],
            "battery": {"capacity_mah": 3000, "chemistry": "li_ion"},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 8


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  11. Acceptance: synthetic current trace → correct lifetime estimate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSyntheticTraceAcceptance:
    """End-to-end: generate synthetic current trace, profile it, estimate lifetime."""

    def _make_synthetic_trace(self, duration_s: int = 3600) -> list[dict[str, Any]]:
        """Simulate 1 hour: 20% active (500 mA), 30% idle (50 mA), 50% sleep (2 mA)."""
        trace: list[dict[str, Any]] = []
        t = 0.0
        step = 1.0

        active_end = duration_s * 0.2
        idle_end = active_end + duration_s * 0.3

        while t < duration_s:
            if t < active_end:
                current = 500.0
            elif t < idle_end:
                current = 50.0
            else:
                current = 2.0
            trace.append({"timestamp_s": t, "current_ma": current, "voltage_v": 3.7})
            t += step

        return trace

    def test_synthetic_trace_lifetime(self):
        trace = self._make_synthetic_trace(3600)

        session = sample_current("ina226", 3600.0, raw_samples=trace)
        assert session.status == ProfilingStatus.completed
        assert session.sample_count == 3600

        expected_avg = 500 * 0.2 + 50 * 0.3 + 2 * 0.5
        assert abs(session.avg_current_ma - expected_avg) < 2.0

        battery = BatterySpec(capacity_mah=3000, cycle_count=0)
        duty = DutyCycleProfile(
            active_pct=20, idle_pct=30, sleep_pct=50,
            active_current_ma=session.avg_current_ma,
            idle_current_ma=50, sleep_current_ma=2,
        )

        est = estimate_battery_lifetime(battery, duty)
        expected_h = 3000 / duty.avg_current_ma
        assert abs(est.lifetime_hours - expected_h) < 0.5
        assert est.mah_per_day > 0

    def test_synthetic_trace_with_features(self):
        battery = BatterySpec(capacity_mah=5000)

        budget_none = compute_feature_power_budget([], battery)
        budget_wifi = compute_feature_power_budget(["wifi_always_on"], battery)
        budget_all = compute_feature_power_budget(
            ["wifi_always_on", "camera_streaming", "ai_inference"],
            battery,
        )

        assert budget_wifi.adjusted_lifetime_hours < budget_none.adjusted_lifetime_hours
        assert budget_all.adjusted_lifetime_hours < budget_wifi.adjusted_lifetime_hours
        assert budget_all.total_mah_per_day > budget_wifi.total_mah_per_day

    def test_synthetic_trace_transitions(self):
        trace = self._make_synthetic_trace(3600)
        events = detect_sleep_transitions(trace)
        assert len(events) >= 2
        assert events[0].direction == "entry"

    def test_full_pipeline(self):
        """Full pipeline: trace → profiling → transitions → lifetime → budget."""
        trace = self._make_synthetic_trace(3600)

        session = sample_current("ina226", 3600.0, raw_samples=trace)
        assert session.status == ProfilingStatus.completed

        transitions = detect_sleep_transitions(trace)
        assert len(transitions) >= 1

        battery = BatterySpec(capacity_mah=4000, cycle_count=50)
        est = estimate_battery_lifetime(
            battery,
            {"active_pct": 20, "idle_pct": 30, "sleep_pct": 50,
             "active_current_ma": session.avg_current_ma,
             "idle_current_ma": 50, "sleep_current_ma": 2},
        )
        assert est.lifetime_hours > 0
        assert est.mah_per_day > 0

        budget = compute_feature_power_budget(
            ["wifi_always_on", "bt_advertising"],
            battery,
        )
        assert budget.adjusted_lifetime_hours < est.lifetime_hours or \
               abs(budget.adjusted_lifetime_hours - est.lifetime_hours) < 1
