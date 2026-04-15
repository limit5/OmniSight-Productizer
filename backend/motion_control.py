"""C25 — L4-CORE-25 Motion control / G-code / CNC abstraction (#255).

Unified motion control framework: G-code interpreter, stepper driver abstraction,
heater PID loops, endstop handling, homing, and thermal runaway safety shutoff.

Supported stepper drivers: TMC2209 (UART/StallGuard), A4988, DRV8825.
G-code subset: G0, G1, G28, M104, M109, M140.
Heaters: hotend + heated bed with independent PID loops.

Public API:
    drivers      = list_stepper_drivers()
    axes_defs    = list_axes()
    heaters_defs = list_heaters()
    endstop_defs = list_endstop_types()
    gcode_defs   = list_gcode_commands()
    machine      = create_machine(config)
    machine.load_gcode(program)
    trace        = machine.execute()
    recipes      = list_test_recipes()
    report       = run_test_recipe(recipe_id)
    artifacts    = list_artifacts()
    verdict      = validate_gate()
"""

from __future__ import annotations

import logging
import math
import re
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _PROJECT_ROOT / "configs" / "motion_control.yaml"


# ── Enums ──────────────────────────────────────────────────────────────

class MotionDomain(str, Enum):
    gcode_interpreter = "gcode_interpreter"
    stepper_drivers = "stepper_drivers"
    heater_pid = "heater_pid"
    endstops = "endstops"
    thermal_safety = "thermal_safety"


class StepperDriverId(str, Enum):
    tmc2209 = "tmc2209"
    a4988 = "a4988"
    drv8825 = "drv8825"


class StepperInterface(str, Enum):
    uart = "uart"
    step_dir = "step_dir"


class AxisId(str, Enum):
    X = "X"
    Y = "Y"
    Z = "Z"
    E = "E"


class HeaterId(str, Enum):
    hotend = "hotend"
    bed = "bed"


class EndstopType(str, Enum):
    mechanical = "mechanical"
    optical = "optical"
    stallguard = "stallguard"


class HomingMode(str, Enum):
    single_probe = "single_probe"
    double_probe = "double_probe"


class MachineState(str, Enum):
    idle = "idle"
    running = "running"
    homing = "homing"
    heating = "heating"
    paused = "paused"
    error = "error"
    thermal_shutdown = "thermal_shutdown"


class GCodeType(str, Enum):
    rapid_move = "G0"
    linear_move = "G1"
    home = "G28"
    set_hotend_temp = "M104"
    wait_hotend_temp = "M109"
    set_bed_temp = "M140"


class TestStatus(str, Enum):
    passed = "passed"
    failed = "failed"
    pending = "pending"
    skipped = "skipped"
    error = "error"


class GateVerdict(str, Enum):
    passed = "passed"
    failed = "failed"
    error = "error"


# ── Data models ────────────────────────────────────────────────────────

@dataclass
class StepperDriverDef:
    driver_id: str
    name: str
    description: str = ""
    interface: str = "step_dir"
    microstep_options: list[int] = field(default_factory=list)
    default_microsteps: int = 16
    max_current_ma: int = 2000
    features: list[str] = field(default_factory=list)
    stall_threshold_range: Optional[list[int]] = None
    default_stall_threshold: Optional[int] = None


@dataclass
class AxisDef:
    axis_id: str
    name: str
    steps_per_mm: float = 80.0
    max_feedrate_mm_s: float = 300.0
    max_accel_mm_s2: float = 3000.0
    homing_feedrate_mm_s: float = 50.0
    travel_mm: float = 220.0


@dataclass
class HeaterDef:
    heater_id: str
    name: str
    description: str = ""
    max_temp_c: float = 300.0
    min_temp_c: float = 0.0
    pid_kp: float = 22.2
    pid_ki: float = 1.08
    pid_kd: float = 114.0
    thermal_runaway_period_s: float = 40.0
    thermal_runaway_hysteresis_c: float = 4.0
    thermal_runaway_max_deviation_c: float = 25.0
    thermal_runaway_grace_period_s: float = 60.0


@dataclass
class EndstopDef:
    endstop_type: str
    name: str
    description: str = ""
    debounce_ms: int = 10
    active_low: bool = True


@dataclass
class GCodeCommandDef:
    command_id: str
    name: str
    description: str = ""
    axes: list[str] = field(default_factory=list)
    parameters: list[str] = field(default_factory=list)


@dataclass
class GCodeLine:
    line_number: int
    raw: str
    command: str
    params: dict[str, float] = field(default_factory=dict)


@dataclass
class MotionStep:
    line_number: int
    command: str
    position: dict[str, float] = field(default_factory=dict)
    feedrate: float = 0.0
    hotend_target: Optional[float] = None
    bed_target: Optional[float] = None
    hotend_temp: Optional[float] = None
    bed_temp: Optional[float] = None
    event: str = ""
    timestamp_ms: float = 0.0


@dataclass
class MotionTrace:
    steps: list[MotionStep] = field(default_factory=list)
    total_distance_mm: float = 0.0
    total_time_ms: float = 0.0
    final_position: dict[str, float] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


@dataclass
class PIDState:
    target: float = 0.0
    current: float = 25.0
    output: float = 0.0
    integral: float = 0.0
    prev_error: float = 0.0
    enabled: bool = False


@dataclass
class AxisConfig:
    axis_id: str
    driver_id: str = "tmc2209"
    steps_per_mm: float = 80.0
    max_feedrate_mm_s: float = 300.0
    max_accel_mm_s2: float = 3000.0
    homing_feedrate_mm_s: float = 50.0
    travel_mm: float = 220.0
    endstop_type: str = "mechanical"
    homing_mode: str = "double_probe"
    inverted: bool = False
    microsteps: int = 16


@dataclass
class MachineConfig:
    axes: dict[str, AxisConfig] = field(default_factory=dict)
    hotend_max_temp: float = 300.0
    bed_max_temp: float = 120.0
    hotend_pid: tuple[float, float, float] = (22.2, 1.08, 114.0)
    bed_pid: tuple[float, float, float] = (70.0, 1.5, 600.0)
    thermal_runaway_enabled: bool = True
    thermal_runaway_period_s: float = 40.0
    thermal_runaway_hysteresis_c: float = 4.0
    thermal_runaway_max_deviation_c: float = 25.0
    thermal_runaway_grace_period_s: float = 60.0


@dataclass
class TestRecipeDef:
    recipe_id: str
    name: str
    description: str = ""
    domains: list[str] = field(default_factory=list)


@dataclass
class TestResult:
    recipe_id: str
    status: str
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    duration_ms: float = 0.0
    details: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ArtifactDef:
    artifact_id: str
    kind: str
    description: str = ""


