"""C14 — Sensor fusion library tests.

Covers: config loading, IMU driver lookup, GPS NMEA parsing, GPS UBX parsing,
barometer drivers, barometric altitude, EKF orientation, calibration routines,
test recipes, trajectory fixtures, SoC compatibility, cert artifacts, audit
logging, and REST endpoint smoke tests.
"""

import math
import struct

import pytest

from backend.sensor_fusion import (
    CalibrationStatus,
    EKFState,
    IMUSample,
    NMEASentenceType,
    SensorBus,
    SensorType,
    TestCategory,
    TestStatus,
    altitude_to_pressure,
    build_ubx_message,
    check_soc_compatibility,
    clear_sensor_fusion_certs,
    evaluate_ekf_against_fixture,
    generate_cert_artifacts,
    generate_rotation_trajectory,
    generate_static_trajectory,
    get_barometer_driver,
    get_calibration_profile,
    get_ekf_profile,
    get_gps_protocol,
    get_imu_driver,
    get_recipes_by_category,
    get_recipes_by_sensor_type,
    get_sensor_fusion_certs,
    get_test_recipe,
    get_trajectory_fixture,
    list_artifact_definitions,
    list_barometer_drivers,
    list_calibration_profiles,
    list_ekf_profiles,
    list_gps_protocols,
    list_imu_drivers,
    list_test_recipes,
    list_trajectory_fixtures,
    parse_nmea_sentence,
    parse_ubx_message,
    pressure_to_altitude,
    register_sensor_fusion_cert,
    reload_sensor_fusion_config_for_tests,
    run_ekf_orientation,
    run_imu_calibration,
    run_sensor_test,
)


