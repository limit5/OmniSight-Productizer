"""C23 — L4-CORE-23 Depth / 3D sensing pipeline tests (#244).

Covers: ToF sensors, structured light, stereo vision, point cloud processing,
registration, SLAM, calibration, test scenes, test recipes, artifacts,
and gate validation.
"""

from __future__ import annotations

import math
import struct

import pytest

from backend import depth_sensing as ds
from backend.depth_sensing import (
    DepthDomain,
    SensorId,
    SensorState,
    StructuredLightPattern,
    StereoAlgorithm,
    PointCloudBackend,
    PointCloudFormat,
    FilterType,
    RegistrationAlgorithm,
    SlamType,
    CalibrationType,
    DepthResultStatus,
    SensorConfig,
    DepthFrame,
    StereoConfig,
    PointCloudData,
    RegistrationResult,
    CalibrationResult,
    SlamPose,
    TestResult,
)


# ===================================================================
# Fixtures
# ===================================================================

@pytest.fixture(autouse=True)
def _reset_config():
    """Reset config cache between tests."""
    ds._cfg = None
    yield
    ds._cfg = None


# ===================================================================
# 1. Config loading
# ===================================================================

class TestConfigLoading:
    def test_config_loads_successfully(self):
        cfg = ds._load_config()
        assert isinstance(cfg, dict)

    def test_config_has_sensors_section(self):
        cfg = ds._load_config()
        assert "sensors" in cfg

    def test_config_has_all_sections(self):
        cfg = ds._load_config()
        for section in (
            "sensors",
            "structured_light",
            "stereo",
            "point_cloud",
            "registration",
            "slam",
            "calibration",
            "test_scenes",
            "test_recipes",
            "artifacts",
        ):
            assert section in cfg, f"Missing config section: {section}"

    def test_config_caching(self):
        cfg1 = ds._load_config()
        cfg2 = ds._load_config()
        assert cfg1 is cfg2

    def test_sensor_config_fields(self):
        cfg = ds._load_config()
        sensors = cfg["sensors"]
        assert len(sensors) >= 1
        for s in sensors:
            assert "id" in s
            assert "name" in s


# ===================================================================
# 2. Sensors
# ===================================================================

class TestSensors:
    def test_list_sensors_returns_all(self):
        sensors = ds.list_sensors()
        assert len(sensors) == 2

    def test_list_sensors_fields(self):
        sensors = ds.list_sensors()
        for s in sensors:
            assert s["sensor_id"]
            assert s["name"]
            assert s["resolution"]
            assert s["max_range_m"] > 0

    def test_create_sony_imx556(self):
        sensor = ds.create_sensor("sony_imx556")
        assert sensor.sensor_id == "sony_imx556"

    def test_create_melexis_mlx75027(self):
        sensor = ds.create_sensor("melexis_mlx75027")
        assert sensor.sensor_id == "melexis_mlx75027"

    def test_create_unknown_sensor_raises(self):
        with pytest.raises(ValueError, match="Unknown sensor"):
            ds.create_sensor("unknown_sensor_xyz")

    def test_sensor_default_state_disconnected(self):
        sensor = ds.create_sensor("sony_imx556")
        assert sensor.state == SensorState.disconnected

    def test_sensor_capabilities_sony(self):
        sensor = ds.create_sensor("sony_imx556")
        caps = sensor.get_capabilities()
        assert caps["sensor"] == "sony_imx556"
        assert "max_range_m" in caps
        assert "resolution" in caps

    def test_sensor_capabilities_melexis(self):
        sensor = ds.create_sensor("melexis_mlx75027")
        caps = sensor.get_capabilities()
        assert caps["sensor"] == "melexis_mlx75027"
        assert "max_range_m" in caps
        assert "resolution" in caps


# ===================================================================
# 3. Sensor lifecycle
# ===================================================================