# ── Config loader ──────────────────────────────────────────────────────

_cfg: dict[str, Any] | None = None


def _load_config() -> dict[str, Any]:
    global _cfg
    if _cfg is not None:
        return _cfg
    with open(_CONFIG_PATH) as f:
        raw = yaml.safe_load(f)
    _cfg = raw.get("motion_control", raw)
    return _cfg


def _get_cfg() -> dict[str, Any]:
    return _load_config()


# ── G-code parser ──────────────────────────────────────────────────────

_GCODE_RE = re.compile(
    r"^\s*([GMTNS]\d+)"
    r"((?:\s+[A-Z]-?[\d.]+)*)"
    r"\s*(?:;.*)?$",
    re.IGNORECASE,
)

_PARAM_RE = re.compile(r"([A-Z])(-?[\d.]+)", re.IGNORECASE)

_SUPPORTED_COMMANDS = {"G0", "G1", "G28", "M104", "M109", "M140"}


def parse_gcode_line(raw: str, line_number: int = 0) -> Optional[GCodeLine]:
    stripped = raw.strip()
    if not stripped or stripped.startswith(";"):
        return None

    m = _GCODE_RE.match(stripped)
    if m is None:
        return None

    command = m.group(1).upper()
    if command not in _SUPPORTED_COMMANDS:
        return None

    params: dict[str, float] = {}
    param_str = m.group(2) or ""
    for pm in _PARAM_RE.finditer(param_str):
        key = pm.group(1).upper()
        try:
            params[key] = float(pm.group(2))
        except ValueError:
            pass

    return GCodeLine(
        line_number=line_number,
        raw=stripped,
        command=command,
        params=params,
    )


def parse_gcode_program(program: str) -> list[GCodeLine]:
    lines: list[GCodeLine] = []
    for i, raw in enumerate(program.splitlines(), 1):
        parsed = parse_gcode_line(raw, i)
        if parsed is not None:
            lines.append(parsed)
    return lines


# ── Stepper driver abstraction ─────────────────────────────────────────

class StepperDriver(ABC):
    def __init__(self, driver_id: str, axis_id: str, config: AxisConfig):
        self.driver_id = driver_id
        self.axis_id = axis_id
        self._config = config
        self._enabled = False
        self._position_steps: int = 0
        self._microsteps = config.microsteps
        self._current_ma: int = 800
        self._step_count: int = 0

    @property
    def position_mm(self) -> float:
        return self._position_steps / (self._config.steps_per_mm * self._microsteps / 16)

    @abstractmethod
    def enable(self) -> bool: ...

    @abstractmethod
    def disable(self) -> bool: ...

    @abstractmethod
    def set_microsteps(self, microsteps: int) -> bool: ...

    @abstractmethod
    def set_current(self, current_ma: int) -> bool: ...

    def step(self, steps: int) -> int:
        if not self._enabled:
            return 0
        direction = 1 if steps >= 0 else -1
        if self._config.inverted:
            direction = -direction
        actual = abs(steps)
        self._position_steps += direction * actual
        self._step_count += actual
        return actual

    def move_to_mm(self, target_mm: float) -> int:
        steps_per_mm = self._config.steps_per_mm * self._microsteps / 16
        target_steps = int(round(target_mm * steps_per_mm))
        delta = target_steps - self._position_steps
        return self.step(delta)

    def home(self) -> None:
        self._position_steps = 0

    def get_status(self) -> dict[str, Any]:
        return {
            "driver_id": self.driver_id,
            "axis_id": self.axis_id,
            "enabled": self._enabled,
            "position_steps": self._position_steps,
            "position_mm": round(self.position_mm, 4),
            "microsteps": self._microsteps,
            "current_ma": self._current_ma,
            "step_count": self._step_count,
        }


class TMC2209Driver(StepperDriver):
    _VALID_MICROSTEPS = {1, 2, 4, 8, 16, 32, 64, 128, 256}
    _MAX_CURRENT = 2000

    def __init__(self, axis_id: str, config: AxisConfig):
        super().__init__("tmc2209", axis_id, config)
        self._stealthchop = True
        self._stall_threshold = 100

    def enable(self) -> bool:
        self._enabled = True
        return True

    def disable(self) -> bool:
        self._enabled = False
        return True

    def set_microsteps(self, microsteps: int) -> bool:
        if microsteps not in self._VALID_MICROSTEPS:
            return False
        self._microsteps = microsteps
        return True

    def set_current(self, current_ma: int) -> bool:
        if current_ma < 0 or current_ma > self._MAX_CURRENT:
            return False
        self._current_ma = current_ma
        return True

    def set_stall_threshold(self, threshold: int) -> bool:
        if threshold < 0 or threshold > 255:
            return False
        self._stall_threshold = threshold
        return True

    def set_stealthchop(self, enabled: bool) -> None:
        self._stealthchop = enabled

    def read_stall_value(self) -> int:
        return self._stall_threshold

    def get_status(self) -> dict[str, Any]:
        status = super().get_status()
        status["stealthchop"] = self._stealthchop
        status["stall_threshold"] = self._stall_threshold
        return status


class A4988Driver(StepperDriver):
    _VALID_MICROSTEPS = {1, 2, 4, 8, 16}
    _MAX_CURRENT = 2000

    def __init__(self, axis_id: str, config: AxisConfig):
        super().__init__("a4988", axis_id, config)
        self._sleep = False

    def enable(self) -> bool:
        self._sleep = False
        self._enabled = True
        return True

    def disable(self) -> bool:
        self._enabled = False
        return True

    def set_microsteps(self, microsteps: int) -> bool:
        if microsteps not in self._VALID_MICROSTEPS:
            return False
        self._microsteps = microsteps
        return True

    def set_current(self, current_ma: int) -> bool:
        if current_ma < 0 or current_ma > self._MAX_CURRENT:
            return False
        self._current_ma = current_ma
        return True

    def sleep(self) -> None:
        self._sleep = True
        self._enabled = False

    def get_status(self) -> dict[str, Any]:
        status = super().get_status()
        status["sleep"] = self._sleep
        return status


