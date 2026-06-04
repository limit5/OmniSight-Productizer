/*
 * [OP-2048] Case 6 sensor fusion integration helper.
 *
 * The shell wrapper owns qemu execution. This helper owns the synthetic
 * IMU/GPS/barometer stream and verifies convergence through the GPS parser
 * and EKF flight integration without requiring real sensor hardware.
 */

#include "ekf-flight-integration.h"
#include "gps-nmea-driver.h"

#include <array>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <string>

namespace {

constexpr double kEarthRadiusM = 6378137.0;
constexpr double kDegreesToRadians = 3.14159265358979323846 / 180.0;
constexpr double kRadiansToDegrees = 180.0 / 3.14159265358979323846;
constexpr double kGravityMps2 = 9.80665;
constexpr double kBaseLatitudeDeg = 37.4219999;
constexpr double kBaseLongitudeDeg = -122.0840575;
constexpr double kExpectedAltitudeM = 42.0;
constexpr double kDtS = 0.01;
constexpr int kSamples = 10;
constexpr double kHorizontalToleranceM = 0.10;
constexpr double kVerticalToleranceM = 0.30;

struct Pose {
	double latitude_deg;
	double longitude_deg;
	double altitude_m;
};

static double deg_to_rad(double deg)
{
	return deg * kDegreesToRadians;
}

static Pose offset_pose(double north_m, double east_m, double altitude_m)
{
	const double lat = kBaseLatitudeDeg +
			   (north_m / kEarthRadiusM) * kRadiansToDegrees;
	const double lon =
		kBaseLongitudeDeg +
		(east_m / (kEarthRadiusM * std::cos(deg_to_rad(kBaseLatitudeDeg)))) *
			kRadiansToDegrees;

	return { lat, lon, altitude_m };
}

static double horizontal_error_m(const Pose &expected, double latitude_deg,
				 double longitude_deg)
{
	const double dlat = deg_to_rad(latitude_deg - expected.latitude_deg);
	const double dlon = deg_to_rad(longitude_deg - expected.longitude_deg);
	const double north = dlat * kEarthRadiusM;
	const double east =
		dlon * kEarthRadiusM * std::cos(deg_to_rad(expected.latitude_deg));

	return std::hypot(north, east);
}

static std::string nmea_lat(double latitude_deg, char *hemisphere)
{
	const double abs_lat = std::fabs(latitude_deg);
	const int degrees = static_cast<int>(abs_lat);
	const double minutes = (abs_lat - degrees) * 60.0;
	std::ostringstream out;

	*hemisphere = latitude_deg < 0.0 ? 'S' : 'N';
	out << std::setfill('0') << std::setw(2) << degrees << std::fixed
	    << std::setprecision(6) << std::setw(9) << minutes;
	return out.str();
}

static std::string nmea_lon(double longitude_deg, char *hemisphere)
{
	const double abs_lon = std::fabs(longitude_deg);
	const int degrees = static_cast<int>(abs_lon);
	const double minutes = (abs_lon - degrees) * 60.0;
	std::ostringstream out;

	*hemisphere = longitude_deg < 0.0 ? 'W' : 'E';
	out << std::setfill('0') << std::setw(3) << degrees << std::fixed
	    << std::setprecision(6) << std::setw(9) << minutes;
	return out.str();
}

static std::string with_nmea_checksum(const std::string &body)
{
	uint8_t checksum = 0;
	std::ostringstream out;

	for (char ch : body)
		checksum ^= static_cast<uint8_t>(ch);

	out << "$" << body << "*" << std::uppercase << std::hex << std::setw(2)
	    << std::setfill('0') << static_cast<int>(checksum) << "\r\n";
	return out.str();
}

static std::string gga_sentence(const Pose &pose)
{
	char lat_hemisphere = 'N';
	char lon_hemisphere = 'E';
	const std::string lat = nmea_lat(pose.latitude_deg, &lat_hemisphere);
	const std::string lon = nmea_lon(pose.longitude_deg, &lon_hemisphere);
	std::ostringstream body;

	body << "GPGGA,120000," << lat << "," << lat_hemisphere << "," << lon
	     << "," << lon_hemisphere << ",1,12,0.5," << std::fixed
	     << std::setprecision(2) << pose.altitude_m << ",M,0.0,M,,";
	return with_nmea_checksum(body.str());
}

static int require_status(bool ok, const char *message)
{
	if (ok)
		return 0;
	std::cerr << "sensor-fusion: " << message << "\n";
	return 1;
}

} // namespace