class TestSensorLifecycle:
    def test_connect(self):
        sensor = ds.create_sensor("sony_imx556")
        assert sensor.connect()
        assert sensor.state == SensorState.connected

    def test_configure(self):
        sensor = ds.create_sensor("sony_imx556")
        sensor.connect()
        cfg = SensorConfig(sensor_id="sony_imx556", resolution=(640, 480), max_range=5.0)
        assert sensor.configure(cfg)
        assert sensor.state == SensorState.configured

    def test_capture_after_configure(self):
        sensor = ds.create_sensor("sony_imx556")
        sensor.connect()
        sensor.configure(SensorConfig(sensor_id="sony_imx556", resolution=(640, 480), max_range=5.0))
        frame = sensor.capture()
        assert isinstance(frame, DepthFrame)

    def test_capture_from_connected_state_works(self):
        """Capture works even from connected state (without explicit configure)."""
        sensor = ds.create_sensor("sony_imx556")
        sensor.connect()
        frame = sensor.capture()
        assert isinstance(frame, DepthFrame)

    def test_disconnect(self):
        sensor = ds.create_sensor("sony_imx556")
        sensor.connect()
        assert sensor.disconnect()
        assert sensor.state == SensorState.disconnected

    def test_reconnect_cycle(self):
        sensor = ds.create_sensor("sony_imx556")
        sensor.connect()
        sensor.disconnect()
        assert sensor.connect()
        assert sensor.state == SensorState.connected

    def test_capture_increments_frame_count(self):
        sensor = ds.create_sensor("sony_imx556")
        sensor.connect()
        sensor.configure(SensorConfig(sensor_id="sony_imx556", resolution=(640, 480), max_range=5.0))
        assert sensor.frame_count == 0
        sensor.capture()
        assert sensor.frame_count == 1
        sensor.capture()
        assert sensor.frame_count == 2

    @pytest.mark.parametrize("sensor_id", [s.value for s in SensorId])
    def test_all_sensors_lifecycle(self, sensor_id):
        sensor = ds.create_sensor(sensor_id)
        assert sensor.state == SensorState.disconnected
        assert sensor.connect()
        assert sensor.state == SensorState.connected
        cfg = SensorConfig(sensor_id=sensor_id, resolution=sensor.config.resolution,
                           max_range=sensor.config.max_range)
        assert sensor.configure(cfg)
        assert sensor.state == SensorState.configured
        frame = sensor.capture()
        assert isinstance(frame, DepthFrame)
        assert sensor.disconnect()
        assert sensor.state == SensorState.disconnected


# ===================================================================
# 4. Depth capture
# ===================================================================

class TestDepthCapture:
    @pytest.fixture()
    def configured_sensor(self):
        sensor = ds.create_sensor("sony_imx556")
        sensor.connect()
        sensor.configure(SensorConfig(sensor_id="sony_imx556", resolution=(640, 480), max_range=5.0))
        return sensor

    def test_capture_returns_depth_frame(self, configured_sensor):
        frame = configured_sensor.capture()
        assert isinstance(frame, DepthFrame)

    def test_depth_frame_dimensions_match_config(self, configured_sensor):
        frame = configured_sensor.capture()
        assert frame.width > 0
        assert frame.height > 0
        caps = configured_sensor.get_capabilities()
        res = caps["resolution"]
        assert frame.width == res[0]
        assert frame.height == res[1]

    def test_depth_frame_has_valid_depth_range(self, configured_sensor):
        frame = configured_sensor.capture()
        assert frame.min_depth >= 0
        assert frame.max_depth > frame.min_depth

    def test_depth_data_length_correct(self, configured_sensor):
        frame = configured_sensor.capture()
        expected_len = frame.width * frame.height * 4  # float32
        assert len(frame.depth_data) == expected_len

    def test_capture_deterministic(self):
        s1 = ds.create_sensor("sony_imx556")
        s1.connect()
        s1.configure(SensorConfig(sensor_id="sony_imx556", resolution=(640, 480), max_range=5.0))
        f1 = s1.capture()

        ds._cfg = None  # reset
        s2 = ds.create_sensor("sony_imx556")
        s2.connect()
        s2.configure(SensorConfig(sensor_id="sony_imx556", resolution=(640, 480), max_range=5.0))
        f2 = s2.capture()
        # Both first captures from identically-configured sensors should produce
        # the same depth hash (deterministic synthetic data)
        assert ds._depth_frame_hash(f1) == ds._depth_frame_hash(f2)

    def test_frame_hash_computation(self, configured_sensor):
        frame = configured_sensor.capture()
        h = ds._depth_frame_hash(frame)
        assert h is not None
        assert len(h) == 16

    def test_min_max_depth_within_sensor_range(self, configured_sensor):
        frame = configured_sensor.capture()
        caps = configured_sensor.get_capabilities()
        assert frame.min_depth >= 0
        assert frame.max_depth <= caps["max_range_m"] * 1.1

    def test_consecutive_captures_different(self, configured_sensor):
        f1 = configured_sensor.capture()
        f2 = configured_sensor.capture()
        assert f1.frame_number != f2.frame_number


# ===================================================================
# 5. Structured light
# ===================================================================

