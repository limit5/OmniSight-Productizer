"""Scaffold: Depth / 3D Sensing Pipeline Integration Example.

Demonstrates the full depth-sensing workflow:
  1. Creating and configuring a ToF sensor
  2. Capturing depth frames
  3. Running stereo disparity
  4. Generating point clouds
  5. Running ICP registration

Usage:
    python -m configs.skills.depth_sensing.scaffolds.sensor_integration
"""

# --- 1. ToF Sensor: Create, Connect, Capture ---

from backend.depth_sensing import create_tof_sensor, ToFConfig

tof_config = ToFConfig(
    sensor_id="sony_imx556",
    resolution=(640, 480),
    fps=30,
    modulation_freq_mhz=100,
)
sensor = create_tof_sensor("sony_imx556", tof_config)
sensor.connect()
depth_frame = sensor.capture()
print(f"ToF depth frame: shape={depth_frame.shape}, dtype={depth_frame.dtype}")
sensor.disconnect()


# --- 2. Stereo Disparity Pipeline ---

from backend.depth_sensing import StereoPipeline, StereoConfig

stereo_cfg = StereoConfig(
    algorithm="sgbm",
    num_disparities=128,
    block_size=9,
    p1=8 * 3 * 9 * 9,
    p2=32 * 3 * 9 * 9,
)
pipeline = StereoPipeline(stereo_cfg)

# Assume left_img, right_img, and calib are loaded from files
# rectified_left, rectified_right = pipeline.rectify(left_img, right_img, calib)
# disparity = pipeline.compute_disparity(rectified_left, rectified_right)
# print(f"Disparity map: shape={disparity.shape}, range=[{disparity.min()}, {disparity.max()}]")


# --- 3. Point Cloud Generation ---

from backend.depth_sensing import PointCloudEngine

engine = PointCloudEngine(backend="open3d")

# From a depth map captured by the ToF sensor
# cloud = engine.from_depth_map(depth_frame, intrinsics)
# cloud = engine.statistical_outlier_removal(cloud, nb_neighbors=20, std_ratio=2.0)
# engine.save(cloud, "output.ply")
# print(f"Point cloud: {cloud.num_points} points")


# --- 4. ICP Registration ---

from backend.depth_sensing import register_clouds, RegistrationConfig

reg_cfg = RegistrationConfig(
    method="icp",
    max_iterations=50,
    tolerance=1e-6,
)

# Align two consecutive point clouds
# transform = register_clouds(source_cloud, target_cloud, reg_cfg)
# aligned = source_cloud.transform(transform)
# print(f"ICP fitness: {transform.fitness:.4f}, RMSE: {transform.inlier_rmse:.4f}")


# --- 5. SLAM Hook ---

from backend.depth_sensing import SLAMHook

slam = SLAMHook(backend="visual")
# slam.initialize(intrinsics, initial_pose)
# for cloud, odom in depth_stream:
#     slam.update(cloud, odom)
# global_map = slam.get_map()
# print(f"SLAM map: {global_map.num_points} points, {slam.num_keyframes} keyframes")
