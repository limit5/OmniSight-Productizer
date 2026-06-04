/* SPDX-License-Identifier: MIT
 *
 * Cases 6+7 MAVLink transport wrapper (OP-2023).
 */
#include "mavlink-transport.h"

#include <array>
#include <cerrno>
#include <cstring>
#include <utility>

#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <termios.h>
#include <unistd.h>

namespace omnisight::embedded::uav::mavlink {
namespace {

constexpr uint8_t kMavlinkV2Magic = 0xfd;
constexpr size_t kMavlinkV2HeaderBytes = 10;
constexpr size_t kMavlinkChecksumBytes = 2;

static int close_fd(int fd)
{
	if (fd >= 0)
		(void)::close(fd);
	return -1;
}

static bool set_nonblocking(int fd)
{
	const int flags = ::fcntl(fd, F_GETFL, 0);

	if (flags < 0)
		return false;
	return ::fcntl(fd, F_SETFL, flags | O_NONBLOCK) == 0;
}

static void accumulate_crc(uint8_t data, uint16_t *crc)
{
	data ^= static_cast<uint8_t>(*crc & 0xff);
	data ^= static_cast<uint8_t>(data << 4);
	*crc = static_cast<uint16_t>((*crc >> 8) ^
				     (static_cast<uint16_t>(data) << 8) ^
				     (static_cast<uint16_t>(data) << 3) ^
				     (static_cast<uint16_t>(data) >> 4));
}

static uint16_t x25_crc(const uint8_t *data, size_t len, uint8_t crc_extra)
{
	uint16_t crc = 0xffff;

	for (size_t i = 0; i < len; ++i)
		accumulate_crc(data[i], &crc);
	accumulate_crc(crc_extra, &crc);
	return crc;
}

static bool crc_extra_for(uint32_t message_id, uint8_t *extra)
{
	switch (message_id) {
	case static_cast<uint32_t>(MavlinkMessageId::kHeartbeat):
		*extra = 50;
		return true;
	case static_cast<uint32_t>(MavlinkMessageId::kAttitude):
		*extra = 39;
		return true;
	case static_cast<uint32_t>(MavlinkMessageId::kGlobalPositionInt):
		*extra = 104;
		return true;
	case static_cast<uint32_t>(MavlinkMessageId::kCommandLong):
		*extra = 152;
		return true;
	default:
		return false;
	}
}

static bool payload_length_for(uint32_t message_id, size_t *expected)
{
	switch (message_id) {
	case static_cast<uint32_t>(MavlinkMessageId::kHeartbeat):
		*expected = 9;
		return true;
	case static_cast<uint32_t>(MavlinkMessageId::kAttitude):
		*expected = 28;
		return true;
	case static_cast<uint32_t>(MavlinkMessageId::kGlobalPositionInt):
		*expected = 28;
		return true;
	case static_cast<uint32_t>(MavlinkMessageId::kCommandLong):
		*expected = 33;
		return true;
	default:
		return false;
	}
}

static bool payload_length_valid(uint32_t message_id, size_t len)
{
	size_t expected = 0;

	return payload_length_for(message_id, &expected) && len <= expected;
}

static void write_u16(std::vector<uint8_t> *payload, uint16_t value)
{
	payload->push_back(static_cast<uint8_t>(value & 0xff));
	payload->push_back(static_cast<uint8_t>((value >> 8) & 0xff));
}

static void write_i16(std::vector<uint8_t> *payload, int16_t value)
{
	write_u16(payload, static_cast<uint16_t>(value));
}

static void write_u32(std::vector<uint8_t> *payload, uint32_t value)
{
	payload->push_back(static_cast<uint8_t>(value & 0xff));
	payload->push_back(static_cast<uint8_t>((value >> 8) & 0xff));
	payload->push_back(static_cast<uint8_t>((value >> 16) & 0xff));
	payload->push_back(static_cast<uint8_t>((value >> 24) & 0xff));
}

static void write_i32(std::vector<uint8_t> *payload, int32_t value)
{
	write_u32(payload, static_cast<uint32_t>(value));
}

static void write_float(std::vector<uint8_t> *payload, float value)
{
	static_assert(sizeof(float) == sizeof(uint32_t), "float must be 32-bit");
	uint32_t raw = 0;

	std::memcpy(&raw, &value, sizeof(raw));
	write_u32(payload, raw);
}

static uint16_t read_u16(const std::vector<uint8_t> &payload, size_t offset)
{
	return static_cast<uint16_t>(payload[offset]) |
	       (static_cast<uint16_t>(payload[offset + 1]) << 8);
}

static int16_t read_i16(const std::vector<uint8_t> &payload, size_t offset)
{
	return static_cast<int16_t>(read_u16(payload, offset));
}

static uint32_t read_u32(const std::vector<uint8_t> &payload, size_t offset)
{
	return static_cast<uint32_t>(payload[offset]) |
	       (static_cast<uint32_t>(payload[offset + 1]) << 8) |
	       (static_cast<uint32_t>(payload[offset + 2]) << 16) |
	       (static_cast<uint32_t>(payload[offset + 3]) << 24);
}

static int32_t read_i32(const std::vector<uint8_t> &payload, size_t offset)
{
	return static_cast<int32_t>(read_u32(payload, offset));
}

static float read_float(const std::vector<uint8_t> &payload, size_t offset)
{
	const uint32_t raw = read_u32(payload, offset);
	float value = 0.0F;

	std::memcpy(&value, &raw, sizeof(value));
	return value;
}

static speed_t baud_to_speed(uint32_t baud_rate)
{
	switch (baud_rate) {
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
	case 230400:
		return B230400;
	case 460800:
		return B460800;
	case 921600:
		return B921600;
	default:
		return 0;
	}
}

static bool configure_serial(int fd, uint32_t baud_rate, std::string *error)
{
	termios options {};
	const speed_t speed = baud_to_speed(baud_rate);

	if (speed == 0) {
		*error = "unsupported baud rate";
		return false;
	}
	if (::tcgetattr(fd, &options) != 0) {
		*error = std::strerror(errno);
		return false;
	}

	::cfmakeraw(&options);
	(void)::cfsetispeed(&options, speed);
	(void)::cfsetospeed(&options, speed);
	options.c_cflag |= static_cast<tcflag_t>(CLOCAL | CREAD);
	options.c_cflag &= static_cast<tcflag_t>(~CSTOPB);
	options.c_cflag &= static_cast<tcflag_t>(~PARENB);
	options.c_cflag &= static_cast<tcflag_t>(~CSIZE);
	options.c_cflag |= CS8;
	options.c_cc[VMIN] = 0;
	options.c_cc[VTIME] = 0;

	if (::tcsetattr(fd, TCSANOW, &options) != 0) {
		*error = std::strerror(errno);
		return false;
	}
	return true;
}

static bool sockaddr_for(const std::string &address, uint16_t port,
			 sockaddr_in *addr)
{
	std::memset(addr, 0, sizeof(*addr));
	addr->sin_family = AF_INET;
	addr->sin_port = htons(port);
	return ::inet_pton(AF_INET, address.c_str(), &addr->sin_addr) == 1;
}

class MavlinkParser {
public:
	bool push(uint8_t byte, MavlinkMessage *message)
	{
		if (buffer_.empty() && byte != kMavlinkV2Magic)
			return false;

		buffer_.push_back(byte);
		if (buffer_.size() < kMavlinkV2HeaderBytes)
			return false;

		const size_t payload_len = buffer_[1];
		const bool signed_frame = (buffer_[2] & 0x01U) != 0U;
		const size_t frame_len = kMavlinkV2HeaderBytes + payload_len +
					 kMavlinkChecksumBytes +
					 (signed_frame ? 13U : 0U);

		if (buffer_.size() < frame_len)
			return false;

		const bool parsed = parse_frame(frame_len, message);
		buffer_.clear();
		return parsed;
	}

private:
	bool parse_frame(size_t frame_len, MavlinkMessage *message) const
	{
		const size_t payload_len = buffer_[1];
		const uint32_t message_id = static_cast<uint32_t>(buffer_[7]) |
					    (static_cast<uint32_t>(buffer_[8]) << 8) |
					    (static_cast<uint32_t>(buffer_[9]) << 16);
		uint8_t crc_extra = 0;
		size_t expected_payload_len = 0;

		if (frame_len < kMavlinkV2HeaderBytes + payload_len +
					kMavlinkChecksumBytes ||
		    !crc_extra_for(message_id, &crc_extra) ||
		    !payload_length_for(message_id, &expected_payload_len) ||
		    payload_len > expected_payload_len)
			return false;

		const size_t checksum_offset = kMavlinkV2HeaderBytes + payload_len;
		const uint16_t expected = x25_crc(buffer_.data() + 1,
						  kMavlinkV2HeaderBytes - 1 +
							  payload_len,
						  crc_extra);
		const uint16_t actual =
			static_cast<uint16_t>(buffer_[checksum_offset]) |
			(static_cast<uint16_t>(buffer_[checksum_offset + 1]) << 8);

		if (expected != actual)
			return false;

		message->message_id = message_id;
		message->system_id = buffer_[5];
		message->component_id = buffer_[6];
		message->payload.assign(buffer_.begin() + kMavlinkV2HeaderBytes,
					buffer_.begin() + kMavlinkV2HeaderBytes +
						payload_len);
		message->payload.resize(expected_payload_len, 0);
		return true;
	}

