/* SPDX-License-Identifier: MIT
 *
 * GPS NMEA-0183 + u-blox UBX driver for Case 6 UAV sensors (OP-2025).
 */
#ifndef OMNISIGHT_EMBEDDED_UAV_SENSORS_GPS_NMEA_DRIVER_H_
#define OMNISIGHT_EMBEDDED_UAV_SENSORS_GPS_NMEA_DRIVER_H_

#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>

namespace omnisight::embedded::uav::sensors {

enum class GpsDriverStatus {
	kOk = 0,
	kInvalidArgument,
	kParseError,
	kUnavailable,
	kIoError,
	kInvalidState,
};

struct GpsFix {
	double latitude_deg = 0.0;
	double longitude_deg = 0.0;
	double altitude_m = 0.0;
	double hdop = 0.0;
	uint8_t satellites = 0;
	bool valid = false;
};

struct GpsDriverConfig {
	std::string serial_device;
	uint32_t baud_rate = 9600;
};

class GpsDriver {
public:
	using FixCallback = std::function<void(const GpsFix &)>;

	GpsDriver();
	explicit GpsDriver(GpsDriverConfig config);
	~GpsDriver();

	GpsDriver(const GpsDriver &) = delete;
	GpsDriver &operator=(const GpsDriver &) = delete;
	GpsDriver(GpsDriver &&) noexcept;
	GpsDriver &operator=(GpsDriver &&) noexcept;

	GpsDriverStatus configure(GpsDriverConfig config);
	void setFixCallback(FixCallback callback);

	GpsDriverStatus start();
	void stop();
	bool running() const;
	bool available() const;

	GpsDriverStatus ingest(const uint8_t *data, size_t size);
	GpsDriverStatus ingest(const std::string &data);

	GpsFix lastFix() const;
	const std::string &lastError() const;

private:
	class Impl;

	std::unique_ptr<Impl> impl_;
};

} // namespace omnisight::embedded::uav::sensors

#endif // OMNISIGHT_EMBEDDED_UAV_SENSORS_GPS_NMEA_DRIVER_H_
