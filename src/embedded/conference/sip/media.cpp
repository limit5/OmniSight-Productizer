/* SPDX-License-Identifier: MIT
 *
 * Case 5 SIP RTP/RTCP media plane (OP-2004).
 */
#include "media.h"

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <limits>
#include <random>
#include <sstream>
#include <utility>

#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

namespace omnisight::embedded::conference::sip {
namespace {

constexpr size_t kMaxUdpPacketBytes = 1500;
constexpr size_t kRtpHeaderBytes = 12;
constexpr uint8_t kRtpVersion = 2;
constexpr uint16_t kDefaultJitterPackets = 8;

static bool starts_with(const std::string &value, const char *prefix)
{
	return value.rfind(prefix, 0) == 0;
}

static std::string value_after(const std::string &line, const char *prefix)
{
	const std::string key(prefix);

	if (!starts_with(line, prefix))
		return {};
	return line.substr(key.size());
}

static std::vector<std::string> split_lines(const std::string &value)
{
	std::vector<std::string> lines;
	std::istringstream stream(value);
	std::string line;

	while (std::getline(stream, line)) {
		if (!line.empty() && line.back() == '\r')
			line.pop_back();
		if (!line.empty())
			lines.push_back(line);
	}

	return lines;
}

static std::vector<std::string> split_words(const std::string &value)
{
	std::vector<std::string> words;
	std::istringstream stream(value);
	std::string word;

	while (stream >> word)
		words.push_back(word);
	return words;
}

static bool parse_u16(const std::string &value, uint16_t *out)
{
	char *end = nullptr;
	errno = 0;
	const unsigned long parsed = std::strtoul(value.c_str(), &end, 10);

	if (errno != 0 || !end || *end != '\0' ||
	    parsed > std::numeric_limits<uint16_t>::max())
		return false;
	*out = static_cast<uint16_t>(parsed);
	return true;
}

static bool parse_u32_token(const std::string &value, uint32_t *out)
{
	char *end = nullptr;
	errno = 0;
	const unsigned long parsed = std::strtoul(value.c_str(), &end, 10);

	if (errno != 0 || !end || (*end != '\0' && *end != '/') ||
	    parsed > std::numeric_limits<uint32_t>::max())
		return false;
	*out = static_cast<uint32_t>(parsed);
	return true;
}

static bool parse_u8(const std::string &value, uint8_t *out)
{
	uint16_t parsed = 0;

	if (!parse_u16(value, &parsed) || parsed > 127)
		return false;
	*out = static_cast<uint8_t>(parsed);
	return true;
}

static SipMediaCodec codec_from_name(const std::string &name)
{
	if (name == "PCMU")
		return SipMediaCodec::kPcmu;
	if (name == "PCMA")
		return SipMediaCodec::kPcma;
	if (name == "opus" || name == "OPUS")
		return SipMediaCodec::kOpus;
	if (name == "H264" || name == "h264")
		return SipMediaCodec::kH264;
	return SipMediaCodec::kUnknown;
}

static uint32_t static_clock_rate(uint8_t payload_type)
{
	switch (payload_type) {
	case 0:
	case 8:
		return 8000;
	default:
		return 0;
	}
}

static SipMediaCodec static_codec(uint8_t payload_type)
{
	switch (payload_type) {
	case 0:
		return SipMediaCodec::kPcmu;
	case 8:
		return SipMediaCodec::kPcma;
	default:
		return SipMediaCodec::kUnknown;
	}
}

static bool valid_config(const SipMediaConfig &config)
{
	return !config.local.address.empty() && !config.remote.address.empty() &&
	       config.remote.rtp_port != 0 &&
	       config.format.codec != SipMediaCodec::kUnknown &&
	       config.format.clock_rate_hz != 0;
}

static uint16_t default_rtcp_port(uint16_t rtp_port)
{
	return rtp_port == std::numeric_limits<uint16_t>::max() ? rtp_port :
								 rtp_port + 1;
}

static uint16_t read_be16(const uint8_t *data)
{
	return static_cast<uint16_t>((static_cast<uint16_t>(data[0]) << 8) |
				     data[1]);
}

static uint32_t read_be32(const uint8_t *data)
{
	return (static_cast<uint32_t>(data[0]) << 24) |
	       (static_cast<uint32_t>(data[1]) << 16) |
	       (static_cast<uint32_t>(data[2]) << 8) | data[3];
}

static void write_be16(uint8_t *data, uint16_t value)
{
	data[0] = static_cast<uint8_t>(value >> 8);
	data[1] = static_cast<uint8_t>(value & 0xff);
}

static void write_be32(uint8_t *data, uint32_t value)
{
	data[0] = static_cast<uint8_t>((value >> 24) & 0xff);
	data[1] = static_cast<uint8_t>((value >> 16) & 0xff);
	data[2] = static_cast<uint8_t>((value >> 8) & 0xff);
	data[3] = static_cast<uint8_t>(value & 0xff);
}

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

static bool sockaddr_for(const SipMediaEndpoint &endpoint, uint16_t port,
			 sockaddr_in *addr)
{
	std::memset(addr, 0, sizeof(*addr));
	addr->sin_family = AF_INET;
	addr->sin_port = htons(port);
	return ::inet_pton(AF_INET, endpoint.address.c_str(),
			   &addr->sin_addr) == 1;
}

static bool bind_udp(const SipMediaEndpoint &endpoint, uint16_t port, int *fd,
		     uint16_t *bound_port, std::string *error)
{
	int next = ::socket(AF_INET, SOCK_DGRAM, 0);
	sockaddr_in addr;

	if (next < 0) {
		*error = std::strerror(errno);
		return false;
	}
	if (!set_nonblocking(next)) {
		*error = std::strerror(errno);
		(void)close_fd(next);
		return false;
	}
	if (!sockaddr_for(endpoint, port, &addr)) {
		*error = "local media address is not an IPv4 address";
		(void)close_fd(next);
		return false;
	}
	if (::bind(next, reinterpret_cast<const sockaddr *>(&addr),
		   sizeof(addr)) != 0) {
		*error = std::strerror(errno);
		(void)close_fd(next);
		return false;
	}

	socklen_t len = sizeof(addr);
	if (::getsockname(next, reinterpret_cast<sockaddr *>(&addr), &len) != 0) {
		*error = std::strerror(errno);
		(void)close_fd(next);
		return false;
	}

	*fd = next;
	*bound_port = ntohs(addr.sin_port);
	return true;
}

struct BufferedFrame {
	uint16_t sequence = 0;
	SipMediaFrame frame;
};

static bool sequence_less(uint16_t left, uint16_t right)
{
	return static_cast<int16_t>(left - right) < 0;
}

} // namespace

class SipMediaPlane::Impl {
public:
	Impl() = default;

