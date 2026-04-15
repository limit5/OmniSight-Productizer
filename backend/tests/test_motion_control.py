"""C25 — L4-CORE-25 Motion control / G-code / CNC abstraction tests (#255).

Covers: G-code interpreter (G0/G1/G28/M104/M109/M140), stepper driver
abstraction (TMC2209/A4988/DRV8825), heater PID loops, endstop handling,
homing sequences, thermal runaway safety shutoff, motion trace validation,
test recipes, artifacts, and gate validation.
"""

from __future__ import annotations

import pytest

from backend import motion_control as mc


# ═══════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _reset_config():
    mc._cfg = None
    yield
    mc._cfg = None


@pytest.fixture
def default_machine():
    return mc.create_machine()


@pytest.fixture
def axis_config():
    return mc.AxisConfig(axis_id="X", steps_per_mm=80, max_feedrate_mm_s=300)


@pytest.fixture
def machine_config():
    return mc.MachineConfig(axes=mc._default_axes_config())


# ═══════════════════════════════════════════════════════════════════════
# 1. Config loading
# ═══════════════════════════════════════════════════════════════════════

class TestConfigLoading:
    def test_load_config(self):
        cfg = mc._load_config()
        assert isinstance(cfg, dict)
        assert "stepper_drivers" in cfg
        assert "axes" in cfg
        assert "heaters" in cfg
        assert "gcode_commands" in cfg

    def test_config_cached(self):
        cfg1 = mc._load_config()
        cfg2 = mc._load_config()
        assert cfg1 is cfg2

    def test_list_stepper_drivers(self):
        drivers = mc.list_stepper_drivers()
        assert len(drivers) == 3
        ids = {d["driver_id"] for d in drivers}
        assert ids == {"tmc2209", "a4988", "drv8825"}

    def test_list_axes(self):
        axes = mc.list_axes()
        assert len(axes) == 4
        ids = {a["axis_id"] for a in axes}
        assert ids == {"X", "Y", "Z", "E"}

    def test_list_heaters(self):
        heaters = mc.list_heaters()
        assert len(heaters) == 2
        ids = {h["heater_id"] for h in heaters}
        assert ids == {"hotend", "bed"}

    def test_list_endstop_types(self):
        endstops = mc.list_endstop_types()
        assert len(endstops) == 3
        ids = {e["endstop_type"] for e in endstops}
        assert ids == {"mechanical", "optical", "stallguard"}

    def test_list_gcode_commands(self):
        cmds = mc.list_gcode_commands()
        assert len(cmds) == 6
        ids = {c["command_id"] for c in cmds}
        assert ids == {"G0", "G1", "G28", "M104", "M109", "M140"}

    def test_list_test_recipes(self):
        recipes = mc.list_test_recipes()
        assert len(recipes) == 6

    def test_list_artifacts(self):
        artifacts = mc.list_artifacts()
        assert len(artifacts) == 3


# ═══════════════════════════════════════════════════════════════════════
# 2. G-code parser
# ═══════════════════════════════════════════════════════════════════════