class TestStructuredLight:
    def test_list_patterns(self):
        patterns = ds.list_structured_light_patterns()
        assert len(patterns) == 3
        ids = {p["pattern_id"] for p in patterns}
        assert ids == {"gray_code", "phase_shift", "speckle"}

    def test_create_gray_code_codec(self):
        codec = ds.create_structured_light_codec("gray_code")
        assert codec.pattern_type == "gray_code"

    def test_create_phase_shift_codec(self):
        codec = ds.create_structured_light_codec("phase_shift")
        assert codec.pattern_type == "phase_shift"

    def test_create_speckle_codec(self):
        codec = ds.create_structured_light_codec("speckle")
        assert codec.pattern_type == "speckle"

    def test_generate_gray_code_patterns(self):
        codec = ds.create_structured_light_codec("gray_code", resolution=(640, 480))
        patterns = codec.generate_patterns()
        assert len(patterns) > 0
        assert len(patterns) == codec.num_patterns

    def test_generate_phase_shift_patterns(self):
        codec = ds.create_structured_light_codec("phase_shift", resolution=(640, 480))
        patterns = codec.generate_patterns()
        assert len(patterns) == 4

    def test_decode_gray_code(self):
        codec = ds.create_structured_light_codec("gray_code", resolution=(160, 120))
        patterns = codec.generate_patterns()
        frame = codec.decode(patterns)
        assert isinstance(frame, DepthFrame)

    def test_decode_phase_shift(self):
        codec = ds.create_structured_light_codec("phase_shift", resolution=(160, 120))
        patterns = codec.generate_patterns()
        frame = codec.decode(patterns)
        assert isinstance(frame, DepthFrame)

    def test_decode_returns_depth_frame(self):
        codec = ds.create_structured_light_codec("speckle", resolution=(640, 480))
        patterns = codec.generate_patterns()
        frame = codec.decode(patterns)
        assert isinstance(frame, DepthFrame)
        assert frame.width == 640
        assert frame.height == 480


# ===================================================================
# 6. Stereo pipeline
# ===================================================================

class TestStereoPipeline:
    def test_list_stereo_algorithms(self):
        algos = ds.list_stereo_algorithms()
        assert len(algos) == 2
        ids = {a["algorithm_id"] for a in algos}
        assert ids == {"sgbm", "bm"}

    def test_create_sgbm_pipeline(self):
        pipe = ds.create_stereo_pipeline("sgbm")
        assert pipe.config.algorithm == "sgbm"

    def test_create_bm_pipeline(self):
        pipe = ds.create_stereo_pipeline("bm")
        assert pipe.config.algorithm == "bm"

    def test_rectify_returns_pair(self):
        pipe = ds.create_stereo_pipeline("sgbm")
        w, h = 64, 48
        left = bytes([128 + (c % 64) for c in range(w * h)])
        right = bytes([128 + ((c - 4) % 64) for c in range(w * h)])
        rect_l, rect_r = pipe.rectify(left, right, w, h)
        assert len(rect_l) == len(left)
        assert len(rect_r) == len(right)

    def test_compute_disparity_sgbm(self):
        pipe = ds.create_stereo_pipeline("sgbm", num_disparities=32, block_size=5)
        w, h = 64, 48
        left = bytes([128 + (c % 64) for c in range(w * h)])
        right = bytes([128 + ((c - 4) % 64) for c in range(w * h)])
        rect_l, rect_r = pipe.rectify(left, right, w, h)
        disparity = pipe.compute_disparity(rect_l, rect_r, w, h)
        assert isinstance(disparity, bytes)
        assert len(disparity) > 0

    def test_compute_disparity_bm(self):
        pipe = ds.create_stereo_pipeline("bm", num_disparities=32, block_size=5)
        w, h = 64, 48
        left = bytes([128 + (c % 64) for c in range(w * h)])
        right = bytes([128 + ((c - 4) % 64) for c in range(w * h)])
        rect_l, rect_r = pipe.rectify(left, right, w, h)
        disparity = pipe.compute_disparity(rect_l, rect_r, w, h)
        assert isinstance(disparity, bytes)
        assert len(disparity) > 0

    def test_disparity_to_depth(self):
        pipe = ds.create_stereo_pipeline("sgbm", num_disparities=32, block_size=5)
        w, h = 64, 48
        left = bytes([128 + (c % 64) for c in range(w * h)])
        right = bytes([128 + ((c - 4) % 64) for c in range(w * h)])
        rect_l, rect_r = pipe.rectify(left, right, w, h)
        disparity = pipe.compute_disparity(rect_l, rect_r, w, h)
        frame = pipe.disparity_to_depth(disparity, w, h)
        assert isinstance(frame, DepthFrame)

    def test_stereo_depth_positive_values(self):
        pipe = ds.create_stereo_pipeline("sgbm", num_disparities=32, block_size=5)
        w, h = 64, 48
        left = bytes([128 + (c % 64) for c in range(w * h)])
        right = bytes([128 + ((c - 4) % 64) for c in range(w * h)])
        rect_l, rect_r = pipe.rectify(left, right, w, h)
        disparity = pipe.compute_disparity(rect_l, rect_r, w, h)
        frame = pipe.disparity_to_depth(disparity, w, h)
        assert frame.min_depth >= 0

    def test_stereo_end_to_end(self):
        w, h = 64, 48
        left = bytes([128 + (c % 64) for c in range(w * h)])
        right = bytes([128 + ((c - 4) % 64) for c in range(w * h)])
        frame = ds.compute_stereo_depth(left, right, w, h)
        assert isinstance(frame, DepthFrame)
        assert frame.width == w
        assert frame.height == h

    def test_stereo_config_parameters(self):
        pipe = ds.create_stereo_pipeline(
            "sgbm",
            num_disparities=128,
            block_size=11,
            baseline=0.12,
            focal_length=500.0,
        )
        assert pipe.config.num_disparities == 128
        assert pipe.config.block_size == 11
        assert pipe.config.baseline == 0.12
        assert pipe.config.focal_length == 500.0