class DRV8825Driver(StepperDriver):
    _VALID_MICROSTEPS = {1, 2, 4, 8, 16, 32}
    _MAX_CURRENT = 2500

    def __init__(self, axis_id: str, config: AxisConfig):
        super().__init__("drv8825", axis_id, config)
        self._fault = False

    def enable(self) -> bool:
        if self._fault:
            return False
        self._enabled = True
        return True

    def disable(self) -> bool:
        self._enabled = False
        return True

    def set_microsteps(self, microsteps: int) -> bool:
        if microsteps not in self._VALID_MICROSTEPS:
            return False
        self._microsteps = microsteps
        return True

    def set_current(self, current_ma: int) -> bool:
        if current_ma < 0 or current_ma > self._MAX_CURRENT:
            return False
        self._current_ma = current_ma
        return True

    def clear_fault(self) -> None:
        self._fault = False

    def inject_fault(self) -> None:
        self._fault = True
        self._enabled = False

    def get_status(self) -> dict[str, Any]:
        status = super().get_status()
        status["fault"] = self._fault
        return status


_DRIVER_MAP: dict[str, type[StepperDriver]] = {
    StepperDriverId.tmc2209.value: TMC2209Driver,
    StepperDriverId.a4988.value: A4988Driver,
    StepperDriverId.drv8825.value: DRV8825Driver,
}


def create_stepper_driver(driver_id: str, axis_id: str, config: AxisConfig) -> StepperDriver:
    cls = _DRIVER_MAP.get(driver_id)
    if cls is None:
        raise ValueError(f"Unknown stepper driver: {driver_id}")
    return cls(axis_id, config)


# ── Heater + PID controller ───────────────────────────────────────────

class HeaterPID:
    def __init__(
        self,
        heater_id: str,
        kp: float,
        ki: float,
        kd: float,
        max_temp: float,
        min_temp: float = 0.0,
    ):
        self.heater_id = heater_id
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.max_temp = max_temp
        self.min_temp = min_temp
        self._state = PIDState()
        self._dt = 0.1
        self._output_min = 0.0
        self._output_max = 255.0
        self._windup_guard = 100.0

    @property
    def target(self) -> float:
        return self._state.target

    @property
    def current(self) -> float:
        return self._state.current

    @property
    def enabled(self) -> bool:
        return self._state.enabled

    @property
    def output(self) -> float:
        return self._state.output

    def set_target(self, temp: float) -> bool:
        if temp < self.min_temp or temp > self.max_temp:
            return False
        self._state.target = temp
        self._state.enabled = temp > 0
        return True

    def disable(self) -> None:
        self._state.target = 0.0
        self._state.enabled = False
        self._state.output = 0.0
        self._state.integral = 0.0
        self._state.prev_error = 0.0

    def update(self, current_temp: float, dt: float = 0.1) -> float:
        self._state.current = current_temp

        if not self._state.enabled:
            self._state.output = 0.0
            return 0.0

        error = self._state.target - current_temp

        self._state.integral += error * dt
        self._state.integral = max(
            -self._windup_guard,
            min(self._windup_guard, self._state.integral),
        )

        derivative = (error - self._state.prev_error) / dt if dt > 0 else 0.0
        self._state.prev_error = error

        output = (
            self.kp * error
            + self.ki * self._state.integral
            + self.kd * derivative
        )

        output = max(self._output_min, min(self._output_max, output))
        self._state.output = output
        return output

    def simulate_step(self, dt: float = 0.1, ambient: float = 25.0) -> float:
        if not self._state.enabled:
            cooling = 0.01 * (self._state.current - ambient) * dt
            self._state.current = max(ambient, self._state.current - cooling)
            return self._state.current

        output = self.update(self._state.current, dt)

        heat_gain = (output / self._output_max) * 8.0 * dt
        heat_loss = 0.01 * (self._state.current - ambient) * dt
        self._state.current += heat_gain - heat_loss
        self._state.current = max(self.min_temp, min(self.max_temp + 20, self._state.current))
        return self._state.current

    def get_status(self) -> dict[str, Any]:
        return {
            "heater_id": self.heater_id,
            "target": self._state.target,
            "current": round(self._state.current, 2),
            "output": round(self._state.output, 2),
            "enabled": self._state.enabled,
        }


# ── Endstop handling ──────────────────────────────────────────────────

class Endstop:
    def __init__(
        self,
        axis_id: str,
        endstop_type: str = "mechanical",
        active_low: bool = True,
        debounce_ms: int = 10,
        trigger_position_mm: float = 0.0,
    ):
        self.axis_id = axis_id
        self.endstop_type = endstop_type
        self.active_low = active_low
        self.debounce_ms = debounce_ms
        self.trigger_position_mm = trigger_position_mm
        self._triggered = False
        self._last_trigger_time: float = 0.0

    @property
    def triggered(self) -> bool:
        return self._triggered

    def update(self, position_mm: float) -> bool:
        raw = position_mm <= self.trigger_position_mm
        if self.active_low:
            raw = not raw

        now = time.monotonic()
        if raw != self._triggered:
            if (now - self._last_trigger_time) * 1000 >= self.debounce_ms:
                self._triggered = not self._triggered
                self._last_trigger_time = now

        return self._triggered

    def force_trigger(self) -> None:
        self._triggered = True
        self._last_trigger_time = time.monotonic()

    def reset(self) -> None:
        self._triggered = False

    def check_at_position(self, position_mm: float) -> bool:
        return position_mm <= self.trigger_position_mm

    def get_status(self) -> dict[str, Any]:
        return {
            "axis_id": self.axis_id,
            "endstop_type": self.endstop_type,
            "triggered": self._triggered,
            "trigger_position_mm": self.trigger_position_mm,
        }


# ── Thermal runaway monitor ──────────────────────────────────────────