class TestGCodeParser:
    def test_parse_g0(self):
        line = mc.parse_gcode_line("G0 X10 Y20 Z5", 1)
        assert line is not None
        assert line.command == "G0"
        assert line.params == {"X": 10.0, "Y": 20.0, "Z": 5.0}

    def test_parse_g1_with_feedrate(self):
        line = mc.parse_gcode_line("G1 X50 Y50 E5 F1500", 2)
        assert line is not None
        assert line.command == "G1"
        assert line.params["F"] == 1500.0
        assert line.params["E"] == 5.0

    def test_parse_g28_no_params(self):
        line = mc.parse_gcode_line("G28", 3)
        assert line is not None
        assert line.command == "G28"
        assert line.params == {}

    def test_parse_g28_with_axis(self):
        line = mc.parse_gcode_line("G28 X0", 3)
        assert line is not None
        assert line.command == "G28"
        assert "X" in line.params

    def test_parse_m104(self):
        line = mc.parse_gcode_line("M104 S200", 4)
        assert line is not None
        assert line.command == "M104"
        assert line.params["S"] == 200.0

    def test_parse_m109(self):
        line = mc.parse_gcode_line("M109 S210", 5)
        assert line is not None
        assert line.command == "M109"
        assert line.params["S"] == 210.0

    def test_parse_m140(self):
        line = mc.parse_gcode_line("M140 S60", 6)
        assert line is not None
        assert line.command == "M140"
        assert line.params["S"] == 60.0

    def test_skip_comment(self):
        assert mc.parse_gcode_line("; this is a comment", 1) is None

    def test_skip_empty(self):
        assert mc.parse_gcode_line("", 1) is None
        assert mc.parse_gcode_line("   ", 1) is None

    def test_skip_unsupported(self):
        assert mc.parse_gcode_line("G92 E0", 1) is None
        assert mc.parse_gcode_line("M82", 1) is None

    def test_inline_comment(self):
        line = mc.parse_gcode_line("G1 X10 ; move to X10", 1)
        assert line is not None
        assert line.command == "G1"
        assert line.params["X"] == 10.0

    def test_parse_program(self):
        program = "G28\nG1 X10 Y20 F600\n; comment\nG0 X0 Y0\n"
        lines = mc.parse_gcode_program(program)
        assert len(lines) == 3
        assert lines[0].command == "G28"
        assert lines[1].command == "G1"
        assert lines[2].command == "G0"

    def test_parse_negative_coords(self):
        line = mc.parse_gcode_line("G1 X-5 Y-10", 1)
        assert line is not None
        assert line.params["X"] == -5.0
        assert line.params["Y"] == -10.0

    def test_parse_decimal_coords(self):
        line = mc.parse_gcode_line("G1 X10.5 Y20.3 Z0.2", 1)
        assert line is not None
        assert line.params["X"] == 10.5
        assert line.params["Y"] == 20.3
        assert line.params["Z"] == 0.2


# ═══════════════════════════════════════════════════════════════════════
# 3. Stepper driver abstraction
# ═══════════════════════════════════════════════════════════════════════