int main()
{
	using omnisight::embedded::uav::sensors::FlightBarometerSample;
	using omnisight::embedded::uav::sensors::FlightEKF;
	using omnisight::embedded::uav::sensors::FlightEKFConfig;
	using omnisight::embedded::uav::sensors::FlightEKFStatus;
	using omnisight::embedded::uav::sensors::FlightGPSFix;
	using omnisight::embedded::uav::sensors::FlightIMUSample;
	using omnisight::embedded::uav::sensors::GpsDriver;
	using omnisight::embedded::uav::sensors::GpsDriverStatus;

	FlightEKFConfig config;
	config.initial_covariance = 0.5;
	config.process_noise_position = 0.01;
	config.process_noise_velocity = 0.01;
	config.gps_position_variance = 0.01;
	config.gps_altitude_variance = 0.04;
	config.gps_velocity_variance = 0.01;
	config.barometer_altitude_variance = 0.01;

	FlightEKF ekf(config);
	GpsDriver gps;
	FlightGPSFix latest_fix;

	gps.setFixCallback([&latest_fix](const auto &fix) {
		latest_fix.latitude_deg = fix.latitude_deg;
		latest_fix.longitude_deg = fix.longitude_deg;
		latest_fix.altitude_m = fix.altitude_m;
		latest_fix.horizontal_accuracy_m = 0.05;
		latest_fix.vertical_accuracy_m = 0.10;
		latest_fix.speed_accuracy_mps = 0.05;
		latest_fix.fix_type = 3;
		latest_fix.valid = fix.valid;
	});

	for (int i = 0; i < kSamples; ++i) {
		const double noise_m = (i % 2 == 0) ? 0.02 : -0.02;
		const Pose measured = offset_pose(noise_m, -noise_m,
						  kExpectedAltitudeM + noise_m);
		const std::string nmea = gga_sentence(measured);

		if (gps.ingest(nmea) != GpsDriverStatus::kOk ||
		    require_status(latest_fix.valid, "GPS parser rejected synthetic fix"))
			return 1;

		FlightIMUSample imu;
		imu.accel_mps2 = { 0.0, 0.0, kGravityMps2 };
		imu.gyro_rps = { 0.0, 0.0, 0.0 };
		imu.mag_ut = { 25.0, 0.0, 40.0 };
		imu.has_magnetometer = true;

		if (ekf.predict(imu, kDtS) != FlightEKFStatus::kOk)
			return require_status(false, "EKF predict rejected IMU sample");
		if (ekf.update(latest_fix) != FlightEKFStatus::kOk)
			return require_status(false, "EKF rejected GPS fix");

		const FlightBarometerSample barometer {
			.altitude_m = kExpectedAltitudeM - noise_m,
			.variance_m2 = 0.01,
			.valid = true,
		};

		if (ekf.update(barometer) != FlightEKFStatus::kOk)
			return require_status(false, "EKF rejected barometer sample");
	}

	const auto state = ekf.getState();
	const Pose expected = offset_pose(0.0, 0.0, kExpectedAltitudeM);
	const double h_error = horizontal_error_m(expected, state.latitude_deg,
						 state.longitude_deg);
	const double v_error = std::fabs(state.altitude_m - expected.altitude_m);

	if (require_status(state.converged, "EKF did not report convergence") ||
	    require_status(state.predict_count == kSamples,
			   "EKF predict count did not match synthetic stream") ||
	    require_status(state.gps_update_count == kSamples,
			   "EKF GPS update count did not match synthetic stream") ||
	    require_status(state.barometer_update_count == kSamples,
			   "EKF barometer update count did not match synthetic stream") ||
	    require_status(h_error <= kHorizontalToleranceM,
			   "EKF horizontal error exceeded 10cm") ||
	    require_status(v_error <= kVerticalToleranceM,
			   "EKF vertical error exceeded 30cm"))
		return 1;

	std::cout << "sensor-fusion: converged in " << (kSamples * kDtS * 1000.0)
		  << "ms horizontal_error_m=" << h_error
		  << " vertical_error_m=" << v_error << "\n";
	return 0;
}