@pytest.fixture(autouse=True)
def _reload_config():
    reload_sensor_fusion_config_for_tests()
    clear_sensor_fusion_certs()
    yield
    reload_sensor_fusion_config_for_tests()
    clear_sensor_fusion_certs()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Config loading
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestConfigLoading:
    def test_imu_drivers_loaded(self):
        drivers = list_imu_drivers()
        assert len(drivers) == 3

    def test_gps_protocols_loaded(self):
        protocols = list_gps_protocols()
        assert len(protocols) == 2

    def test_barometer_drivers_loaded(self):
        drivers = list_barometer_drivers()
        assert len(drivers) == 2

    def test_ekf_profiles_loaded(self):
        profiles = list_ekf_profiles()
        assert len(profiles) == 2

    def test_calibration_profiles_loaded(self):
        profiles = list_calibration_profiles()
        assert len(profiles) == 3

    def test_test_recipes_loaded(self):
        recipes = list_test_recipes()
        assert len(recipes) == 13

    def test_trajectory_fixtures_loaded(self):
        fixtures = list_trajectory_fixtures()
        assert len(fixtures) == 4

    def test_artifact_definitions_loaded(self):
        defs = list_artifact_definitions()
        assert len(defs) == 5


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  IMU driver queries
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestIMUDrivers:
    def test_get_mpu6050(self):
        d = get_imu_driver("mpu6050")
        assert d is not None
        assert d.name == "MPU-6050"
        assert d.vendor == "InvenSense / TDK"
        assert d.axes == 6
        assert 16 in d.accel_range_g
        assert 2000 in d.gyro_range_dps

    def test_get_lsm6ds3(self):
        d = get_imu_driver("lsm6ds3")
        assert d is not None
        assert d.name == "LSM6DS3"
        assert d.step_counter is True

    def test_get_bmi270(self):
        d = get_imu_driver("bmi270")
        assert d is not None
        assert d.name == "BMI270"
        assert d.requires_config_upload is True
        assert d.gesture_recognition is True

    def test_get_nonexistent(self):
        assert get_imu_driver("nonexistent") is None

    def test_mpu6050_registers(self):
        d = get_imu_driver("mpu6050")
        assert "who_am_i" in d.registers
        assert d.registers["who_am_i"].expected == "0x68"

    def test_lsm6ds3_registers(self):
        d = get_imu_driver("lsm6ds3")
        assert d.registers["who_am_i"].expected == "0x69"

    def test_bmi270_registers(self):
        d = get_imu_driver("bmi270")
        assert d.registers["chip_id"].expected == "0x24"

    def test_imu_driver_to_dict(self):
        d = get_imu_driver("mpu6050")
        dd = d.to_dict()
        assert dd["driver_id"] == "mpu6050"
        assert dd["name"] == "MPU-6050"
        assert isinstance(dd["accel_range_g"], list)

    def test_each_driver_has_compatible_socs(self):
        for d in list_imu_drivers():
            assert len(d.compatible_socs) > 0

    def test_each_driver_has_init_sequence(self):
        for d in list_imu_drivers():
            assert len(d.init_sequence) > 0

    def test_mpu6050_sample_rate(self):
        d = get_imu_driver("mpu6050")
        assert d.sample_rate_hz == 1000

    def test_lsm6ds3_sample_rate(self):
        d = get_imu_driver("lsm6ds3")
        assert d.sample_rate_hz == 6660

    def test_bmi270_fifo_depth(self):
        d = get_imu_driver("bmi270")
        assert d.fifo_depth == 6144


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GPS protocol queries
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestGPSProtocols:
    def test_get_nmea(self):
        p = get_gps_protocol("nmea")
        assert p is not None
        assert p.name == "NMEA 0183"
        assert p.baud_default == 9600

    def test_get_ubx(self):
        p = get_gps_protocol("ubx")
        assert p is not None
        assert p.name == "u-blox UBX Binary Protocol"
        assert p.sync_chars == [0xB5, 0x62]

    def test_nmea_sentences(self):
        p = get_gps_protocol("nmea")
        sentence_ids = [s["id"] for s in p.supported_sentences]
        assert "GGA" in sentence_ids
        assert "RMC" in sentence_ids

    def test_ubx_message_classes(self):
        p = get_gps_protocol("ubx")
        class_names = [c["name"] for c in p.message_classes]
        assert "NAV" in class_names
        assert "CFG" in class_names

    def test_nmea_talker_ids(self):
        p = get_gps_protocol("nmea")
        ids = [t["id"] for t in p.talker_ids]
        assert "GP" in ids
        assert "GN" in ids

    def test_get_nonexistent_gps(self):
        assert get_gps_protocol("nonexistent") is None

    def test_gps_to_dict(self):
        p = get_gps_protocol("nmea")
        d = p.to_dict()
        assert d["protocol_id"] == "nmea"
        assert d["baud_default"] == 9600


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  NMEA parsing
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestNMEAParsing:
    def test_parse_gga(self):
        sentence = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,47.0,M,,*4F"
        r = parse_nmea_sentence(sentence)
        assert r.sentence_type == "GGA"
        assert r.talker_id == "GP"
        assert r.checksum_ok is True
        assert r.valid is True
        assert r.fields["num_satellites"] == 8
        assert r.fields["fix_quality"] == 1
        assert abs(r.fields["altitude_m"] - 545.4) < 0.1
        assert abs(r.fields["latitude"] - 48.1173) < 0.01

    def test_parse_rmc(self):
        sentence = "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A"
        r = parse_nmea_sentence(sentence)
        assert r.sentence_type == "RMC"
        assert r.valid is True
        assert r.fields["status"] == "A"
        assert abs(r.fields["speed_knots"] - 22.4) < 0.1

    def test_parse_invalid_start(self):
        r = parse_nmea_sentence("GPGGA,123519,...")
        assert r.valid is False
        assert r.error != ""

    def test_parse_bad_checksum(self):
        sentence = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,47.0,M,,*FF"  # wrong checksum on purpose
        r = parse_nmea_sentence(sentence)
        assert r.checksum_ok is False
        assert r.valid is False

    def test_parse_gsa(self):
        sentence = "$GPGSA,A,3,04,05,,09,12,,,24,,,,,2.5,1.3,2.1*39"
        r = parse_nmea_sentence(sentence)
        assert r.sentence_type == "GSA"
        assert r.checksum_ok is True
        assert r.fields["fix_type"] == 3
        assert 4 in r.fields["satellite_ids"]

    def test_parse_vtg(self):
        sentence = "$GPVTG,054.7,T,034.4,M,005.5,N,010.2,K*48"
        r = parse_nmea_sentence(sentence)
        assert r.sentence_type == "VTG"
        assert r.checksum_ok is True
        assert abs(r.fields["speed_knots"] - 5.5) < 0.1
        assert abs(r.fields["speed_kmh"] - 10.2) < 0.1

    def test_parse_gll(self):
        sentence = "$GPGLL,4916.45,N,12311.12,W,225444,A*31"
        r = parse_nmea_sentence(sentence)
        assert r.sentence_type == "GLL"
        assert r.checksum_ok is True
        assert r.fields["status"] == "A"
        assert r.fields["latitude"] > 49.0

    def test_to_dict(self):
        sentence = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,47.0,M,,*4F"
        r = parse_nmea_sentence(sentence)
        d = r.to_dict()
        assert d["sentence_type"] == "GGA"
        assert d["valid"] is True

    def test_empty_sentence(self):
        r = parse_nmea_sentence("$*00")
        assert r.valid is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  UBX parsing
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestUBXParsing:
    def test_parse_too_short(self):
        r = parse_ubx_message(b"\xB5\x62\x01\x07")
        assert r.valid is False
        assert r.error != ""

    def test_parse_invalid_sync(self):
        r = parse_ubx_message(b"\x00\x00\x01\x07\x00\x00\x00\x00")
        assert r.valid is False
        assert "sync" in r.error.lower()

    def test_build_and_parse(self):
        msg = build_ubx_message(0x06, 0x08, b"\x00\x01\x00\x01\x00\x00")
        r = parse_ubx_message(msg)
        assert r.valid is True
        assert r.msg_class == 0x06
        assert r.msg_id == 0x08
        assert r.class_name == "CFG"
        assert r.msg_name == "CFG-RATE"

    def test_build_empty_payload(self):
        msg = build_ubx_message(0x06, 0x00)
        r = parse_ubx_message(msg)
        assert r.valid is True
        assert len(r.payload) == 0

    def test_ack_ack(self):
        msg = build_ubx_message(0x05, 0x01, bytes([0x06, 0x08]))
        r = parse_ubx_message(msg)
        assert r.valid is True
        assert r.msg_name == "ACK-ACK"

    def test_ack_nak(self):
        msg = build_ubx_message(0x05, 0x00, bytes([0x06, 0x08]))
        r = parse_ubx_message(msg)
        assert r.valid is True
        assert r.msg_name == "ACK-NAK"

    def test_nav_pvt_parse(self):
        payload = bytearray(92)
        struct.pack_into("<I", payload, 0, 123456)  # iTOW
        struct.pack_into("<H", payload, 4, 2026)    # year
        payload[6] = 4                               # month
        payload[7] = 15                              # day
        payload[8] = 12                              # hour
        payload[9] = 30                              # min
        payload[10] = 45                             # sec
        payload[20] = 3                              # fixType = 3D
        payload[23] = 12                             # numSV
        struct.pack_into("<i", payload, 24, int(121.5 * 1e7))  # lon
        struct.pack_into("<i", payload, 28, int(25.03 * 1e7))  # lat
        struct.pack_into("<i", payload, 32, 100000)  # height mm
        struct.pack_into("<i", payload, 36, 50000)   # hMSL mm

        msg = build_ubx_message(0x01, 0x07, bytes(payload))
        r = parse_ubx_message(msg)
        assert r.valid is True
        assert r.msg_name == "NAV-PVT"
        assert r.parsed_fields["fixType"] == 3
        assert r.parsed_fields["numSV"] == 12
        assert abs(r.parsed_fields["lon_deg"] - 121.5) < 0.01
        assert abs(r.parsed_fields["lat_deg"] - 25.03) < 0.01
        assert r.parsed_fields["year"] == 2026

    def test_to_dict(self):
        msg = build_ubx_message(0x06, 0x08, b"\x00\x01")
        r = parse_ubx_message(msg)
        d = r.to_dict()
        assert d["msg_class"] == "0x06"
        assert d["valid"] is True

    def test_incomplete_message(self):
        msg = build_ubx_message(0x01, 0x07, b"\x01\x02\x03\x04")
        r = parse_ubx_message(msg[:6])
        assert r.valid is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Barometer drivers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestBarometerDrivers:
    def test_get_bmp280(self):
        d = get_barometer_driver("bmp280")
        assert d is not None
        assert d.name == "BMP280"
        assert d.pressure_range_hpa == [300, 1100]

    def test_get_lps22(self):
        d = get_barometer_driver("lps22")
        assert d is not None
        assert d.name == "LPS22HB"
        assert d.pressure_range_hpa == [260, 1260]

    def test_bmp280_registers(self):
        d = get_barometer_driver("bmp280")
        assert d.registers["chip_id"].expected == "0x58"

    def test_lps22_registers(self):
        d = get_barometer_driver("lps22")
        assert d.registers["who_am_i"].expected == "0xB1"

    def test_bmp280_modes(self):
        d = get_barometer_driver("bmp280")
        assert "normal" in d.modes
        assert "forced" in d.modes

    def test_get_nonexistent_baro(self):
        assert get_barometer_driver("nonexistent") is None

    def test_baro_to_dict(self):
        d = get_barometer_driver("bmp280")
        dd = d.to_dict()
        assert dd["driver_id"] == "bmp280"
        assert dd["sample_rate_hz"] == 157

    def test_bmp280_compensation(self):
        d = get_barometer_driver("bmp280")
        assert "polynomial" in d.compensation.lower()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Barometric altitude
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestBarometricAltitude:
    def test_sea_level(self):
        alt = pressure_to_altitude(101325.0)
        assert abs(alt) < 1.0

    def test_typical_altitude(self):
        p = altitude_to_pressure(1000.0)
        alt = pressure_to_altitude(p)
        assert abs(alt - 1000.0) < 1.0

    def test_high_altitude(self):
        p = altitude_to_pressure(5000.0)
        alt = pressure_to_altitude(p)
        assert abs(alt - 5000.0) < 5.0

    def test_zero_pressure(self):
        assert pressure_to_altitude(0.0) == 0.0

    def test_negative_pressure(self):
        assert pressure_to_altitude(-100.0) == 0.0

    def test_round_trip(self):
        for target_alt in [0, 100, 500, 1000, 3000, 8000]:
            p = altitude_to_pressure(float(target_alt))
            alt = pressure_to_altitude(p)
            assert abs(alt - target_alt) < 5.0, f"Failed at {target_alt}m"

    def test_altitude_to_pressure_extreme(self):
        p = altitude_to_pressure(50000.0)
        assert p == 0.0

    def test_custom_sea_level(self):
        alt = pressure_to_altitude(100000.0, sea_level_pressure_pa=101325.0)
        assert alt > 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  EKF profiles
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestEKFProfiles:
    def test_get_orientation_9dof(self):
        p = get_ekf_profile("orientation_9dof")
        assert p is not None
        assert p.state_dim == 7
        assert p.measurement_dim == 6
        assert p.prediction_model == "quaternion_kinematics"

    def test_get_position_15state(self):
        p = get_ekf_profile("position_15state")
        assert p is not None
        assert p.state_dim == 15

    def test_ekf_profile_noise_params(self):
        p = get_ekf_profile("orientation_9dof")
        assert "gyro_noise" in p.process_noise
        assert "accel_noise" in p.measurement_noise

    def test_ekf_state_vector(self):
        p = get_ekf_profile("orientation_9dof")
        assert len(p.state_vector) == 7
        names = [s["name"] for s in p.state_vector]
        assert "q0" in names
        assert "gyro_bias_x" in names

    def test_get_nonexistent_ekf(self):
        assert get_ekf_profile("nonexistent") is None

    def test_ekf_to_dict(self):
        p = get_ekf_profile("orientation_9dof")
        d = p.to_dict()
        assert d["state_dim"] == 7


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  EKF orientation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestEKFOrientation:
    def test_static_level(self):
        samples = generate_static_trajectory(
            duration_s=5.0, sample_rate_hz=100,
            roll_deg=0, pitch_deg=0, yaw_deg=0,
            accel_noise=0.01, gyro_noise=0.0001,
        )
        result = run_ekf_orientation(samples)
        assert result.state in (EKFState.converged, EKFState.converging)
        assert abs(result.euler_deg["roll"]) < 5.0
        assert abs(result.euler_deg["pitch"]) < 5.0

    def test_static_tilted(self):
        samples = generate_static_trajectory(
            duration_s=5.0, sample_rate_hz=100,
            roll_deg=0, pitch_deg=30, yaw_deg=0,
            accel_noise=0.01, gyro_noise=0.0001,
        )
        result = run_ekf_orientation(samples)
        assert abs(result.euler_deg["pitch"] - 30.0) < 10.0

    def test_unknown_profile(self):
        result = run_ekf_orientation([], profile_id="nonexistent")
        assert result.state == EKFState.diverged

    def test_empty_samples(self):
        result = run_ekf_orientation([])
        assert result.state == EKFState.uninitialized

    def test_initial_orientation(self):
        samples = generate_static_trajectory(
            duration_s=2.0, sample_rate_hz=100,
            roll_deg=0, pitch_deg=0,
        )
        result = run_ekf_orientation(
            samples,
            initial_orientation={"roll": 0, "pitch": 0, "yaw": 45},
        )
        assert result.iterations > 0

    def test_rotation_tracking(self):
        samples = generate_rotation_trajectory(
            duration_s=5.0, sample_rate_hz=100,
            angular_rate_dps=10.0, axis="yaw",
            gyro_noise=0.0001,
        )
        result = run_ekf_orientation(samples)
        assert result.iterations > 0
        assert result.state in (EKFState.converged, EKFState.converging)

    def test_ekf_result_to_dict(self):
        samples = generate_static_trajectory(duration_s=1.0, sample_rate_hz=50)
        result = run_ekf_orientation(samples)
        d = result.to_dict()
        assert "quaternion" in d
        assert "euler_deg" in d
        assert "covariance_trace" in d

    def test_covariance_decreases(self):
        samples = generate_static_trajectory(
            duration_s=10.0, sample_rate_hz=100,
            accel_noise=0.01, gyro_noise=0.0001,
        )
        result = run_ekf_orientation(samples)
        profile = get_ekf_profile("orientation_9dof")
        initial_cov = profile.initial_covariance * profile.state_dim
        assert result.covariance_trace < initial_cov

    def test_gyro_bias_estimation(self):
        samples = generate_static_trajectory(
            duration_s=10.0, sample_rate_hz=100,
            accel_noise=0.01, gyro_noise=0.0001,
        )
        for s in samples:
            s.gyro_x += 0.01
        result = run_ekf_orientation(samples)
        assert result.gyro_bias is not None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Trajectory fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestTrajectoryFixtures:
    def test_static_level(self):
        f = get_trajectory_fixture("static_level")
        assert f is not None
        assert f.expected_orientation["roll_deg"] == 0.0

    def test_static_tilted(self):
        f = get_trajectory_fixture("static_tilted_30")
        assert f is not None
        assert f.expected_orientation["pitch_deg"] == 30.0

    def test_slow_rotation(self):
        f = get_trajectory_fixture("slow_rotation_yaw")
        assert f is not None
        assert f.angular_rate_dps == 10.0
        assert f.expected_final_orientation["yaw_deg"] == 360.0

    def test_figure_eight(self):
        f = get_trajectory_fixture("figure_eight")
        assert f is not None
        assert f.return_to_origin is True

    def test_evaluate_pass(self):
        samples = generate_static_trajectory(
            duration_s=5.0, sample_rate_hz=100,
            accel_noise=0.005, gyro_noise=0.0001,
        )
        result = run_ekf_orientation(samples)
        eval_result = evaluate_ekf_against_fixture(result, "static_level")
        assert eval_result["passed"] is True
        assert eval_result["max_error_deg"] < 2.0

    def test_evaluate_unknown_fixture(self):
        from backend.sensor_fusion import EKFResult
        r = EKFResult(profile_id="test", state=EKFState.converged)
        ev = evaluate_ekf_against_fixture(r, "nonexistent")
        assert ev["passed"] is False

    def test_fixture_to_dict(self):
        f = get_trajectory_fixture("static_level")
        d = f.to_dict()
        assert d["fixture_id"] == "static_level"
        assert "expected_orientation" in d

    def test_get_nonexistent_fixture(self):
        assert get_trajectory_fixture("nonexistent") is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Calibration routines
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCalibration:
    def _make_static_data(self, bias=(0, 0, 0), scale=(1, 1, 1)):
        g = 9.81
        positions = {
            "z_up": [IMUSample(t * 0.01, 0 + bias[0], 0 + bias[1], g * scale[2] + bias[2], 0.001, -0.002, 0.0005) for t in range(100)],
            "z_down": [IMUSample(t * 0.01, 0 + bias[0], 0 + bias[1], -g * scale[2] + bias[2], 0.001, -0.002, 0.0005) for t in range(100)],
            "x_up": [IMUSample(t * 0.01, g * scale[0] + bias[0], 0 + bias[1], 0 + bias[2], 0.001, -0.002, 0.0005) for t in range(100)],
            "x_down": [IMUSample(t * 0.01, -g * scale[0] + bias[0], 0 + bias[1], 0 + bias[2], 0.001, -0.002, 0.0005) for t in range(100)],
            "y_up": [IMUSample(t * 0.01, 0 + bias[0], g * scale[1] + bias[1], 0 + bias[2], 0.001, -0.002, 0.0005) for t in range(100)],
            "y_down": [IMUSample(t * 0.01, 0 + bias[0], -g * scale[1] + bias[1], 0 + bias[2], 0.001, -0.002, 0.0005) for t in range(100)],
        }
        return positions

    def test_ideal_calibration(self):
        data = self._make_static_data()
        result = run_imu_calibration(data)
        assert result.status == CalibrationStatus.calibrated
        assert result.residual_g < 0.1

    def test_with_bias(self):
        data = self._make_static_data(bias=(0.5, -0.3, 0.2))
        result = run_imu_calibration(data)
        assert result.status == CalibrationStatus.calibrated
        assert abs(result.accel_bias[0] - 0.5) < 0.1
        assert abs(result.accel_bias[1] - (-0.3)) < 0.1

    def test_gyro_bias(self):
        data = self._make_static_data()
        for _pos, samples in data.items():
            for s in samples:
                s.gyro_x = 0.05
                s.gyro_y = -0.02
        result = run_imu_calibration(data)
        assert abs(result.gyro_bias[0] - 0.05) < 0.01
        assert abs(result.gyro_bias[1] - (-0.02)) < 0.01

    def test_unknown_profile(self):
        result = run_imu_calibration({}, profile_id="nonexistent")
        assert result.status == CalibrationStatus.failed

    def test_empty_data(self):
        result = run_imu_calibration({})
        assert result.status == CalibrationStatus.failed

    def test_calibration_result_to_dict(self):
        data = self._make_static_data()
        result = run_imu_calibration(data)
        d = result.to_dict()
        assert "accel_bias" in d
        assert "gyro_bias" in d
        assert d["status"] == "calibrated"

    def test_calibration_profiles_query(self):
        p = get_calibration_profile("imu_6axis")
        assert p is not None
        assert "accel_bias" in p.parameters

    def test_magnetometer_profile(self):
        p = get_calibration_profile("magnetometer")
        assert p is not None
        assert "hard_iron" in p.parameters

    def test_barometer_profile(self):
        p = get_calibration_profile("barometer")
        assert p is not None
        assert "pressure_offset" in p.parameters

    def test_nonexistent_profile(self):
        assert get_calibration_profile("nonexistent") is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Test recipes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestTestRecipes:
    def test_get_recipe(self):
        r = get_test_recipe("imu-read-whoami")
        assert r is not None
        assert r.sensor_type == "imu"
        assert r.category == "functional"

    def test_imu_recipes(self):
        recipes = get_recipes_by_sensor_type("imu")
        assert len(recipes) >= 4

    def test_gps_recipes(self):
        recipes = get_recipes_by_sensor_type("gps")
        assert len(recipes) >= 3

    def test_baro_recipes(self):
        recipes = get_recipes_by_sensor_type("barometer")
        assert len(recipes) >= 2

    def test_fusion_recipes(self):
        recipes = get_recipes_by_sensor_type("fusion")
        assert len(recipes) >= 2

    def test_calibration_recipes(self):
        recipes = get_recipes_by_category("calibration")
        assert len(recipes) >= 2

    def test_functional_recipes(self):
        recipes = get_recipes_by_category("functional")
        assert len(recipes) >= 5

    def test_nonexistent_recipe(self):
        assert get_test_recipe("nonexistent") is None

    def test_recipe_to_dict(self):
        r = get_test_recipe("imu-read-whoami")
        d = r.to_dict()
        assert d["recipe_id"] == "imu-read-whoami"
        assert d["sensor_type"] == "imu"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Test stub runner
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSensorTestRunner:
    def test_stub_run(self):
        result = run_sensor_test("imu-read-whoami", "mpu6050@0x68")
        assert result.status == TestStatus.pending
        assert result.sensor_type == "imu"

    def test_unknown_recipe(self):
        result = run_sensor_test("nonexistent", "device")
        assert result.status == TestStatus.error

    def test_result_to_dict(self):
        result = run_sensor_test("gps-nmea-parse", "gps-module")
        d = result.to_dict()
        assert d["status"] == "pending"
        assert d["sensor_type"] == "gps"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SoC compatibility
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSocCompatibility:
    def test_esp32_all_sensors(self):
        compat = check_soc_compatibility("esp32")
        assert compat["mpu6050"] is True
        assert compat["bmp280"] is True

    def test_stm32f4_all_sensors(self):
        compat = check_soc_compatibility("stm32f4")
        for _sid, supported in compat.items():
            assert supported is True

    def test_unknown_soc(self):
        compat = check_soc_compatibility("unknown_chip_xyz")
        for _sid, supported in compat.items():
            assert supported is False

    def test_specific_sensors(self):
        compat = check_soc_compatibility("esp32", ["mpu6050", "bmp280"])
        assert len(compat) == 2
        assert compat["mpu6050"] is True

    def test_case_insensitive(self):
        compat = check_soc_compatibility("ESP32")
        assert compat["mpu6050"] is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Cert artifact generator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCertArtifacts:
    def test_generate_artifacts(self):
        arts = generate_cert_artifacts("imu")
        assert len(arts) == 5
        assert all(a.status == "pending" for a in arts)

    def test_provided_artifacts(self):
        arts = generate_cert_artifacts(
            "imu",
            spec={"provided_artifacts": ["sensor_calibration_report"]},
        )
        provided = [a for a in arts if a.status == "provided"]
        assert len(provided) == 1

    def test_artifact_to_dict(self):
        arts = generate_cert_artifacts("fusion")
        d = arts[0].to_dict()
        assert "artifact_id" in d
        assert "sensor_type" in d


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Doc suite generator integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestDocSuiteIntegration:
    def test_register_and_get_certs(self):
        register_sensor_fusion_cert("IMU Calibration", "Passed", "CAL-001")
        certs = get_sensor_fusion_certs()
        assert len(certs) == 1
        assert certs[0]["standard"] == "IMU Calibration"

    def test_clear_certs(self):
        register_sensor_fusion_cert("Test", "Pending")
        clear_sensor_fusion_certs()
        assert len(get_sensor_fusion_certs()) == 0

    def test_multiple_certs(self):
        register_sensor_fusion_cert("IMU Cal", "Passed")
        register_sensor_fusion_cert("GPS Fix", "Passed")
        register_sensor_fusion_cert("Baro Offset", "Pending")
        assert len(get_sensor_fusion_certs()) == 3


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Synthetic trajectory generators
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestTrajectoryGenerators:
    def test_static_trajectory_length(self):
        samples = generate_static_trajectory(duration_s=5.0, sample_rate_hz=100)
        assert len(samples) == 500

    def test_static_trajectory_gravity(self):
        samples = generate_static_trajectory(
            duration_s=2.0, sample_rate_hz=100,
            accel_noise=0.0,
        )
        for s in samples:
            assert abs(s.accel_z - 9.81) < 0.01
            assert abs(s.accel_x) < 0.01

    def test_static_tilted_trajectory(self):
        samples = generate_static_trajectory(
            duration_s=2.0, sample_rate_hz=100,
            pitch_deg=30, accel_noise=0.0,
        )
        expected_az = 9.81 * math.cos(math.radians(30))
        for s in samples:
            assert abs(s.accel_z - expected_az) < 0.01

    def test_rotation_trajectory_length(self):
        samples = generate_rotation_trajectory(duration_s=10.0, sample_rate_hz=100)
        assert len(samples) == 1000

    def test_rotation_trajectory_rate(self):
        rate_dps = 15.0
        samples = generate_rotation_trajectory(
            duration_s=5.0, sample_rate_hz=100,
            angular_rate_dps=rate_dps, axis="yaw",
            gyro_noise=0.0,
        )
        expected_rad = math.radians(rate_dps)
        for s in samples:
            assert abs(s.gyro_z - expected_rad) < 0.001


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Audit logging
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestAuditLogging:
    @pytest.mark.asyncio
    async def test_log_sensor_test_no_crash(self):
        from backend.sensor_fusion import log_sensor_test_result, SensorTestResult
        result = SensorTestResult(
            recipe_id="imu-read-whoami",
            sensor_type="imu",
            status=TestStatus.passed,
            target_device="test",
        )
        r = await log_sensor_test_result(result)
        # May be None if audit module not available in test context

    @pytest.mark.asyncio
    async def test_log_ekf_result_no_crash(self):
        from backend.sensor_fusion import log_ekf_result, EKFResult
        result = EKFResult(profile_id="test", state=EKFState.converged)
        r = await log_ekf_result(result)

    @pytest.mark.asyncio
    async def test_log_calibration_result_no_crash(self):
        from backend.sensor_fusion import log_calibration_result, CalibrationResult
        result = CalibrationResult(
            profile_id="imu_6axis",
            status=CalibrationStatus.calibrated,
        )
        r = await log_calibration_result(result)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Edge cases
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestEdgeCases:
    def test_nmea_no_dollar(self):
        r = parse_nmea_sentence("not a sentence")
        assert r.valid is False

    def test_nmea_empty(self):
        r = parse_nmea_sentence("")
        assert r.valid is False

    def test_ubx_empty(self):
        r = parse_ubx_message(b"")
        assert r.valid is False

    def test_pressure_boundary(self):
        alt = pressure_to_altitude(1.0)
        assert alt > 0

    def test_calibration_single_position(self):
        data = {
            "z_up": [IMUSample(0.0, 5.0, 5.0, 5.0)],
        }
        result = run_imu_calibration(data)
        assert result.status == CalibrationStatus.failed

    def test_ekf_single_sample(self):
        samples = [IMUSample(0.0, 0, 0, 9.81)]
        result = run_ekf_orientation(samples)
        assert result.state == EKFState.uninitialized

    def test_all_enums_have_values(self):
        for e in (SensorType, SensorBus, TestCategory, TestStatus, CalibrationStatus, EKFState, NMEASentenceType):
            assert len(e) > 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  REST endpoint smoke tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_imu_drivers_endpoint(client):
    response = await client.get("/api/v1/sensor-fusion/imu/drivers")
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 3