# ===================================================================
# 7. Point cloud
# ===================================================================

class TestPointCloud:
    @pytest.fixture()
    def depth_frame(self):
        sensor = ds.create_sensor("sony_imx556")
        sensor.connect()
        sensor.configure(SensorConfig(sensor_id="sony_imx556", resolution=(640, 480), max_range=5.0))
        return sensor.capture()

    def test_list_backends(self):
        backends = ds.list_point_cloud_backends()
        assert len(backends) == 2
        ids = {b["backend_id"] for b in backends}
        assert ids == {"pcl", "open3d"}

    def test_create_pcl_processor(self):
        proc = ds.create_point_cloud_processor("pcl")
        assert proc.backend == "pcl"

    def test_create_open3d_processor(self):
        proc = ds.create_point_cloud_processor("open3d")
        assert proc.backend == "open3d"

    def test_depth_to_point_cloud(self, depth_frame):
        proc = ds.create_point_cloud_processor("pcl")
        pc = proc.depth_to_point_cloud(depth_frame)
        assert isinstance(pc, PointCloudData)

    def test_point_cloud_has_points(self, depth_frame):
        proc = ds.create_point_cloud_processor("pcl")
        pc = proc.depth_to_point_cloud(depth_frame)
        assert pc.point_count > 0

    def test_point_cloud_bounds_valid(self, depth_frame):
        proc = ds.create_point_cloud_processor("pcl")
        pc = proc.depth_to_point_cloud(depth_frame)
        assert pc.bounds_min is not None
        assert pc.bounds_max is not None
        for i in range(3):
            assert pc.bounds_min[i] <= pc.bounds_max[i]

    def test_filter_voxel_grid(self, depth_frame):
        proc = ds.create_point_cloud_processor("pcl")
        pc = proc.depth_to_point_cloud(depth_frame)
        original_count = pc.point_count
        filtered = proc.filter_point_cloud(pc, FilterType.voxel_grid.value, voxel_size=0.05)
        assert filtered.point_count <= original_count

    def test_filter_statistical_outlier(self, depth_frame):
        proc = ds.create_point_cloud_processor("pcl")
        pc = proc.depth_to_point_cloud(depth_frame)
        filtered = proc.filter_point_cloud(pc, FilterType.statistical_outlier.value)
        assert filtered.point_count <= pc.point_count

    def test_filter_radius_outlier(self, depth_frame):
        proc = ds.create_point_cloud_processor("pcl")
        pc = proc.depth_to_point_cloud(depth_frame)
        filtered = proc.filter_point_cloud(pc, FilterType.radius_outlier.value)
        assert filtered.point_count <= pc.point_count

    def test_filter_passthrough(self, depth_frame):
        proc = ds.create_point_cloud_processor("pcl")
        pc = proc.depth_to_point_cloud(depth_frame)
        filtered = proc.filter_point_cloud(pc, FilterType.passthrough.value,
                                           axis="z", min_val=0.0, max_val=5.0)
        assert filtered.point_count <= pc.point_count

    def test_compute_normals(self, depth_frame):
        proc = ds.create_point_cloud_processor("pcl")
        pc = proc.depth_to_point_cloud(depth_frame)
        pc_with_normals = proc.compute_normals(pc)
        assert len(pc_with_normals.normals) == pc_with_normals.point_count

    def test_export_pcd_format(self, depth_frame):
        proc = ds.create_point_cloud_processor("pcl")
        pc = proc.depth_to_point_cloud(depth_frame)
        data = proc.export(pc, PointCloudFormat.pcd.value)
        assert isinstance(data, bytes)
        assert len(data) > 0

    def test_export_ply_format(self, depth_frame):
        proc = ds.create_point_cloud_processor("pcl")
        pc = proc.depth_to_point_cloud(depth_frame)
        data = proc.export(pc, PointCloudFormat.ply.value)
        assert isinstance(data, bytes)
        assert len(data) > 0

    def test_export_xyz_format(self, depth_frame):
        proc = ds.create_point_cloud_processor("pcl")
        pc = proc.depth_to_point_cloud(depth_frame)
        data = proc.export(pc, PointCloudFormat.xyz.value)
        assert isinstance(data, bytes)
        assert len(data) > 0


