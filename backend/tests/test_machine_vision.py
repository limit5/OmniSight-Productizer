"""C24 — L4-CORE-24 Machine vision & industrial imaging framework tests (#254).

Covers: GenICam driver abstraction, GigE/USB3/CameraLink/CoaXPress transports,
hardware trigger + encoder sync, multi-camera calibration (checkerboard +
bundle adjustment), line-scan support, PLC integration (Modbus/OPC-UA),
test recipes, artifacts, and gate validation.
"""

from __future__ import annotations


import pytest

from backend import machine_vision as mv


# ═══════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def _reset_config():
    mv._cfg = None
    yield
    mv._cfg = None


@pytest.fixture
def gige_camera():
    cam = mv.create_camera("gige_vision", "test_model")
    cam.connect()
    cam.configure(mv.CameraConfig(transport_id="gige_vision", camera_model="test_model"))
    yield cam
    if cam.state != mv.CameraState.disconnected:
        cam.disconnect()


@pytest.fixture
def usb3_camera():
    cam = mv.create_camera("usb3_vision", "test_model")
    cam.connect()
    cam.configure(mv.CameraConfig(transport_id="usb3_vision", camera_model="test_model"))
    yield cam
    if cam.state != mv.CameraState.disconnected:
        cam.disconnect()


@pytest.fixture
def encoder():
    return mv.create_encoder("quadrature_ab", 1024, 1)


@pytest.fixture
def calibration_frames():
    return [mv._generate_synthetic_frame(640, 480, "Mono8", "checkerboard", seed=i) for i in range(15)]


# ═══════════════════════════════════════════════════════════════════════
# 1. Config loading
# ═══════════════════════════════════════════════════════════════════════


class TestConfigLoading:
    def test_load_config(self):
        cfg = mv._load_config()
        assert isinstance(cfg, dict)
        assert "transports" in cfg
        assert "genicam_features" in cfg
        assert "camera_models" in cfg
        assert "trigger_modes" in cfg
        assert "calibration_methods" in cfg

    def test_config_cached(self):
        cfg1 = mv._load_config()
        cfg2 = mv._load_config()
        assert cfg1 is cfg2

    def test_config_has_plc_integration(self):
        cfg = mv._load_config()
        assert "plc_integration" in cfg
        plc = cfg["plc_integration"]
        assert "modbus_registers" in plc
        assert "opcua_nodes" in plc
        assert "trigger_mapping" in plc

    def test_config_has_encoder(self):
        cfg = mv._load_config()
        assert "encoder_config" in cfg
        enc = cfg["encoder_config"]
        assert "interface_types" in enc
        assert "resolutions" in enc

    def test_config_has_line_scan(self):
        cfg = mv._load_config()
        assert "line_scan_config" in cfg
        ls = cfg["line_scan_config"]
        assert "scan_directions" in ls
        assert "transport_mechanisms" in ls


# ═══════════════════════════════════════════════════════════════════════
# 2. Transport listing & discovery
# ═══════════════════════════════════════════════════════════════════════


class TestTransports:
    def test_list_transports(self):
        transports = mv.list_transports()
        assert len(transports) == 4
        ids = {t["transport_id"] for t in transports}
        assert ids == {"gige_vision", "usb3_vision", "camera_link", "coaxpress"}

    def test_transport_fields(self):
        transports = mv.list_transports()
        for t in transports:
            assert t["name"]
            assert t["standard"]
            assert t["backend"]
            assert t["max_bandwidth_mbps"] > 0
            assert len(t["features"]) > 0

    def test_gige_vision_details(self):
        transports = mv.list_transports()
        gige = [t for t in transports if t["transport_id"] == "gige_vision"][0]
        assert gige["standard"] == "GigE Vision 2.2"
        assert gige["backend"] == "aravis"
        assert gige["max_bandwidth_mbps"] == 1000
        assert "gvsp_streaming" in gige["features"]

    def test_usb3_vision_details(self):
        transports = mv.list_transports()
        usb3 = [t for t in transports if t["transport_id"] == "usb3_vision"][0]
        assert usb3["standard"] == "USB3 Vision 1.1"
        assert usb3["max_bandwidth_mbps"] == 5000


# ═══════════════════════════════════════════════════════════════════════
# 3. GenICam features
# ═══════════════════════════════════════════════════════════════════════


