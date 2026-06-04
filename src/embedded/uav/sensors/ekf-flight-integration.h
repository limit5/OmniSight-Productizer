/* SPDX-License-Identifier: MIT
 *
 * Cases 6+7 EKF flight integration (OP-2029).
 */
#ifndef OMNISIGHT_EMBEDDED_UAV_SENSORS_EKF_FLIGHT_INTEGRATION_H_
#define OMNISIGHT_EMBEDDED_UAV_SENSORS_EKF_FLIGHT_INTEGRATION_H_

#include <array>
#include <cstdint>

namespace omnisight::embedded::uav::sensors {

enum class FlightEKFStatus {
	kOk = 0,
	kInvalidArgument,
	kUninitialized,
};

struct FlightEKFConfig {
	double initial_covariance = 10.0;
	double process_noise_position = 0.15;
	double process_noise_velocity = 0.25;
	double process_noise_orientation = 0.001;
	double gps_position_variance = 4.0;
	double gps_altitude_variance = 9.0;
	double gps_velocity_variance = 1.0;
	double barometer_altitude_variance = 2.25;
	double accel_correction_gain = 0.05;
	double magnetometer_correction_gain = 0.02;
};

struct FlightIMUSample {
	std::array<double, 3> accel_mps2 {};
	std::array<double, 3> gyro_rps {};
	std::array<double, 3> mag_ut {};
	bool has_magnetometer = false;
};

struct FlightGPSFix {
	double latitude_deg = 0.0;
	double longitude_deg = 0.0;
	double altitude_m = 0.0;
	double velocity_north_mps = 0.0;
	double velocity_east_mps = 0.0;
	double velocity_down_mps = 0.0;
	double horizontal_accuracy_m = 2.0;
	double vertical_accuracy_m = 3.0;
	double speed_accuracy_mps = 1.0;
	uint8_t fix_type = 0;
	bool valid = false;
};

struct FlightBarometerSample {
	double altitude_m = 0.0;
	double variance_m2 = 0.0;
	bool valid = false;
};

struct FlightEKFState {
	double latitude_deg = 0.0;
	double longitude_deg = 0.0;
	double altitude_m = 0.0;
	double velocity_north_mps = 0.0;
	double velocity_east_mps = 0.0;
	double velocity_down_mps = 0.0;
	std::array<double, 4> orientation_quat { 1.0, 0.0, 0.0, 0.0 };
	uint64_t predict_count = 0;
	uint64_t gps_update_count = 0;
	uint64_t barometer_update_count = 0;
	bool origin_locked = false;
	bool converged = false;
};

class FlightEKF {
public:
	FlightEKF();
	explicit FlightEKF(const FlightEKFConfig &config);

	void reset();
	void reset(const FlightEKFConfig &config);

	FlightEKFStatus predict(const FlightIMUSample &imu, double dt_s);
	FlightEKFStatus update(const FlightGPSFix &fix);
	FlightEKFStatus update(const FlightBarometerSample &barometer);

	FlightEKFState getState() const;

private:
	FlightEKFConfig config_;
	FlightEKFState state_;
	std::array<double, 3> position_ned_m_ {};
	std::array<double, 10> covariance_ {};
	double origin_latitude_deg_ = 0.0;
	double origin_longitude_deg_ = 0.0;
};

} // namespace omnisight::embedded::uav::sensors

#endif // OMNISIGHT_EMBEDDED_UAV_SENSORS_EKF_FLIGHT_INTEGRATION_H_