	explicit Impl(SipMediaConfig config)
	{
		(void)configure(std::move(config));
	}

	~Impl()
	{
		stop();
	}

	SipMediaStatus configure(SipMediaConfig config)
	{
		if (running_)
			return fail(SipMediaStatus::kInvalidState,
				    "media plane is running");
		if (config.remote.rtcp_port == 0)
			config.remote.rtcp_port = default_rtcp_port(
				config.remote.rtp_port);
		if (config.jitter_buffer_depth_packets == 0)
			config.jitter_buffer_depth_packets = kDefaultJitterPackets;
		if (config.ssrc == 0)
			config.ssrc = randomSsrc();
		if (!valid_config(config))
			return fail(SipMediaStatus::kInvalidArgument,
				    "SIP media configuration is incomplete");

		config_ = std::move(config);
		last_error_.clear();
		return SipMediaStatus::kOk;
	}

	SipMediaStatus start()
	{
		if (running_)
			return SipMediaStatus::kOk;
		if (!valid_config(config_))
			return fail(SipMediaStatus::kInvalidState,
				    "SIP media plane is not configured");

		if (!sockaddr_for(config_.remote, config_.remote.rtp_port,
				  &remote_rtp_))
			return fail(SipMediaStatus::kInvalidArgument,
				    "remote RTP address is not an IPv4 address");
		if (!sockaddr_for(config_.remote, config_.remote.rtcp_port,
				  &remote_rtcp_))
			return fail(SipMediaStatus::kInvalidArgument,
				    "remote RTCP address is not an IPv4 address");

		if (!bind_udp(config_.local, config_.local.rtp_port, &rtp_fd_,
			      &local_rtp_port_, &last_error_))
			return SipMediaStatus::kIoError;

		const uint16_t rtcp_port = config_.local.rtcp_port != 0 ?
			config_.local.rtcp_port :
			(config_.local.rtp_port != 0 ?
				 default_rtcp_port(local_rtp_port_) :
				 0);
		if (!bind_udp(config_.local, rtcp_port, &rtcp_fd_,
			      &local_rtcp_port_, &last_error_)) {
			rtp_fd_ = close_fd(rtp_fd_);
			return SipMediaStatus::kIoError;
		}

		running_ = true;
		last_error_.clear();
		return SipMediaStatus::kOk;
	}