class ThermalRunawayMonitor:
    """Two-phase thermal runaway detection (matches Marlin firmware approach).

    Phase 1 (heating up): after grace period, check that temperature is
    making progress — if it drops while heater is on, trip.

    Phase 2 (at target): once temperature has reached within max_deviation
    of target, switch to maintenance mode — trip if deviation exceeds
    max_deviation.
    """

    def __init__(
        self,
        heater_id: str,
        period_s: float = 40.0,
        hysteresis_c: float = 4.0,
        max_deviation_c: float = 15.0,
        grace_period_s: float = 60.0,
    ):
        self.heater_id = heater_id
        self.period_s = period_s
        self.hysteresis_c = hysteresis_c
        self.max_deviation_c = max_deviation_c
        self.grace_period_s = grace_period_s
        self._tripped = False
        self._heating_start_time: Optional[float] = None
        self._last_check_time: Optional[float] = None
        self._last_temp: Optional[float] = None
        self._reached_target = False
        self._fault_reason: str = ""

    @property
    def tripped(self) -> bool:
        return self._tripped

    @property
    def fault_reason(self) -> str:
        return self._fault_reason

    def start_monitoring(self, sim_time: Optional[float] = None) -> None:
        self._tripped = False
        self._fault_reason = ""
        self._reached_target = False
        t = sim_time if sim_time is not None else time.monotonic()
        self._heating_start_time = t
        self._last_check_time = t
        self._last_temp = None

    def check(
        self,
        current_temp: float,
        target_temp: float,
        heater_output: float,
        sim_time: Optional[float] = None,
    ) -> bool:
        if self._tripped:
            return True

        if target_temp <= 0:
            self._heating_start_time = None
            self._reached_target = False
            return False

        now = sim_time if sim_time is not None else time.monotonic()

        if self._heating_start_time is None:
            self._heating_start_time = now
            self._last_check_time = now
            self._last_temp = current_temp
            return False

        elapsed = now - self._heating_start_time

        if elapsed < self.grace_period_s:
            self._last_check_time = now
            self._last_temp = current_temp
            return False

        if not self._reached_target:
            if abs(current_temp - target_temp) <= self.max_deviation_c:
                self._reached_target = True
                self._last_check_time = now
                self._last_temp = current_temp
                return False

        if self._last_temp is not None:
            check_elapsed = now - (self._last_check_time or now)
            if check_elapsed >= self.period_s:
                if heater_output > 0 and current_temp < (self._last_temp - self.hysteresis_c):
                    self._tripped = True
                    self._fault_reason = (
                        f"Temperature dropping while heating: "
                        f"{self._last_temp:.1f}→{current_temp:.1f}°C "
                        f"over {check_elapsed:.0f}s"
                    )
                    return True
                self._last_check_time = now
                self._last_temp = current_temp

        if self._reached_target and abs(current_temp - target_temp) > self.max_deviation_c:
            self._tripped = True
            self._fault_reason = (
                f"Temperature deviation too large: "
                f"current={current_temp:.1f}°C target={target_temp:.1f}°C "
                f"deviation={abs(current_temp - target_temp):.1f}°C"
            )
            return True

        return False

    def reset(self) -> None:
        self._tripped = False
        self._fault_reason = ""
        self._heating_start_time = None
        self._last_check_time = None
        self._last_temp = None

    def get_status(self) -> dict[str, Any]:
        return {
            "heater_id": self.heater_id,
            "tripped": self._tripped,
            "fault_reason": self._fault_reason,
        }


# ── Machine (integrates all components) ───────────────────────────────

def _default_axes_config() -> dict[str, AxisConfig]:
    return {
        "X": AxisConfig(axis_id="X", steps_per_mm=80, max_feedrate_mm_s=300, max_accel_mm_s2=3000, homing_feedrate_mm_s=50, travel_mm=220),
        "Y": AxisConfig(axis_id="Y", steps_per_mm=80, max_feedrate_mm_s=300, max_accel_mm_s2=3000, homing_feedrate_mm_s=50, travel_mm=220),
        "Z": AxisConfig(axis_id="Z", steps_per_mm=400, max_feedrate_mm_s=5, max_accel_mm_s2=100, homing_feedrate_mm_s=3, travel_mm=250),
        "E": AxisConfig(axis_id="E", steps_per_mm=93, max_feedrate_mm_s=50, max_accel_mm_s2=5000, homing_feedrate_mm_s=0, travel_mm=0),
    }