class TestGenICamFeatures:
    def test_list_all_features(self):
        features = mv.list_genicam_features()
        assert len(features) >= 10
        ids = {f["feature_id"] for f in features}
        assert "exposure_time" in ids
        assert "gain" in ids
        assert "pixel_format" in ids

    def test_list_by_category(self):
        acq = mv.list_genicam_features(category="AcquisitionControl")
        img = mv.list_genicam_features(category="ImageFormatControl")
        assert len(acq) > 0
        assert len(img) > 0
        all_f = mv.list_genicam_features()
        assert len(all_f) >= len(acq) + len(img)

    def test_feature_fields(self):
        features = mv.list_genicam_features()
        for f in features:
            assert f["feature_id"]
            assert f["name"]
            assert f["category"]
            assert f["feature_type"]

    def test_set_valid_feature(self, gige_camera):
        assert gige_camera.set_feature("exposure_time", 5000.0)
        assert gige_camera.get_feature("exposure_time") == 5000.0

    def test_set_enum_feature(self, gige_camera):
        assert gige_camera.set_feature("pixel_format", "Mono8")
        assert gige_camera.get_feature("pixel_format") == "Mono8"

    def test_reject_invalid_enum(self, gige_camera):
        assert not gige_camera.set_feature("pixel_format", "InvalidFormat")

    def test_reject_out_of_range(self, gige_camera):
        assert not gige_camera.set_feature("gain", 100.0)

    def test_reject_unknown_feature(self, gige_camera):
        assert not gige_camera.set_feature("nonexistent_feature", 42)


# ═══════════════════════════════════════════════════════════════════════
# 4. Camera models
# ═══════════════════════════════════════════════════════════════════════


class TestCameraModels:
    def test_list_all_models(self):
        models = mv.list_camera_models()
        assert len(models) >= 6

    def test_filter_area_scan(self):
        area = mv.list_camera_models(scan_type="area")
        for m in area:
            assert m["scan_type"] == "area"
        assert len(area) >= 3

    def test_filter_line_scan(self):
        line = mv.list_camera_models(scan_type="line")
        for m in line:
            assert m["scan_type"] == "line"
        assert len(line) >= 2

    def test_model_fields(self):
        models = mv.list_camera_models()
        for m in models:
            assert m["model_id"]
            assert m["name"]
            assert m["vendor"]
            assert m["transport"]
            assert len(m["pixel_formats"]) > 0


# ═══════════════════════════════════════════════════════════════════════
# 5. Camera lifecycle
# ═══════════════════════════════════════════════════════════════════════


class TestCameraLifecycle:
    @pytest.mark.parametrize("transport", ["gige_vision", "usb3_vision", "camera_link", "coaxpress"])
    def test_full_lifecycle(self, transport):
        cam = mv.create_camera(transport, "test_model")
        assert cam.state == mv.CameraState.disconnected

        assert cam.connect()
        assert cam.state == mv.CameraState.connected

        cfg = mv.CameraConfig(transport_id=transport, camera_model="test_model")
        assert cam.configure(cfg)
        assert cam.state == mv.CameraState.configured

        frame = cam.acquire()
        assert frame.frame_number > 0
        assert len(frame.pixel_data) > 0
        assert frame.width == 640
        assert frame.height == 480
        assert frame.transport_id == transport

        assert cam.disconnect()
        assert cam.state == mv.CameraState.disconnected

    def test_double_connect(self):
        cam = mv.create_camera("gige_vision")
        assert cam.connect()
        assert not cam.connect()
        cam.disconnect()

    def test_double_disconnect(self):
        cam = mv.create_camera("gige_vision")
        assert not cam.disconnect()

    def test_configure_disconnected(self):
        cam = mv.create_camera("gige_vision")
        assert not cam.configure(mv.CameraConfig(transport_id="gige_vision"))

    def test_acquire_disconnected(self):
        cam = mv.create_camera("gige_vision")
        frame = cam.acquire()
        assert frame.frame_number == -1
        assert len(frame.pixel_data) == 0

    def test_unknown_transport(self):
        with pytest.raises(ValueError, match="Unknown transport"):
            mv.create_camera("unknown_transport")

    def test_transport_info(self, gige_camera):
        info = gige_camera.get_transport_info()
        assert info["transport"] == "gige_vision"
        assert info["backend"] == "aravis"
        assert "gvsp_streaming" in info["features"]

    def test_usb3_transport_info(self, usb3_camera):
        info = usb3_camera.get_transport_info()
        assert info["transport"] == "usb3_vision"
        assert info["backend"] == "libusb"
        assert info["hot_plug"] is True

    def test_camera_status(self, gige_camera):
        status = gige_camera.get_status()
        assert "camera_id" in status
        assert status["state"] == "configured"
        assert "features" in status

    def test_multiple_acquisitions(self, gige_camera):
        frames = [gige_camera.acquire() for _ in range(5)]
        assert frames[-1].frame_number == 5
        assert gige_camera.frame_count == 5
        frame_nums = [f.frame_number for f in frames]
        assert frame_nums == [1, 2, 3, 4, 5]