	void stop()
	{
		running_ = false;
		rtp_fd_ = close_fd(rtp_fd_);
		rtcp_fd_ = close_fd(rtcp_fd_);
		jitter_buffer_.clear();
		local_rtp_port_ = 0;
		local_rtcp_port_ = 0;
		sequence_ = 0;
	}

	SipMediaStatus onCallAnswered(const SipMediaConfig &negotiated)
	{
		stop();
		const SipMediaStatus status = configure(negotiated);

		if (status != SipMediaStatus::kOk)
			return status;
		return start();
	}

	void onBye()
	{
		stop();
	}

	SipMediaStatus sendFrame(const SipMediaFrame &frame)
	{
		if (!running_)
			return fail(SipMediaStatus::kInvalidState,
				    "SIP media plane is not running");
		if (frame.payload.empty())
			return fail(SipMediaStatus::kInvalidArgument,
				    "RTP payload is empty");
		if (frame.payload.size() + kRtpHeaderBytes > kMaxUdpPacketBytes)
			return fail(SipMediaStatus::kInvalidArgument,
				    "RTP payload exceeds single-packet MTU");

		std::array<uint8_t, kMaxUdpPacketBytes> packet = {};
		const size_t size = frame.payload.size() + kRtpHeaderBytes;

		packet[0] = static_cast<uint8_t>(kRtpVersion << 6);
		packet[1] = config_.format.payload_type & 0x7f;
		if (frame.marker)
			packet[1] |= 0x80;
		write_be16(&packet[2], sequence_++);
		write_be32(&packet[4], frame.rtp_timestamp);
		write_be32(&packet[8], config_.ssrc);
		std::copy(frame.payload.begin(), frame.payload.end(),
			  packet.begin() + kRtpHeaderBytes);

		if (::sendto(rtp_fd_, packet.data(), size, 0,
			     reinterpret_cast<const sockaddr *>(&remote_rtp_),
			     sizeof(remote_rtp_)) < 0)
			return fail(SipMediaStatus::kIoError, std::strerror(errno));

		last_error_.clear();
		return SipMediaStatus::kOk;
	}

	SipMediaStatus sendRtcp(const SipRtcpPacket &packet)
	{
		if (!running_)
			return fail(SipMediaStatus::kInvalidState,
				    "SIP media plane is not running");
		if (packet.payload.empty() ||
		    packet.payload.size() > kMaxUdpPacketBytes)
			return fail(SipMediaStatus::kInvalidArgument,
				    "RTCP packet size is invalid");

		if (::sendto(rtcp_fd_, packet.payload.data(), packet.payload.size(),
			     0,
			     reinterpret_cast<const sockaddr *>(&remote_rtcp_),
			     sizeof(remote_rtcp_)) < 0)
			return fail(SipMediaStatus::kIoError, std::strerror(errno));

		last_error_.clear();
		return SipMediaStatus::kOk;
	}

	SipMediaStatus poll()
	{
		if (!running_)
			return fail(SipMediaStatus::kInvalidState,
				    "SIP media plane is not running");

		SipMediaStatus status = drainRtp();
		if (status != SipMediaStatus::kOk)
			return status;
		status = drainRtcp();
		if (status != SipMediaStatus::kOk)
			return status;

		emitReadyFrames();
		last_error_.clear();
		return SipMediaStatus::kOk;
	}

	void setReceiveCallback(SipMediaFrameCallback callback)
	{
		receive_callback_ = std::move(callback);
	}

	void setRtcpCallback(SipRtcpPacketCallback callback)
	{
		rtcp_callback_ = std::move(callback);
	}

	bool running() const
	{
		return running_;
	}

	uint16_t localRtpPort() const
	{
		return local_rtp_port_;
	}

	uint16_t localRtcpPort() const
	{
		return local_rtcp_port_;
	}

	const std::string &lastError() const
	{
		return last_error_;
	}

private:
	static uint32_t randomSsrc()
	{
		std::random_device device;

		return device();
	}

	SipMediaStatus drainRtp()
	{
		for (;;) {
			std::array<uint8_t, kMaxUdpPacketBytes> packet = {};
			const ssize_t size = ::recv(rtp_fd_, packet.data(),
						    packet.size(), 0);

			if (size < 0) {
				if (errno == EAGAIN || errno == EWOULDBLOCK)
					return SipMediaStatus::kOk;
				return fail(SipMediaStatus::kIoError,
					    std::strerror(errno));
			}
			if (static_cast<size_t>(size) < kRtpHeaderBytes)
				continue;
			bufferRtp(packet.data(), static_cast<size_t>(size));
		}
	}