	std::vector<uint8_t> buffer_;
};

} // namespace

class MavlinkTransport::Impl {
public:
	explicit Impl(MavlinkTransportConfig config) : config_(std::move(config)) {}
	~Impl() { close(); }

	MavlinkStatus open(const MavlinkTransportConfig &config)
	{
		close();
		config_ = config;

		if (config.system_id == 0 || config.component_id == 0)
			return fail(MavlinkStatus::kInvalidArgument,
				    "system and component id must be non-zero");

		switch (config.backend) {
		case MavlinkBackend::kSerial:
			return open_serial(config.serial);
		case MavlinkBackend::kUdp:
			return open_udp(config.udp);
		}

		return fail(MavlinkStatus::kInvalidArgument, "unknown backend");
	}

	void close()
	{
		fd_ = close_fd(fd_);
		opened_ = false;
	}

	MavlinkStatus send(const MavlinkMessage &message)
	{
		uint8_t crc_extra = 0;

		if (!opened_)
			return fail(MavlinkStatus::kInvalidState, "transport is not open");
		if (!crc_extra_for(message.message_id, &crc_extra) ||
		    !payload_length_valid(message.message_id, message.payload.size()))
			return fail(MavlinkStatus::kUnsupportedMessage,
				    "unsupported MAVLink message");

		std::vector<uint8_t> frame;
		frame.reserve(kMavlinkV2HeaderBytes + message.payload.size() +
			      kMavlinkChecksumBytes);
		frame.push_back(kMavlinkV2Magic);
		frame.push_back(static_cast<uint8_t>(message.payload.size()));
		frame.push_back(0);
		frame.push_back(0);
		frame.push_back(sequence_++);
		frame.push_back(message.system_id);
		frame.push_back(message.component_id);
		frame.push_back(static_cast<uint8_t>(message.message_id & 0xff));
		frame.push_back(static_cast<uint8_t>((message.message_id >> 8) & 0xff));
		frame.push_back(static_cast<uint8_t>((message.message_id >> 16) & 0xff));
		frame.insert(frame.end(), message.payload.begin(), message.payload.end());

		const uint16_t crc =
			x25_crc(frame.data() + 1, frame.size() - 1, crc_extra);
		frame.push_back(static_cast<uint8_t>(crc & 0xff));
		frame.push_back(static_cast<uint8_t>((crc >> 8) & 0xff));

		const ssize_t written = write_frame(frame);
		if (written < 0 || static_cast<size_t>(written) != frame.size())
			return fail(MavlinkStatus::kIoError, std::strerror(errno));
		return MavlinkStatus::kOk;
	}