class Machine:
    def __init__(self, config: Optional[MachineConfig] = None):
        if config is None:
            config = MachineConfig(axes=_default_axes_config())
        if not config.axes:
            config.axes = _default_axes_config()

        self._config = config
        self._state = MachineState.idle
        self._position: dict[str, float] = {"X": 0.0, "Y": 0.0, "Z": 0.0, "E": 0.0}
        self._feedrate: float = 60.0

        self._drivers: dict[str, StepperDriver] = {}
        for axis_id, ax_cfg in config.axes.items():
            self._drivers[axis_id] = create_stepper_driver(ax_cfg.driver_id, axis_id, ax_cfg)
            self._drivers[axis_id].enable()

        kp_h, ki_h, kd_h = config.hotend_pid
        self._hotend = HeaterPID("hotend", kp_h, ki_h, kd_h, config.hotend_max_temp)
        kp_b, ki_b, kd_b = config.bed_pid
        self._bed = HeaterPID("bed", kp_b, ki_b, kd_b, config.bed_max_temp)

        self._endstops: dict[str, Endstop] = {}
        for axis_id, ax_cfg in config.axes.items():
            if axis_id != "E":
                self._endstops[axis_id] = Endstop(
                    axis_id=axis_id,
                    endstop_type=ax_cfg.endstop_type,
                    trigger_position_mm=0.0,
                )

        self._thermal_monitors: dict[str, ThermalRunawayMonitor] = {}
        if config.thermal_runaway_enabled:
            self._thermal_monitors["hotend"] = ThermalRunawayMonitor(
                "hotend",
                period_s=config.thermal_runaway_period_s,
                hysteresis_c=config.thermal_runaway_hysteresis_c,
                max_deviation_c=config.thermal_runaway_max_deviation_c,
                grace_period_s=config.thermal_runaway_grace_period_s,
            )
            self._thermal_monitors["bed"] = ThermalRunawayMonitor(
                "bed",
                period_s=config.thermal_runaway_period_s,
                hysteresis_c=config.thermal_runaway_hysteresis_c,
                max_deviation_c=config.thermal_runaway_max_deviation_c,
                grace_period_s=config.thermal_runaway_grace_period_s,
            )

        self._program: list[GCodeLine] = []
        self._trace: MotionTrace = MotionTrace()
        self._sim_time: float = 0.0

    @property
    def state(self) -> MachineState:
        return self._state

    @property
    def position(self) -> dict[str, float]:
        return dict(self._position)

    @property
    def hotend(self) -> HeaterPID:
        return self._hotend

    @property
    def bed(self) -> HeaterPID:
        return self._bed

    def get_driver(self, axis_id: str) -> StepperDriver:
        if axis_id not in self._drivers:
            raise ValueError(f"No driver for axis: {axis_id}")
        return self._drivers[axis_id]

    def get_endstop(self, axis_id: str) -> Endstop:
        if axis_id not in self._endstops:
            raise ValueError(f"No endstop for axis: {axis_id}")
        return self._endstops[axis_id]

    def load_gcode(self, program: str) -> int:
        self._program = parse_gcode_program(program)
        self._trace = MotionTrace()
        return len(self._program)

    def execute(self) -> MotionTrace:
        self._state = MachineState.running
        self._trace = MotionTrace()
        self._sim_time = 0.0

        for line in self._program:
            if self._state == MachineState.thermal_shutdown:
                self._trace.errors.append(
                    f"Line {line.line_number}: aborted due to thermal shutdown"
                )
                break

            try:
                self._execute_line(line)
            except Exception as exc:
                self._trace.errors.append(f"Line {line.line_number}: {exc}")
                self._state = MachineState.error
                break

        self._trace.final_position = dict(self._position)
        self._trace.total_time_ms = self._sim_time

        if self._state == MachineState.running:
            self._state = MachineState.idle

        return self._trace

    def _execute_line(self, line: GCodeLine) -> None:
        cmd = line.command

        if cmd in ("G0", "G1"):
            self._execute_move(line)
        elif cmd == "G28":
            self._execute_home(line)
        elif cmd == "M104":
            self._execute_set_hotend(line)
        elif cmd == "M109":
            self._execute_wait_hotend(line)
        elif cmd == "M140":
            self._execute_set_bed(line)

    def _execute_move(self, line: GCodeLine) -> None:
        if "F" in line.params:
            self._feedrate = line.params["F"]

        old_pos = dict(self._position)
        new_pos = dict(self._position)

        for axis in ("X", "Y", "Z", "E"):
            if axis in line.params:
                target = line.params[axis]
                if axis in self._config.axes:
                    ax_cfg = self._config.axes[axis]
                    if axis != "E" and (target < 0 or target > ax_cfg.travel_mm):
                        self._trace.errors.append(
                            f"Line {line.line_number}: {axis}={target} out of range [0, {ax_cfg.travel_mm}]"
                        )
                        continue
                new_pos[axis] = target

        dist = 0.0
        for axis in ("X", "Y", "Z"):
            d = new_pos[axis] - old_pos[axis]
            dist += d * d
        dist = math.sqrt(dist)

        feedrate_mm_s = self._feedrate / 60.0
        if line.command == "G0":
            min_max = min(
                self._config.axes[a].max_feedrate_mm_s
                for a in ("X", "Y", "Z")
                if a in self._config.axes
            )
            feedrate_mm_s = min_max

        move_time_ms = (dist / feedrate_mm_s * 1000) if feedrate_mm_s > 0 and dist > 0 else 0

        for axis in ("X", "Y", "Z", "E"):
            if new_pos[axis] != old_pos[axis] and axis in self._drivers:
                self._drivers[axis].move_to_mm(new_pos[axis])

        self._position = new_pos
        self._sim_time += move_time_ms
        self._trace.total_distance_mm += dist

        self._check_thermal_runaway()

        self._trace.steps.append(MotionStep(
            line_number=line.line_number,
            command=line.command,
            position=dict(self._position),
            feedrate=self._feedrate,
            hotend_target=self._hotend.target,
            bed_target=self._bed.target,
            hotend_temp=round(self._hotend.current, 2),
            bed_temp=round(self._bed.current, 2),
            event="move",
            timestamp_ms=round(self._sim_time, 2),
        ))

    def _execute_home(self, line: GCodeLine) -> None:
        axes_to_home = []
        for axis in ("X", "Y", "Z"):
            if axis in line.params or not any(a in line.params for a in ("X", "Y", "Z")):
                if axis in self._endstops:
                    axes_to_home.append(axis)

        self._state = MachineState.homing

        for axis in axes_to_home:
            endstop = self._endstops[axis]
            driver = self._drivers[axis]
            ax_cfg = self._config.axes[axis]

            homing_time_ms = (ax_cfg.travel_mm / ax_cfg.homing_feedrate_mm_s * 1000) if ax_cfg.homing_feedrate_mm_s > 0 else 0

            endstop.force_trigger()
            driver.home()
            self._position[axis] = 0.0
            endstop.reset()

            self._sim_time += homing_time_ms

        self._state = MachineState.running

        self._trace.steps.append(MotionStep(
            line_number=line.line_number,
            command="G28",
            position=dict(self._position),
            feedrate=self._feedrate,
            hotend_target=self._hotend.target,
            bed_target=self._bed.target,
            hotend_temp=round(self._hotend.current, 2),
            bed_temp=round(self._bed.current, 2),
            event=f"home:{','.join(axes_to_home)}",
            timestamp_ms=round(self._sim_time, 2),
        ))

    def _execute_set_hotend(self, line: GCodeLine) -> None:
        temp = line.params.get("S", 0.0)
        self._hotend.set_target(temp)

        if temp > 0 and "hotend" in self._thermal_monitors:
            self._thermal_monitors["hotend"].start_monitoring(self._sim_time)

        self._trace.steps.append(MotionStep(
            line_number=line.line_number,
            command="M104",
            position=dict(self._position),
            feedrate=self._feedrate,
            hotend_target=temp,
            bed_target=self._bed.target,
            hotend_temp=round(self._hotend.current, 2),
            bed_temp=round(self._bed.current, 2),
            event=f"set_hotend:{temp}",
            timestamp_ms=round(self._sim_time, 2),
        ))

    def _execute_wait_hotend(self, line: GCodeLine) -> None:
        temp = line.params.get("S", line.params.get("R", 0.0))
        self._hotend.set_target(temp)

        if temp > 0 and "hotend" in self._thermal_monitors:
            self._thermal_monitors["hotend"].start_monitoring(self._sim_time)

        self._state = MachineState.heating

        tolerance = 2.0
        max_iterations = 10000
        dt = 0.5
        for _ in range(max_iterations):
            self._hotend.simulate_step(dt)
            self._sim_time += dt * 1000

            self._check_thermal_runaway()
            if self._state == MachineState.thermal_shutdown:
                break

            if abs(self._hotend.current - temp) <= tolerance:
                break

        if self._state != MachineState.thermal_shutdown:
            self._state = MachineState.running

        self._trace.steps.append(MotionStep(
            line_number=line.line_number,
            command="M109",
            position=dict(self._position),
            feedrate=self._feedrate,
            hotend_target=temp,
            bed_target=self._bed.target,
            hotend_temp=round(self._hotend.current, 2),
            bed_temp=round(self._bed.current, 2),
            event=f"wait_hotend:{temp}",
            timestamp_ms=round(self._sim_time, 2),
        ))

    def _execute_set_bed(self, line: GCodeLine) -> None:
        temp = line.params.get("S", 0.0)
        self._bed.set_target(temp)

        if temp > 0 and "bed" in self._thermal_monitors:
            self._thermal_monitors["bed"].start_monitoring(self._sim_time)

        self._trace.steps.append(MotionStep(
            line_number=line.line_number,
            command="M140",
            position=dict(self._position),
            feedrate=self._feedrate,
            hotend_target=self._hotend.target,
            bed_target=temp,
            hotend_temp=round(self._hotend.current, 2),
            bed_temp=round(self._bed.current, 2),
            event=f"set_bed:{temp}",
            timestamp_ms=round(self._sim_time, 2),
        ))

    def _check_thermal_runaway(self) -> None:
        for hid, monitor in self._thermal_monitors.items():
            heater = self._hotend if hid == "hotend" else self._bed
            if monitor.check(heater.current, heater.target, heater.output, self._sim_time):
                self._state = MachineState.thermal_shutdown
                self._hotend.disable()
                self._bed.disable()
                for drv in self._drivers.values():
                    drv.disable()
                self._trace.errors.append(
                    f"THERMAL RUNAWAY on {hid}: {monitor.fault_reason}"
                )
                logger.critical("Thermal runaway shutoff: %s — %s", hid, monitor.fault_reason)
                break

    def emergency_stop(self) -> None:
        self._hotend.disable()
        self._bed.disable()
        for drv in self._drivers.values():
            drv.disable()
        self._state = MachineState.error

    def get_status(self) -> dict[str, Any]:
        return {
            "state": self._state.value,
            "position": dict(self._position),
            "feedrate": self._feedrate,
            "hotend": self._hotend.get_status(),
            "bed": self._bed.get_status(),
            "drivers": {k: v.get_status() for k, v in self._drivers.items()},
            "endstops": {k: v.get_status() for k, v in self._endstops.items()},
            "thermal_monitors": {k: v.get_status() for k, v in self._thermal_monitors.items()},
        }