class TestStepperDrivers:
    def test_create_tmc2209(self, axis_config):
        drv = mc.create_stepper_driver("tmc2209", "X", axis_config)
        assert isinstance(drv, mc.TMC2209Driver)
        assert drv.driver_id == "tmc2209"

    def test_create_a4988(self, axis_config):
        drv = mc.create_stepper_driver("a4988", "X", axis_config)
        assert isinstance(drv, mc.A4988Driver)

    def test_create_drv8825(self, axis_config):
        drv = mc.create_stepper_driver("drv8825", "X", axis_config)
        assert isinstance(drv, mc.DRV8825Driver)

    def test_create_unknown_raises(self, axis_config):
        with pytest.raises(ValueError, match="Unknown stepper driver"):
            mc.create_stepper_driver("unknown", "X", axis_config)

    def test_enable_disable(self, axis_config):
        drv = mc.create_stepper_driver("tmc2209", "X", axis_config)
        assert drv.enable()
        assert drv._enabled
        assert drv.disable()
        assert not drv._enabled

    def test_step_requires_enable(self, axis_config):
        drv = mc.create_stepper_driver("tmc2209", "X", axis_config)
        assert drv.step(100) == 0
        drv.enable()
        assert drv.step(100) == 100

    def test_step_negative(self, axis_config):
        drv = mc.create_stepper_driver("a4988", "X", axis_config)
        drv.enable()
        drv.step(100)
        drv.step(-50)
        assert drv._position_steps == 50

    def test_move_to_mm(self, axis_config):
        drv = mc.create_stepper_driver("tmc2209", "X", axis_config)
        drv.enable()
        drv.move_to_mm(10.0)
        assert abs(drv.position_mm - 10.0) < 0.1

    def test_home(self, axis_config):
        drv = mc.create_stepper_driver("tmc2209", "X", axis_config)
        drv.enable()
        drv.move_to_mm(50.0)
        drv.home()
        assert drv._position_steps == 0

    def test_tmc2209_microsteps(self, axis_config):
        drv = mc.TMC2209Driver("X", axis_config)
        assert drv.set_microsteps(256)
        assert drv._microsteps == 256
        assert not drv.set_microsteps(3)

    def test_tmc2209_stall_threshold(self, axis_config):
        drv = mc.TMC2209Driver("X", axis_config)
        assert drv.set_stall_threshold(50)
        assert drv._stall_threshold == 50
        assert not drv.set_stall_threshold(-1)
        assert not drv.set_stall_threshold(256)

    def test_tmc2209_stealthchop(self, axis_config):
        drv = mc.TMC2209Driver("X", axis_config)
        drv.set_stealthchop(False)
        assert not drv._stealthchop
        drv.set_stealthchop(True)
        assert drv._stealthchop

    def test_a4988_microsteps(self, axis_config):
        drv = mc.A4988Driver("X", axis_config)
        assert drv.set_microsteps(8)
        assert not drv.set_microsteps(32)

    def test_a4988_sleep(self, axis_config):
        drv = mc.A4988Driver("X", axis_config)
        drv.enable()
        drv.sleep()
        assert drv._sleep
        assert not drv._enabled

    def test_drv8825_microsteps(self, axis_config):
        drv = mc.DRV8825Driver("X", axis_config)
        assert drv.set_microsteps(32)
        assert not drv.set_microsteps(64)

    def test_drv8825_fault(self, axis_config):
        drv = mc.DRV8825Driver("X", axis_config)
        drv.inject_fault()
        assert drv._fault
        assert not drv.enable()
        drv.clear_fault()
        assert drv.enable()

    def test_current_limits(self, axis_config):
        for did in ("tmc2209", "a4988", "drv8825"):
            drv = mc.create_stepper_driver(did, "X", axis_config)
            assert not drv.set_current(-1)
            assert drv.set_current(500)

    def test_driver_status(self, axis_config):
        drv = mc.create_stepper_driver("tmc2209", "X", axis_config)
        drv.enable()
        drv.step(100)
        status = drv.get_status()
        assert status["enabled"] is True
        assert status["position_steps"] == 100
        assert status["driver_id"] == "tmc2209"

    def test_inverted_axis(self):
        cfg = mc.AxisConfig(axis_id="X", inverted=True)
        drv = mc.create_stepper_driver("tmc2209", "X", cfg)
        drv.enable()
        drv.step(100)
        assert drv._position_steps == -100


# ═══════════════════════════════════════════════════════════════════════
# 4. Heater + PID
# ═══════════════════════════════════════════════════════════════════════

