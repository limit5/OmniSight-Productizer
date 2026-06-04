/* SPDX-License-Identifier: MIT
 *
 * GPS NMEA-0183 + u-blox UBX driver for Case 6 UAV sensors (OP-2025).
 */
#include "gps-nmea-driver.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <cctype>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <thread>
#include <utility>
#include <vector>

#if defined(OMNISIGHT_GPS_WITH_POSIX_SERIAL)
#include <cerrno>
#include <fcntl.h>
#include <poll.h>
#include <termios.h>
#include <unistd.h>
#endif

namespace omnisight::embedded::uav::sensors {
namespace {

constexpr uint8_t kUbxSync1 = 0xb5;
constexpr uint8_t kUbxSync2 = 0x62;
constexpr size_t kMaxNmeaSentence = 96;
constexpr size_t kMaxUbxFrame = 512;

enum class UbxParseState {
	kSync1 = 0,
	kSync2,
	kClass,
	kId,
	kLen1,
	kLen2,
	kPayload,
	kChecksumA,
	kChecksumB,
};

static bool valid_baud_rate(uint32_t baud_rate)
{
	return baud_rate == 4800 || baud_rate == 9600 || baud_rate == 19200 ||
	       baud_rate == 38400 || baud_rate == 57600 || baud_rate == 115200;
}

static int hex_value(char ch)
{
	if (ch >= '0' && ch <= '9')
		return ch - '0';
	ch = static_cast<char>(std::toupper(static_cast<unsigned char>(ch)));
	if (ch >= 'A' && ch <= 'F')
		return 10 + ch - 'A';
	return -1;
}

static uint8_t nmea_checksum(const std::string &sentence, size_t star)
{
	uint8_t checksum = 0;

	for (size_t i = 1; i < star; ++i)
		checksum ^= static_cast<uint8_t>(sentence[i]);

	return checksum;
}

static double safe_float(const std::vector<std::string> &fields, size_t index)
{
	if (index >= fields.size() || fields[index].empty())
		return 0.0;

	char *end = nullptr;
	const double value = std::strtod(fields[index].c_str(), &end);

	return end == fields[index].c_str() ? 0.0 : value;
}

static int safe_int(const std::vector<std::string> &fields, size_t index)
{
	if (index >= fields.size() || fields[index].empty())
		return 0;

	char *end = nullptr;
	const long value = std::strtol(fields[index].c_str(), &end, 10);

	return end == fields[index].c_str() ? 0 : static_cast<int>(value);
}

static std::vector<std::string> split_csv(const std::string &data)
{
	std::vector<std::string> fields;
	size_t start = 0;

	while (start <= data.size()) {
		const size_t comma = data.find(',', start);

		if (comma == std::string::npos) {
			fields.emplace_back(data.substr(start));
			break;
		}
		fields.emplace_back(data.substr(start, comma - start));
		start = comma + 1;
	}

	return fields;
}

static bool parse_lat_lon(const std::string &lat_value,
			  const std::string &lat_dir,
			  const std::string &lon_value,
			  const std::string &lon_dir,
			  double *latitude_deg,
			  double *longitude_deg)
{
	if (!latitude_deg || !longitude_deg || lat_value.empty() ||
	    lon_value.empty())
		return false;

	const double raw_lat = std::strtod(lat_value.c_str(), nullptr);
	const double raw_lon = std::strtod(lon_value.c_str(), nullptr);
	const double lat_deg = std::floor(raw_lat / 100.0);
	const double lon_deg = std::floor(raw_lon / 100.0);

	*latitude_deg = lat_deg + (raw_lat - lat_deg * 100.0) / 60.0;
	*longitude_deg = lon_deg + (raw_lon - lon_deg * 100.0) / 60.0;

	if (lat_dir == "S")
		*latitude_deg = -*latitude_deg;
	if (lon_dir == "W")
		*longitude_deg = -*longitude_deg;

	return true;
}

static uint16_t le16(const std::vector<uint8_t> &payload, size_t offset)
{
	return static_cast<uint16_t>(payload[offset]) |
	       static_cast<uint16_t>(payload[offset + 1]) << 8;
}

static uint32_t le32(const std::vector<uint8_t> &payload, size_t offset)
{
	return static_cast<uint32_t>(payload[offset]) |
	       static_cast<uint32_t>(payload[offset + 1]) << 8 |
	       static_cast<uint32_t>(payload[offset + 2]) << 16 |
	       static_cast<uint32_t>(payload[offset + 3]) << 24;
}

static int32_t sle32(const std::vector<uint8_t> &payload, size_t offset)
{
	return static_cast<int32_t>(le32(payload, offset));
}

static void ubx_checksum(const uint8_t *data, size_t size, uint8_t *ck_a,
			 uint8_t *ck_b)
{
	uint8_t a = 0;
	uint8_t b = 0;

	for (size_t i = 0; i < size; ++i) {
		a = static_cast<uint8_t>(a + data[i]);
		b = static_cast<uint8_t>(b + a);
	}

	*ck_a = a;
	*ck_b = b;
}

#if defined(OMNISIGHT_GPS_WITH_POSIX_SERIAL)
static speed_t termios_baud(uint32_t baud_rate)
{
	switch (baud_rate) {
	case 4800:
		return B4800;
	case 9600:
		return B9600;
	case 19200:
		return B19200;
	case 38400:
		return B38400;
	case 57600:
		return B57600;
	case 115200:
		return B115200;
	default:
		return B9600;
	}
}
#endif

} // namespace

class GpsDriver::Impl {
public:
	Impl() = default;
	explicit Impl(GpsDriverConfig config) : config_(std::move(config)) {}