# ═══════════════════════════════════════════════════════════════════════
# 6. Hardware trigger & encoder sync
# ═══════════════════════════════════════════════════════════════════════


class TestTriggerModes:
    def test_list_trigger_modes(self):
        modes = mv.list_trigger_modes()
        assert len(modes) >= 7
        ids = {m["mode_id"] for m in modes}
        assert "free_running" in ids
        assert "hardware_rising" in ids
        assert "encoder_position" in ids
        assert "action_command" in ids

    def test_configure_trigger(self, gige_camera):
        assert gige_camera.configure_trigger("hardware_rising", "Line0", "rising_edge")
        status = gige_camera.get_status()
        assert status["trigger_mode"] == "hardware_rising"

    def test_all_trigger_modes(self, gige_camera):
        for mode in mv.TriggerModeId:
            assert gige_camera.configure_trigger(mode.value)

    def test_invalid_trigger_mode(self, gige_camera):
        assert not gige_camera.configure_trigger("invalid_mode")

    def test_triggered_frame_has_timestamp(self, gige_camera):
        gige_camera.configure_trigger("hardware_rising")
        frame = gige_camera.acquire()
        assert frame.trigger_timestamp is not None

    def test_free_running_no_trigger_ts(self, gige_camera):
        gige_camera.configure_trigger("free_running")
        frame = gige_camera.acquire()
        assert frame.trigger_timestamp is None


class TestEncoder:
    def test_create_encoder(self):
        enc = mv.create_encoder("quadrature_ab", 1024, 1)
        assert enc.config.interface_type == "quadrature_ab"
        assert enc.config.resolution == 1024

    @pytest.mark.parametrize("iface", ["quadrature_ab", "quadrature_abz", "step_direction", "pulse_counter"])
    def test_all_interfaces(self, iface):
        enc = mv.create_encoder(iface)
        assert enc.config.interface_type == iface

    def test_invalid_interface(self):
        with pytest.raises(ValueError, match="Unknown encoder interface"):
            mv.create_encoder("invalid_interface")

    def test_read_initial_position(self, encoder):
        state = encoder.read_position()
        assert state.position == 0
        assert state.direction == "forward"

    def test_simulate_movement(self, encoder):
        state = encoder.simulate_movement(100)
        assert state.position == 100

    def test_reverse_direction(self):
        enc = mv.create_encoder("quadrature_ab", 1024, 1, direction="reverse")
        state = enc.simulate_movement(50)
        assert state.position == -50

    def test_reset(self, encoder):
        encoder.simulate_movement(500)
        encoder.reset()
        state = encoder.read_position()
        assert state.position == 0
        assert state.index_count == 0

    def test_trigger_positions(self):
        enc = mv.create_encoder("quadrature_ab", 1024, 4)
        positions = enc.get_trigger_positions(0, 20)
        assert positions == [0, 4, 8, 12, 16, 20]

    def test_index_reset(self):
        enc = mv.create_encoder("quadrature_abz", 100, 1)
        enc._config.index_reset = True
        enc.simulate_movement(250)
        state = enc.read_position()
        assert state.index_count >= 2

    def test_list_encoder_interfaces(self):
        interfaces = mv.list_encoder_interfaces()
        assert len(interfaces) == 1
        info = interfaces[0]
        assert "quadrature_ab" in info["interface_types"]
        assert len(info["resolutions"]) > 0


# ═══════════════════════════════════════════════════════════════════════
# 7. Multi-camera calibration
# ═══════════════════════════════════════════════════════════════════════


