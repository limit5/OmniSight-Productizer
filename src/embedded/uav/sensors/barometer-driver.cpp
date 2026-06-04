/* SPDX-License-Identifier: MIT
 *
 * Cases 6+7 UAV barometer driver (OP-2035).
 */
#include "barometer-driver.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <cstring>
#include <mutex>
#include <thread>
#include <utility>

#include <fcntl.h>
#include <linux/i2c-dev.h>
#include <sys/ioctl.h>
#include <unistd.h>

namespace omnisight::embedded::uav::sensors {
namespace {

constexpr uint16_t kBmp388DefaultAddress = 0x77;
constexpr uint16_t kMs5611DefaultAddress = 0x77;
constexpr uint8_t kBmp388ChipIdRegister = 0x00;
constexpr uint8_t kBmp388ExpectedChipId = 0x50;
constexpr uint8_t kBmp388PressureDataRegister = 0x04;
constexpr uint8_t kBmp388CalibrationRegister = 0x31;
constexpr uint8_t kBmp388PowerControlRegister = 0x1b;
constexpr uint8_t kBmp388OversamplingRegister = 0x1c;
constexpr uint8_t kBmp388OdrRegister = 0x1d;
constexpr uint8_t kBmp388ConfigRegister = 0x1f;
constexpr uint8_t kBmp388ResetRegister = 0x7e;
constexpr uint8_t kBmp388SoftResetCommand = 0xb6;
constexpr uint8_t kMs5611ResetCommand = 0x1e;
constexpr uint8_t kMs5611PromBaseCommand = 0xa0;
constexpr uint8_t kMs5611ConvertD1Osr4096 = 0x48;
constexpr uint8_t kMs5611ConvertD2Osr4096 = 0x58;
constexpr uint8_t kMs5611AdcReadCommand = 0x00;

static int close_fd(int fd)
{
	if (fd >= 0)
		(void)::close(fd);
	return -1;
}

static uint16_t le_u16(const uint8_t *data)
{
	return static_cast<uint16_t>(data[0]) |
	       (static_cast<uint16_t>(data[1]) << 8);
}

static int16_t le_i16(const uint8_t *data)
{
	return static_cast<int16_t>(le_u16(data));
}

static uint16_t be_u16(const uint8_t *data)
{
	return (static_cast<uint16_t>(data[0]) << 8) |
	       static_cast<uint16_t>(data[1]);
}

static uint32_t be_u24(const uint8_t *data)
{
	return (static_cast<uint32_t>(data[0]) << 16) |
	       (static_cast<uint32_t>(data[1]) << 8) |
	       static_cast<uint32_t>(data[2]);
}

static uint32_t le_u24(const uint8_t *data)
{
	return static_cast<uint32_t>(data[0]) |
	       (static_cast<uint32_t>(data[1]) << 8) |
	       (static_cast<uint32_t>(data[2]) << 16);
}

static uint16_t default_address(BarometerChip chip)
{
	switch (chip) {
	case BarometerChip::kBmp388:
		return kBmp388DefaultAddress;
	case BarometerChip::kMs5611:
		return kMs5611DefaultAddress;
	default:
		return 0;
	}
}

static double altitude_from_pressure(double pressure_pa, double sea_level_pressure_pa)
{
	if (pressure_pa <= 0.0 || sea_level_pressure_pa <= 0.0)
		return 0.0;
	return 44330.0 * (1.0 - std::pow(pressure_pa / sea_level_pressure_pa,
					 1.0 / 5.255));
}

struct Bmp388Calibration {
	double t1 = 0.0;
	double t2 = 0.0;
	double t3 = 0.0;
	double p1 = 0.0;
	double p2 = 0.0;
	double p3 = 0.0;
	double p4 = 0.0;
	double p5 = 0.0;
	double p6 = 0.0;
	double p7 = 0.0;
	double p8 = 0.0;
	double p9 = 0.0;
	double p10 = 0.0;
	double p11 = 0.0;
};

static Bmp388Calibration parse_bmp388_calibration(const uint8_t *data)
{
	Bmp388Calibration cal {};

	cal.t1 = static_cast<double>(le_u16(&data[0])) * 256.0;
	cal.t2 = static_cast<double>(le_u16(&data[2])) / 1073741824.0;
	cal.t3 = static_cast<double>(static_cast<int8_t>(data[4])) /
		 281474976710656.0;
	cal.p1 = (static_cast<double>(le_i16(&data[5])) - 16384.0) /
		 1048576.0;
	cal.p2 = (static_cast<double>(le_i16(&data[7])) - 16384.0) /
		 536870912.0;
	cal.p3 = static_cast<double>(static_cast<int8_t>(data[9])) /
		 4294967296.0;
	cal.p4 = static_cast<double>(static_cast<int8_t>(data[10])) /
		 137438953472.0;
	cal.p5 = static_cast<double>(le_u16(&data[11])) * 8.0;
	cal.p6 = static_cast<double>(le_u16(&data[13])) / 64.0;
	cal.p7 = static_cast<double>(static_cast<int8_t>(data[15])) / 256.0;
	cal.p8 = static_cast<double>(static_cast<int8_t>(data[16])) / 32768.0;
	cal.p9 = static_cast<double>(le_i16(&data[17])) / 281474976710656.0;
	cal.p10 = static_cast<double>(static_cast<int8_t>(data[19])) /
		  281474976710656.0;
	cal.p11 = static_cast<double>(static_cast<int8_t>(data[20])) /
		  36893488147419103232.0;
	return cal;
}

static void compensate_bmp388(uint32_t raw_pressure, uint32_t raw_temp,
			      const Bmp388Calibration &cal, double *pressure_pa,
			      double *temp_c)
{
	const double partial_t1 = static_cast<double>(raw_temp) - cal.t1;
	const double partial_t2 = partial_t1 * cal.t2;
	const double t_lin = partial_t2 + (partial_t1 * partial_t1) * cal.t3;
	const double t_sq = t_lin * t_lin;
	const double t_cube = t_sq * t_lin;
	const double raw = static_cast<double>(raw_pressure);
	const double raw_sq = raw * raw;
	const double raw_cube = raw_sq * raw;
	const double out1 = cal.p5 + cal.p6 * t_lin + cal.p7 * t_sq +
			    cal.p8 * t_cube;
	const double out2 = raw * (cal.p1 + cal.p2 * t_lin + cal.p3 * t_sq +
				   cal.p4 * t_cube);
	const double out3 = raw_sq * (cal.p9 + cal.p10 * t_lin);
	const double out4 = raw_cube * cal.p11;

	*temp_c = t_lin;
	*pressure_pa = out1 + out2 + out3 + out4;
}

} // namespace

class BarometerDriver::Impl {
public:
	Impl() = default;
	explicit Impl(BarometerConfig config) : config_(std::move(config)) {}
	~Impl()
	{
		stop();
		close();
	}