	~Impl()
	{
		stop();
	}

	GpsDriverStatus configure(GpsDriverConfig config)
	{
		if (!valid_baud_rate(config.baud_rate))
			return fail(GpsDriverStatus::kInvalidArgument,
				    "unsupported GPS baud rate");
		if (running_.load())
			return fail(GpsDriverStatus::kInvalidState,
				    "cannot configure a running GPS driver");

		std::lock_guard<std::mutex> lock(mutex_);

		config_ = std::move(config);
		return GpsDriverStatus::kOk;
	}

	void setFixCallback(FixCallback callback)
	{
		std::lock_guard<std::mutex> lock(mutex_);

		callback_ = std::move(callback);
	}

	GpsDriverStatus start()
	{
		if (config_.serial_device.empty())
			return fail(GpsDriverStatus::kInvalidArgument,
				    "serial device is required to start GPS driver");
		if (!valid_baud_rate(config_.baud_rate))
			return fail(GpsDriverStatus::kInvalidArgument,
				    "unsupported GPS baud rate");

#if !defined(OMNISIGHT_GPS_WITH_POSIX_SERIAL)
		return fail(GpsDriverStatus::kUnavailable,
			    "POSIX serial support is not enabled");
#else
		bool expected = false;

		if (!running_.compare_exchange_strong(expected, true))
			return GpsDriverStatus::kOk;

		worker_ = std::thread(&Impl::run, this);
		return GpsDriverStatus::kOk;
#endif
	}

	void stop()
	{
		running_.store(false);
		if (worker_.joinable())
			worker_.join();
	}

	bool running() const
	{
		return running_.load();
	}

	bool available() const
	{
#if defined(OMNISIGHT_GPS_WITH_POSIX_SERIAL)
		return true;
#else
		return false;
#endif
	}

	GpsDriverStatus ingest(const uint8_t *data, size_t size)
	{
		if (!data && size != 0)
			return fail(GpsDriverStatus::kInvalidArgument,
				    "GPS ingest data pointer is null");

		for (size_t i = 0; i < size; ++i)
			ingestByte(data[i]);

		return GpsDriverStatus::kOk;
	}

	GpsFix lastFix() const
	{
		std::lock_guard<std::mutex> lock(mutex_);

		return last_fix_;
	}