class TestHeaterPID:
    def test_create_heater(self):
        pid = mc.HeaterPID("hotend", kp=22.2, ki=1.08, kd=114.0, max_temp=300.0)
        assert pid.heater_id == "hotend"
        assert not pid.enabled

    def test_set_target(self):
        pid = mc.HeaterPID("hotend", kp=22.2, ki=1.08, kd=114.0, max_temp=300.0)
        assert pid.set_target(200.0)
        assert pid.target == 200.0
        assert pid.enabled

    def test_set_target_out_of_range(self):
        pid = mc.HeaterPID("hotend", kp=22.2, ki=1.08, kd=114.0, max_temp=300.0)
        assert not pid.set_target(350.0)
        assert not pid.set_target(-10.0)

    def test_disable(self):
        pid = mc.HeaterPID("hotend", kp=22.2, ki=1.08, kd=114.0, max_temp=300.0)
        pid.set_target(200.0)
        pid.disable()
        assert not pid.enabled
        assert pid.target == 0.0
        assert pid.output == 0.0

    def test_pid_update(self):
        pid = mc.HeaterPID("hotend", kp=22.2, ki=1.08, kd=114.0, max_temp=300.0)
        pid.set_target(200.0)
        output = pid.update(25.0, dt=0.1)
        assert output > 0

    def test_pid_zero_when_disabled(self):
        pid = mc.HeaterPID("hotend", kp=22.2, ki=1.08, kd=114.0, max_temp=300.0)
        output = pid.update(25.0, dt=0.1)
        assert output == 0.0

    def test_pid_convergence_hotend(self):
        pid = mc.HeaterPID("hotend", kp=22.2, ki=1.08, kd=114.0, max_temp=300.0)
        pid.set_target(200.0)
        for _ in range(2000):
            pid.simulate_step(dt=0.5)
        assert abs(pid.current - 200.0) < 5.0

    def test_pid_convergence_bed(self):
        pid = mc.HeaterPID("bed", kp=70.0, ki=1.5, kd=600.0, max_temp=120.0)
        pid.set_target(60.0)
        for _ in range(2000):
            pid.simulate_step(dt=0.5)
        assert abs(pid.current - 60.0) < 5.0

    def test_pid_cools_when_disabled(self):
        pid = mc.HeaterPID("hotend", kp=22.2, ki=1.08, kd=114.0, max_temp=300.0)
        pid._state.current = 200.0
        pid.disable()
        for _ in range(100):
            pid.simulate_step(dt=1.0, ambient=25.0)
        assert pid.current < 200.0

    def test_heater_status(self):
        pid = mc.HeaterPID("hotend", kp=22.2, ki=1.08, kd=114.0, max_temp=300.0)
        pid.set_target(200.0)
        status = pid.get_status()
        assert status["heater_id"] == "hotend"
        assert status["target"] == 200.0
        assert status["enabled"] is True


# ═══════════════════════════════════════════════════════════════════════
# 5. Endstop handling
# ═══════════════════════════════════════════════════════════════════════

class TestEndstops:
    def test_create_endstop(self):
        es = mc.Endstop("X", endstop_type="mechanical")
        assert es.axis_id == "X"
        assert not es.triggered

    def test_force_trigger(self):
        es = mc.Endstop("X")
        es.force_trigger()
        assert es.triggered

    def test_reset(self):
        es = mc.Endstop("X")
        es.force_trigger()
        es.reset()
        assert not es.triggered

    def test_check_at_position(self):
        es = mc.Endstop("X", trigger_position_mm=0.0)
        assert es.check_at_position(0.0)
        assert es.check_at_position(-1.0)
        assert not es.check_at_position(1.0)

    def test_endstop_status(self):
        es = mc.Endstop("X", endstop_type="optical")
        status = es.get_status()
        assert status["axis_id"] == "X"
        assert status["endstop_type"] == "optical"
        assert status["triggered"] is False


# ═══════════════════════════════════════════════════════════════════════
# 6. Thermal runaway monitor
# ═══════════════════════════════════════════════════════════════════════