@pytest.mark.asyncio
async def test_imu_driver_detail_endpoint(client):
    response = await client.get("/api/v1/sensor-fusion/imu/drivers/mpu6050")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "MPU-6050"


@pytest.mark.asyncio
async def test_imu_driver_not_found(client):
    response = await client.get("/api/v1/sensor-fusion/imu/drivers/nonexistent")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_gps_protocols_endpoint(client):
    response = await client.get("/api/v1/sensor-fusion/gps/protocols")
    assert response.status_code == 200
    assert response.json()["count"] == 2


@pytest.mark.asyncio
async def test_barometer_drivers_endpoint(client):
    response = await client.get("/api/v1/sensor-fusion/barometer/drivers")
    assert response.status_code == 200
    assert response.json()["count"] == 2


@pytest.mark.asyncio
async def test_ekf_profiles_endpoint(client):
    response = await client.get("/api/v1/sensor-fusion/ekf/profiles")
    assert response.status_code == 200
    assert response.json()["count"] == 2


@pytest.mark.asyncio
async def test_calibration_profiles_endpoint(client):
    response = await client.get("/api/v1/sensor-fusion/calibration/profiles")
    assert response.status_code == 200
    assert response.json()["count"] == 3


@pytest.mark.asyncio
async def test_test_recipes_endpoint(client):
    response = await client.get("/api/v1/sensor-fusion/test/recipes")
    assert response.status_code == 200
    assert response.json()["count"] == 13