	const std::string &lastError() const
	{
		return last_error_;
	}

private:
#if defined(OMNISIGHT_GPS_WITH_POSIX_SERIAL)
	void run()
	{
		const int fd = openSerial();

		if (fd < 0) {
			running_.store(false);
			return;
		}

		while (running_.load()) {
			pollfd pfd {
				.fd = fd,
				.events = POLLIN,
				.revents = 0,
			};
			const int rc = poll(&pfd, 1, 50);

			if (rc < 0) {
				if (errno == EINTR)
					continue;
				setIoError("poll", errno);
				break;
			}
			if (rc == 0 || !(pfd.revents & POLLIN))
				continue;

			std::array<uint8_t, 256> buffer {};
			const ssize_t got = read(fd, buffer.data(), buffer.size());

			if (got < 0) {
				if (errno == EAGAIN || errno == EINTR)
					continue;
				setIoError("read", errno);
				break;
			}
			if (got > 0)
				(void)ingest(buffer.data(), static_cast<size_t>(got));
		}

		close(fd);
		running_.store(false);
	}

	int openSerial()
	{
		const int fd = open(config_.serial_device.c_str(), O_RDONLY | O_NOCTTY |
							      O_NONBLOCK);

		if (fd < 0) {
			setIoError("open serial device", errno);
			return -1;
		}

		termios tty {};
		if (tcgetattr(fd, &tty) != 0) {
			setIoError("tcgetattr", errno);
			close(fd);
			return -1;
		}

		cfmakeraw(&tty);
		cfsetispeed(&tty, termios_baud(config_.baud_rate));
		cfsetospeed(&tty, termios_baud(config_.baud_rate));
		tty.c_cflag |= CLOCAL | CREAD;
		tty.c_cflag &= ~CRTSCTS;

		if (tcsetattr(fd, TCSANOW, &tty) != 0) {
			setIoError("tcsetattr", errno);
			close(fd);
			return -1;
		}

		return fd;
	}

	void setIoError(const char *op, int err)
	{
		std::lock_guard<std::mutex> lock(mutex_);

		last_error_ = std::string(op) + ": " + std::strerror(err);
	}
#endif

	void ingestByte(uint8_t byte)
	{
		if (byte == kUbxSync1 && nmea_buffer_.empty()) {
			startUbx(byte);
			return;
		}

		if (ubx_state_ != UbxParseState::kSync1) {
			ingestUbx(byte);
			return;
		}

		if (byte == '$') {
			nmea_buffer_.clear();
			nmea_buffer_.push_back(static_cast<char>(byte));
			return;
		}
		if (nmea_buffer_.empty())
			return;

		if (byte == '\r')
			return;
		if (byte == '\n') {
			parseNmea(nmea_buffer_);
			nmea_buffer_.clear();
			return;
		}

		if (nmea_buffer_.size() >= kMaxNmeaSentence) {
			nmea_buffer_.clear();
			(void)fail(GpsDriverStatus::kParseError,
				   "NMEA sentence exceeds maximum length");
			return;
		}

		nmea_buffer_.push_back(static_cast<char>(byte));
	}

	void startUbx(uint8_t byte)
	{
		ubx_frame_.clear();
		ubx_frame_.push_back(byte);
		ubx_payload_.clear();
		ubx_length_ = 0;
		ubx_state_ = UbxParseState::kSync2;
	}

	void resetUbx()
	{
		ubx_state_ = UbxParseState::kSync1;
		ubx_frame_.clear();
		ubx_payload_.clear();
		ubx_length_ = 0;
	}