	SipMediaStatus drainRtcp()
	{
		for (;;) {
			std::array<uint8_t, kMaxUdpPacketBytes> packet = {};
			const ssize_t size = ::recv(rtcp_fd_, packet.data(),
						    packet.size(), 0);

			if (size < 0) {
				if (errno == EAGAIN || errno == EWOULDBLOCK)
					return SipMediaStatus::kOk;
				return fail(SipMediaStatus::kIoError,
					    std::strerror(errno));
			}
			if (rtcp_callback_) {
				SipRtcpPacket rtcp;

				rtcp.payload.assign(
					packet.begin(),
					packet.begin() + static_cast<size_t>(size));
				rtcp_callback_(rtcp);
			}
		}
	}

	void bufferRtp(const uint8_t *packet, size_t size)
	{
		if ((packet[0] >> 6) != kRtpVersion)
			return;
		if ((packet[1] & 0x7f) != config_.format.payload_type)
			return;

		const uint8_t csrc_count = packet[0] & 0x0f;
		const size_t header_size = kRtpHeaderBytes +
			static_cast<size_t>(csrc_count) * 4;
		if (size < header_size)
			return;

		BufferedFrame buffered;
		buffered.sequence = read_be16(&packet[2]);
		buffered.frame.marker = (packet[1] & 0x80) != 0;
		buffered.frame.rtp_timestamp = read_be32(&packet[4]);
		buffered.frame.payload.assign(packet + header_size, packet + size);

		const auto it = std::lower_bound(
			jitter_buffer_.begin(), jitter_buffer_.end(),
			buffered.sequence,
			[](const BufferedFrame &frame, uint16_t sequence) {
				return sequence_less(frame.sequence, sequence);
			});
		if (it != jitter_buffer_.end() && it->sequence == buffered.sequence)
			return;
		jitter_buffer_.insert(it, std::move(buffered));
	}

	void emitReadyFrames()
	{
		while (!jitter_buffer_.empty() &&
		       jitter_buffer_.size() >= config_.jitter_buffer_depth_packets) {
			BufferedFrame frame = std::move(jitter_buffer_.front());

			jitter_buffer_.pop_front();
			if (receive_callback_)
				receive_callback_(frame.frame);
		}
	}

	SipMediaStatus fail(SipMediaStatus status, const std::string &error)
	{
		last_error_ = error;
		return status;
	}