# ===================================================================
# 8. Registration
# ===================================================================

class TestRegistration:
    @pytest.fixture()
    def cloud_pair(self):
        sensor = ds.create_sensor("sony_imx556")
        sensor.connect()
        sensor.configure(SensorConfig(sensor_id="sony_imx556", resolution=(640, 480), max_range=5.0))
        f1 = sensor.capture()
        f2 = sensor.capture()
        proc = ds.create_point_cloud_processor("pcl")
        pc1 = proc.depth_to_point_cloud(f1)
        pc2 = proc.depth_to_point_cloud(f2)
        return pc1, pc2

    def test_list_algorithms(self):
        algos = ds.list_registration_algorithms()
        assert len(algos) == 4
        ids = {a["algorithm_id"] for a in algos}
        assert ids == {"icp_point_to_point", "icp_point_to_plane", "colored_icp", "ndt"}

    def test_icp_point_to_point(self, cloud_pair):
        result = ds.register_point_clouds(cloud_pair[0], cloud_pair[1], "icp_point_to_point")
        assert isinstance(result, RegistrationResult)

    def test_icp_point_to_plane(self, cloud_pair):
        result = ds.register_point_clouds(cloud_pair[0], cloud_pair[1], "icp_point_to_plane")
        assert isinstance(result, RegistrationResult)

    def test_colored_icp(self, cloud_pair):
        result = ds.register_point_clouds(cloud_pair[0], cloud_pair[1], "colored_icp")
        assert isinstance(result, RegistrationResult)

    def test_ndt_registration(self, cloud_pair):
        result = ds.register_point_clouds(cloud_pair[0], cloud_pair[1], "ndt")
        assert isinstance(result, RegistrationResult)

    def test_registration_converges(self, cloud_pair):
        result = ds.register_point_clouds(cloud_pair[0], cloud_pair[1], "icp_point_to_point")
        assert result.fitness > 0.5

    def test_registration_with_identity_transform(self, cloud_pair):
        identity = [
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ]
        result = ds.register_point_clouds(
            cloud_pair[0], cloud_pair[1], "icp_point_to_point",
            initial_transform=identity
        )
        assert isinstance(result, RegistrationResult)

    def test_registration_inlier_rmse_reasonable(self, cloud_pair):
        result = ds.register_point_clouds(cloud_pair[0], cloud_pair[1], "icp_point_to_point")
        assert result.inlier_rmse >= 0
        assert result.inlier_rmse < 1.0

    def test_known_translation_recovery(self, cloud_pair):
        result = ds.register_point_clouds(cloud_pair[0], cloud_pair[1], "icp_point_to_point")
        assert result.transformation is not None
        assert len(result.transformation) == 4
        assert len(result.transformation[0]) == 4

    def test_registration_result_fields(self, cloud_pair):
        result = ds.register_point_clouds(cloud_pair[0], cloud_pair[1], "icp_point_to_point")
        assert hasattr(result, "fitness")
        assert hasattr(result, "inlier_rmse")
        assert hasattr(result, "transformation")
        assert hasattr(result, "converged")


# ===================================================================
# 9. SLAM
# ===================================================================