	void ingestUbx(uint8_t byte)
	{
		if (ubx_frame_.size() >= kMaxUbxFrame) {
			resetUbx();
			(void)fail(GpsDriverStatus::kParseError,
				   "UBX frame exceeds maximum length");
			return;
		}

		ubx_frame_.push_back(byte);

		switch (ubx_state_) {
		case UbxParseState::kSync1:
			break;
		case UbxParseState::kSync2:
			if (byte == kUbxSync2)
				ubx_state_ = UbxParseState::kClass;
			else
				resetUbx();
			break;
		case UbxParseState::kClass:
			ubx_class_ = byte;
			ubx_state_ = UbxParseState::kId;
			break;
		case UbxParseState::kId:
			ubx_id_ = byte;
			ubx_state_ = UbxParseState::kLen1;
			break;
		case UbxParseState::kLen1:
			ubx_length_ = byte;
			ubx_state_ = UbxParseState::kLen2;
			break;
		case UbxParseState::kLen2:
			ubx_length_ |= static_cast<uint16_t>(byte) << 8;
			if (ubx_length_ > kMaxUbxFrame - 8) {
				resetUbx();
				(void)fail(GpsDriverStatus::kParseError,
					   "UBX payload exceeds maximum length");
				break;
			}
			ubx_state_ = ubx_length_ == 0 ? UbxParseState::kChecksumA :
							UbxParseState::kPayload;
			break;
		case UbxParseState::kPayload:
			ubx_payload_.push_back(byte);
			if (ubx_payload_.size() == ubx_length_)
				ubx_state_ = UbxParseState::kChecksumA;
			break;
		case UbxParseState::kChecksumA:
			ubx_ck_a_ = byte;
			ubx_state_ = UbxParseState::kChecksumB;
			break;
		case UbxParseState::kChecksumB:
			parseUbx(byte);
			resetUbx();
			break;
		}
	}

	void parseNmea(const std::string &sentence)
	{
		const size_t star = sentence.find('*');

		if (star == std::string::npos || star + 2 >= sentence.size()) {
			(void)fail(GpsDriverStatus::kParseError,
				   "NMEA sentence has no checksum");
			return;
		}

		const int high = hex_value(sentence[star + 1]);
		const int low = hex_value(sentence[star + 2]);

		if (high < 0 || low < 0) {
			(void)fail(GpsDriverStatus::kParseError,
				   "NMEA checksum is not hexadecimal");
			return;
		}

		const uint8_t expected =
			static_cast<uint8_t>((high << 4) | low);

		if (nmea_checksum(sentence, star) != expected) {
			(void)fail(GpsDriverStatus::kParseError,
				   "NMEA checksum mismatch");
			return;
		}

		const std::vector<std::string> fields =
			split_csv(sentence.substr(1, star - 1));
		if (fields.empty() || fields[0].size() < 3)
			return;

		const std::string type = fields[0].substr(fields[0].size() - 3);

		if (type == "GGA")
			parseGga(fields);
		else if (type == "RMC")
			parseRmc(fields);
		else if (type == "GSV")
			parseGsv(fields);
	}

	void parseGga(const std::vector<std::string> &fields)
	{
		if (fields.size() < 10)
			return;

		GpsFix fix = lastFix();
		double latitude = 0.0;
		double longitude = 0.0;

		if (parse_lat_lon(fields[2], fields[3], fields[4], fields[5],
				  &latitude, &longitude)) {
			fix.latitude_deg = latitude;
			fix.longitude_deg = longitude;
		}

		fix.valid = safe_int(fields, 6) > 0;
		fix.satellites = static_cast<uint8_t>(
			std::clamp(safe_int(fields, 7), 0, 255));
		fix.hdop = safe_float(fields, 8);
		fix.altitude_m = safe_float(fields, 9);

		publishFix(fix);
	}

	void parseRmc(const std::vector<std::string> &fields)
	{
		if (fields.size() < 7)
			return;

		GpsFix fix = lastFix();
		double latitude = 0.0;
		double longitude = 0.0;

		if (parse_lat_lon(fields[3], fields[4], fields[5], fields[6],
				  &latitude, &longitude)) {
			fix.latitude_deg = latitude;
			fix.longitude_deg = longitude;
		}
		fix.valid = fields[2] == "A";

		publishFix(fix);
	}

	void parseGsv(const std::vector<std::string> &fields)
	{
		if (fields.size() < 4)
			return;

		GpsFix fix = lastFix();

		fix.satellites = static_cast<uint8_t>(
			std::clamp(safe_int(fields, 3), 0, 255));
		publishFix(fix);
	}

	void parseUbx(uint8_t ck_b)
	{
		uint8_t expected_a = 0;
		uint8_t expected_b = 0;

		ubx_checksum(ubx_frame_.data() + 2, ubx_frame_.size() - 4,
			     &expected_a, &expected_b);
		if (expected_a != ubx_ck_a_ || expected_b != ck_b) {
			(void)fail(GpsDriverStatus::kParseError,
				   "UBX checksum mismatch");
			return;
		}

		if (ubx_class_ == 0x01 && ubx_id_ == 0x07)
			parseNavPvt();
		else if (ubx_class_ == 0x01 && ubx_id_ == 0x03)
			parseNavStatus();
	}