	Impl(const Impl &) = delete;
	Impl &operator=(const Impl &) = delete;

	BarometerStatus open(const BarometerConfig &config)
	{
		if (config.i2c_device.empty()) {
			last_error_ = "i2c_device is required";
			return BarometerStatus::kInvalidArgument;
		}
		if (config.sample_rate_hz == 0) {
			last_error_ = "sample_rate_hz must be greater than zero";
			return BarometerStatus::kInvalidArgument;
		}
		if (default_address(config.chip) == 0) {
			last_error_ = "unsupported barometer chip";
			return BarometerStatus::kUnsupportedChip;
		}

		stop();
		close();
		config_ = config;
		if (config_.i2c_address == 0)
			config_.i2c_address = default_address(config_.chip);

		fd_ = ::open(config_.i2c_device.c_str(), O_RDWR | O_CLOEXEC);
		if (fd_ < 0) {
			last_error_ = std::strerror(errno);
			return BarometerStatus::kIoError;
		}
		if (::ioctl(fd_, I2C_SLAVE, config_.i2c_address) < 0) {
			last_error_ = std::strerror(errno);
			fd_ = close_fd(fd_);
			return BarometerStatus::kIoError;
		}

		const BarometerStatus status = configure_chip();

		if (status != BarometerStatus::kOk)
			fd_ = close_fd(fd_);
		return status;
	}

	void close()
	{
		stop();
		fd_ = close_fd(fd_);
	}

	BarometerStatus start()
	{
		if (fd_ < 0) {
			last_error_ = "barometer is not open";
			return BarometerStatus::kInvalidState;
		}
		if (running_.load())
			return BarometerStatus::kOk;

		running_.store(true);
		worker_ = std::thread([this]() { run_loop(); });
		return BarometerStatus::kOk;
	}

	void stop()
	{
		if (!running_.exchange(false))
			return;
		if (worker_.joinable())
			worker_.join();
	}

	BarometerStatus poll()
	{
		double pressure_pa = 0.0;
		double temp_c = 0.0;
		const BarometerStatus status = read_sample(&pressure_pa, &temp_c);

		if (status != BarometerStatus::kOk)
			return status;

		BarometerReadingCallback callback;
		{
			std::lock_guard<std::mutex> lock(callback_mutex_);
			callback = callback_;
		}
		if (callback) {
			const double altitude_m =
				altitude_from_pressure(pressure_pa,
						       config_.sea_level_pressure_pa);
			callback(altitude_m, pressure_pa, temp_c);
		}
		return BarometerStatus::kOk;
	}