class TestCalibration:
    def test_list_calibration_methods(self):
        methods = mv.list_calibration_methods()
        assert len(methods) >= 6
        ids = {m["method_id"] for m in methods}
        assert "checkerboard" in ids
        assert "stereo_pair" in ids
        assert "multi_camera_bundle" in ids
        assert "hand_eye" in ids

    def test_single_camera_checkerboard(self, calibration_frames):
        result = mv.calibrate_camera(calibration_frames, "checkerboard")
        assert result.success
        assert result.reprojection_error < 1000.0
        assert len(result.camera_matrix) == 3
        assert len(result.camera_matrix[0]) == 3
        assert len(result.distortion_coeffs) == 5
        assert result.num_frames_used == 15

    def test_charuco_calibration(self, calibration_frames):
        result = mv.calibrate_camera(calibration_frames, "charuco")
        assert result.success

    def test_circle_grid_calibration(self, calibration_frames):
        result = mv.calibrate_camera(calibration_frames, "circle_grid")
        assert result.success

    def test_insufficient_frames(self):
        result = mv.calibrate_camera([b"one", b"two"], "checkerboard")
        assert not result.success
        assert result.reprojection_error == float("inf")

    def test_invalid_method(self, calibration_frames):
        result = mv.calibrate_camera(calibration_frames, "invalid_method")
        assert not result.success

    def test_stereo_calibration(self, calibration_frames):
        frames_r = [mv._generate_synthetic_frame(640, 480, "Mono8", "checkerboard", seed=i + 100)
                     for i in range(15)]
        result = mv.calibrate_stereo(calibration_frames, frames_r)
        assert result.success
        assert result.method == "stereo_pair"
        assert len(result.R) == 3
        assert len(result.T) == 3
        assert len(result.E) == 3
        assert len(result.F) == 3
        assert len(result.camera_matrix_left) == 3
        assert len(result.camera_matrix_right) == 3
        assert len(result.distortion_left) == 5
        assert len(result.distortion_right) == 5

    def test_stereo_insufficient_frames(self):
        result = mv.calibrate_stereo([b"a"], [b"b"])
        assert not result.success

    def test_multi_camera_bundle_adjustment(self, calibration_frames):
        frame_sets = [
            [mv._generate_synthetic_frame(640, 480, "Mono8", "checkerboard", seed=i + j * 100)
             for i in range(10)]
            for j in range(4)
        ]
        result = mv.calibrate_multi_camera(frame_sets)
        assert result.success
        assert result.num_cameras == 4
        assert len(result.camera_matrices) == 4
        assert len(result.distortion_coeffs) == 4
        assert len(result.extrinsics) == 4
        assert len(result.reprojection_errors) == 4
        assert result.mean_reprojection_error < 1000.0

    def test_multi_camera_single_camera(self):
        result = mv.calibrate_multi_camera([[b"frame"] * 5])
        assert not result.success

    def test_multi_camera_insufficient_per_camera(self):
        result = mv.calibrate_multi_camera([[b"a", b"b"], [b"c"] * 5])
        assert not result.success


# ═══════════════════════════════════════════════════════════════════════
# 8. Line-scan support
# ═══════════════════════════════════════════════════════════════════════


class TestLineScan:
    def test_list_line_scan_config(self):
        ls = mv.list_line_scan_config()
        assert "scan_directions" in ls
        assert "forward" in ls["scan_directions"]
        assert "transport_mechanisms" in ls
        assert "conveyor_belt" in ls["transport_mechanisms"]
        assert len(ls["typical_line_rates_hz"]) > 0

    def test_generate_lines(self):
        lines = mv.generate_line_scan_lines(1024, 100)
        assert len(lines) == 100
        for line in lines:
            assert len(line) == 1024

    def test_compose_forward(self):
        lines = mv.generate_line_scan_lines(512, 64)
        image = mv.compose_line_scan(lines, 512, direction="forward")
        assert image.width == 512
        assert image.height == 64
        assert image.total_lines == 64
        assert image.direction == "forward"
        assert len(image.pixel_data) == 512 * 64

    def test_compose_reverse(self):
        lines = mv.generate_line_scan_lines(256, 32)
        image = mv.compose_line_scan(lines, 256, direction="reverse")
        assert image.direction == "reverse"
        assert image.height == 32

    def test_compose_bidirectional(self):
        lines = mv.generate_line_scan_lines(256, 32)
        image = mv.compose_line_scan(lines, 256, direction="bidirectional")
        assert image.direction == "bidirectional"
        assert image.height == 32

    def test_line_rate(self):
        lines = mv.generate_line_scan_lines(1024, 100)
        image = mv.compose_line_scan(lines, 1024, line_rate_hz=50000.0)
        assert image.line_rate_hz == 50000.0
        assert image.timestamp_end > image.timestamp_start

    def test_multi_byte_pixel_format(self):
        lines = mv.generate_line_scan_lines(256, 32, pixel_format="RGB8")
        for line in lines:
            assert len(line) == 256 * 3

    def test_line_scan_camera_models(self):
        models = mv.list_camera_models(scan_type="line")
        for m in models:
            assert m["scan_type"] == "line"
            assert m["max_line_rate"] > 0