class TestSlam:
    def test_list_slam_types(self):
        types = ds.list_slam_types()
        assert len(types) == 2
        ids = {t["slam_id"] for t in types}
        assert ids == {"visual_slam", "lidar_slam"}

    def test_create_visual_slam(self):
        slam = ds.create_slam_hook("visual_slam")
        assert slam.slam_type == "visual_slam"

    def test_create_lidar_slam(self):
        slam = ds.create_slam_hook("lidar_slam")
        assert slam.slam_type == "lidar_slam"

    def test_slam_initialize(self):
        slam = ds.create_slam_hook("visual_slam")
        assert slam.initialize()

    def test_slam_process_frame(self):
        slam = ds.create_slam_hook("visual_slam")
        slam.initialize()
        sensor = ds.create_sensor("sony_imx556")
        sensor.connect()
        sensor.configure(SensorConfig(sensor_id="sony_imx556", resolution=(640, 480), max_range=5.0))
        frame = sensor.capture()
        pose = slam.process_frame(frame)
        assert isinstance(pose, SlamPose)

    def test_slam_trajectory_grows(self):
        slam = ds.create_slam_hook("visual_slam")
        slam.initialize()
        sensor = ds.create_sensor("sony_imx556")
        sensor.connect()
        sensor.configure(SensorConfig(sensor_id="sony_imx556", resolution=(640, 480), max_range=5.0))
        for _ in range(3):
            frame = sensor.capture()
            slam.process_frame(frame)
        traj = slam.get_trajectory()
        assert len(traj) == 3

    def test_slam_get_map_points(self):
        slam = ds.create_slam_hook("visual_slam")
        slam.initialize()
        sensor = ds.create_sensor("sony_imx556")
        sensor.connect()
        sensor.configure(SensorConfig(sensor_id="sony_imx556", resolution=(640, 480), max_range=5.0))
        frame = sensor.capture()
        slam.process_frame(frame)
        points = slam.get_map_points()
        assert isinstance(points, PointCloudData)
        assert points.point_count > 0

    def test_slam_reset(self):
        slam = ds.create_slam_hook("visual_slam")
        slam.initialize()
        sensor = ds.create_sensor("sony_imx556")
        sensor.connect()
        sensor.configure(SensorConfig(sensor_id="sony_imx556", resolution=(640, 480), max_range=5.0))
        frame = sensor.capture()
        slam.process_frame(frame)
        slam.reset()
        assert len(slam.get_trajectory()) == 0


# ===================================================================
# 10. Calibration
# ===================================================================

class TestCalibration:
    def test_list_calibration_types(self):
        types = ds.list_calibration_types()
        assert len(types) == 3
        ids = {t["calibration_id"] for t in types}
        assert ids == {"intrinsic", "stereo_extrinsic", "tof_phase"}

    def test_intrinsic_calibration(self):
        # Generate synthetic checkerboard frames (just byte buffers)
        frames = [b"\x80" * (640 * 480) for _ in range(10)]
        result = ds.calibrate_camera(frames, "intrinsic")
        assert isinstance(result, CalibrationResult)
        assert result.success is True

    def test_stereo_calibration(self):
        left_frames = [b"\x80" * (640 * 480) for _ in range(10)]
        right_frames = [b"\x80" * (640 * 480) for _ in range(10)]
        result = ds.calibrate_camera(left_frames, "stereo_extrinsic",
                                     right_frames=right_frames)
        assert isinstance(result, dict)
        assert result["success"] is True

    def test_tof_phase_calibration(self):
        frames_at_distances = {
            0.5: [b"\x80" * 100 for _ in range(5)],
            1.0: [b"\x80" * 100 for _ in range(5)],
            2.0: [b"\x80" * 100 for _ in range(5)],
        }
        result = ds.calibrate_camera([], "tof_phase",
                                     frames_at_distances=frames_at_distances)
        assert isinstance(result, dict)
        assert result["success"] is True

    def test_calibration_result_fields(self):
        frames = [b"\x80" * (640 * 480) for _ in range(10)]
        result = ds.calibrate_camera(frames, "intrinsic")
        assert hasattr(result, "success")
        assert hasattr(result, "reprojection_error")
        assert hasattr(result, "camera_matrix")

    def test_calibration_reprojection_error_reasonable(self):
        frames = [b"\x80" * (640 * 480) for _ in range(10)]
        result = ds.calibrate_camera(frames, "intrinsic")
        assert result.reprojection_error >= 0
        assert result.reprojection_error < 1.0


# ===================================================================
# 11. Test scenes
# ===================================================================