	void on_reading(BarometerReadingCallback callback)
	{
		std::lock_guard<std::mutex> lock(callback_mutex_);
		callback_ = std::move(callback);
	}

	bool open() const { return fd_ >= 0; }
	bool running() const { return running_.load(); }
	const std::string &last_error() const { return last_error_; }

private:
	bool write_command(uint8_t command)
	{
		return ::write(fd_, &command, 1) == 1;
	}

	bool write_register(uint8_t reg, uint8_t value)
	{
		const std::array<uint8_t, 2> data { reg, value };

		return ::write(fd_, data.data(), data.size()) ==
		       static_cast<ssize_t>(data.size());
	}

	bool read_registers(uint8_t reg, uint8_t *data, size_t len)
	{
		if (::write(fd_, &reg, 1) != 1)
			return false;
		return ::read(fd_, data, len) == static_cast<ssize_t>(len);
	}

	BarometerStatus configure_chip()
	{
		switch (config_.chip) {
		case BarometerChip::kBmp388:
			return configure_bmp388();
		case BarometerChip::kMs5611:
			return configure_ms5611();
		default:
			last_error_ = "unsupported barometer chip";
			return BarometerStatus::kUnsupportedChip;
		}
	}

	BarometerStatus configure_bmp388()
	{
		uint8_t chip_id = 0;
		std::array<uint8_t, 21> calibration {};

		if (!write_register(kBmp388ResetRegister, kBmp388SoftResetCommand)) {
			last_error_ = std::strerror(errno);
			return BarometerStatus::kIoError;
		}
		std::this_thread::sleep_for(std::chrono::milliseconds(5));
		if (!read_registers(kBmp388ChipIdRegister, &chip_id, 1)) {
			last_error_ = std::strerror(errno);
			return BarometerStatus::kIoError;
		}
		if (chip_id != kBmp388ExpectedChipId) {
			last_error_ = "BMP388 chip id mismatch";
			return BarometerStatus::kUnsupportedChip;
		}
		if (!read_registers(kBmp388CalibrationRegister, calibration.data(),
				    calibration.size())) {
			last_error_ = std::strerror(errno);
			return BarometerStatus::kIoError;
		}
		bmp388_calibration_ =
			parse_bmp388_calibration(calibration.data());
		if (!write_register(kBmp388OversamplingRegister, 0x12) ||
		    !write_register(kBmp388OdrRegister, 0x03) ||
		    !write_register(kBmp388ConfigRegister, 0x00) ||
		    !write_register(kBmp388PowerControlRegister, 0x33)) {
			last_error_ = std::strerror(errno);
			return BarometerStatus::kIoError;
		}
		std::this_thread::sleep_for(std::chrono::milliseconds(10));
		return BarometerStatus::kOk;
	}

	BarometerStatus configure_ms5611()
	{
		std::array<uint8_t, 2> data {};

		if (!write_command(kMs5611ResetCommand)) {
			last_error_ = std::strerror(errno);
			return BarometerStatus::kIoError;
		}
		std::this_thread::sleep_for(std::chrono::milliseconds(3));
		for (size_t i = 0; i < ms5611_prom_.size(); ++i) {
			const uint8_t command =
				static_cast<uint8_t>(kMs5611PromBaseCommand +
						    i * 2);

			if (!read_registers(command, data.data(), data.size())) {
				last_error_ = std::strerror(errno);
				return BarometerStatus::kIoError;
			}
			ms5611_prom_[i] = be_u16(data.data());
		}
		if (ms5611_prom_[1] == 0 || ms5611_prom_[2] == 0 ||
		    ms5611_prom_[5] == 0 || ms5611_prom_[6] == 0) {
			last_error_ = "MS5611 PROM coefficients are invalid";
			return BarometerStatus::kUnsupportedChip;
		}
		return BarometerStatus::kOk;
	}

	BarometerStatus read_sample(double *pressure_pa, double *temp_c)
	{
		if (fd_ < 0) {
			last_error_ = "barometer is not open";
			return BarometerStatus::kInvalidState;
		}

		switch (config_.chip) {
		case BarometerChip::kBmp388:
			return read_bmp388(pressure_pa, temp_c);
		case BarometerChip::kMs5611:
			return read_ms5611(pressure_pa, temp_c);
		default:
			last_error_ = "unsupported barometer chip";
			return BarometerStatus::kUnsupportedChip;
		}
	}

	BarometerStatus read_bmp388(double *pressure_pa, double *temp_c)
	{
		std::array<uint8_t, 6> data {};

		if (!read_registers(kBmp388PressureDataRegister, data.data(),
				    data.size())) {
			last_error_ = std::strerror(errno);
			return BarometerStatus::kIoError;
		}
		compensate_bmp388(le_u24(&data[0]), le_u24(&data[3]),
				  bmp388_calibration_, pressure_pa, temp_c);
		return BarometerStatus::kOk;
	}