	MavlinkStatus recv(MavlinkMessage *message)
	{
		if (!message)
			return fail(MavlinkStatus::kInvalidArgument, "message is null");
		if (!opened_)
			return fail(MavlinkStatus::kInvalidState, "transport is not open");

		std::array<uint8_t, 512> bytes {};
		for (;;) {
			const ssize_t n = ::read(fd_, bytes.data(), bytes.size());
			if (n < 0) {
				if (errno == EAGAIN || errno == EWOULDBLOCK)
					return MavlinkStatus::kParseError;
				return fail(MavlinkStatus::kIoError, std::strerror(errno));
			}
			if (n == 0)
				return MavlinkStatus::kParseError;

			for (ssize_t i = 0; i < n; ++i) {
				if (parser_.push(bytes[static_cast<size_t>(i)], message))
					return MavlinkStatus::kOk;
			}
		}
	}

	MavlinkStatus poll()
	{
		MavlinkMessage message;

		for (;;) {
			const MavlinkStatus status = recv(&message);
			if (status == MavlinkStatus::kOk) {
				if (callback_)
					callback_(message);
				continue;
			}
			if (status == MavlinkStatus::kParseError)
				return MavlinkStatus::kOk;
			return status;
		}
	}

	void on_message(MavlinkMessageCallback callback) { callback_ = std::move(callback); }