class TestThermalRunaway:
    def test_create_monitor(self):
        mon = mc.ThermalRunawayMonitor("hotend")
        assert not mon.tripped
        assert mon.fault_reason == ""

    def test_no_trip_during_grace(self):
        mon = mc.ThermalRunawayMonitor("hotend", grace_period_s=60.0)
        mon.start_monitoring(sim_time=0.0)
        tripped = mon.check(25.0, 200.0, 255.0, sim_time=30.0)
        assert not tripped

    def test_trip_on_deviation(self):
        mon = mc.ThermalRunawayMonitor(
            "hotend",
            period_s=5.0,
            hysteresis_c=20.0,
            max_deviation_c=10.0,
            grace_period_s=3.0,
        )
        mon.start_monitoring(sim_time=0.0)
        mon.check(198.0, 200.0, 255.0, sim_time=4.0)
        tripped = mon.check(180.0, 200.0, 0.0, sim_time=10.0)
        assert tripped
        assert "deviation" in mon.fault_reason

    def test_trip_on_temp_drop(self):
        mon = mc.ThermalRunawayMonitor(
            "hotend",
            period_s=2.0,
            hysteresis_c=2.0,
            max_deviation_c=100.0,
            grace_period_s=1.0,
        )
        mon.start_monitoring(sim_time=0.0)
        mon.check(190.0, 200.0, 255.0, sim_time=2.0)
        tripped = mon.check(185.0, 200.0, 255.0, sim_time=5.0)
        assert tripped
        assert "dropping" in mon.fault_reason.lower()

    def test_no_false_positive_stable(self):
        mon = mc.ThermalRunawayMonitor(
            "hotend",
            period_s=5.0,
            max_deviation_c=10.0,
            grace_period_s=3.0,
        )
        mon.start_monitoring(sim_time=0.0)
        tripped = mon.check(198.0, 200.0, 200.0, sim_time=100.0)
        assert not tripped

    def test_reset(self):
        mon = mc.ThermalRunawayMonitor("hotend", grace_period_s=0.0, period_s=0.0, max_deviation_c=1.0)
        mon.start_monitoring(sim_time=0.0)
        mon.check(25.0, 200.0, 255.0, sim_time=10.0)
        mon.reset()
        assert not mon.tripped

    def test_no_trip_when_target_zero(self):
        mon = mc.ThermalRunawayMonitor("hotend")
        mon.start_monitoring(sim_time=0.0)
        tripped = mon.check(25.0, 0.0, 0.0, sim_time=100.0)
        assert not tripped

    def test_monitor_status(self):
        mon = mc.ThermalRunawayMonitor("bed")
        status = mon.get_status()
        assert status["heater_id"] == "bed"
        assert status["tripped"] is False


# ═══════════════════════════════════════════════════════════════════════
# 7. Machine integration
# ═══════════════════════════════════════════════════════════════════════

class TestMachine:
    def test_create_default(self, default_machine):
        assert default_machine.state == mc.MachineState.idle
        assert default_machine.position == {"X": 0.0, "Y": 0.0, "Z": 0.0, "E": 0.0}

    def test_get_driver(self, default_machine):
        drv = default_machine.get_driver("X")
        assert drv.driver_id == "tmc2209"

    def test_get_endstop(self, default_machine):
        es = default_machine.get_endstop("X")
        assert es.axis_id == "X"

    def test_get_nonexistent_driver_raises(self, default_machine):
        with pytest.raises(ValueError):
            default_machine.get_driver("W")

    def test_get_nonexistent_endstop_raises(self, default_machine):
        with pytest.raises(ValueError):
            default_machine.get_endstop("E")

    def test_load_gcode(self, default_machine):
        count = default_machine.load_gcode("G28\nG1 X10 F600\n")
        assert count == 2

    def test_execute_simple_move(self, default_machine):
        default_machine.load_gcode("G1 X10 Y20 F600\n")
        trace = default_machine.execute()
        assert len(trace.steps) == 1
        assert trace.final_position["X"] == 10.0
        assert trace.final_position["Y"] == 20.0

    def test_execute_rapid_move(self, default_machine):
        default_machine.load_gcode("G0 X100 Y100\n")
        trace = default_machine.execute()
        assert trace.final_position["X"] == 100.0

    def test_execute_home(self, default_machine):
        default_machine.load_gcode("G1 X50 Y50 Z10 F3000\nG28\n")
        trace = default_machine.execute()
        assert trace.final_position["X"] == 0.0
        assert trace.final_position["Y"] == 0.0
        assert trace.final_position["Z"] == 0.0

    def test_execute_home_single_axis(self, default_machine):
        default_machine.load_gcode("G1 X100 Y100 F3000\nG28 X0\n")
        trace = default_machine.execute()
        assert trace.final_position["X"] == 0.0
        assert trace.final_position["Y"] == 100.0

    def test_execute_set_hotend(self, default_machine):
        default_machine.load_gcode("M104 S200\n")
        trace = default_machine.execute()
        assert trace.steps[0].hotend_target == 200.0

    def test_execute_wait_hotend(self, default_machine):
        default_machine.load_gcode("M109 S200\n")
        trace = default_machine.execute()
        assert trace.steps[0].hotend_temp > 190.0

    def test_execute_set_bed(self, default_machine):
        default_machine.load_gcode("M140 S60\n")
        trace = default_machine.execute()
        assert trace.steps[0].bed_target == 60.0

    def test_motion_trace_distance(self, default_machine):
        default_machine.load_gcode("G1 X10 F600\nG1 X20 F600\n")
        trace = default_machine.execute()
        assert trace.total_distance_mm > 0

    def test_emergency_stop(self, default_machine):
        default_machine.emergency_stop()
        assert default_machine.state == mc.MachineState.error

    def test_machine_status(self, default_machine):
        status = default_machine.get_status()
        assert "state" in status
        assert "position" in status
        assert "hotend" in status
        assert "bed" in status
        assert "drivers" in status
        assert "endstops" in status