	void parseNavPvt()
	{
		if (ubx_payload_.size() < 92)
			return;

		GpsFix fix;

		fix.valid = ubx_payload_[20] >= 2 && (ubx_payload_[21] & 0x01) != 0;
		fix.satellites = ubx_payload_[23];
		fix.longitude_deg = static_cast<double>(sle32(ubx_payload_, 24)) * 1e-7;
		fix.latitude_deg = static_cast<double>(sle32(ubx_payload_, 28)) * 1e-7;
		fix.altitude_m = static_cast<double>(sle32(ubx_payload_, 36)) / 1000.0;
		fix.hdop = static_cast<double>(le16(ubx_payload_, 76)) / 100.0;

		publishFix(fix);
	}

	void parseNavStatus()
	{
		if (ubx_payload_.size() < 16)
			return;

		GpsFix fix = lastFix();

		fix.valid = ubx_payload_[4] >= 2 && (ubx_payload_[5] & 0x01) != 0;
		publishFix(fix);
	}

	void publishFix(const GpsFix &fix)
	{
		FixCallback callback;

		{
			std::lock_guard<std::mutex> lock(mutex_);

			last_fix_ = fix;
			last_error_.clear();
			callback = callback_;
		}

		if (callback)
			callback(fix);
	}

	GpsDriverStatus fail(GpsDriverStatus status, const std::string &error) const
	{
		std::lock_guard<std::mutex> lock(mutex_);

		last_error_ = error;
		return status;
	}

	GpsDriverConfig config_;
	mutable std::mutex mutex_;
	FixCallback callback_;
	GpsFix last_fix_;
	mutable std::string last_error_;
	std::atomic<bool> running_ { false };
	std::thread worker_;

	std::string nmea_buffer_;
	UbxParseState ubx_state_ = UbxParseState::kSync1;
	std::vector<uint8_t> ubx_frame_;
	std::vector<uint8_t> ubx_payload_;
	uint8_t ubx_class_ = 0;
	uint8_t ubx_id_ = 0;
	uint16_t ubx_length_ = 0;
	uint8_t ubx_ck_a_ = 0;
};

GpsDriver::GpsDriver() : impl_(std::make_unique<Impl>()) {}

GpsDriver::GpsDriver(GpsDriverConfig config)
	: impl_(std::make_unique<Impl>(std::move(config)))
{
}

GpsDriver::~GpsDriver() = default;
GpsDriver::GpsDriver(GpsDriver &&) noexcept = default;
GpsDriver &GpsDriver::operator=(GpsDriver &&) noexcept = default;

GpsDriverStatus GpsDriver::configure(GpsDriverConfig config)
{
	return impl_->configure(std::move(config));
}

void GpsDriver::setFixCallback(FixCallback callback)
{
	impl_->setFixCallback(std::move(callback));
}

GpsDriverStatus GpsDriver::start()
{
	return impl_->start();
}

void GpsDriver::stop()
{
	impl_->stop();
}

bool GpsDriver::running() const
{
	return impl_->running();
}

bool GpsDriver::available() const
{
	return impl_->available();
}

GpsDriverStatus GpsDriver::ingest(const uint8_t *data, size_t size)
{
	return impl_->ingest(data, size);
}

GpsDriverStatus GpsDriver::ingest(const std::string &data)
{
	return ingest(reinterpret_cast<const uint8_t *>(data.data()), data.size());
}

GpsFix GpsDriver::lastFix() const
{
	return impl_->lastFix();
}

const std::string &GpsDriver::lastError() const
{
	return impl_->lastError();
}

} // namespace omnisight::embedded::uav::sensors

#if defined(OMNISIGHT_GPS_NMEA_DRIVER_SMOKE_MAIN)
#include <iostream>