	bool open() const { return opened_; }
	const std::string &last_error() const { return last_error_; }

private:
	MavlinkStatus open_serial(const MavlinkSerialConfig &serial)
	{
		if (serial.device.empty())
			return fail(MavlinkStatus::kInvalidArgument, "serial device is empty");

		fd_ = ::open(serial.device.c_str(), O_RDWR | O_NOCTTY | O_NONBLOCK);
		if (fd_ < 0)
			return fail(MavlinkStatus::kIoError, std::strerror(errno));
		if (!configure_serial(fd_, serial.baud_rate, &last_error_) ||
		    !set_nonblocking(fd_)) {
			const std::string error = last_error_.empty() ?
				std::strerror(errno) : last_error_;
			close();
			return fail(MavlinkStatus::kIoError, error);
		}

		opened_ = true;
		return MavlinkStatus::kOk;
	}

	MavlinkStatus open_udp(const MavlinkUdpConfig &udp)
	{
		sockaddr_in local {};

		if (udp.local_port == 0 || udp.remote_port == 0)
			return fail(MavlinkStatus::kInvalidArgument,
				    "UDP ports must be non-zero");
		if (!sockaddr_for(udp.local_address, udp.local_port, &local) ||
		    !sockaddr_for(udp.remote_address, udp.remote_port, &remote_addr_))
			return fail(MavlinkStatus::kInvalidArgument,
				    "invalid UDP address");

		fd_ = ::socket(AF_INET, SOCK_DGRAM, 0);
		if (fd_ < 0)
			return fail(MavlinkStatus::kIoError, std::strerror(errno));
		if (::bind(fd_, reinterpret_cast<sockaddr *>(&local), sizeof(local)) != 0 ||
		    !set_nonblocking(fd_)) {
			const std::string error = std::strerror(errno);
			close();
			return fail(MavlinkStatus::kIoError, error);
		}

		opened_ = true;
		return MavlinkStatus::kOk;
	}

	ssize_t write_frame(const std::vector<uint8_t> &frame)
	{
		if (config_.backend == MavlinkBackend::kUdp) {
			return ::sendto(fd_, frame.data(), frame.size(), 0,
					reinterpret_cast<sockaddr *>(&remote_addr_),
					sizeof(remote_addr_));
		}
		return ::write(fd_, frame.data(), frame.size());
	}