# ── Factory ────────────────────────────────────────────────────────────

def create_machine(config: Optional[MachineConfig] = None) -> Machine:
    return Machine(config)


# ── Public query functions ─────────────────────────────────────────────

def list_stepper_drivers() -> list[dict[str, Any]]:
    cfg = _get_cfg()
    drivers = cfg.get("stepper_drivers", [])
    return [
        asdict(StepperDriverDef(
            driver_id=d["id"],
            name=d["name"],
            description=d.get("description", ""),
            interface=d.get("interface", "step_dir"),
            microstep_options=d.get("microstep_options", []),
            default_microsteps=d.get("default_microsteps", 16),
            max_current_ma=d.get("max_current_ma", 2000),
            features=d.get("features", []),
            stall_threshold_range=d.get("stall_threshold_range"),
            default_stall_threshold=d.get("default_stall_threshold"),
        ))
        for d in drivers
    ]


def list_axes() -> list[dict[str, Any]]:
    cfg = _get_cfg()
    axes = cfg.get("axes", [])
    return [
        asdict(AxisDef(
            axis_id=a["id"],
            name=a["name"],
            steps_per_mm=a.get("default_steps_per_mm", 80),
            max_feedrate_mm_s=a.get("default_max_feedrate_mm_s", 300),
            max_accel_mm_s2=a.get("default_max_accel_mm_s2", 3000),
            homing_feedrate_mm_s=a.get("default_homing_feedrate_mm_s", 50),
            travel_mm=a.get("default_travel_mm", 220),
        ))
        for a in axes
    ]


def list_heaters() -> list[dict[str, Any]]:
    cfg = _get_cfg()
    heaters = cfg.get("heaters", [])
    return [
        asdict(HeaterDef(
            heater_id=h["id"],
            name=h["name"],
            description=h.get("description", ""),
            max_temp_c=h.get("max_temp_c", 300),
            min_temp_c=h.get("min_temp_c", 0),
            pid_kp=h.get("default_pid", {}).get("kp", 22.2),
            pid_ki=h.get("default_pid", {}).get("ki", 1.08),
            pid_kd=h.get("default_pid", {}).get("kd", 114.0),
            thermal_runaway_period_s=h.get("thermal_runaway", {}).get("period_s", 40),
            thermal_runaway_hysteresis_c=h.get("thermal_runaway", {}).get("hysteresis_c", 4),
            thermal_runaway_max_deviation_c=h.get("thermal_runaway", {}).get("max_deviation_c", 15),
            thermal_runaway_grace_period_s=h.get("thermal_runaway", {}).get("grace_period_s", 60),
        ))
        for h in heaters
    ]


def list_endstop_types() -> list[dict[str, Any]]:
    cfg = _get_cfg()
    endstops = cfg.get("endstop_types", [])
    return [
        asdict(EndstopDef(
            endstop_type=e["id"],
            name=e["name"],
            description=e.get("description", ""),
            debounce_ms=e.get("debounce_ms", 10),
            active_low=e.get("active_low", True),
        ))
        for e in endstops
    ]


def list_gcode_commands() -> list[dict[str, Any]]:
    cfg = _get_cfg()
    cmds = cfg.get("gcode_commands", [])
    return [
        asdict(GCodeCommandDef(
            command_id=c["id"],
            name=c["name"],
            description=c.get("description", ""),
            axes=c.get("axes", []),
            parameters=c.get("parameters", []),
        ))
        for c in cmds
    ]


def list_test_recipes() -> list[dict[str, Any]]:
    cfg = _get_cfg()
    recipes = cfg.get("test_recipes", [])
    return [
        asdict(TestRecipeDef(
            recipe_id=r["recipe_id"],
            name=r["name"],
            description=r.get("description", ""),
            domains=r.get("domains", []),
        ))
        for r in recipes
    ]


def list_artifacts() -> list[dict[str, Any]]:
    cfg = _get_cfg()
    arts = cfg.get("artifacts", [])
    return [
        asdict(ArtifactDef(
            artifact_id=a["artifact_id"],
            kind=a["kind"],
            description=a.get("description", ""),
        ))
        for a in arts
    ]


# ── Test recipe runner ─────────────────────────────────────────────────

def _run_recipe_basic_gcode() -> TestResult:
    t0 = time.monotonic()
    details: list[dict[str, Any]] = []
    passed = 0
    failed = 0

    program = "G28\nG1 X10 Y20 F600\nG0 X0 Y0\n"
    lines = parse_gcode_program(program)
    if len(lines) == 3:
        details.append({"test": "parse_3_lines", "status": "passed"})
        passed += 1
    else:
        details.append({"test": "parse_3_lines", "status": "failed", "got": len(lines)})
        failed += 1

    if lines[0].command == "G28":
        details.append({"test": "first_is_G28", "status": "passed"})
        passed += 1
    else:
        details.append({"test": "first_is_G28", "status": "failed"})
        failed += 1

    if lines[1].command == "G1" and lines[1].params.get("X") == 10.0:
        details.append({"test": "G1_params", "status": "passed"})
        passed += 1
    else:
        details.append({"test": "G1_params", "status": "failed"})
        failed += 1

    comment_line = parse_gcode_line("; this is a comment", 99)
    if comment_line is None:
        details.append({"test": "comment_skip", "status": "passed"})
        passed += 1
    else:
        details.append({"test": "comment_skip", "status": "failed"})
        failed += 1

    unsupported = parse_gcode_line("G92 E0", 1)
    if unsupported is None:
        details.append({"test": "unsupported_skip", "status": "passed"})
        passed += 1
    else:
        details.append({"test": "unsupported_skip", "status": "failed"})
        failed += 1

    elapsed = (time.monotonic() - t0) * 1000
    status = TestStatus.passed.value if failed == 0 else TestStatus.failed.value
    return TestResult(
        recipe_id="basic_gcode",
        status=status,
        total=passed + failed,
        passed=passed,
        failed=failed,
        duration_ms=round(elapsed, 2),
        details=details,
    )