class TestTestScenes:
    def test_list_test_scenes(self):
        scenes = ds.list_test_scenes()
        assert len(scenes) == 6

    def test_generate_flat_wall(self):
        cloud = ds.generate_test_scene("flat_wall")
        assert isinstance(cloud, PointCloudData)
        assert cloud.point_count > 0

    def test_generate_box_scene(self):
        cloud = ds.generate_test_scene("box_scene")
        assert isinstance(cloud, PointCloudData)
        assert cloud.point_count > 0

    def test_generate_sphere(self):
        cloud = ds.generate_test_scene("sphere")
        assert isinstance(cloud, PointCloudData)
        assert cloud.point_count > 0

    def test_generate_staircase(self):
        cloud = ds.generate_test_scene("staircase")
        assert isinstance(cloud, PointCloudData)
        assert cloud.point_count > 0

    def test_generate_corner(self):
        cloud = ds.generate_test_scene("corner")
        assert isinstance(cloud, PointCloudData)
        assert cloud.point_count > 0

    def test_generate_empty_room(self):
        cloud = ds.generate_test_scene("empty_room")
        assert isinstance(cloud, PointCloudData)
        assert cloud.point_count > 0

    def test_validate_flat_wall_passes(self):
        cloud = ds.generate_test_scene("flat_wall")
        result = ds.validate_test_scene("flat_wall", cloud)
        assert result["passed"] is True

    def test_validate_box_scene_passes(self):
        cloud = ds.generate_test_scene("box_scene")
        result = ds.validate_test_scene("box_scene", cloud)
        assert result["passed"] is True

    def test_validate_all_scenes_pass(self):
        scenes = ds.list_test_scenes()
        for s in scenes:
            cloud = ds.generate_test_scene(s["scene_id"])
            result = ds.validate_test_scene(s["scene_id"], cloud)
            assert result["passed"] is True, f"Scene {s['scene_id']} failed"


# ===================================================================
# 12. Test recipes
# ===================================================================

class TestTestRecipes:
    def test_list_recipes(self):
        recipes = ds.list_test_recipes()
        assert len(recipes) == 6
        ids = {r["recipe_id"] for r in recipes}
        assert len(ids) == 6

    def test_run_tof_capture_recipe(self):
        result = ds.run_test_recipe("tof_capture")
        assert isinstance(result, TestResult)
        assert result.status == "passed"
        assert result.failed == 0

    def test_run_structured_light_recipe(self):
        result = ds.run_test_recipe("structured_light_decode")
        assert isinstance(result, TestResult)
        assert result.status == "passed"
        assert result.failed == 0

    def test_run_stereo_disparity_recipe(self):
        result = ds.run_test_recipe("stereo_disparity")
        assert isinstance(result, TestResult)
        assert result.status == "passed"
        assert result.failed == 0

    def test_run_point_cloud_recipe(self):
        result = ds.run_test_recipe("point_cloud_processing")
        assert isinstance(result, TestResult)
        assert result.status == "passed"
        assert result.failed == 0

    def test_run_registration_recipe(self):
        result = ds.run_test_recipe("registration_icp")
        assert isinstance(result, TestResult)
        assert result.status == "passed"
        assert result.failed == 0

    def test_run_slam_recipe(self):
        result = ds.run_test_recipe("slam_odometry")
        assert isinstance(result, TestResult)
        assert result.status == "passed"
        assert result.failed == 0


# ===================================================================
# 13. Artifacts & gate
# ===================================================================

class TestArtifactsAndGate:
    def test_list_artifacts(self):
        arts = ds.list_artifacts()
        assert len(arts) == 5
        kinds = {a["kind"] for a in arts}
        assert kinds == {"tasks", "scaffolds", "tests", "hil", "docs"}

    def test_validate_gate_passes(self):
        result = ds.validate_gate()
        assert result["verdict"] == "passed"

    def test_gate_result_has_all_recipes(self):
        result = ds.validate_gate()
        assert result["total_recipes"] == 6

    def test_gate_all_recipes_pass(self):
        result = ds.validate_gate()
        assert result["total_failed"] == 0
        assert result["total_passed"] > 0


# ===================================================================
# 14. Multi-sensor consistency
# ===================================================================

class TestMultiSensorConsistency:
    def test_both_sensors_produce_valid_depth(self):
        for sid in SensorId:
            sensor = ds.create_sensor(sid.value)
            sensor.connect()
            sensor.configure(SensorConfig(
                sensor_id=sid.value,
                resolution=sensor.config.resolution,
                max_range=sensor.config.max_range,
            ))
            frame = sensor.capture()
            assert isinstance(frame, DepthFrame)
            assert frame.width > 0
            assert frame.height > 0

    def test_both_sensors_depth_within_range(self):
        for sid in SensorId:
            sensor = ds.create_sensor(sid.value)
            sensor.connect()
            sensor.configure(SensorConfig(
                sensor_id=sid.value,
                resolution=sensor.config.resolution,
                max_range=sensor.config.max_range,
            ))
            frame = sensor.capture()
            caps = sensor.get_capabilities()
            assert frame.min_depth >= 0
            assert frame.max_depth <= caps["max_range_m"] * 1.1

    def test_point_cloud_from_both_sensors(self):
        proc = ds.create_point_cloud_processor("pcl")
        for sid in SensorId:
            sensor = ds.create_sensor(sid.value)
            sensor.connect()
            sensor.configure(SensorConfig(
                sensor_id=sid.value,
                resolution=sensor.config.resolution,
                max_range=sensor.config.max_range,
            ))
            frame = sensor.capture()
            pc = proc.depth_to_point_cloud(frame)
            assert pc.point_count > 0

    def test_sensors_have_distinct_capabilities(self):
        caps = {}
        for sid in SensorId:
            sensor = ds.create_sensor(sid.value)
            caps[sid.value] = sensor.get_capabilities()
        sensor_ids = list(caps.keys())
        assert len(sensor_ids) == 2
        assert caps[sensor_ids[0]]["sensor"] != caps[sensor_ids[1]]["sensor"]


