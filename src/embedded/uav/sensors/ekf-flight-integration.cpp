/* SPDX-License-Identifier: MIT
 *
 * Cases 6+7 EKF flight integration (OP-2029).
 */
#include "ekf-flight-integration.h"

#include <algorithm>
#include <cmath>

namespace omnisight::embedded::uav::sensors {
namespace {

constexpr double kEarthRadiusM = 6378137.0;
constexpr double kGravityMps2 = 9.80665;
constexpr double kDegreesToRadians = 3.14159265358979323846 / 180.0;
constexpr double kRadiansToDegrees = 180.0 / 3.14159265358979323846;
constexpr double kMinDtS = 0.0001;
constexpr double kMaxDtS = 1.0;

enum CovarianceIndex {
	kCovNorth = 0,
	kCovEast,
	kCovDown,
	kCovVelNorth,
	kCovVelEast,
	kCovVelDown,
	kCovQuatW,
	kCovQuatX,
	kCovQuatY,
	kCovQuatZ,
};

static double clamp_variance(double variance, double fallback)
{
	if (!std::isfinite(variance) || variance <= 0.0)
		return fallback;
	return std::max(variance, 1.0e-6);
}

static std::array<double, 4> normalize_quaternion(std::array<double, 4> q)
{
	const double norm = std::sqrt(q[0] * q[0] + q[1] * q[1] +
				      q[2] * q[2] + q[3] * q[3]);

	if (norm < 1.0e-12 || !std::isfinite(norm))
		return { 1.0, 0.0, 0.0, 0.0 };
	for (double &v : q)
		v /= norm;
	return q;
}

static std::array<double, 4> multiply_quaternion(const std::array<double, 4> &a,
						 const std::array<double, 4> &b)
{
	return {
		a[0] * b[0] - a[1] * b[1] - a[2] * b[2] - a[3] * b[3],
		a[0] * b[1] + a[1] * b[0] + a[2] * b[3] - a[3] * b[2],
		a[0] * b[2] - a[1] * b[3] + a[2] * b[0] + a[3] * b[1],
		a[0] * b[3] + a[1] * b[2] - a[2] * b[1] + a[3] * b[0],
	};
}

static std::array<double, 3> rotate_body_to_ned(const std::array<double, 4> &q,
						const std::array<double, 3> &v)
{
	const double w = q[0];
	const double x = q[1];
	const double y = q[2];
	const double z = q[3];

	return {
		(1.0 - 2.0 * (y * y + z * z)) * v[0] +
			2.0 * (x * y - z * w) * v[1] +
			2.0 * (x * z + y * w) * v[2],
		2.0 * (x * y + z * w) * v[0] +
			(1.0 - 2.0 * (x * x + z * z)) * v[1] +
			2.0 * (y * z - x * w) * v[2],
		2.0 * (x * z - y * w) * v[0] +
			2.0 * (y * z + x * w) * v[1] +
			(1.0 - 2.0 * (x * x + y * y)) * v[2],
	};
}

static std::array<double, 3> rotate_ned_to_body(const std::array<double, 4> &q,
						const std::array<double, 3> &v)
{
	const double w = q[0];
	const double x = q[1];
	const double y = q[2];
	const double z = q[3];

	return {
		(1.0 - 2.0 * (y * y + z * z)) * v[0] +
			2.0 * (x * y + z * w) * v[1] +
			2.0 * (x * z - y * w) * v[2],
		2.0 * (x * y - z * w) * v[0] +
			(1.0 - 2.0 * (x * x + z * z)) * v[1] +
			2.0 * (y * z + x * w) * v[2],
		2.0 * (x * z + y * w) * v[0] +
			2.0 * (y * z - x * w) * v[1] +
			(1.0 - 2.0 * (x * x + y * y)) * v[2],
	};
}

static double yaw_from_quaternion(const std::array<double, 4> &q)
{
	const double siny = 2.0 * (q[0] * q[3] + q[1] * q[2]);
	const double cosy = 1.0 - 2.0 * (q[2] * q[2] + q[3] * q[3]);

	return std::atan2(siny, cosy);
}

static void apply_small_angle(std::array<double, 4> *q, double roll,
			      double pitch, double yaw)
{
	const std::array<double, 4> correction {
		1.0,
		0.5 * roll,
		0.5 * pitch,
		0.5 * yaw,
	};

	*q = normalize_quaternion(multiply_quaternion(*q, correction));
}

static double kalman_gain(double variance, double measurement_variance)
{
	const double r = clamp_variance(measurement_variance, 1.0);

	return variance / (variance + r);
}

static void scalar_update(double measurement, double *value, double *variance,
			  double measurement_variance)
{
	const double gain = kalman_gain(*variance, measurement_variance);

	*value += gain * (measurement - *value);
	*variance = std::max((1.0 - gain) * *variance, 1.0e-9);
}

static void gps_to_ned(double origin_lat_deg, double origin_lon_deg,
		       const FlightGPSFix &fix, std::array<double, 3> *ned)
{
	const double origin_lat_rad = origin_lat_deg * kDegreesToRadians;
	const double dlat = (fix.latitude_deg - origin_lat_deg) * kDegreesToRadians;
	const double dlon = (fix.longitude_deg - origin_lon_deg) * kDegreesToRadians;

	(*ned)[0] = dlat * kEarthRadiusM;
	(*ned)[1] = dlon * kEarthRadiusM * std::cos(origin_lat_rad);
	(*ned)[2] = -fix.altitude_m;
}

static void ned_to_gps(double origin_lat_deg, double origin_lon_deg,
		       const std::array<double, 3> &ned, FlightEKFState *state)
{
	const double origin_lat_rad = origin_lat_deg * kDegreesToRadians;

	state->latitude_deg = origin_lat_deg +
			      (ned[0] / kEarthRadiusM) * kRadiansToDegrees;
	state->longitude_deg = origin_lon_deg +
			       (ned[1] / (kEarthRadiusM * std::cos(origin_lat_rad))) *
				       kRadiansToDegrees;
	state->altitude_m = -ned[2];
}

} // namespace

FlightEKF::FlightEKF()
{
	reset();
}

FlightEKF::FlightEKF(const FlightEKFConfig &config)
{
	reset(config);
}

void FlightEKF::reset()
{
	state_ = FlightEKFState {};
	position_ned_m_ = { 0.0, 0.0, 0.0 };
	origin_latitude_deg_ = 0.0;
	origin_longitude_deg_ = 0.0;
	covariance_.fill(config_.initial_covariance);
}

void FlightEKF::reset(const FlightEKFConfig &config)
{
	config_ = config;
	reset();
}

FlightEKFStatus FlightEKF::predict(const FlightIMUSample &imu, double dt_s)
{
	if (!std::isfinite(dt_s) || dt_s <= 0.0)
		return FlightEKFStatus::kInvalidArgument;

	const double dt = std::clamp(dt_s, kMinDtS, kMaxDtS);
	const double wx = imu.gyro_rps[0];
	const double wy = imu.gyro_rps[1];
	const double wz = imu.gyro_rps[2];
	const double omega = std::sqrt(wx * wx + wy * wy + wz * wz);
	std::array<double, 4> dq { 1.0, 0.0, 0.0, 0.0 };

	if (omega > 1.0e-12) {
		const double half_angle = 0.5 * omega * dt;
		const double scale = std::sin(half_angle) / omega;

		dq = { std::cos(half_angle), wx * scale, wy * scale, wz * scale };
	}

	state_.orientation_quat =
		normalize_quaternion(multiply_quaternion(state_.orientation_quat, dq));

	const double accel_norm = std::sqrt(imu.accel_mps2[0] * imu.accel_mps2[0] +
					    imu.accel_mps2[1] * imu.accel_mps2[1] +
					    imu.accel_mps2[2] * imu.accel_mps2[2]);
	if (accel_norm > 0.8 * kGravityMps2 && accel_norm < 1.2 * kGravityMps2) {
		const auto gravity_body =
			rotate_ned_to_body(state_.orientation_quat,
					   { 0.0, 0.0, kGravityMps2 });
		const double gain = std::clamp(config_.accel_correction_gain, 0.0, 0.25);
		const double ex = imu.accel_mps2[0] - gravity_body[0];
		const double ey = imu.accel_mps2[1] - gravity_body[1];
		const double ez = imu.accel_mps2[2] - gravity_body[2];

		apply_small_angle(&state_.orientation_quat, gain * ey / kGravityMps2,
				  -gain * ex / kGravityMps2, gain * ez / kGravityMps2);
	}

	if (imu.has_magnetometer) {
		const double mag_norm = std::sqrt(imu.mag_ut[0] * imu.mag_ut[0] +
						  imu.mag_ut[1] * imu.mag_ut[1]);
		if (mag_norm > 1.0e-9) {
			const double measured_yaw = std::atan2(imu.mag_ut[1], imu.mag_ut[0]);
			double yaw_error = measured_yaw - yaw_from_quaternion(state_.orientation_quat);

			while (yaw_error > 3.14159265358979323846)
				yaw_error -= 2.0 * 3.14159265358979323846;
			while (yaw_error < -3.14159265358979323846)
				yaw_error += 2.0 * 3.14159265358979323846;
			apply_small_angle(&state_.orientation_quat, 0.0, 0.0,
					  std::clamp(config_.magnetometer_correction_gain,
						     0.0, 0.25) *
						  yaw_error);
		}
	}

	const auto accel_ned = rotate_body_to_ned(state_.orientation_quat, imu.accel_mps2);
	const std::array<double, 3> linear_accel_ned {
		accel_ned[0],
		accel_ned[1],
		accel_ned[2] - kGravityMps2,
	};

	position_ned_m_[0] += state_.velocity_north_mps * dt +
			      0.5 * linear_accel_ned[0] * dt * dt;
	position_ned_m_[1] += state_.velocity_east_mps * dt +
			      0.5 * linear_accel_ned[1] * dt * dt;
	position_ned_m_[2] += state_.velocity_down_mps * dt +
			      0.5 * linear_accel_ned[2] * dt * dt;

	state_.velocity_north_mps += linear_accel_ned[0] * dt;
	state_.velocity_east_mps += linear_accel_ned[1] * dt;
	state_.velocity_down_mps += linear_accel_ned[2] * dt;

	for (size_t i = kCovNorth; i <= kCovDown; ++i)
		covariance_[i] += config_.process_noise_position * dt;
	for (size_t i = kCovVelNorth; i <= kCovVelDown; ++i)
		covariance_[i] += config_.process_noise_velocity * dt;
	for (size_t i = kCovQuatW; i <= kCovQuatZ; ++i)
		covariance_[i] += config_.process_noise_orientation * dt;

	if (state_.origin_locked)
		ned_to_gps(origin_latitude_deg_, origin_longitude_deg_, position_ned_m_, &state_);

	state_.predict_count++;
	return FlightEKFStatus::kOk;
}

FlightEKFStatus FlightEKF::update(const FlightGPSFix &fix)
{
	if (!fix.valid || fix.fix_type < 2 || !std::isfinite(fix.latitude_deg) ||
	    !std::isfinite(fix.longitude_deg) || !std::isfinite(fix.altitude_m))
		return FlightEKFStatus::kInvalidArgument;

	if (!state_.origin_locked) {
		origin_latitude_deg_ = fix.latitude_deg;
		origin_longitude_deg_ = fix.longitude_deg;
		position_ned_m_ = { 0.0, 0.0, -fix.altitude_m };
		state_.origin_locked = true;
	}

	std::array<double, 3> measurement_ned {};

	gps_to_ned(origin_latitude_deg_, origin_longitude_deg_, fix, &measurement_ned);

	const double horizontal_variance =
		clamp_variance(fix.horizontal_accuracy_m * fix.horizontal_accuracy_m,
			       config_.gps_position_variance);
	const double altitude_variance =
		clamp_variance(fix.vertical_accuracy_m * fix.vertical_accuracy_m,
			       config_.gps_altitude_variance);
	const double velocity_variance =
		clamp_variance(fix.speed_accuracy_mps * fix.speed_accuracy_mps,
			       config_.gps_velocity_variance);

	scalar_update(measurement_ned[0], &position_ned_m_[0], &covariance_[kCovNorth],
		      horizontal_variance);
	scalar_update(measurement_ned[1], &position_ned_m_[1], &covariance_[kCovEast],
		      horizontal_variance);
	scalar_update(measurement_ned[2], &position_ned_m_[2], &covariance_[kCovDown],
		      altitude_variance);
	scalar_update(fix.velocity_north_mps, &state_.velocity_north_mps,
		      &covariance_[kCovVelNorth], velocity_variance);
	scalar_update(fix.velocity_east_mps, &state_.velocity_east_mps,
		      &covariance_[kCovVelEast], velocity_variance);
	scalar_update(fix.velocity_down_mps, &state_.velocity_down_mps,
		      &covariance_[kCovVelDown], velocity_variance);

	ned_to_gps(origin_latitude_deg_, origin_longitude_deg_, position_ned_m_, &state_);
	state_.gps_update_count++;
	state_.converged = state_.gps_update_count > 0 && state_.barometer_update_count > 0 &&
			   state_.predict_count > 2;
	return FlightEKFStatus::kOk;
}

FlightEKFStatus FlightEKF::update(const FlightBarometerSample &barometer)
{
	if (!barometer.valid || !std::isfinite(barometer.altitude_m))
		return FlightEKFStatus::kInvalidArgument;
	if (!state_.origin_locked)
		return FlightEKFStatus::kUninitialized;

	double down = -barometer.altitude_m;
	const double variance =
		clamp_variance(barometer.variance_m2, config_.barometer_altitude_variance);

	scalar_update(down, &position_ned_m_[2], &covariance_[kCovDown], variance);
	ned_to_gps(origin_latitude_deg_, origin_longitude_deg_, position_ned_m_, &state_);
	state_.barometer_update_count++;
	state_.converged = state_.gps_update_count > 0 && state_.barometer_update_count > 0 &&
			   state_.predict_count > 2;
	return FlightEKFStatus::kOk;
}

FlightEKFState FlightEKF::getState() const
{
	return state_;
}

} // namespace omnisight::embedded::uav::sensors
