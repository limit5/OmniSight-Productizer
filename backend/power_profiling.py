"""C11 — L4-CORE-11 Power / battery profiling (#225).

Sleep-state transition detector, current profiling sampler,
battery lifetime model, and per-feature power budget analysis.

Provides:
  - Sleep-state transition detection (entry/exit event trace)
  - Current profiling sampler (external shunt ADC integration)
  - Battery lifetime model (capacity × avg draw × duty cycle)
  - Per-feature mAh/day breakdown
  - get_power_certs() for doc_suite_generator integration

Public API:
    states   = list_sleep_states()
    events   = detect_sleep_transitions(trace)
    samples  = sample_current(adc_config, duration_s)
    estimate = estimate_battery_lifetime(battery, profile, duty_cycle)
    budget   = compute_feature_power_budget(features, battery)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_POWER_PROFILES_PATH = _PROJECT_ROOT / "configs" / "power_profiles.yaml"


# -- Enums --

class SleepState(str, Enum):
    s0_active = "s0_active"
    s1_idle = "s1_idle"
    s2_standby = "s2_standby"
    s3_suspend = "s3_suspend"
    s4_hibernate = "s4_hibernate"
    s5_off = "s5_off"


class TransitionDirection(str, Enum):
    entry = "entry"
    exit = "exit"


class ProfilingStatus(str, Enum):
    running = "running"
    completed = "completed"
    error = "error"
    pending = "pending"


class ADCInterface(str, Enum):
    i2c = "i2c"
    spi = "spi"
    internal = "internal"


# -- Data models --

@dataclass
class SleepStateDef:
    state_id: str
    name: str
    description: str = ""
    typical_draw_pct: float = 100.0
    wake_latency_ms: float = 0.0
    order: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "state_id": self.state_id,
            "name": self.name,
            "description": self.description,
            "typical_draw_pct": self.typical_draw_pct,
            "wake_latency_ms": self.wake_latency_ms,
            "order": self.order,
        }


@dataclass
class PowerDomainDef:
    domain_id: str
    name: str
    description: str = ""
    typical_active_ma: float = 0.0
    typical_sleep_ma: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain_id": self.domain_id,
            "name": self.name,
            "description": self.description,
            "typical_active_ma": self.typical_active_ma,
            "typical_sleep_ma": self.typical_sleep_ma,
        }


@dataclass
class ADCConfig:
    adc_id: str
    name: str
    description: str = ""
    interface: str = "i2c"
    max_current_a: float = 3.2
    resolution_bits: int = 12
    sample_rate_hz: int = 500
    shunt_resistor_ohm: float = 0.1

    @property
    def lsb_current_a(self) -> float:
        return self.max_current_a / (2 ** self.resolution_bits)

    def to_dict(self) -> dict[str, Any]:
        return {
            "adc_id": self.adc_id,
            "name": self.name,
            "description": self.description,
            "interface": self.interface,
            "max_current_a": self.max_current_a,
            "resolution_bits": self.resolution_bits,
            "sample_rate_hz": self.sample_rate_hz,
            "shunt_resistor_ohm": self.shunt_resistor_ohm,
            "lsb_current_a": self.lsb_current_a,
        }


@dataclass
class SleepTransitionEvent:
    timestamp_s: float
    from_state: str
    to_state: str
    direction: str
    duration_in_state_s: float = 0.0
    current_ma_before: float = 0.0
    current_ma_after: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp_s": self.timestamp_s,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "direction": self.direction,
            "duration_in_state_s": self.duration_in_state_s,
            "current_ma_before": self.current_ma_before,
            "current_ma_after": self.current_ma_after,
        }


@dataclass
class CurrentSample:
    timestamp_s: float
    current_ma: float
    voltage_v: float = 0.0
    power_mw: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp_s": self.timestamp_s,
            "current_ma": self.current_ma,
            "voltage_v": self.voltage_v,
            "power_mw": self.power_mw,
        }


@dataclass
class ProfilingSession:
    session_id: str
    adc_id: str
    status: ProfilingStatus = ProfilingStatus.pending
    start_time: float = field(default_factory=time.time)
    duration_s: float = 0.0
    sample_count: int = 0
    samples: list[CurrentSample] = field(default_factory=list)
    avg_current_ma: float = 0.0
    peak_current_ma: float = 0.0
    min_current_ma: float = 0.0
    total_charge_mah: float = 0.0
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "adc_id": self.adc_id,
            "status": self.status.value,
            "start_time": self.start_time,
            "duration_s": self.duration_s,
            "sample_count": self.sample_count,
            "avg_current_ma": self.avg_current_ma,
            "peak_current_ma": self.peak_current_ma,
            "min_current_ma": self.min_current_ma,
            "total_charge_mah": self.total_charge_mah,
            "message": self.message,
        }


@dataclass
class BatterySpec:
    chemistry: str = "li_ion"
    capacity_mah: float = 3000.0
    nominal_voltage_v: float = 3.7
    min_voltage_v: float = 3.0
    max_voltage_v: float = 4.2
    cycle_count: int = 0
    degradation_pct_per_100_cycles: float = 2.0

    @property
    def effective_capacity_mah(self) -> float:
        degradation = (self.cycle_count / 100.0) * self.degradation_pct_per_100_cycles
        degradation = min(degradation, 80.0)
        return self.capacity_mah * (1.0 - degradation / 100.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chemistry": self.chemistry,
            "capacity_mah": self.capacity_mah,
            "nominal_voltage_v": self.nominal_voltage_v,
            "min_voltage_v": self.min_voltage_v,
            "max_voltage_v": self.max_voltage_v,
            "cycle_count": self.cycle_count,
            "degradation_pct_per_100_cycles": self.degradation_pct_per_100_cycles,
            "effective_capacity_mah": self.effective_capacity_mah,
        }


@dataclass
class DutyCycleProfile:
    active_pct: float = 20.0
    idle_pct: float = 30.0
    sleep_pct: float = 50.0
    active_current_ma: float = 500.0
    idle_current_ma: float = 50.0
    sleep_current_ma: float = 2.0

    @property
    def avg_current_ma(self) -> float:
        return (
            self.active_current_ma * self.active_pct / 100.0
            + self.idle_current_ma * self.idle_pct / 100.0
            + self.sleep_current_ma * self.sleep_pct / 100.0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "active_pct": self.active_pct,
            "idle_pct": self.idle_pct,
            "sleep_pct": self.sleep_pct,
            "active_current_ma": self.active_current_ma,
            "idle_current_ma": self.idle_current_ma,
            "sleep_current_ma": self.sleep_current_ma,
            "avg_current_ma": self.avg_current_ma,
        }


@dataclass
class LifetimeEstimate:
    battery: BatterySpec
    duty_cycle: DutyCycleProfile
    avg_current_ma: float = 0.0
    lifetime_hours: float = 0.0
    lifetime_days: float = 0.0
    mah_per_day: float = 0.0
    confidence: str = "estimated"
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "battery": self.battery.to_dict(),
            "duty_cycle": self.duty_cycle.to_dict(),
            "avg_current_ma": round(self.avg_current_ma, 3),
            "lifetime_hours": round(self.lifetime_hours, 2),
            "lifetime_days": round(self.lifetime_days, 3),
            "mah_per_day": round(self.mah_per_day, 2),
            "confidence": self.confidence,
            "message": self.message,
        }


@dataclass
class FeatureToggleDef:
    toggle_id: str
    name: str
    description: str = ""
    domains_affected: list[str] = field(default_factory=list)
    extra_draw_ma: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "toggle_id": self.toggle_id,
            "name": self.name,
            "description": self.description,
            "domains_affected": self.domains_affected,
            "extra_draw_ma": self.extra_draw_ma,
        }


@dataclass
class FeaturePowerBudgetItem:
    toggle_id: str
    name: str
    enabled: bool = False
    extra_draw_ma: float = 0.0
    mah_per_day: float = 0.0
    lifetime_impact_hours: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "toggle_id": self.toggle_id,
            "name": self.name,
            "enabled": self.enabled,
            "extra_draw_ma": self.extra_draw_ma,
            "mah_per_day": round(self.mah_per_day, 2),
            "lifetime_impact_hours": round(self.lifetime_impact_hours, 2),
        }


@dataclass
class FeaturePowerBudget:
    battery: BatterySpec
    base_avg_current_ma: float = 0.0
    total_avg_current_ma: float = 0.0
    base_lifetime_hours: float = 0.0
    adjusted_lifetime_hours: float = 0.0
    total_mah_per_day: float = 0.0
    items: list[FeaturePowerBudgetItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "battery": self.battery.to_dict(),
            "base_avg_current_ma": round(self.base_avg_current_ma, 3),
            "total_avg_current_ma": round(self.total_avg_current_ma, 3),
            "base_lifetime_hours": round(self.base_lifetime_hours, 2),
            "adjusted_lifetime_hours": round(self.adjusted_lifetime_hours, 2),
            "total_mah_per_day": round(self.total_mah_per_day, 2),
            "items": [i.to_dict() for i in self.items],
        }


# -- Config loading (cached) --

_POWER_CACHE: dict | None = None


def _load_power_profiles() -> dict:
    global _POWER_CACHE
    if _POWER_CACHE is None:
        try:
            _POWER_CACHE = yaml.safe_load(
                _POWER_PROFILES_PATH.read_text(encoding="utf-8")
            )
        except Exception as exc:
            logger.warning(
                "power_profiles.yaml load failed: %s — using empty config", exc
            )
            _POWER_CACHE = {
                "sleep_states": {},
                "power_domains": {},
                "adc_configurations": {},
                "feature_toggles": {},
                "battery_chemistries": {},
            }
    return _POWER_CACHE


def reload_power_profiles_for_tests() -> None:
    global _POWER_CACHE
    _POWER_CACHE = None


def _parse_sleep_state(state_id: str, data: dict) -> SleepStateDef:
    return SleepStateDef(
        state_id=state_id,
        name=data.get("name", state_id),
        description=data.get("description", ""),
        typical_draw_pct=float(data.get("typical_draw_pct", 100)),
        wake_latency_ms=float(data.get("wake_latency_ms", 0)),
        order=int(data.get("order", 0)),
    )


def _parse_domain(domain_id: str, data: dict) -> PowerDomainDef:
    return PowerDomainDef(
        domain_id=domain_id,
        name=data.get("name", domain_id),
        description=data.get("description", ""),
        typical_active_ma=float(data.get("typical_active_ma", 0)),
        typical_sleep_ma=float(data.get("typical_sleep_ma", 0)),
    )


def _parse_adc(adc_id: str, data: dict) -> ADCConfig:
    return ADCConfig(
        adc_id=adc_id,
        name=data.get("name", adc_id),
        description=data.get("description", ""),
        interface=data.get("interface", "i2c"),
        max_current_a=float(data.get("max_current_a", 3.2)),
        resolution_bits=int(data.get("resolution_bits", 12)),
        sample_rate_hz=int(data.get("sample_rate_hz", 500)),
        shunt_resistor_ohm=float(data.get("shunt_resistor_ohm", 0.1)),
    )


def _parse_feature_toggle(toggle_id: str, data: dict) -> FeatureToggleDef:
    return FeatureToggleDef(
        toggle_id=toggle_id,
        name=data.get("name", toggle_id),
        description=data.get("description", ""),
        domains_affected=data.get("domains_affected", []),
        extra_draw_ma=float(data.get("extra_draw_ma", 0)),
    )


# -- Public config queries --

def list_sleep_states() -> list[SleepStateDef]:
    raw = _load_power_profiles().get("sleep_states", {})
    states = [_parse_sleep_state(k, v) for k, v in raw.items()]
    states.sort(key=lambda s: s.order)
    return states


def get_sleep_state(state_id: str) -> SleepStateDef | None:
    raw = _load_power_profiles().get("sleep_states", {})
    if state_id not in raw:
        return None
    return _parse_sleep_state(state_id, raw[state_id])


def list_power_domains() -> list[PowerDomainDef]:
    raw = _load_power_profiles().get("power_domains", {})
    return [_parse_domain(k, v) for k, v in raw.items()]


def get_power_domain(domain_id: str) -> PowerDomainDef | None:
    raw = _load_power_profiles().get("power_domains", {})
    if domain_id not in raw:
        return None
    return _parse_domain(domain_id, raw[domain_id])


def list_adc_configs() -> list[ADCConfig]:
    raw = _load_power_profiles().get("adc_configurations", {})
    return [_parse_adc(k, v) for k, v in raw.items()]


def get_adc_config(adc_id: str) -> ADCConfig | None:
    raw = _load_power_profiles().get("adc_configurations", {})
    if adc_id not in raw:
        return None
    return _parse_adc(adc_id, raw[adc_id])


def list_feature_toggles() -> list[FeatureToggleDef]:
    raw = _load_power_profiles().get("feature_toggles", {})
    return [_parse_feature_toggle(k, v) for k, v in raw.items()]


def get_feature_toggle(toggle_id: str) -> FeatureToggleDef | None:
    raw = _load_power_profiles().get("feature_toggles", {})
    if toggle_id not in raw:
        return None
    return _parse_feature_toggle(toggle_id, raw[toggle_id])


def get_battery_chemistry(chemistry_id: str) -> dict[str, Any] | None:
    raw = _load_power_profiles().get("battery_chemistries", {})
    if chemistry_id not in raw:
        return None
    return raw[chemistry_id]


def list_battery_chemistries() -> list[dict[str, Any]]:
    raw = _load_power_profiles().get("battery_chemistries", {})
    return [{"chemistry_id": k, **v} for k, v in raw.items()]


# -- Sleep-state transition detector --

def detect_sleep_transitions(
    trace: list[dict[str, Any]],
) -> list[SleepTransitionEvent]:
    """Detect sleep-state transitions from a timestamped current trace.

    Each trace entry: {"timestamp_s": float, "current_ma": float}
    Transitions are inferred by mapping current levels to known sleep states.
    """
    if not trace or len(trace) < 2:
        return []

    states = list_sleep_states()
    if not states:
        return []

    domains = list_power_domains()
    total_active_ma = sum(d.typical_active_ma for d in domains) if domains else 500.0
    if total_active_ma <= 0:
        total_active_ma = 500.0

    thresholds = []
    for s in sorted(states, key=lambda x: x.order):
        threshold_ma = total_active_ma * s.typical_draw_pct / 100.0
        thresholds.append((s.state_id, threshold_ma))

    def classify_state(current_ma: float) -> str:
        best_state = thresholds[0][0]
        best_diff = abs(current_ma - thresholds[0][1])
        for state_id, threshold in thresholds[1:]:
            diff = abs(current_ma - threshold)
            if diff < best_diff:
                best_diff = diff
                best_state = state_id
        return best_state

    events: list[SleepTransitionEvent] = []
    prev_state = classify_state(trace[0].get("current_ma", 0))
    prev_ts = trace[0].get("timestamp_s", 0.0)
    prev_current = trace[0].get("current_ma", 0.0)

    for sample in trace[1:]:
        ts = sample.get("timestamp_s", 0.0)
        current_ma = sample.get("current_ma", 0.0)
        cur_state = classify_state(current_ma)

        if cur_state != prev_state:
            state_def_from = get_sleep_state(prev_state)
            state_def_to = get_sleep_state(cur_state)
            order_from = state_def_from.order if state_def_from else 0
            order_to = state_def_to.order if state_def_to else 0

            direction = (
                TransitionDirection.entry.value
                if order_to > order_from
                else TransitionDirection.exit.value
            )

            events.append(SleepTransitionEvent(
                timestamp_s=ts,
                from_state=prev_state,
                to_state=cur_state,
                direction=direction,
                duration_in_state_s=ts - prev_ts,
                current_ma_before=prev_current,
                current_ma_after=current_ma,
            ))
            prev_state = cur_state
            prev_ts = ts
        prev_current = current_ma

    return events


# -- Current profiling sampler --

def sample_current(
    adc_id: str,
    duration_s: float = 10.0,
    *,
    raw_samples: list[dict[str, Any]] | None = None,
) -> ProfilingSession:
    """Sample current using an external shunt ADC.

    In production this interfaces with real hardware via I2C/SPI.
    For simulation/testing, pass raw_samples directly.
    """
    adc = get_adc_config(adc_id)
    if adc is None:
        return ProfilingSession(
            session_id=f"err-{int(time.time())}",
            adc_id=adc_id,
            status=ProfilingStatus.error,
            message=f"Unknown ADC: {adc_id!r}. Available: {[a.adc_id for a in list_adc_configs()]}",
        )

    session_id = f"prof-{adc_id}-{int(time.time())}"

    if raw_samples is not None:
        return _process_raw_samples(session_id, adc, raw_samples, duration_s)

    expected_samples = int(adc.sample_rate_hz * duration_s)
    return ProfilingSession(
        session_id=session_id,
        adc_id=adc_id,
        status=ProfilingStatus.pending,
        duration_s=duration_s,
        sample_count=expected_samples,
        message=f"Stub: {adc.name} @ {adc.sample_rate_hz} Hz for {duration_s}s — "
                f"awaiting hardware connection. Interface: {adc.interface}, "
                f"shunt: {adc.shunt_resistor_ohm} Ω, max: {adc.max_current_a} A",
    )


def _process_raw_samples(
    session_id: str,
    adc: ADCConfig,
    raw_samples: list[dict[str, Any]],
    duration_s: float,
) -> ProfilingSession:
    samples: list[CurrentSample] = []
    currents: list[float] = []

    for s in raw_samples:
        current_ma = float(s.get("current_ma", 0))
        voltage_v = float(s.get("voltage_v", adc.max_current_a * adc.shunt_resistor_ohm))
        ts = float(s.get("timestamp_s", 0))
        power_mw = current_ma * voltage_v
        samples.append(CurrentSample(
            timestamp_s=ts,
            current_ma=current_ma,
            voltage_v=voltage_v,
            power_mw=power_mw,
        ))
        currents.append(current_ma)

    if not currents:
        return ProfilingSession(
            session_id=session_id,
            adc_id=adc.adc_id,
            status=ProfilingStatus.error,
            message="No valid samples in raw data",
        )

    avg_ma = sum(currents) / len(currents)
    peak_ma = max(currents)
    min_ma = min(currents)

    actual_duration = duration_s
    if len(samples) >= 2:
        actual_duration = samples[-1].timestamp_s - samples[0].timestamp_s
        if actual_duration <= 0:
            actual_duration = duration_s

    total_charge_mah = avg_ma * actual_duration / 3600.0

    return ProfilingSession(
        session_id=session_id,
        adc_id=adc.adc_id,
        status=ProfilingStatus.completed,
        duration_s=actual_duration,
        sample_count=len(samples),
        samples=samples,
        avg_current_ma=avg_ma,
        peak_current_ma=peak_ma,
        min_current_ma=min_ma,
        total_charge_mah=total_charge_mah,
        message=f"Processed {len(samples)} samples over {actual_duration:.1f}s — "
                f"avg {avg_ma:.1f} mA, peak {peak_ma:.1f} mA",
    )


# -- Battery lifetime model --

def estimate_battery_lifetime(
    battery: BatterySpec | dict[str, Any],
    duty_cycle: DutyCycleProfile | dict[str, Any],
) -> LifetimeEstimate:
    """Estimate battery lifetime from capacity × average draw × duty cycle.

    Args:
        battery: BatterySpec or dict with capacity_mah, chemistry, etc.
        duty_cycle: DutyCycleProfile or dict with active/idle/sleep pct + currents.
    """
    if isinstance(battery, dict):
        chem = battery.get("chemistry", "li_ion")
        chem_data = get_battery_chemistry(chem)
        battery = BatterySpec(
            chemistry=chem,
            capacity_mah=float(battery.get("capacity_mah", 3000)),
            nominal_voltage_v=float(battery.get("nominal_voltage_v",
                                    chem_data.get("nominal_voltage_v", 3.7) if chem_data else 3.7)),
            min_voltage_v=float(battery.get("min_voltage_v",
                                chem_data.get("min_voltage_v", 3.0) if chem_data else 3.0)),
            max_voltage_v=float(battery.get("max_voltage_v",
                                chem_data.get("max_voltage_v", 4.2) if chem_data else 4.2)),
            cycle_count=int(battery.get("cycle_count", 0)),
            degradation_pct_per_100_cycles=float(battery.get("degradation_pct_per_100_cycles",
                                                  chem_data.get("cycle_degradation_pct_per_100", 2.0)
                                                  if chem_data else 2.0)),
        )

    if isinstance(duty_cycle, dict):
        duty_cycle = DutyCycleProfile(
            active_pct=float(duty_cycle.get("active_pct", 20)),
            idle_pct=float(duty_cycle.get("idle_pct", 30)),
            sleep_pct=float(duty_cycle.get("sleep_pct", 50)),
            active_current_ma=float(duty_cycle.get("active_current_ma", 500)),
            idle_current_ma=float(duty_cycle.get("idle_current_ma", 50)),
            sleep_current_ma=float(duty_cycle.get("sleep_current_ma", 2)),
        )

    avg_current = duty_cycle.avg_current_ma
    effective_cap = battery.effective_capacity_mah

    if avg_current <= 0:
        return LifetimeEstimate(
            battery=battery,
            duty_cycle=duty_cycle,
            avg_current_ma=0,
            lifetime_hours=float("inf"),
            lifetime_days=float("inf"),
            mah_per_day=0,
            confidence="estimated",
            message="Zero average current — infinite battery lifetime",
        )

    lifetime_h = effective_cap / avg_current
    lifetime_d = lifetime_h / 24.0
    mah_per_day = avg_current * 24.0

    return LifetimeEstimate(
        battery=battery,
        duty_cycle=duty_cycle,
        avg_current_ma=avg_current,
        lifetime_hours=lifetime_h,
        lifetime_days=lifetime_d,
        mah_per_day=mah_per_day,
        confidence="estimated",
        message=f"Estimated {lifetime_d:.1f} days ({lifetime_h:.0f}h) at "
                f"{avg_current:.1f} mA avg — {mah_per_day:.0f} mAh/day "
                f"from {effective_cap:.0f} mAh effective capacity",
    )


# -- Feature power budget (mAh/day per feature toggle) --

def compute_feature_power_budget(
    enabled_features: list[str],
    battery: BatterySpec | dict[str, Any],
    base_duty_cycle: DutyCycleProfile | dict[str, Any] | None = None,
) -> FeaturePowerBudget:
    """Compute mAh/day impact of each feature toggle on battery lifetime.

    Args:
        enabled_features: List of feature toggle IDs that are enabled.
        battery: Battery specification.
        base_duty_cycle: Base duty cycle without extra features.
    """
    if isinstance(battery, dict):
        chem = battery.get("chemistry", "li_ion")
        chem_data = get_battery_chemistry(chem)
        battery = BatterySpec(
            chemistry=chem,
            capacity_mah=float(battery.get("capacity_mah", 3000)),
            nominal_voltage_v=float(battery.get("nominal_voltage_v",
                                    chem_data.get("nominal_voltage_v", 3.7) if chem_data else 3.7)),
            cycle_count=int(battery.get("cycle_count", 0)),
            degradation_pct_per_100_cycles=float(battery.get("degradation_pct_per_100_cycles",
                                                  chem_data.get("cycle_degradation_pct_per_100", 2.0)
                                                  if chem_data else 2.0)),
        )

    if base_duty_cycle is None:
        base_duty_cycle = DutyCycleProfile()
    elif isinstance(base_duty_cycle, dict):
        base_duty_cycle = DutyCycleProfile(
            active_pct=float(base_duty_cycle.get("active_pct", 20)),
            idle_pct=float(base_duty_cycle.get("idle_pct", 30)),
            sleep_pct=float(base_duty_cycle.get("sleep_pct", 50)),
            active_current_ma=float(base_duty_cycle.get("active_current_ma", 500)),
            idle_current_ma=float(base_duty_cycle.get("idle_current_ma", 50)),
            sleep_current_ma=float(base_duty_cycle.get("sleep_current_ma", 2)),
        )

    base_avg = base_duty_cycle.avg_current_ma
    effective_cap = battery.effective_capacity_mah

    base_lifetime_h = effective_cap / base_avg if base_avg > 0 else float("inf")

    all_toggles = list_feature_toggles()
    {t.toggle_id: t for t in all_toggles}

    items: list[FeaturePowerBudgetItem] = []
    total_extra_ma = 0.0

    for toggle in all_toggles:
        enabled = toggle.toggle_id in enabled_features
        extra = toggle.extra_draw_ma if enabled else 0.0
        mah_day = extra * 24.0
        lifetime_impact = 0.0
        if extra > 0 and base_avg > 0:
            new_avg = base_avg + extra
            new_lifetime = effective_cap / new_avg if new_avg > 0 else float("inf")
            lifetime_impact = base_lifetime_h - new_lifetime

        items.append(FeaturePowerBudgetItem(
            toggle_id=toggle.toggle_id,
            name=toggle.name,
            enabled=enabled,
            extra_draw_ma=extra,
            mah_per_day=mah_day,
            lifetime_impact_hours=lifetime_impact,
        ))

        if enabled:
            total_extra_ma += extra

    total_avg = base_avg + total_extra_ma
    adjusted_lifetime = effective_cap / total_avg if total_avg > 0 else float("inf")
    total_mah_day = total_avg * 24.0

    return FeaturePowerBudget(
        battery=battery,
        base_avg_current_ma=base_avg,
        total_avg_current_ma=total_avg,
        base_lifetime_hours=base_lifetime_h,
        adjusted_lifetime_hours=adjusted_lifetime,
        total_mah_per_day=total_mah_day,
        items=items,
    )


# -- Doc suite generator integration --

_ACTIVE_POWER_CERTS: list[dict[str, Any]] = []


def register_power_cert(
    standard: str,
    status: str = "Pending",
    cert_id: str = "",
    details: dict[str, Any] | None = None,
) -> None:
    _ACTIVE_POWER_CERTS.append({
        "standard": standard,
        "status": status,
        "cert_id": cert_id,
        "details": details or {},
    })


def get_power_certs() -> list[dict[str, Any]]:
    return list(_ACTIVE_POWER_CERTS)


def clear_power_certs() -> None:
    _ACTIVE_POWER_CERTS.clear()


# -- Audit log integration --

async def log_profiling_result(session: ProfilingSession) -> Optional[int]:
    try:
        from backend import audit
        return await audit.log(
            action="power_profiling",
            entity_kind="profiling_session",
            entity_id=session.session_id,
            before=None,
            after=session.to_dict(),
            actor="power_profiling",
        )
    except Exception as exc:
        logger.warning("Failed to log profiling result to audit: %s", exc)
        return None


def log_profiling_result_sync(session: ProfilingSession) -> None:
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("log_profiling_result_sync skipped (no running loop)")
        return
    loop.create_task(log_profiling_result(session))


async def log_lifetime_estimate(estimate: LifetimeEstimate) -> Optional[int]:
    try:
        from backend import audit
        return await audit.log(
            action="battery_lifetime_estimate",
            entity_kind="lifetime_estimate",
            entity_id=f"est-{int(time.time())}",
            before=None,
            after=estimate.to_dict(),
            actor="power_profiling",
        )
    except Exception as exc:
        logger.warning("Failed to log lifetime estimate to audit: %s", exc)
        return None