	SipMediaConfig config_;
	std::string last_error_;
	SipMediaFrameCallback receive_callback_;
	SipRtcpPacketCallback rtcp_callback_;
	std::deque<BufferedFrame> jitter_buffer_;
	sockaddr_in remote_rtp_ = {};
	sockaddr_in remote_rtcp_ = {};
	int rtp_fd_ = -1;
	int rtcp_fd_ = -1;
	uint16_t local_rtp_port_ = 0;
	uint16_t local_rtcp_port_ = 0;
	uint16_t sequence_ = 0;
	bool running_ = false;
};

SipMediaPlane::SipMediaPlane() : impl_(std::make_unique<Impl>())
{
}

SipMediaPlane::SipMediaPlane(SipMediaConfig config)
	: impl_(std::make_unique<Impl>(std::move(config)))
{
}

SipMediaPlane::~SipMediaPlane() = default;

SipMediaPlane::SipMediaPlane(SipMediaPlane &&) noexcept = default;

SipMediaPlane &SipMediaPlane::operator=(SipMediaPlane &&) noexcept = default;

SipMediaStatus SipMediaPlane::configure(SipMediaConfig config)
{
	return impl_->configure(std::move(config));
}

SipMediaStatus SipMediaPlane::start()
{
	return impl_->start();
}

void SipMediaPlane::stop()
{
	impl_->stop();
}

SipMediaStatus SipMediaPlane::onCallAnswered(const SipMediaConfig &negotiated)
{
	return impl_->onCallAnswered(negotiated);
}

void SipMediaPlane::onBye()
{
	impl_->onBye();
}

SipMediaStatus SipMediaPlane::sendFrame(const SipMediaFrame &frame)
{
	return impl_->sendFrame(frame);
}

SipMediaStatus SipMediaPlane::sendRtcp(const SipRtcpPacket &packet)
{
	return impl_->sendRtcp(packet);
}

SipMediaStatus SipMediaPlane::poll()
{
	return impl_->poll();
}

void SipMediaPlane::setReceiveCallback(SipMediaFrameCallback callback)
{
	impl_->setReceiveCallback(std::move(callback));
}

void SipMediaPlane::setRtcpCallback(SipRtcpPacketCallback callback)
{
	impl_->setRtcpCallback(std::move(callback));
}

bool SipMediaPlane::running() const
{
	return impl_->running();
}

uint16_t SipMediaPlane::localRtpPort() const
{
	return impl_->localRtpPort();
}

uint16_t SipMediaPlane::localRtcpPort() const
{
	return impl_->localRtcpPort();
}

const std::string &SipMediaPlane::lastError() const
{
	return impl_->lastError();
}

SipMediaStatus SipMediaPlane::parseSdpMedia(const std::string &sdp,
					    const std::string &kind,
					    SipMediaEndpoint *endpoint,
					    SipMediaFormat *format,
					    std::string *error)
{
	if (!endpoint || !format) {
		if (error)
			*error = "SDP destination is null";
		return SipMediaStatus::kInvalidArgument;
	}
	if (sdp.empty() || kind.empty()) {
		if (error)
			*error = "SDP and media kind are required";
		return SipMediaStatus::kInvalidArgument;
	}

	SipMediaEndpoint parsed_endpoint;
	SipMediaFormat parsed_format;
	bool in_media = false;
	bool found_media = false;
	std::vector<uint8_t> payload_types;

	for (const std::string &line : split_lines(sdp)) {
		if (starts_with(line, "c=IN IP4 ")) {
			const std::string address = value_after(line, "c=IN IP4 ");
			if (!address.empty() && (!found_media || in_media))
				parsed_endpoint.address = address;
			continue;
		}

		if (starts_with(line, "m=")) {
			const std::vector<std::string> words =
				split_words(value_after(line, "m="));
			in_media = false;
			if (words.size() < 4 || words[0] != kind)
				continue;
			if (!parse_u16(words[1], &parsed_endpoint.rtp_port)) {
				if (error)
					*error = "SDP media port is invalid";
				return SipMediaStatus::kParseError;
			}
			for (size_t i = 3; i < words.size(); ++i) {
				uint8_t payload_type = 0;

				if (parse_u8(words[i], &payload_type))
					payload_types.push_back(payload_type);
			}
			in_media = true;
			found_media = true;
			continue;
		}

		if (!in_media)
			continue;

		const std::string rtcp = value_after(line, "a=rtcp:");
		if (!rtcp.empty()) {
			const std::vector<std::string> words = split_words(rtcp);

			if (!words.empty())
				(void)parse_u16(words[0],
						&parsed_endpoint.rtcp_port);
			continue;
		}

		const std::string rtpmap = value_after(line, "a=rtpmap:");
		if (!rtpmap.empty()) {
			const std::string::size_type space = rtpmap.find(' ');
			if (space == std::string::npos)
				continue;

			uint8_t payload_type = 0;
			if (!parse_u8(rtpmap.substr(0, space), &payload_type))
				continue;

			const std::vector<std::string> parts =
				split_words(rtpmap.substr(space + 1));
			if (parts.empty())
				continue;

			const std::string::size_type slash = parts[0].find('/');
			const std::string codec = slash == std::string::npos ?
				parts[0] :
				parts[0].substr(0, slash);
			const std::string rate = slash == std::string::npos ?
				std::string() :
				parts[0].substr(slash + 1);
			uint32_t clock_rate = 0;

			parsed_format.payload_type = payload_type;
			parsed_format.codec = codec_from_name(codec);
			if (parse_u32_token(rate, &clock_rate))
				parsed_format.clock_rate_hz = clock_rate;
		}
	}

	if (!found_media || parsed_endpoint.rtp_port == 0 ||
	    payload_types.empty()) {
		if (error)
			*error = "SDP media section is missing";
		return SipMediaStatus::kParseError;
	}
	if (parsed_endpoint.rtcp_port == 0)
		parsed_endpoint.rtcp_port =
			default_rtcp_port(parsed_endpoint.rtp_port);
	if (parsed_format.codec == SipMediaCodec::kUnknown) {
		parsed_format.payload_type = payload_types.front();
		parsed_format.codec = static_codec(parsed_format.payload_type);
		parsed_format.clock_rate_hz =
			static_clock_rate(parsed_format.payload_type);
	}
	if (parsed_format.codec == SipMediaCodec::kUnknown ||
	    parsed_format.clock_rate_hz == 0) {
		if (error)
			*error = "SDP media codec is unsupported";
		return SipMediaStatus::kParseError;
	}

	*endpoint = std::move(parsed_endpoint);
	*format = parsed_format;
	if (error)
		error->clear();
	return SipMediaStatus::kOk;
}

} // namespace omnisight::embedded::conference::sip