# ═══════════════════════════════════════════════════════════════════════
# 9. PLC integration (Modbus/OPC-UA via CORE-13)
# ═══════════════════════════════════════════════════════════════════════


class TestPLCIntegration:
    def test_get_plc_context(self):
        ctx = mv.get_plc_context()
        assert ctx.protocol == "modbus+opcua"
        assert len(ctx.registers) >= 6
        assert len(ctx.nodes) >= 4
        assert len(ctx.trigger_mapping) >= 3

    def test_read_modbus_register(self):
        r = mv.read_plc_register("modbus", 40001)
        assert r["status"] == "ok"
        assert r["name"] == "trigger_count"
        assert r["simulated"] is True

    def test_read_modbus_not_found(self):
        r = mv.read_plc_register("modbus", 99999)
        assert r["status"] == "not_found"

    def test_write_modbus_writable(self):
        r = mv.write_plc_register("modbus", 40003, 1)
        assert r["status"] == "ok"
        assert r["value"] == 1
        assert r["name"] == "inspection_result"

    def test_write_modbus_read_only(self):
        r = mv.write_plc_register("modbus", 40001, 0)
        assert r["status"] == "read_only"

    def test_read_opcua_node(self):
        r = mv.read_plc_register("opcua", "ns=2;s=Camera.TriggerCount")
        assert r["status"] == "ok"
        assert r["name"] == "trigger_count"

    def test_read_opcua_not_found(self):
        r = mv.read_plc_register("opcua", "ns=99;s=Missing")
        assert r["status"] == "not_found"

    def test_write_opcua_writable(self):
        r = mv.write_plc_register("opcua", "ns=2;s=Camera.InspectionResult", 1)
        assert r["status"] == "ok"

    def test_write_opcua_read_only(self):
        r = mv.write_plc_register("opcua", "ns=2;s=Camera.TriggerCount", 0)
        assert r["status"] == "read_only"

    def test_unsupported_protocol(self):
        r = mv.read_plc_register("profinet", 1)
        assert r["status"] == "unsupported_protocol"

    def test_trigger_mapping(self):
        ctx = mv.get_plc_context()
        cam_trigger = [m for m in ctx.trigger_mapping if m.get("plc_signal") == "camera_trigger_out"]
        assert len(cam_trigger) == 1
        assert cam_trigger[0]["camera_input"] == "Line0"

    def test_modbus_discrete_input(self):
        r = mv.read_plc_register("modbus", 10001)
        assert r["status"] == "ok"
        assert r["name"] == "part_present"


# ═══════════════════════════════════════════════════════════════════════
# 10. Synthetic frame generation
# ═══════════════════════════════════════════════════════════════════════


class TestSyntheticFrames:
    def test_gradient_frame(self):
        frame = mv._generate_synthetic_frame(100, 100, "Mono8", "gradient")
        assert len(frame) == 100 * 100

    def test_checkerboard_frame(self):
        frame = mv._generate_synthetic_frame(200, 200, "Mono8", "checkerboard")
        assert len(frame) == 200 * 200

    def test_noise_frame(self):
        frame = mv._generate_synthetic_frame(64, 64, "Mono8", "noise")
        assert len(frame) == 64 * 64

    def test_rgb_frame(self):
        frame = mv._generate_synthetic_frame(100, 100, "RGB8", "gradient")
        assert len(frame) == 100 * 100 * 3

    def test_mono16_frame(self):
        frame = mv._generate_synthetic_frame(100, 100, "Mono16", "gradient")
        assert len(frame) == 100 * 100 * 2

    def test_deterministic(self):
        f1 = mv._generate_synthetic_frame(64, 64, "Mono8", "gradient", seed=42)
        f2 = mv._generate_synthetic_frame(64, 64, "Mono8", "gradient", seed=42)
        assert f1 == f2


# ═══════════════════════════════════════════════════════════════════════
# 11. Test recipes
# ═══════════════════════════════════════════════════════════════════════


