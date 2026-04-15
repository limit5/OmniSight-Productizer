"""C7 — Unit + integration tests for the HIL plugin API (#216).

Covers:
  - HIL plugin protocol (ABC, dataclasses, lifecycle runner)
  - Camera family plugin (focus/WB/stream-latency)
  - Audio family plugin (SNR/AEC/THD)
  - Display family plugin (uniformity/touch-latency/color-accuracy)
  - HIL registry (register, lookup, create, list)
  - Skill pack HIL requirement parsing + validation
  - Skill pack HIL run (full lifecycle)
  - Mock HIL plugin lifecycle (custom plugin)
  - API endpoints: /hil/plugins, /hil/plugins/{name},
    /hil/validate/{skill}, /hil/run/{skill}
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from backend.hil_plugin import (
    HILPlugin,
    Measurement,
    MeasurementStatus,
    PluginFamily,
    PluginInfo,
    PluginRunSummary,
    VerifyResult,
    run_plugin_lifecycle,
)
from backend.hil_plugins.camera import CameraHILPlugin, CAMERA_METRICS
from backend.hil_plugins.audio import AudioHILPlugin, AUDIO_METRICS
from backend.hil_plugins.display import DisplayHILPlugin, DISPLAY_METRICS
from backend.hil_registry import (
    HILRequirement,
    HILValidationResult,
    create_plugin,
    get_plugin_class,
    list_registered_plugins,
    parse_skill_hil_requirements,
    register_builtin,
    run_skill_hil,
    validate_skill_hil,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")


def _make_skill_with_hil(base: Path, name: str, hil_plugins: list) -> Path:
    """Create a skill dir with hil_plugins declared in manifest."""
    skill_dir = base / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "name": name,
        "version": "1.0.0",
        "artifacts": [
            {"kind": "tasks", "path": "tasks.yaml"},
            {"kind": "scaffolds", "path": "scaffolds/"},
            {"kind": "tests", "path": "tests/"},
            {"kind": "hil", "path": "hil/"},
            {"kind": "docs", "path": "docs/"},
        ],
        "hil_plugins": hil_plugins,
    }
    _write_yaml(skill_dir / "skill.yaml", manifest)
    _write_yaml(skill_dir / "tasks.yaml", {"schema_version": 1, "tasks": []})
    for d in ("scaffolds", "tests", "hil", "docs"):
        (skill_dir / d).mkdir(exist_ok=True)
    return skill_dir


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  1. Protocol dataclasses
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestMeasurement:
    def test_defaults(self):
        m = Measurement(plugin_name="test", metric_name="foo", value=42)
        assert m.plugin_name == "test"
        assert m.metric_name == "foo"
        assert m.value == 42
        assert m.unit == ""
        assert isinstance(m.timestamp, float)
        assert m.metadata == {}

    def test_with_metadata(self):
        m = Measurement(
            plugin_name="cam", metric_name="focus", value=150.5,
            unit="lv", metadata={"res": "1080p"},
        )
        assert m.metadata["res"] == "1080p"
        assert m.unit == "lv"


class TestVerifyResult:
    def test_pass(self):
        r = VerifyResult(passed=True, metric_name="snr", measured_value=80.0)
        assert r.passed is True

    def test_fail_with_message(self):
        r = VerifyResult(
            passed=False, metric_name="latency", measured_value=300,
            criterion="max=200", message="value 300 > max 200",
        )
        assert r.passed is False
        assert "300" in r.message


class TestPluginInfo:
    def test_creation(self):
        info = PluginInfo(
            name="test-plugin", family=PluginFamily.camera,
            version="1.0.0", supported_metrics=["a", "b"],
        )
        assert info.name == "test-plugin"
        assert info.family == PluginFamily.camera
        assert len(info.supported_metrics) == 2


class TestPluginRunSummary:
    def test_all_passed(self):
        s = PluginRunSummary(plugin_name="x", family="camera")
        s.results = [
            VerifyResult(passed=True, metric_name="a", measured_value=1),
            VerifyResult(passed=True, metric_name="b", measured_value=2),
        ]
        assert s.all_passed is True
        assert s.pass_count == 2
        assert s.fail_count == 0

    def test_mixed(self):
        s = PluginRunSummary(plugin_name="x", family="audio")
        s.results = [
            VerifyResult(passed=True, metric_name="a", measured_value=1),
            VerifyResult(passed=False, metric_name="b", measured_value=2),
        ]
        assert s.all_passed is False
        assert s.pass_count == 1
        assert s.fail_count == 1

    def test_empty(self):
        s = PluginRunSummary(plugin_name="x", family="display")
        assert s.all_passed is True
        assert s.pass_count == 0


class TestMeasurementStatus:
    def test_values(self):
        assert MeasurementStatus.ok.value == "ok"
        assert MeasurementStatus.fail.value == "fail"
        assert MeasurementStatus.error.value == "error"
        assert MeasurementStatus.skipped.value == "skipped"


class TestPluginFamily:
    def test_values(self):
        assert PluginFamily.camera.value == "camera"
        assert PluginFamily.audio.value == "audio"
        assert PluginFamily.display.value == "display"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2. Camera family plugin
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCameraPlugin:
    def test_info(self):
        p = CameraHILPlugin()
        assert p.plugin_info.name == "camera"
        assert p.plugin_info.family == PluginFamily.camera
        assert set(CAMERA_METRICS) == {"focus_sharpness", "white_balance", "stream_latency"}

    def test_measure_focus(self):
        p = CameraHILPlugin(device_id="cam-01")
        m = p.measure("focus_sharpness", resolution="3840x2160")
        assert m.metric_name == "focus_sharpness"
        assert m.unit == "laplacian_variance"
        assert m.metadata["resolution"] == "3840x2160"
        assert m.metadata["device_id"] == "cam-01"

    def test_measure_wb(self):
        p = CameraHILPlugin()
        m = p.measure("white_balance", illuminant="D50")
        assert m.metric_name == "white_balance"
        assert m.metadata["illuminant"] == "D50"

    def test_measure_stream_latency(self):
        p = CameraHILPlugin()
        m = p.measure("stream_latency", codec="h265", frames=60)
        assert m.metric_name == "stream_latency"
        assert m.unit == "ms"
        assert m.metadata["codec"] == "h265"
        assert m.metadata["frames"] == 60

    def test_measure_unsupported(self):
        p = CameraHILPlugin()
        with pytest.raises(ValueError, match="unsupported metric"):
            p.measure("nonexistent")

    def test_verify_pass(self):
        p = CameraHILPlugin()
        m = Measurement(plugin_name="camera", metric_name="focus_sharpness", value=150.0)
        r = p.verify(m, {"min": 100})
        assert r.passed is True

    def test_verify_fail_min(self):
        p = CameraHILPlugin()
        m = Measurement(plugin_name="camera", metric_name="focus_sharpness", value=50.0)
        r = p.verify(m, {"min": 100})
        assert r.passed is False
        assert "< min" in r.message

    def test_verify_fail_max(self):
        p = CameraHILPlugin()
        m = Measurement(plugin_name="camera", metric_name="stream_latency", value=300.0)
        r = p.verify(m, {"max": 200})
        assert r.passed is False
        assert "> max" in r.message

    def test_teardown_idempotent(self):
        p = CameraHILPlugin()
        p.teardown()
        p.teardown()

    def test_measure_after_teardown_raises(self):
        p = CameraHILPlugin()
        p.teardown()
        with pytest.raises(RuntimeError, match="torn down"):
            p.measure("focus_sharpness")

    def test_supports_metric(self):
        p = CameraHILPlugin()
        assert p.supports_metric("focus_sharpness") is True
        assert p.supports_metric("nonexistent") is False

    def test_measure_and_verify(self):
        p = CameraHILPlugin()
        m, v = p.measure_and_verify("focus_sharpness", {"min": -1})
        assert m.metric_name == "focus_sharpness"
        assert v.passed is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  3. Audio family plugin
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestAudioPlugin:
    def test_info(self):
        p = AudioHILPlugin()
        assert p.plugin_info.name == "audio"
        assert p.plugin_info.family == PluginFamily.audio
        assert set(AUDIO_METRICS) == {"snr", "aec", "thd"}

    def test_measure_snr(self):
        p = AudioHILPlugin(device_id="mic-01")
        m = p.measure("snr", sample_rate=96000, duration_s=2.0)
        assert m.metric_name == "snr"
        assert m.unit == "dB"
        assert m.metadata["sample_rate"] == 96000
        assert m.metadata["device_id"] == "mic-01"

    def test_measure_aec(self):
        p = AudioHILPlugin()
        m = p.measure("aec", reference_level_dbfs=-10.0, playback_delay_ms=100)
        assert m.metric_name == "aec"
        assert m.metadata["reference_level_dbfs"] == -10.0

    def test_measure_thd(self):
        p = AudioHILPlugin()
        m = p.measure("thd", frequency_hz=440)
        assert m.metric_name == "thd"
        assert m.unit == "percent"
        assert m.metadata["frequency_hz"] == 440

    def test_measure_unsupported(self):
        p = AudioHILPlugin()
        with pytest.raises(ValueError, match="unsupported metric"):
            p.measure("invalid")

    def test_verify_pass(self):
        p = AudioHILPlugin()
        m = Measurement(plugin_name="audio", metric_name="snr", value=80.0)
        r = p.verify(m, {"min": 60})
        assert r.passed is True

    def test_verify_fail(self):
        p = AudioHILPlugin()
        m = Measurement(plugin_name="audio", metric_name="thd", value=5.0)
        r = p.verify(m, {"max": 1.0})
        assert r.passed is False

    def test_teardown(self):
        p = AudioHILPlugin()
        p.teardown()
        with pytest.raises(RuntimeError):
            p.measure("snr")

    def test_supports_metric(self):
        p = AudioHILPlugin()
        assert p.supports_metric("snr") is True
        assert p.supports_metric("aec") is True
        assert p.supports_metric("thd") is True
        assert p.supports_metric("nope") is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  4. Display family plugin
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestDisplayPlugin:
    def test_info(self):
        p = DisplayHILPlugin()
        assert p.plugin_info.name == "display"
        assert p.plugin_info.family == PluginFamily.display
        assert set(DISPLAY_METRICS) == {"uniformity", "touch_latency", "color_accuracy"}

    def test_measure_uniformity(self):
        p = DisplayHILPlugin(device_id="panel-a")
        m = p.measure("uniformity", zones=16, test_pattern="grey50")
        assert m.metric_name == "uniformity"
        assert m.unit == "ratio"
        assert m.metadata["zones"] == 16

    def test_measure_touch_latency(self):
        p = DisplayHILPlugin()
        m = p.measure("touch_latency", touch_points=10, method="stylus")
        assert m.metric_name == "touch_latency"
        assert m.metadata["method"] == "stylus"

    def test_measure_color_accuracy(self):
        p = DisplayHILPlugin()
        m = p.measure("color_accuracy", profile="AdobeRGB", patches=48)
        assert m.metric_name == "color_accuracy"
        assert m.unit == "delta_e_2000"
        assert m.metadata["patches"] == 48

    def test_measure_unsupported(self):
        p = DisplayHILPlugin()
        with pytest.raises(ValueError, match="unsupported metric"):
            p.measure("brightness")

    def test_verify_pass(self):
        p = DisplayHILPlugin()
        m = Measurement(plugin_name="display", metric_name="uniformity", value=0.95)
        r = p.verify(m, {"min": 0.85})
        assert r.passed is True

    def test_verify_fail(self):
        p = DisplayHILPlugin()
        m = Measurement(plugin_name="display", metric_name="touch_latency", value=80.0)
        r = p.verify(m, {"max": 50.0})
        assert r.passed is False

    def test_teardown(self):
        p = DisplayHILPlugin()
        p.teardown()
        p.teardown()
        with pytest.raises(RuntimeError):
            p.measure("uniformity")

    def test_supports_metric(self):
        p = DisplayHILPlugin()
        assert p.supports_metric("uniformity") is True
        assert p.supports_metric("touch_latency") is True
        assert p.supports_metric("color_accuracy") is True
        assert p.supports_metric("fake") is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  5. Lifecycle runner
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestLifecycleRunner:
    def test_successful_run(self):
        p = CameraHILPlugin()
        specs = [
            ("focus_sharpness", {}, {"min": -1}),
            ("stream_latency", {"codec": "h264"}, {"max": 999}),
        ]
        summary = run_plugin_lifecycle(p, specs)
        assert summary.status == MeasurementStatus.ok
        assert len(summary.measurements) == 2
        assert len(summary.results) == 2
        assert summary.all_passed is True
        assert summary.duration_s > 0

    def test_failing_criteria(self):
        p = AudioHILPlugin()
        specs = [("snr", {}, {"min": 100})]
        summary = run_plugin_lifecycle(p, specs)
        assert summary.status == MeasurementStatus.fail
        assert summary.fail_count == 1

    def test_measure_error(self):
        p = CameraHILPlugin()
        specs = [("invalid_metric", {}, {})]
        summary = run_plugin_lifecycle(p, specs)
        assert summary.status == MeasurementStatus.error
        assert "unsupported metric" in summary.error_message

    def test_teardown_always_called(self):
        p = CameraHILPlugin()
        specs = [("focus_sharpness", {}, {})]
        run_plugin_lifecycle(p, specs)
        assert p._initialized is False

    def test_teardown_called_on_error(self):
        p = CameraHILPlugin()
        specs = [("bad", {}, {})]
        run_plugin_lifecycle(p, specs)
        assert p._initialized is False

    def test_empty_metrics(self):
        p = AudioHILPlugin()
        summary = run_plugin_lifecycle(p, [])
        assert summary.status == MeasurementStatus.ok
        assert len(summary.measurements) == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  6. HIL Registry
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestHILRegistry:
    def test_list_registered(self):
        plugins = list_registered_plugins()
        assert "camera" in plugins
        assert "audio" in plugins
        assert "display" in plugins
        assert plugins["camera"].family == PluginFamily.camera

    def test_get_plugin_class(self):
        assert get_plugin_class("camera") is CameraHILPlugin
        assert get_plugin_class("audio") is AudioHILPlugin
        assert get_plugin_class("display") is DisplayHILPlugin
        assert get_plugin_class("nonexistent") is None

    def test_create_plugin(self):
        p = create_plugin("camera", device_id="test-cam")
        assert isinstance(p, CameraHILPlugin)
        assert p.device_id == "test-cam"

    def test_create_unknown_raises(self):
        with pytest.raises(KeyError, match="not registered"):
            create_plugin("unknown")

    def test_register_custom(self):
        class CustomPlugin(HILPlugin):
            def __init__(self, **kw):
                self.plugin_info = PluginInfo(
                    name="custom", family=PluginFamily.camera,
                    version="0.1.0", supported_metrics=["x"],
                )
            def measure(self, metric, **params):
                return Measurement(plugin_name="custom", metric_name=metric, value=99)
            def verify(self, measurement, criteria):
                return VerifyResult(passed=True, metric_name=measurement.metric_name, measured_value=measurement.value)
            def teardown(self):
                pass

        register_builtin("custom-test", CustomPlugin)
        assert get_plugin_class("custom-test") is CustomPlugin
        p = create_plugin("custom-test")
        m = p.measure("x")
        assert m.value == 99


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  7. Skill pack HIL requirements
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSkillHILRequirements:
    def test_parse_simple_list(self, tmp_path):
        skill_dir = _make_skill_with_hil(tmp_path, "test-skill", ["camera", "audio"])
        reqs = parse_skill_hil_requirements(skill_dir)
        assert len(reqs) == 2
        assert reqs[0].plugin_name == "camera"
        assert reqs[1].plugin_name == "audio"
        assert reqs[0].metrics == []

    def test_parse_extended_format(self, tmp_path):
        hil_plugins = [
            {
                "name": "camera",
                "metrics": ["focus_sharpness", "stream_latency"],
                "criteria": {
                    "focus_sharpness": {"min": 100},
                    "stream_latency": {"max": 200},
                },
            }
        ]
        skill_dir = _make_skill_with_hil(tmp_path, "ext-skill", hil_plugins)
        reqs = parse_skill_hil_requirements(skill_dir)
        assert len(reqs) == 1
        assert reqs[0].plugin_name == "camera"
        assert reqs[0].metrics == ["focus_sharpness", "stream_latency"]
        assert reqs[0].criteria["focus_sharpness"] == {"min": 100}

    def test_parse_no_manifest(self, tmp_path):
        skill_dir = tmp_path / "no-manifest"
        skill_dir.mkdir()
        reqs = parse_skill_hil_requirements(skill_dir)
        assert reqs == []

    def test_parse_no_hil_key(self, tmp_path):
        skill_dir = tmp_path / "no-hil"
        skill_dir.mkdir()
        _write_yaml(skill_dir / "skill.yaml", {"schema_version": 1, "name": "no-hil"})
        reqs = parse_skill_hil_requirements(skill_dir)
        assert reqs == []

    def test_parse_mixed_format(self, tmp_path):
        hil_plugins = [
            "audio",
            {"name": "display", "metrics": ["uniformity"]},
        ]
        skill_dir = _make_skill_with_hil(tmp_path, "mixed-skill", hil_plugins)
        reqs = parse_skill_hil_requirements(skill_dir)
        assert len(reqs) == 2
        assert reqs[0].plugin_name == "audio"
        assert reqs[0].metrics == []
        assert reqs[1].plugin_name == "display"
        assert reqs[1].metrics == ["uniformity"]


class TestSkillHILValidation:
    def test_valid_simple(self, tmp_path):
        skill_dir = _make_skill_with_hil(tmp_path, "valid-skill", ["camera", "audio"])
        result = validate_skill_hil("valid-skill", skill_dir)
        assert result.ok is True
        assert result.missing_plugins == []

    def test_missing_plugin(self, tmp_path):
        skill_dir = _make_skill_with_hil(tmp_path, "bad-skill", ["camera", "lidar"])
        result = validate_skill_hil("bad-skill", skill_dir)
        assert result.ok is False
        assert "lidar" in result.missing_plugins

    def test_unsupported_metric(self, tmp_path):
        hil_plugins = [{"name": "camera", "metrics": ["focus_sharpness", "thermal_drift"]}]
        skill_dir = _make_skill_with_hil(tmp_path, "metric-skill", hil_plugins)
        result = validate_skill_hil("metric-skill", skill_dir)
        assert result.ok is False
        assert "thermal_drift" in result.missing_metrics.get("camera", [])

    def test_no_requirements(self, tmp_path):
        skill_dir = _make_skill_with_hil(tmp_path, "empty-skill", [])
        result = validate_skill_hil("empty-skill", skill_dir)
        assert result.ok is True

    def test_valid_extended(self, tmp_path):
        hil_plugins = [
            {"name": "audio", "metrics": ["snr", "aec"]},
            {"name": "display", "metrics": ["uniformity", "touch_latency"]},
        ]
        skill_dir = _make_skill_with_hil(tmp_path, "ext-valid", hil_plugins)
        result = validate_skill_hil("ext-valid", skill_dir)
        assert result.ok is True


class TestSkillHILRun:
    def test_run_simple(self, tmp_path):
        skill_dir = _make_skill_with_hil(tmp_path, "run-skill", ["camera"])
        summaries = run_skill_hil("run-skill", skill_dir)
        assert len(summaries) == 1
        assert summaries[0].plugin_name == "camera"
        assert len(summaries[0].measurements) == 3

    def test_run_with_criteria(self, tmp_path):
        hil_plugins = [
            {
                "name": "audio",
                "metrics": ["snr"],
                "criteria": {"snr": {"min": 60}},
            }
        ]
        skill_dir = _make_skill_with_hil(tmp_path, "crit-skill", hil_plugins)
        summaries = run_skill_hil("crit-skill", skill_dir)
        assert len(summaries) == 1
        assert summaries[0].fail_count >= 1

    def test_run_multiple_plugins(self, tmp_path):
        skill_dir = _make_skill_with_hil(
            tmp_path, "multi-skill", ["camera", "audio", "display"]
        )
        summaries = run_skill_hil("multi-skill", skill_dir)
        assert len(summaries) == 3
        names = {s.plugin_name for s in summaries}
        assert names == {"camera", "audio", "display"}

    def test_run_empty(self, tmp_path):
        skill_dir = _make_skill_with_hil(tmp_path, "empty-run", [])
        summaries = run_skill_hil("empty-run", skill_dir)
        assert summaries == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  8. Mock HIL plugin lifecycle (integration)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class MockHILPlugin(HILPlugin):
    """A mock plugin that simulates real hardware measurements."""

    def __init__(self, values: dict[str, float] | None = None, **_kw):
        self._values = values or {"temp": 25.0, "humidity": 60.0}
        self._torn_down = False
        self._measure_count = 0
        self.plugin_info = PluginInfo(
            name="mock",
            family=PluginFamily.camera,
            version="0.1.0",
            description="Mock HIL for testing",
            supported_metrics=list(self._values.keys()),
        )

    def measure(self, metric: str, **params: Any) -> Measurement:
        if self._torn_down:
            raise RuntimeError("plugin torn down")
        if metric not in self._values:
            raise ValueError(f"unsupported: {metric}")
        self._measure_count += 1
        return Measurement(
            plugin_name="mock",
            metric_name=metric,
            value=self._values[metric],
            unit="mock_unit",
            metadata={"call_number": self._measure_count},
        )

    def verify(self, measurement: Measurement, criteria: dict[str, Any]) -> VerifyResult:
        passed = True
        msgs = []
        v = measurement.value
        if "min" in criteria and v < criteria["min"]:
            passed = False
            msgs.append(f"{v} < {criteria['min']}")
        if "max" in criteria and v > criteria["max"]:
            passed = False
            msgs.append(f"{v} > {criteria['max']}")
        return VerifyResult(
            passed=passed,
            metric_name=measurement.metric_name,
            measured_value=v,
            criterion=str(criteria),
            message="; ".join(msgs) if msgs else "PASS",
        )

    def teardown(self) -> None:
        self._torn_down = True


class TestMockLifecycle:
    def test_full_lifecycle_pass(self):
        plugin = MockHILPlugin({"temp": 25.0, "humidity": 60.0})
        specs = [
            ("temp", {}, {"min": 20, "max": 30}),
            ("humidity", {}, {"min": 40, "max": 80}),
        ]
        summary = run_plugin_lifecycle(plugin, specs)
        assert summary.status == MeasurementStatus.ok
        assert summary.all_passed is True
        assert summary.pass_count == 2
        assert summary.fail_count == 0
        assert plugin._torn_down is True
        assert plugin._measure_count == 2

    def test_full_lifecycle_fail(self):
        plugin = MockHILPlugin({"temp": 50.0})
        specs = [("temp", {}, {"max": 40})]
        summary = run_plugin_lifecycle(plugin, specs)
        assert summary.status == MeasurementStatus.fail
        assert summary.fail_count == 1
        assert plugin._torn_down is True

    def test_lifecycle_error(self):
        plugin = MockHILPlugin({"temp": 25.0})
        specs = [("invalid", {}, {})]
        summary = run_plugin_lifecycle(plugin, specs)
        assert summary.status == MeasurementStatus.error
        assert "unsupported" in summary.error_message
        assert plugin._torn_down is True

    def test_sequential_measures_tracked(self):
        plugin = MockHILPlugin({"a": 1.0, "b": 2.0, "c": 3.0})
        specs = [("a", {}, {}), ("b", {}, {}), ("c", {}, {})]
        summary = run_plugin_lifecycle(plugin, specs)
        assert len(summary.measurements) == 3
        assert summary.measurements[0].metadata["call_number"] == 1
        assert summary.measurements[2].metadata["call_number"] == 3

    def test_teardown_failure_captured(self):
        class BadTeardownPlugin(MockHILPlugin):
            def teardown(self):
                raise RuntimeError("hardware stuck")

        plugin = BadTeardownPlugin({"temp": 25.0})
        specs = [("temp", {}, {})]
        summary = run_plugin_lifecycle(plugin, specs)
        assert summary.status == MeasurementStatus.error
        assert "teardown failed" in summary.error_message

    def test_register_and_run_mock(self):
        register_builtin("mock-lifecycle", MockHILPlugin)
        p = create_plugin("mock-lifecycle", values={"x": 42.0})
        m = p.measure("x")
        assert m.value == 42.0
        v = p.verify(m, {"min": 40, "max": 50})
        assert v.passed is True
        p.teardown()
        assert p._torn_down is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  9. Edge cases
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestEdgeCases:
    def test_verify_no_criteria(self):
        p = CameraHILPlugin()
        m = Measurement(plugin_name="camera", metric_name="focus_sharpness", value=0.0)
        r = p.verify(m, {})
        assert r.passed is True

    def test_verify_both_min_max(self):
        p = AudioHILPlugin()
        m = Measurement(plugin_name="audio", metric_name="snr", value=50.0)
        r = p.verify(m, {"min": 60, "max": 100})
        assert r.passed is False
        assert "< min" in r.message

    def test_multiple_failures_in_verify(self):
        p = DisplayHILPlugin()
        m = Measurement(plugin_name="display", metric_name="uniformity", value=200.0)
        r = p.verify(m, {"min": 300, "max": 100})
        assert r.passed is False
        assert "< min" in r.message

    def test_parse_invalid_yaml(self, tmp_path):
        skill_dir = tmp_path / "bad-yaml"
        skill_dir.mkdir()
        (skill_dir / "skill.yaml").write_text("not: [valid: yaml: {", encoding="utf-8")
        reqs = parse_skill_hil_requirements(skill_dir)
        assert reqs == []

    def test_parse_non_dict_manifest(self, tmp_path):
        skill_dir = tmp_path / "list-yaml"
        skill_dir.mkdir()
        (skill_dir / "skill.yaml").write_text("- item1\n- item2\n", encoding="utf-8")
        reqs = parse_skill_hil_requirements(skill_dir)
        assert reqs == []

    def test_parse_non_list_hil_plugins(self, tmp_path):
        skill_dir = tmp_path / "str-hil"
        skill_dir.mkdir()
        _write_yaml(skill_dir / "skill.yaml", {
            "schema_version": 1, "name": "str-hil", "hil_plugins": "camera",
        })
        reqs = parse_skill_hil_requirements(skill_dir)
        assert reqs == []