	BarometerStatus read_ms5611(double *pressure_pa, double *temp_c)
	{
		uint32_t d1 = 0;
		uint32_t d2 = 0;
		BarometerStatus status = read_ms5611_adc(kMs5611ConvertD1Osr4096,
							 &d1);

		if (status != BarometerStatus::kOk)
			return status;
		status = read_ms5611_adc(kMs5611ConvertD2Osr4096, &d2);
		if (status != BarometerStatus::kOk)
			return status;

		const int64_t dt = static_cast<int64_t>(d2) -
				   static_cast<int64_t>(ms5611_prom_[5]) * 256;
		int64_t temp = 2000 + dt * ms5611_prom_[6] / 8388608;
		int64_t off = static_cast<int64_t>(ms5611_prom_[2]) * 65536 +
			      static_cast<int64_t>(ms5611_prom_[4]) * dt / 128;
		int64_t sens = static_cast<int64_t>(ms5611_prom_[1]) * 32768 +
			       static_cast<int64_t>(ms5611_prom_[3]) * dt / 256;

		if (temp < 2000) {
			const int64_t temp_delta = temp - 2000;
			int64_t off2 = 5 * temp_delta * temp_delta / 2;
			int64_t sens2 = 5 * temp_delta * temp_delta / 4;

			if (temp < -1500) {
				const int64_t cold_delta = temp + 1500;

				off2 += 7 * cold_delta * cold_delta;
				sens2 += 11 * cold_delta * cold_delta / 2;
			}
			temp -= dt * dt / 2147483648LL;
			off -= off2;
			sens -= sens2;
		}

		*pressure_pa = static_cast<double>(
			(static_cast<int64_t>(d1) * sens / 2097152 - off) /
			32768);
		*temp_c = static_cast<double>(temp) / 100.0;
		return BarometerStatus::kOk;
	}

	BarometerStatus read_ms5611_adc(uint8_t command, uint32_t *value)
	{
		std::array<uint8_t, 3> data {};

		if (!write_command(command)) {
			last_error_ = std::strerror(errno);
			return BarometerStatus::kIoError;
		}
		std::this_thread::sleep_for(std::chrono::milliseconds(10));
		if (!read_registers(kMs5611AdcReadCommand, data.data(),
				    data.size())) {
			last_error_ = std::strerror(errno);
			return BarometerStatus::kIoError;
		}
		*value = be_u24(data.data());
		return BarometerStatus::kOk;
	}

	void run_loop()
	{
		while (running_.load()) {
			const auto started = std::chrono::steady_clock::now();

			(void)poll();
			const auto period = std::chrono::milliseconds(
				std::max<uint32_t>(1, 1000 / config_.sample_rate_hz));
			const auto elapsed = std::chrono::steady_clock::now() -
					     started;

			if (elapsed < period)
				std::this_thread::sleep_for(period - elapsed);
		}
	}

	BarometerConfig config_;
	int fd_ = -1;
	std::string last_error_;
	Bmp388Calibration bmp388_calibration_;
	std::array<uint16_t, 8> ms5611_prom_ {};
	std::atomic<bool> running_ { false };
	std::thread worker_;
	std::mutex callback_mutex_;
	BarometerReadingCallback callback_;
};

BarometerDriver::BarometerDriver() : impl_(std::make_unique<Impl>()) {}

BarometerDriver::BarometerDriver(BarometerConfig config)
	: impl_(std::make_unique<Impl>(std::move(config)))
{
}

BarometerDriver::~BarometerDriver() = default;
BarometerDriver::BarometerDriver(BarometerDriver &&) noexcept = default;
BarometerDriver &BarometerDriver::operator=(BarometerDriver &&) noexcept = default;

BarometerStatus BarometerDriver::open(const BarometerConfig &config)
{
	return impl_->open(config);
}

void BarometerDriver::close()
{
	impl_->close();
}

BarometerStatus BarometerDriver::start()
{
	return impl_->start();
}

void BarometerDriver::stop()
{
	impl_->stop();
}

BarometerStatus BarometerDriver::poll()
{
	return impl_->poll();
}

void BarometerDriver::onReading(BarometerReadingCallback callback)
{
	impl_->on_reading(std::move(callback));
}

bool BarometerDriver::open() const
{
	return impl_->open();
}

bool BarometerDriver::running() const
{
	return impl_->running();
}

const std::string &BarometerDriver::lastError() const
{
	return impl_->last_error();
}

} // namespace omnisight::embedded::uav::sensors
