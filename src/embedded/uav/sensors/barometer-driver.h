/* SPDX-License-Identifier: MIT
 *
 * Cases 6+7 UAV barometer driver (OP-2035).
 */
#ifndef OMNISIGHT_EMBEDDED_UAV_SENSORS_BAROMETER_DRIVER_H_
#define OMNISIGHT_EMBEDDED_UAV_SENSORS_BAROMETER_DRIVER_H_

#include <cstdint>
#include <functional>
#include <memory>
#include <string>

namespace omnisight::embedded::uav::sensors {

enum class BarometerStatus {
	kOk = 0,
	kInvalidArgument,
	kInvalidState,
	kUnsupportedChip,
	kIoError,
};

enum class BarometerChip {
	kBmp388 = 0,
	kMs5611,
};

struct BarometerConfig {
	BarometerChip chip = BarometerChip::kBmp388;
	std::string i2c_device = "/dev/i2c-1";
	uint16_t i2c_address = 0;
	uint32_t sample_rate_hz = 10;
	double sea_level_pressure_pa = 101325.0;
};

using BarometerReadingCallback = std::function<void(double altitude_m,
						    double pressure_pa,
						    double temp_c)>;

class BarometerDriver {
public:
	BarometerDriver();
	explicit BarometerDriver(BarometerConfig config);
	~BarometerDriver();

	BarometerDriver(const BarometerDriver &) = delete;
	BarometerDriver &operator=(const BarometerDriver &) = delete;
	BarometerDriver(BarometerDriver &&) noexcept;
	BarometerDriver &operator=(BarometerDriver &&) noexcept;

	BarometerStatus open(const BarometerConfig &config);
	void close();

	BarometerStatus start();
	void stop();
	BarometerStatus poll();
	void onReading(BarometerReadingCallback callback);

	bool open() const;
	bool running() const;
	const std::string &lastError() const;

private:
	class Impl;

	std::unique_ptr<Impl> impl_;
};

} // namespace omnisight::embedded::uav::sensors

#endif // OMNISIGHT_EMBEDDED_UAV_SENSORS_BAROMETER_DRIVER_H_