@pytest.mark.asyncio
async def test_test_recipes_by_sensor_type(client):
    response = await client.get("/api/v1/sensor-fusion/test/recipes?sensor_type=imu")
    assert response.status_code == 200
    assert response.json()["count"] >= 4


@pytest.mark.asyncio
async def test_trajectory_fixtures_endpoint(client):
    response = await client.get("/api/v1/sensor-fusion/trajectory/fixtures")
    assert response.status_code == 200
    assert response.json()["count"] == 4


@pytest.mark.asyncio
async def test_nmea_parse_endpoint(client):
    response = await client.post(
        "/api/v1/sensor-fusion/gps/nmea/parse",
        json={"sentence": "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,47.0,M,,*4F"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["valid"] is True
    assert data["sentence_type"] == "GGA"


@pytest.mark.asyncio
async def test_altitude_endpoint(client):
    response = await client.post(
        "/api/v1/sensor-fusion/barometer/altitude",
        json={"pressure_pa": 101325.0},
    )
    assert response.status_code == 200
    assert abs(response.json()["altitude_m"]) < 1.0


@pytest.mark.asyncio
async def test_soc_compat_endpoint(client):
    response = await client.post(
        "/api/v1/sensor-fusion/soc-compat",
        json={"soc_id": "esp32"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["compatibility"]["mpu6050"] is True


@pytest.mark.asyncio
async def test_artifacts_endpoint(client):
    response = await client.get("/api/v1/sensor-fusion/artifacts")
    assert response.status_code == 200
    assert response.json()["count"] == 5