# ═══════════════════════════════════════════════════════════════════════
# 8. Machine thermal shutdown
# ═══════════════════════════════════════════════════════════════════════

class TestMachineThermalShutdown:
    def test_thermal_shutdown_disables_all(self):
        machine = mc.create_machine(mc.MachineConfig(
            axes=mc._default_axes_config(),
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

        assert machine.state == mc.MachineState.thermal_shutdown
        assert not machine._hotend.enabled
        assert not machine._bed.enabled
        for drv in machine._drivers.values():
            assert not drv._enabled

    def test_thermal_shutdown_stops_execution(self):
        machine = mc.create_machine(mc.MachineConfig(
            axes=mc._default_axes_config(),
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

        machine.load_gcode("G1 X10 F600\nG1 X20 F600\nG1 X30 F600\n")
        machine._state = mc.MachineState.running
        machine._check_thermal_runaway()

        assert machine.state == mc.MachineState.thermal_shutdown


# ═══════════════════════════════════════════════════════════════════════
# 9. G-code → Motion trace (full pipeline)
# ═══════════════════════════════════════════════════════════════════════

class TestGCodeMotionTrace:
    def test_full_print_sequence(self):
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
        machine = mc.create_machine()
        count = machine.load_gcode(program)
        assert count == 10

        trace = machine.execute()
        assert not trace.errors
        assert len(trace.steps) == 10

        fp = trace.final_position
        assert fp["X"] == 0.0
        assert fp["Y"] == 0.0
        assert fp["Z"] == 10.0

        assert trace.total_distance_mm > 0
        assert trace.total_time_ms > 0

    def test_home_then_move(self):
        machine = mc.create_machine()
        machine.load_gcode("G28\nG1 X100 Y100 Z50 F3000\n")
        trace = machine.execute()
        assert trace.final_position["X"] == 100.0
        assert trace.final_position["Z"] == 50.0

    def test_multiple_homes(self):
        machine = mc.create_machine()
        machine.load_gcode("G1 X50 F3000\nG28\nG1 X100 F3000\nG28\n")
        trace = machine.execute()
        assert trace.final_position["X"] == 0.0

    def test_heating_sequence(self):
        machine = mc.create_machine()
        machine.load_gcode("M140 S60\nM104 S200\nM109 S200\n")
        trace = machine.execute()
        assert len(trace.steps) == 3

        m109_step = trace.steps[2]
        assert m109_step.hotend_temp > 190

    def test_extruder_tracking(self):
        machine = mc.create_machine()
        machine.load_gcode("G1 X10 E5 F600\nG1 X20 E10 F600\n")
        trace = machine.execute()
        assert trace.final_position["E"] == 10.0

    def test_out_of_bounds_warning(self):
        machine = mc.create_machine()
        machine.load_gcode("G1 X999 F600\n")
        trace = machine.execute()
        assert any("out of range" in e for e in trace.errors)

    def test_idle_after_execution(self):
        machine = mc.create_machine()
        machine.load_gcode("G28\nG1 X10 F600\n")
        machine.execute()
        assert machine.state == mc.MachineState.idle


# ═══════════════════════════════════════════════════════════════════════
# 10. Test recipes
# ═══════════════════════════════════════════════════════════════════════

class TestRecipes:
    def test_basic_gcode_recipe(self):
        result = mc.run_test_recipe("basic_gcode")
        assert result["status"] == "passed"
        assert result["failed"] == 0

    def test_stepper_lifecycle_recipe(self):
        result = mc.run_test_recipe("stepper_lifecycle")
        assert result["status"] == "passed"
        assert result["failed"] == 0

    def test_pid_convergence_recipe(self):
        result = mc.run_test_recipe("pid_convergence")
        assert result["status"] == "passed"
        assert result["failed"] == 0

    def test_endstop_homing_recipe(self):
        result = mc.run_test_recipe("endstop_homing")
        assert result["status"] == "passed"
        assert result["failed"] == 0

    def test_thermal_runaway_recipe(self):
        result = mc.run_test_recipe("thermal_runaway")
        assert result["status"] == "passed"
        assert result["failed"] == 0

    def test_gcode_motion_trace_recipe(self):
        result = mc.run_test_recipe("gcode_motion_trace")
        assert result["status"] == "passed"
        assert result["failed"] == 0

    def test_unknown_recipe_raises(self):
        with pytest.raises(ValueError, match="Unknown test recipe"):
            mc.run_test_recipe("nonexistent")


# ═══════════════════════════════════════════════════════════════════════
# 11. Gate validation
# ═══════════════════════════════════════════════════════════════════════

class TestGateValidation:
    def test_gate_passes(self):
        result = mc.validate_gate()
        assert result["verdict"] == "passed"
        assert result["total_failed"] == 0
        assert len(result["recipes"]) == 6

    def test_gate_all_recipes_pass(self):
        result = mc.validate_gate()
        for r in result["recipes"]:
            assert r["status"] == "passed", f"Recipe {r['recipe_id']} failed"


# ═══════════════════════════════════════════════════════════════════════
# 12. Machine with different driver configs
# ═══════════════════════════════════════════════════════════════════════

class TestMachineDriverConfigs:
    def test_a4988_machine(self):
        axes = mc._default_axes_config()
        for ax in axes.values():
            ax.driver_id = "a4988"
        machine = mc.create_machine(mc.MachineConfig(axes=axes))
        machine.load_gcode("G28\nG1 X50 Y50 F3000\n")
        trace = machine.execute()
        assert trace.final_position["X"] == 50.0

    def test_drv8825_machine(self):
        axes = mc._default_axes_config()
        for ax in axes.values():
            ax.driver_id = "drv8825"
        machine = mc.create_machine(mc.MachineConfig(axes=axes))
        machine.load_gcode("G28\nG1 X50 Y50 F3000\n")
        trace = machine.execute()
        assert trace.final_position["X"] == 50.0

    def test_no_thermal_runaway(self):
        machine = mc.create_machine(mc.MachineConfig(
            axes=mc._default_axes_config(),
            thermal_runaway_enabled=False,
        ))
        assert len(machine._thermal_monitors) == 0


# ═══════════════════════════════════════════════════════════════════════
# 13. Edge cases
# ═══════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    def test_empty_program(self):
        machine = mc.create_machine()
        count = machine.load_gcode("")
        assert count == 0
        trace = machine.execute()
        assert len(trace.steps) == 0

    def test_comments_only(self):
        machine = mc.create_machine()
        count = machine.load_gcode("; comment 1\n; comment 2\n")
        assert count == 0

    def test_zero_feedrate_move(self):
        machine = mc.create_machine()
        machine.load_gcode("G1 X10 Y10 F0\n")
        trace = machine.execute()
        assert trace.final_position["X"] == 10.0

    def test_heater_set_zero_disables(self):
        pid = mc.HeaterPID("hotend", kp=22.2, ki=1.08, kd=114.0, max_temp=300.0)
        pid.set_target(200.0)
        pid.set_target(0.0)
        assert not pid.enabled

    def test_factory_create_machine(self):
        machine = mc.create_machine()
        assert isinstance(machine, mc.Machine)
        assert machine.state == mc.MachineState.idle