class TestTestRecipes:
    def test_list_recipes(self):
        recipes = mv.list_test_recipes()
        assert len(recipes) >= 8
        ids = {r["recipe_id"] for r in recipes}
        assert "transport_discovery" in ids
        assert "camera_lifecycle" in ids
        assert "trigger_modes" in ids
        assert "multi_camera_calibration" in ids
        assert "line_scan_acquisition" in ids
        assert "plc_roundtrip" in ids
        assert "genicam_feature_access" in ids
        assert "error_handling" in ids

    def test_transport_discovery_recipe(self):
        result = mv.run_test_recipe("transport_discovery")
        assert result.status == "passed"
        assert result.passed >= 5
        assert result.failed == 0

    def test_camera_lifecycle_recipe(self):
        result = mv.run_test_recipe("camera_lifecycle")
        assert result.status == "passed"
        assert result.passed == 4
        assert result.failed == 0

    def test_trigger_mode_recipe(self):
        result = mv.run_test_recipe("trigger_modes")
        assert result.status == "passed"
        assert result.failed == 0

    def test_calibration_recipe(self):
        result = mv.run_test_recipe("multi_camera_calibration")
        assert result.status == "passed"
        assert result.passed == 3
        assert result.failed == 0

    def test_line_scan_recipe(self):
        result = mv.run_test_recipe("line_scan_acquisition")
        assert result.status == "passed"
        assert result.passed == 4
        assert result.failed == 0

    def test_plc_roundtrip_recipe(self):
        result = mv.run_test_recipe("plc_roundtrip")
        assert result.status == "passed"
        assert result.failed == 0

    def test_genicam_feature_recipe(self):
        result = mv.run_test_recipe("genicam_feature_access")
        assert result.status == "passed"
        assert result.failed == 0

    def test_error_handling_recipe(self):
        result = mv.run_test_recipe("error_handling")
        assert result.status == "passed"
        assert result.failed == 0

    def test_unknown_recipe(self):
        result = mv.run_test_recipe("nonexistent")
        assert result.status == "error"


# ═══════════════════════════════════════════════════════════════════════
# 12. Artifacts & gate validation
# ═══════════════════════════════════════════════════════════════════════


class TestArtifactsAndGate:
    def test_list_artifacts(self):
        artifacts = mv.list_artifacts()
        assert len(artifacts) >= 4
        ids = {a["artifact_id"] for a in artifacts}
        assert "genicam_feature_report" in ids
        assert "calibration_result" in ids
        assert "trigger_timing_report" in ids
        assert "plc_register_map" in ids

    def test_artifact_fields(self):
        artifacts = mv.list_artifacts()
        for a in artifacts:
            assert a["artifact_id"]
            assert a["kind"]
            assert a["description"]

    def test_validate_gate(self):
        result = mv.validate_gate()
        assert result["verdict"] == "passed"
        assert result["total_recipes"] >= 8
        assert result["total_passed"] > 0
        assert result["total_failed"] == 0
        assert result["duration_ms"] >= 0


# ═══════════════════════════════════════════════════════════════════════
# 13. Edge cases and integration
# ═══════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    def test_frame_hash(self):
        h1 = mv._frame_hash(b"test_data_1")
        h2 = mv._frame_hash(b"test_data_2")
        assert h1 != h2
        assert len(h1) == 16

    def test_camera_hash(self):
        h = mv._camera_hash("test_camera")
        assert isinstance(h, int)

    def test_encoder_with_large_movement(self):
        enc = mv.create_encoder("quadrature_ab", 100, 1)
        enc._config.index_reset = True
        enc.simulate_movement(1000)
        state = enc.read_position()
        assert state.position == 1000
        assert state.index_count >= 10

    def test_compose_empty_lines(self):
        image = mv.compose_line_scan([], 100)
        assert image.height == 0
        assert len(image.pixel_data) == 0

    def test_multi_camera_calibration_two_cameras(self, calibration_frames):
        frame_sets = [calibration_frames[:10],
                      [mv._generate_synthetic_frame(640, 480, "Mono8", "checkerboard", seed=i + 200)
                       for i in range(10)]]
        result = mv.calibrate_multi_camera(frame_sets)
        assert result.success
        assert result.num_cameras == 2

    def test_plc_write_not_found(self):
        r = mv.write_plc_register("modbus", 99999, 42)
        assert r["status"] == "not_found"

    def test_plc_write_unsupported(self):
        r = mv.write_plc_register("profinet", 1, 0)
        assert r["status"] == "unsupported_protocol"
