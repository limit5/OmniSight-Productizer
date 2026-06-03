/* SPDX-License-Identifier: MIT
 *
 * Case 5 WebRTC outbound media pipeline (OP-1995).
 */
#ifndef OMNISIGHT_EMBEDDED_CONFERENCE_WEBRTC_OUTBOUND_MEDIA_H_
#define OMNISIGHT_EMBEDDED_CONFERENCE_WEBRTC_OUTBOUND_MEDIA_H_

#include "peer-connection.h"

#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <vector>

namespace omnisight::embedded::conference::webrtc {

enum class OutboundMediaStatus {
	kOk = 0,
	kInvalidArgument,
	kUnavailable,
	kInvalidState,
	kEncodeError,
	kPeerConnectionError,
};

enum class OutboundVideoCodec {
	kH264 = 0,
	kVp8,
};

enum class OutboundVideoFormat {
	kH264AnnexB = 0,
	kH264Avcc,
	kVp8Payload,
	kYuyv422,
	kNv12,
};

enum class OutboundAudioCodec {
	kOpus = 0,
};

enum class OutboundAudioFormat {
	kPcmS16Interleaved = 0,
};

enum class OutboundMediaKind {
	kAudio = 0,
	kVideo,
};

struct OutboundVideoConfig {
	OutboundVideoCodec codec = OutboundVideoCodec::kH264;
	uint32_t width = 1920;
	uint32_t height = 1080;
	uint32_t fps = 30;
	uint32_t bitrate_bps = 2'500'000;
	std::string profile = "baseline";
	MediaTrackConfig track{
		"video",
		"conference",
		"camera",
		0x51350001U,
	};
};

struct OutboundAudioConfig {
	OutboundAudioCodec codec = OutboundAudioCodec::kOpus;
	uint32_t sample_rate_hz = 48000;
	uint32_t channels = 1;
	uint32_t frame_duration_ms = 20;
	uint32_t bitrate_bps = 64'000;
	MediaTrackConfig track{
		"audio",
		"conference",
		"microphone",
		0x51350002U,
	};
};

struct OutboundMediaConfig {
	OutboundVideoConfig video;
	OutboundAudioConfig audio;
	bool enable_video = true;
	bool enable_audio = true;
};

struct UvcFrame {
	OutboundVideoFormat format = OutboundVideoFormat::kH264AnnexB;
	const uint8_t *data = nullptr;
	size_t size = 0;
	uint64_t timestamp_us = 0;
	bool keyframe = false;
};

struct UacFrame {
	OutboundAudioFormat format = OutboundAudioFormat::kPcmS16Interleaved;
	const int16_t *samples = nullptr;
	size_t frames = 0;
	uint32_t sample_rate_hz = 48000;
	uint32_t channels = 1;
	uint64_t timestamp_us = 0;
};

struct EncodedMediaPacket {
	OutboundMediaKind kind = OutboundMediaKind::kVideo;
	uint32_t payload_type = 96;
	uint32_t ssrc = 0;
	uint64_t timestamp_us = 0;
	bool keyframe = false;
	std::vector<uint8_t> payload;
};

using EncodedPacketSink = std::function<OutboundMediaStatus(
	const EncodedMediaPacket &packet)>;

class OutboundMediaPipeline {
public:
	OutboundMediaPipeline(PeerConnection &peer_connection,
			      OutboundMediaConfig config = {});
	~OutboundMediaPipeline();

	OutboundMediaPipeline(const OutboundMediaPipeline &) = delete;
	OutboundMediaPipeline &operator=(const OutboundMediaPipeline &) = delete;
	OutboundMediaPipeline(OutboundMediaPipeline &&) noexcept;
	OutboundMediaPipeline &operator=(OutboundMediaPipeline &&) noexcept;

	OutboundMediaStatus start();
	OutboundMediaStatus pushVideoFrame(const UvcFrame &frame);
	OutboundMediaStatus pushAudioFrame(const UacFrame &frame);
	void setPacketSink(EncodedPacketSink sink);
	void close();

	bool running() const;
	bool videoEnabled() const;
	bool audioEnabled() const;
	const std::string &lastError() const;

private:
	class Impl;

	std::unique_ptr<Impl> impl_;
};

const char *toString(OutboundMediaStatus status);
const char *toString(OutboundVideoCodec codec);
const char *toString(OutboundAudioCodec codec);

} // namespace omnisight::embedded::conference::webrtc

#endif // OMNISIGHT_EMBEDDED_CONFERENCE_WEBRTC_OUTBOUND_MEDIA_H_
