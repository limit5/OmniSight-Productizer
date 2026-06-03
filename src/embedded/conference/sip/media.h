/* SPDX-License-Identifier: MIT
 *
 * Case 5 SIP RTP/RTCP media plane (OP-2004).
 */
#ifndef OMNISIGHT_EMBEDDED_CONFERENCE_SIP_MEDIA_H_
#define OMNISIGHT_EMBEDDED_CONFERENCE_SIP_MEDIA_H_

#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <vector>

namespace omnisight::embedded::conference::sip {

enum class SipMediaStatus {
	kOk = 0,
	kInvalidArgument,
	kInvalidState,
	kParseError,
	kIoError,
};

enum class SipMediaCodec {
	kUnknown = 0,
	kPcmu,
	kPcma,
	kOpus,
	kH264,
};

struct SipMediaFormat {
	SipMediaCodec codec = SipMediaCodec::kUnknown;
	uint8_t payload_type = 0;
	uint32_t clock_rate_hz = 0;
	uint8_t channels = 1;
};

struct SipMediaEndpoint {
	std::string address = "127.0.0.1";
	uint16_t rtp_port = 0;
	uint16_t rtcp_port = 0;
};

struct SipMediaConfig {
	SipMediaEndpoint local;
	SipMediaEndpoint remote;
	SipMediaFormat format;
	uint32_t ssrc = 0;
	uint16_t jitter_buffer_depth_packets = 8;
};

struct SipMediaFrame {
	std::vector<uint8_t> payload;
	uint32_t rtp_timestamp = 0;
	bool marker = false;
};

struct SipRtcpPacket {
	std::vector<uint8_t> payload;
};

using SipMediaFrameCallback = std::function<void(const SipMediaFrame &)>;
using SipRtcpPacketCallback = std::function<void(const SipRtcpPacket &)>;

class SipMediaPlane {
public:
	SipMediaPlane();
	explicit SipMediaPlane(SipMediaConfig config);
	~SipMediaPlane();

	SipMediaPlane(const SipMediaPlane &) = delete;
	SipMediaPlane &operator=(const SipMediaPlane &) = delete;
	SipMediaPlane(SipMediaPlane &&) noexcept;
	SipMediaPlane &operator=(SipMediaPlane &&) noexcept;

	SipMediaStatus configure(SipMediaConfig config);
	SipMediaStatus start();
	void stop();

	SipMediaStatus onCallAnswered(const SipMediaConfig &negotiated);
	void onBye();

	SipMediaStatus sendFrame(const SipMediaFrame &frame);
	SipMediaStatus sendRtcp(const SipRtcpPacket &packet);
	SipMediaStatus poll();

	void setReceiveCallback(SipMediaFrameCallback callback);
	void setRtcpCallback(SipRtcpPacketCallback callback);

	bool running() const;
	uint16_t localRtpPort() const;
	uint16_t localRtcpPort() const;
	const std::string &lastError() const;

	static SipMediaStatus parseSdpMedia(const std::string &sdp,
					    const std::string &kind,
					    SipMediaEndpoint *endpoint,
					    SipMediaFormat *format,
					    std::string *error = nullptr);

private:
	class Impl;

	std::unique_ptr<Impl> impl_;
};

} // namespace omnisight::embedded::conference::sip

#endif // OMNISIGHT_EMBEDDED_CONFERENCE_SIP_MEDIA_H_