	MavlinkStatus fail(MavlinkStatus status, const std::string &error) const
	{
		last_error_ = error;
		return status;
	}

	MavlinkTransportConfig config_;
	int fd_ = -1;
	bool opened_ = false;
	uint8_t sequence_ = 0;
	sockaddr_in remote_addr_ {};
	MavlinkParser parser_;
	MavlinkMessageCallback callback_;
	mutable std::string last_error_;
};

MavlinkTransport::MavlinkTransport()
	: impl_(std::make_unique<Impl>(MavlinkTransportConfig {}))
{
}

MavlinkTransport::MavlinkTransport(MavlinkTransportConfig config)
	: impl_(std::make_unique<Impl>(std::move(config)))
{
}

MavlinkTransport::~MavlinkTransport() = default;
MavlinkTransport::MavlinkTransport(MavlinkTransport &&) noexcept = default;
MavlinkTransport &MavlinkTransport::operator=(MavlinkTransport &&) noexcept = default;

MavlinkStatus MavlinkTransport::open(const MavlinkTransportConfig &config)
{
	return impl_->open(config);
}

void MavlinkTransport::close()
{
	impl_->close();
}

MavlinkStatus MavlinkTransport::send(const MavlinkMessage &message)
{
	return impl_->send(message);
}

MavlinkStatus MavlinkTransport::recv(MavlinkMessage *message)
{
	return impl_->recv(message);
}

MavlinkStatus MavlinkTransport::poll()
{
	return impl_->poll();
}

void MavlinkTransport::onMessage(MavlinkMessageCallback callback)
{
	impl_->on_message(std::move(callback));
}

bool MavlinkTransport::open() const
{
	return impl_->open();
}

const std::string &MavlinkTransport::lastError() const
{
	return impl_->last_error();
}

MavlinkMessage MavlinkTransport::heartbeat(const MavlinkHeartbeat &heartbeat,
					   uint8_t system_id,
					   uint8_t component_id)
{
	MavlinkMessage message;

	message.message_id = static_cast<uint32_t>(MavlinkMessageId::kHeartbeat);
	message.system_id = system_id;
	message.component_id = component_id;
	write_u32(&message.payload, heartbeat.custom_mode);
	message.payload.push_back(heartbeat.type);
	message.payload.push_back(heartbeat.autopilot);
	message.payload.push_back(heartbeat.base_mode);
	message.payload.push_back(heartbeat.system_status);
	message.payload.push_back(heartbeat.mavlink_version);
	return message;
}

MavlinkMessage MavlinkTransport::attitude(const MavlinkAttitude &attitude,
					  uint8_t system_id,
					  uint8_t component_id)
{
	MavlinkMessage message;

	message.message_id = static_cast<uint32_t>(MavlinkMessageId::kAttitude);
	message.system_id = system_id;
	message.component_id = component_id;
	write_u32(&message.payload, attitude.time_boot_ms);
	write_float(&message.payload, attitude.roll);
	write_float(&message.payload, attitude.pitch);
	write_float(&message.payload, attitude.yaw);
	write_float(&message.payload, attitude.rollspeed);
	write_float(&message.payload, attitude.pitchspeed);
	write_float(&message.payload, attitude.yawspeed);
	return message;
}

MavlinkMessage MavlinkTransport::globalPositionInt(
	const MavlinkGlobalPositionInt &position, uint8_t system_id,
	uint8_t component_id)
{
	MavlinkMessage message;

	message.message_id =
		static_cast<uint32_t>(MavlinkMessageId::kGlobalPositionInt);
	message.system_id = system_id;
	message.component_id = component_id;
	write_u32(&message.payload, position.time_boot_ms);
	write_i32(&message.payload, position.lat);
	write_i32(&message.payload, position.lon);
	write_i32(&message.payload, position.alt);
	write_i32(&message.payload, position.relative_alt);
	write_i16(&message.payload, position.vx);
	write_i16(&message.payload, position.vy);
	write_i16(&message.payload, position.vz);
	write_u16(&message.payload, position.hdg);
	return message;
}

MavlinkMessage MavlinkTransport::commandLong(const MavlinkCommandLong &command,
					     uint8_t system_id,
					     uint8_t component_id)
{
	MavlinkMessage message;

	message.message_id = static_cast<uint32_t>(MavlinkMessageId::kCommandLong);
	message.system_id = system_id;
	message.component_id = component_id;
	write_float(&message.payload, command.param1);
	write_float(&message.payload, command.param2);
	write_float(&message.payload, command.param3);
	write_float(&message.payload, command.param4);
	write_float(&message.payload, command.param5);
	write_float(&message.payload, command.param6);
	write_float(&message.payload, command.param7);
	write_u16(&message.payload, command.command);
	message.payload.push_back(command.target_system);
	message.payload.push_back(command.target_component);
	message.payload.push_back(command.confirmation);
	return message;
}

bool MavlinkTransport::parseHeartbeat(const MavlinkMessage &message,
				      MavlinkHeartbeat *heartbeat)
{
	if (!heartbeat || message.message_id !=
				  static_cast<uint32_t>(MavlinkMessageId::kHeartbeat) ||
	    message.payload.size() != 9)
		return false;

	heartbeat->custom_mode = read_u32(message.payload, 0);
	heartbeat->type = message.payload[4];
	heartbeat->autopilot = message.payload[5];
	heartbeat->base_mode = message.payload[6];
	heartbeat->system_status = message.payload[7];
	heartbeat->mavlink_version = message.payload[8];
	return true;
}

bool MavlinkTransport::parseAttitude(const MavlinkMessage &message,
				     MavlinkAttitude *attitude)
{
	if (!attitude || message.message_id !=
				 static_cast<uint32_t>(MavlinkMessageId::kAttitude) ||
	    message.payload.size() != 28)
		return false;

	attitude->time_boot_ms = read_u32(message.payload, 0);
	attitude->roll = read_float(message.payload, 4);
	attitude->pitch = read_float(message.payload, 8);
	attitude->yaw = read_float(message.payload, 12);
	attitude->rollspeed = read_float(message.payload, 16);
	attitude->pitchspeed = read_float(message.payload, 20);
	attitude->yawspeed = read_float(message.payload, 24);
	return true;
}

bool MavlinkTransport::parseGlobalPositionInt(
	const MavlinkMessage &message, MavlinkGlobalPositionInt *position)
{
	if (!position || message.message_id !=
				  static_cast<uint32_t>(MavlinkMessageId::kGlobalPositionInt) ||
	    message.payload.size() != 28)
		return false;

	position->time_boot_ms = read_u32(message.payload, 0);
	position->lat = read_i32(message.payload, 4);
	position->lon = read_i32(message.payload, 8);
	position->alt = read_i32(message.payload, 12);
	position->relative_alt = read_i32(message.payload, 16);
	position->vx = read_i16(message.payload, 20);
	position->vy = read_i16(message.payload, 22);
	position->vz = read_i16(message.payload, 24);
	position->hdg = read_u16(message.payload, 26);
	return true;
}

bool MavlinkTransport::parseCommandLong(const MavlinkMessage &message,
					MavlinkCommandLong *command)
{
	if (!command || message.message_id !=
				 static_cast<uint32_t>(MavlinkMessageId::kCommandLong) ||
	    message.payload.size() != 33)
		return false;

	command->param1 = read_float(message.payload, 0);
	command->param2 = read_float(message.payload, 4);
	command->param3 = read_float(message.payload, 8);
	command->param4 = read_float(message.payload, 12);
	command->param5 = read_float(message.payload, 16);
	command->param6 = read_float(message.payload, 20);
	command->param7 = read_float(message.payload, 24);
	command->command = read_u16(message.payload, 28);
	command->target_system = message.payload[30];
	command->target_component = message.payload[31];
	command->confirmation = message.payload[32];
	return true;
}

} // namespace omnisight::embedded::uav::mavlink