namespace {

static void put_u16(std::vector<uint8_t> *payload, size_t offset, uint16_t value)
{
	(*payload)[offset] = static_cast<uint8_t>(value & 0xff);
	(*payload)[offset + 1] = static_cast<uint8_t>((value >> 8) & 0xff);
}

static void put_i32(std::vector<uint8_t> *payload, size_t offset, int32_t value)
{
	const auto raw = static_cast<uint32_t>(value);

	(*payload)[offset] = static_cast<uint8_t>(raw & 0xff);
	(*payload)[offset + 1] = static_cast<uint8_t>((raw >> 8) & 0xff);
	(*payload)[offset + 2] = static_cast<uint8_t>((raw >> 16) & 0xff);
	(*payload)[offset + 3] = static_cast<uint8_t>((raw >> 24) & 0xff);
}

static std::vector<uint8_t> ubx_frame(uint8_t msg_class, uint8_t msg_id,
				      const std::vector<uint8_t> &payload)
{
	std::vector<uint8_t> frame {
		0xb5,
		0x62,
		msg_class,
		msg_id,
		static_cast<uint8_t>(payload.size() & 0xff),
		static_cast<uint8_t>((payload.size() >> 8) & 0xff),
	};
	uint8_t ck_a = 0;
	uint8_t ck_b = 0;

	frame.insert(frame.end(), payload.begin(), payload.end());
	for (size_t i = 2; i < frame.size(); ++i) {
		ck_a = static_cast<uint8_t>(ck_a + frame[i]);
		ck_b = static_cast<uint8_t>(ck_b + ck_a);
	}
	frame.push_back(ck_a);
	frame.push_back(ck_b);
	return frame;
}

} // namespace

int main()
{
	using omnisight::embedded::uav::sensors::GpsDriver;
	using omnisight::embedded::uav::sensors::GpsDriverStatus;

	GpsDriver driver;
	bool callback_seen = false;

	driver.setFixCallback([&callback_seen](const auto &fix) {
		callback_seen = fix.valid && fix.satellites == 8 &&
				std::fabs(fix.latitude_deg - 48.1173) < 0.0001 &&
				std::fabs(fix.longitude_deg - 11.5166667) < 0.0001 &&
				std::fabs(fix.altitude_m - 545.4) < 0.1 &&
				std::fabs(fix.hdop - 0.9) < 0.01;
	});

	const std::string nmea =
		"$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,"
		"46.9,M,,*47\r\n";

	if (driver.ingest(nmea) != GpsDriverStatus::kOk || !callback_seen)
		return 1;

	const std::string rmc =
		"$GPRMC,123520,A,4807.038,N,01131.000,E,0.02,31.66,"
		"230394,,,A*69\r\n";
	const std::string gsv = "$GPGSV,1,1,08,01,40,083,41*40\r\n";

	if (driver.ingest(rmc) != GpsDriverStatus::kOk)
		return 1;
	if (driver.ingest(gsv) != GpsDriverStatus::kOk)
		return 1;

	std::vector<uint8_t> nav_status_payload(16);
	nav_status_payload[4] = 3;
	nav_status_payload[5] = 0x01;
	const auto nav_status = ubx_frame(0x01, 0x03, nav_status_payload);

	if (driver.ingest(nav_status.data(), nav_status.size()) !=
	    GpsDriverStatus::kOk)
		return 1;

	std::vector<uint8_t> nav_pvt_payload(92);
	nav_pvt_payload[20] = 3;
	nav_pvt_payload[21] = 0x01;
	nav_pvt_payload[23] = 12;
	put_i32(&nav_pvt_payload, 24, 115166667);
	put_i32(&nav_pvt_payload, 28, 481173000);
	put_i32(&nav_pvt_payload, 36, 545400);
	put_u16(&nav_pvt_payload, 76, 90);
	const auto nav_pvt = ubx_frame(0x01, 0x07, nav_pvt_payload);

	if (driver.ingest(nav_pvt.data(), nav_pvt.size()) != GpsDriverStatus::kOk)
		return 1;

	const auto fix = driver.lastFix();

	if (!fix.valid || fix.satellites != 12 ||
	    std::fabs(fix.hdop - 0.9) > 0.01)
		return 1;

	return 0;
}
#endif