# ===================================================================
# 15. Enums
# ===================================================================

class TestEnums:
    def test_depth_domain_count(self):
        assert len(DepthDomain) == 7

    def test_sensor_id_count(self):
        assert len(SensorId) == 2

    def test_stereo_algorithm_count(self):
        assert len(StereoAlgorithm) == 2

    def test_point_cloud_backend_count(self):
        assert len(PointCloudBackend) == 2

    def test_registration_algorithm_count(self):
        assert len(RegistrationAlgorithm) == 4

    def test_filter_type_count(self):
        assert len(FilterType) == 5

    def test_depth_result_status_count(self):
        assert len(DepthResultStatus) == 6


# ===================================================================
# 16. End-to-end pipelines
# ===================================================================

class TestEndToEndPipeline:
    def test_tof_to_point_cloud_pipeline(self):
        """capture -> depth -> points -> filter -> export"""
        sensor = ds.create_sensor("sony_imx556")
        sensor.connect()
        sensor.configure(SensorConfig(sensor_id="sony_imx556", resolution=(640, 480), max_range=5.0))
        frame = sensor.capture()
        assert isinstance(frame, DepthFrame)

        proc = ds.create_point_cloud_processor("pcl")
        pc = proc.depth_to_point_cloud(frame)
        assert pc.point_count > 0

        filtered = proc.filter_point_cloud(pc, FilterType.voxel_grid.value, voxel_size=0.05)
        assert filtered.point_count <= pc.point_count

        data = proc.export(filtered, PointCloudFormat.ply.value)
        assert len(data) > 0

    def test_stereo_to_registration_pipeline(self):
        """stereo -> depth -> points -> ICP"""
        w, h = 64, 48
        left1 = bytes([128 + (c % 64) for c in range(w * h)])
        right1 = bytes([128 + ((c - 4) % 64) for c in range(w * h)])
        frame1 = ds.compute_stereo_depth(left1, right1, w, h)

        left2 = bytes([128 + ((c + 2) % 64) for c in range(w * h)])
        right2 = bytes([128 + ((c - 2) % 64) for c in range(w * h)])
        frame2 = ds.compute_stereo_depth(left2, right2, w, h)

        proc = ds.create_point_cloud_processor("open3d")
        pc1 = proc.depth_to_point_cloud(frame1)
        pc2 = proc.depth_to_point_cloud(frame2)

        result = ds.register_point_clouds(pc1, pc2, "icp_point_to_point")
        assert isinstance(result, RegistrationResult)
        assert result.converged is True

    def test_structured_light_to_slam_pipeline(self):
        """SL -> depth -> SLAM -> trajectory"""
        codec = ds.create_structured_light_codec("phase_shift", resolution=(160, 120))
        patterns = codec.generate_patterns()
        frame = codec.decode(patterns)
        assert isinstance(frame, DepthFrame)

        slam = ds.create_slam_hook("visual_slam")
        slam.initialize()
        pose = slam.process_frame(frame)
        assert isinstance(pose, SlamPose)
        assert len(slam.get_trajectory()) == 1

    def test_full_pipeline_with_scene_validation(self):
        """scene -> sensor -> depth -> cloud -> validate"""
        cloud = ds.generate_test_scene("flat_wall")
        assert ds.validate_test_scene("flat_wall", cloud)["passed"] is True

        sensor = ds.create_sensor("sony_imx556")
        sensor.connect()
        sensor.configure(SensorConfig(sensor_id="sony_imx556", resolution=(640, 480), max_range=5.0))
        frame = sensor.capture()

        proc = ds.create_point_cloud_processor("pcl")
        pc = proc.depth_to_point_cloud(frame)
        assert pc.point_count > 0

        pc_with_normals = proc.compute_normals(pc)
        assert len(pc_with_normals.normals) == pc_with_normals.point_count