def _run_recipe_stepper_lifecycle() -> TestResult:
    t0 = time.monotonic()
    details: list[dict[str, Any]] = []
    passed = 0
    failed = 0

    for driver_id in ("tmc2209", "a4988", "drv8825"):
        cfg = AxisConfig(axis_id="X")
        drv = create_stepper_driver(driver_id, "X", cfg)

        if drv.enable():
            details.append({"test": f"{driver_id}_enable", "status": "passed"})
            passed += 1
        else:
            details.append({"test": f"{driver_id}_enable", "status": "failed"})
            failed += 1

        steps = drv.step(100)
        if steps == 100:
            details.append({"test": f"{driver_id}_step", "status": "passed"})
            passed += 1
        else:
            details.append({"test": f"{driver_id}_step", "status": "failed", "got": steps})
            failed += 1

        if drv.set_microsteps(8):
            details.append({"test": f"{driver_id}_microstep", "status": "passed"})
            passed += 1
        else:
            details.append({"test": f"{driver_id}_microstep", "status": "failed"})
            failed += 1

        if drv.disable():
            details.append({"test": f"{driver_id}_disable", "status": "passed"})
            passed += 1
        else:
            details.append({"test": f"{driver_id}_disable", "status": "failed"})
            failed += 1

    elapsed = (time.monotonic() - t0) * 1000
    status = TestStatus.passed.value if failed == 0 else TestStatus.failed.value
    return TestResult(
        recipe_id="stepper_lifecycle",
        status=status,
        total=passed + failed,
        passed=passed,
        failed=failed,
        duration_ms=round(elapsed, 2),
        details=details,
    )


def _run_recipe_pid_convergence() -> TestResult:
    t0 = time.monotonic()
    details: list[dict[str, Any]] = []
    passed = 0
    failed = 0

    pid = HeaterPID("hotend", kp=22.2, ki=1.08, kd=114.0, max_temp=300.0)
    pid.set_target(200.0)

    for _ in range(2000):
        pid.simulate_step(dt=0.5)

    if abs(pid.current - 200.0) < 5.0:
        details.append({"test": "hotend_converge_200", "status": "passed", "temp": round(pid.current, 2)})
        passed += 1
    else:
        details.append({"test": "hotend_converge_200", "status": "failed", "temp": round(pid.current, 2)})
        failed += 1

    bed_pid = HeaterPID("bed", kp=70.0, ki=1.5, kd=600.0, max_temp=120.0)
    bed_pid.set_target(60.0)

    for _ in range(2000):
        bed_pid.simulate_step(dt=0.5)

    if abs(bed_pid.current - 60.0) < 5.0:
        details.append({"test": "bed_converge_60", "status": "passed", "temp": round(bed_pid.current, 2)})
        passed += 1
    else:
        details.append({"test": "bed_converge_60", "status": "failed", "temp": round(bed_pid.current, 2)})
        failed += 1

    pid.disable()
    if not pid.enabled and pid.target == 0.0:
        details.append({"test": "heater_disable", "status": "passed"})
        passed += 1
    else:
        details.append({"test": "heater_disable", "status": "failed"})
        failed += 1

    elapsed = (time.monotonic() - t0) * 1000
    status = TestStatus.passed.value if failed == 0 else TestStatus.failed.value
    return TestResult(
        recipe_id="pid_convergence",
        status=status,
        total=passed + failed,
        passed=passed,
        failed=failed,
        duration_ms=round(elapsed, 2),
        details=details,
    )


def _run_recipe_endstop_homing() -> TestResult:
    t0 = time.monotonic()
    details: list[dict[str, Any]] = []
    passed = 0
    failed = 0

    machine = create_machine()
    machine.load_gcode("G1 X50 Y50 Z10 F3000\nG28\n")
    trace = machine.execute()

    pos = trace.final_position
    if pos.get("X") == 0.0 and pos.get("Y") == 0.0 and pos.get("Z") == 0.0:
        details.append({"test": "home_resets_xyz", "status": "passed"})
        passed += 1
    else:
        details.append({"test": "home_resets_xyz", "status": "failed", "pos": pos})
        failed += 1

    machine2 = create_machine()
    machine2.load_gcode("G1 X100 Y100 F3000\nG28 X0\n")
    trace2 = machine2.execute()
    pos2 = trace2.final_position
    if pos2.get("X") == 0.0 and pos2.get("Y") == 100.0:
        details.append({"test": "home_single_axis", "status": "passed"})
        passed += 1
    else:
        details.append({"test": "home_single_axis", "status": "failed", "pos": pos2})
        failed += 1

    home_step = None
    for s in trace.steps:
        if s.command == "G28":
            home_step = s
            break

    if home_step and "home:" in home_step.event:
        details.append({"test": "home_event_logged", "status": "passed"})
        passed += 1
    else:
        details.append({"test": "home_event_logged", "status": "failed"})
        failed += 1

    elapsed = (time.monotonic() - t0) * 1000
    status = TestStatus.passed.value if failed == 0 else TestStatus.failed.value
    return TestResult(
        recipe_id="endstop_homing",
        status=status,
        total=passed + failed,
        passed=passed,
        failed=failed,
        duration_ms=round(elapsed, 2),
        details=details,
    )


def _run_recipe_thermal_runaway() -> TestResult:
    t0 = time.monotonic()
    details: list[dict[str, Any]] = []
    passed = 0
    failed = 0

    monitor = ThermalRunawayMonitor(
        "hotend",
        period_s=2.0,
        hysteresis_c=2.0,
        max_deviation_c=10.0,
        grace_period_s=1.0,
    )
    monitor.start_monitoring(sim_time=0.0)
    monitor.check(190.0, 200.0, 255.0, sim_time=2.0)
    tripped = monitor.check(185.0, 200.0, 255.0, sim_time=5.0)

    if tripped:
        details.append({"test": "runaway_detected", "status": "passed", "reason": monitor.fault_reason})
        passed += 1
    else:
        details.append({"test": "runaway_detected", "status": "failed"})
        failed += 1

    monitor2 = ThermalRunawayMonitor(
        "hotend",
        period_s=5.0,
        hysteresis_c=2.0,
        max_deviation_c=10.0,
        grace_period_s=3.0,
    )
    monitor2.start_monitoring(sim_time=0.0)
    tripped2 = monitor2.check(198.0, 200.0, 200.0, sim_time=100.0)
    if not tripped2:
        details.append({"test": "no_false_positive", "status": "passed"})
        passed += 1
    else:
        details.append({"test": "no_false_positive", "status": "failed"})
        failed += 1

    machine = create_machine(MachineConfig(
        axes=_default_axes_config(),
        thermal_runaway_enabled=True,
        thermal_runaway_period_s=0.1,
        thermal_runaway_hysteresis_c=1.0,
        thermal_runaway_max_deviation_c=5.0,
        thermal_runaway_grace_period_s=0.01,
    ))
    machine._hotend.set_target(200.0)
    machine._thermal_monitors["hotend"].start_monitoring(0.0)
    machine._thermal_monitors["hotend"].check(198.0, 200.0, 255.0, sim_time=0.02)
    machine._hotend._state.current = 100.0
    machine._sim_time = 1000.0
    machine._check_thermal_runaway()

    if machine.state == MachineState.thermal_shutdown:
        details.append({"test": "machine_shutdown", "status": "passed"})
        passed += 1
    else:
        details.append({"test": "machine_shutdown", "status": "failed", "state": machine.state.value})
        failed += 1

    elapsed = (time.monotonic() - t0) * 1000
    status = TestStatus.passed.value if failed == 0 else TestStatus.failed.value
    return TestResult(
        recipe_id="thermal_runaway",
        status=status,
        total=passed + failed,
        passed=passed,
        failed=failed,
        duration_ms=round(elapsed, 2),
        details=details,
    )


def _run_recipe_gcode_motion_trace() -> TestResult:
    t0 = time.monotonic()
    details: list[dict[str, Any]] = []
    passed = 0
    failed = 0

    program = """\
G28
M140 S60
M104 S200
M109 S200
G1 X10 Y10 Z0.3 F3000
G1 X50 Y10 E5 F1500
G1 X50 Y50 E10 F1500
G1 X10 Y50 E15 F1500
G1 X10 Y10 E20 F1500
G0 X0 Y0 Z10
"""

    machine = create_machine()
    count = machine.load_gcode(program)

    if count == 10:
        details.append({"test": "parsed_10_lines", "status": "passed"})
        passed += 1
    else:
        details.append({"test": "parsed_10_lines", "status": "failed", "got": count})
        failed += 1

    trace = machine.execute()

    if not trace.errors:
        details.append({"test": "no_errors", "status": "passed"})
        passed += 1
    else:
        details.append({"test": "no_errors", "status": "failed", "errors": trace.errors})
        failed += 1

    fp = trace.final_position
    if fp.get("X") == 0.0 and fp.get("Y") == 0.0 and fp.get("Z") == 10.0:
        details.append({"test": "final_position", "status": "passed"})
        passed += 1
    else:
        details.append({"test": "final_position", "status": "failed", "pos": fp})
        failed += 1

    if trace.total_distance_mm > 0:
        details.append({"test": "distance_positive", "status": "passed", "dist": round(trace.total_distance_mm, 2)})
        passed += 1
    else:
        details.append({"test": "distance_positive", "status": "failed"})
        failed += 1

    if len(trace.steps) == 10:
        details.append({"test": "trace_10_steps", "status": "passed"})
        passed += 1
    else:
        details.append({"test": "trace_10_steps", "status": "failed", "got": len(trace.steps)})
        failed += 1

    hotend_step = None
    for s in trace.steps:
        if s.command == "M109":
            hotend_step = s
            break
    if hotend_step and hotend_step.hotend_temp is not None and hotend_step.hotend_temp > 190:
        details.append({"test": "hotend_reached_temp", "status": "passed", "temp": hotend_step.hotend_temp})
        passed += 1
    else:
        t_val = hotend_step.hotend_temp if hotend_step else None
        details.append({"test": "hotend_reached_temp", "status": "failed", "temp": t_val})
        failed += 1

    bed_step = None
    for s in trace.steps:
        if s.command == "M140":
            bed_step = s
            break
    if bed_step and bed_step.bed_target == 60.0:
        details.append({"test": "bed_target_set", "status": "passed"})
        passed += 1
    else:
        details.append({"test": "bed_target_set", "status": "failed"})
        failed += 1

    if machine.state == MachineState.idle:
        details.append({"test": "machine_idle", "status": "passed"})
        passed += 1
    else:
        details.append({"test": "machine_idle", "status": "failed", "state": machine.state.value})
        failed += 1

    elapsed = (time.monotonic() - t0) * 1000
    status = TestStatus.passed.value if failed == 0 else TestStatus.failed.value
    return TestResult(
        recipe_id="gcode_motion_trace",
        status=status,
        total=passed + failed,
        passed=passed,
        failed=failed,
        duration_ms=round(elapsed, 2),
        details=details,
    )


_RECIPE_RUNNERS = {
    "basic_gcode": _run_recipe_basic_gcode,
    "stepper_lifecycle": _run_recipe_stepper_lifecycle,
    "pid_convergence": _run_recipe_pid_convergence,
    "endstop_homing": _run_recipe_endstop_homing,
    "thermal_runaway": _run_recipe_thermal_runaway,
    "gcode_motion_trace": _run_recipe_gcode_motion_trace,
}


def run_test_recipe(recipe_id: str) -> dict[str, Any]:
    runner = _RECIPE_RUNNERS.get(recipe_id)
    if runner is None:
        raise ValueError(f"Unknown test recipe: {recipe_id}")
    result = runner()
    return asdict(result)


# ── Gate validation ────────────────────────────────────────────────────

def validate_gate() -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    total_passed = 0
    total_failed = 0

    for recipe_id in _RECIPE_RUNNERS:
        try:
            result = run_test_recipe(recipe_id)
            results.append(result)
            total_passed += result.get("passed", 0)
            total_failed += result.get("failed", 0)
        except Exception as exc:
            results.append({
                "recipe_id": recipe_id,
                "status": TestStatus.error.value,
                "error": str(exc),
            })
            total_failed += 1

    verdict = GateVerdict.passed.value if total_failed == 0 else GateVerdict.failed.value

    return {
        "verdict": verdict,
        "total_passed": total_passed,
        "total_failed": total_failed,
        "recipes": results,
    }
