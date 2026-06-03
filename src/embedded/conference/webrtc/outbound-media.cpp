/* SPDX-License-Identifier: MIT
 *
 * Case 5 WebRTC outbound media pipeline (OP-1995).
 */
#include "outbound-media.h"

#include <algorithm>
#include <cstring>
#include <exception>
#include <utility>

#if defined(OMNISIGHT_WEBRTC_WITH_OPUS)
#include <opus/opus.h>
#endif

namespace omnisight::embedded::conference::webrtc {
namespace {

constexpr uint32_t kPayloadTypeH264 = 96;
constexpr uint32_t kPayloadTypeVp8 = 97;
constexpr uint32_t kPayloadTypeOpus = 111;
constexpr size_t kMaxOpusPacketBytes = 1275;

bool valid_track(const MediaTrackConfig &track)
{
	return !track.mid.empty() && !track.stream_id.empty() &&
	       !track.track_id.empty() && track.ssrc != 0;
}

bool valid_video_config(const OutboundVideoConfig &config)
{
	if (!valid_track(config.track))
		return false;
	if (config.width == 0 || config.height == 0 || config.fps == 0)
		return false;
	if (config.codec == OutboundVideoCodec::kH264)
		return config.profile.empty() || config.profile == "baseline";
	return true;
}

bool valid_audio_config(const OutboundAudioConfig &config)
{
	if (!valid_track(config.track))
		return false;
	if (config.codec != OutboundAudioCodec::kOpus)
		return false;
	if (config.sample_rate_hz != 48000)
		return false;
	if (config.channels != 1 && config.channels != 2)
		return false;
	return config.frame_duration_ms == 10 ||
	       config.frame_duration_ms == 20 ||
	       config.frame_duration_ms == 40 ||
	       config.frame_duration_ms == 60;
}

bool annexb_start_code(const uint8_t *data, size_t size)
{
	if (size >= 4 && data[0] == 0 && data[1] == 0 && data[2] == 0 &&
	    data[3] == 1)
		return true;
	return size >= 3 && data[0] == 0 && data[1] == 0 && data[2] == 1;
}

std::vector<uint8_t> avcc_to_annexb(const uint8_t *data, size_t size)
{
	std::vector<uint8_t> out;
	size_t offset = 0;

	while (offset + 4 <= size) {
		const uint32_t nalu_size =
			(static_cast<uint32_t>(data[offset]) << 24) |
			(static_cast<uint32_t>(data[offset + 1]) << 16) |
			(static_cast<uint32_t>(data[offset + 2]) << 8) |
			static_cast<uint32_t>(data[offset + 3]);

		offset += 4;
		if (nalu_size == 0 || offset + nalu_size > size)
			return {};

		out.push_back(0);
		out.push_back(0);
		out.push_back(0);
		out.push_back(1);
		out.insert(out.end(), data + offset, data + offset + nalu_size);
		offset += nalu_size;
	}

	if (offset != size)
		return {};
	return out;
}

uint32_t payload_type_for(OutboundVideoCodec codec)
{
	return codec == OutboundVideoCodec::kVp8 ? kPayloadTypeVp8 :
						       kPayloadTypeH264;
}

} // namespace

class OutboundMediaPipeline::Impl {
public:
	Impl(PeerConnection &peer_connection, OutboundMediaConfig config)
		: peer_connection_(peer_connection), config_(std::move(config))
	{
	}

	~Impl()
	{
		close();
	}

	OutboundMediaStatus start()
	{
		if (running_)
			return OutboundMediaStatus::kOk;
		if (!config_.enable_video && !config_.enable_audio) {
			last_error_ = "outbound media requires audio or video";
			return OutboundMediaStatus::kInvalidArgument;
		}
		if (config_.enable_video && !valid_video_config(config_.video)) {
			last_error_ = "video track or encoder configuration is invalid";
			return OutboundMediaStatus::kInvalidArgument;
		}
		if (config_.enable_audio && !valid_audio_config(config_.audio)) {
			last_error_ = "audio track or Opus configuration is invalid";
			return OutboundMediaStatus::kInvalidArgument;
		}

		PeerConnectionStatus pc_status = peer_connection_.start();
		if (pc_status != PeerConnectionStatus::kOk)
			return peer_error(pc_status);

		if (config_.enable_audio) {
			pc_status = peer_connection_.addAudioTrack(config_.audio.track);
			if (pc_status != PeerConnectionStatus::kOk)
				return peer_error(pc_status);
			OutboundMediaStatus audio_status = startAudioEncoder();
			if (audio_status != OutboundMediaStatus::kOk)
				return audio_status;
		}
		if (config_.enable_video) {
			pc_status = peer_connection_.addVideoTrack(config_.video.track);
			if (pc_status != PeerConnectionStatus::kOk)
				return peer_error(pc_status);
		}

		running_ = true;
		return OutboundMediaStatus::kOk;
	}

	OutboundMediaStatus pushVideoFrame(const UvcFrame &frame)
	{
		if (!running_)
			return invalid_state("outbound media pipeline is not running");
		if (!config_.enable_video)
			return invalid_state("video is disabled");
		if (frame.data == nullptr || frame.size == 0)
			return invalid_argument("UVC frame is empty");

		EncodedMediaPacket packet;
		packet.kind = OutboundMediaKind::kVideo;
		packet.payload_type = payload_type_for(config_.video.codec);
		packet.ssrc = config_.video.track.ssrc;
		packet.timestamp_us = frame.timestamp_us;
		packet.keyframe = frame.keyframe;

		switch (config_.video.codec) {
		case OutboundVideoCodec::kH264:
			if (frame.format == OutboundVideoFormat::kH264AnnexB) {
				if (!annexb_start_code(frame.data, frame.size))
					return encode_error("H264 Annex-B frame lacks a start code");
				packet.payload.assign(frame.data, frame.data + frame.size);
			} else if (frame.format == OutboundVideoFormat::kH264Avcc) {
				packet.payload = avcc_to_annexb(frame.data, frame.size);
				if (packet.payload.empty())
					return encode_error("invalid H264 AVCC access unit");
			} else {
				return unavailable("raw H264 software encode is not enabled");
			}
			break;
		case OutboundVideoCodec::kVp8:
			if (frame.format != OutboundVideoFormat::kVp8Payload)
				return unavailable("raw VP8 software encode is not enabled");
			packet.payload.assign(frame.data, frame.data + frame.size);
			break;
		}

		return emit(packet);
	}

	OutboundMediaStatus pushAudioFrame(const UacFrame &frame)
	{
		if (!running_)
			return invalid_state("outbound media pipeline is not running");
		if (!config_.enable_audio)
			return invalid_state("audio is disabled");
		if (frame.samples == nullptr || frame.frames == 0)
			return invalid_argument("UAC frame is empty");
		if (frame.format != OutboundAudioFormat::kPcmS16Interleaved)
			return invalid_argument("unsupported UAC sample format");
		if (frame.sample_rate_hz != config_.audio.sample_rate_hz ||
		    frame.channels != config_.audio.channels)
			return invalid_argument("UAC frame does not match Opus encoder config");

		EncodedMediaPacket packet;
		packet.kind = OutboundMediaKind::kAudio;
		packet.payload_type = kPayloadTypeOpus;
		packet.ssrc = config_.audio.track.ssrc;
		packet.timestamp_us = frame.timestamp_us;

		OutboundMediaStatus status = encodeAudio(frame, packet.payload);
		if (status != OutboundMediaStatus::kOk)
			return status;

		return emit(packet);
	}

	void setPacketSink(EncodedPacketSink sink)
	{
		sink_ = std::move(sink);
	}

	void close()
	{
#if defined(OMNISIGHT_WEBRTC_WITH_OPUS)
		if (opus_encoder_)
			opus_encoder_destroy(opus_encoder_);
		opus_encoder_ = nullptr;
#endif
		running_ = false;
	}

	bool running() const
	{
		return running_;
	}

	bool videoEnabled() const
	{
		return config_.enable_video;
	}

	bool audioEnabled() const
	{
		return config_.enable_audio;
	}

	const std::string &lastError() const
	{
		return last_error_;
	}

private:
	OutboundMediaStatus peer_error(PeerConnectionStatus status)
	{
		last_error_ = peer_connection_.lastError();
		if (last_error_.empty())
			last_error_ = "PeerConnection rejected outbound media track";
		return status == PeerConnectionStatus::kUnavailable ?
			       OutboundMediaStatus::kUnavailable :
			       OutboundMediaStatus::kPeerConnectionError;
	}

	OutboundMediaStatus startAudioEncoder()
	{
#if !defined(OMNISIGHT_WEBRTC_WITH_OPUS)
		last_error_ = "Opus encoder support is not enabled";
		return OutboundMediaStatus::kUnavailable;
#else
		int error = OPUS_OK;

		opus_encoder_ = opus_encoder_create(
			static_cast<opus_int32>(config_.audio.sample_rate_hz),
			static_cast<int>(config_.audio.channels),
			OPUS_APPLICATION_VOIP, &error);
		if (error != OPUS_OK || opus_encoder_ == nullptr) {
			last_error_ = "failed to create Opus encoder";
			return OutboundMediaStatus::kEncodeError;
		}
		opus_encoder_ctl(opus_encoder_, OPUS_SET_BITRATE(
						 static_cast<opus_int32>(
							 config_.audio.bitrate_bps)));
		return OutboundMediaStatus::kOk;
#endif
	}

	OutboundMediaStatus encodeAudio(const UacFrame &frame,
					std::vector<uint8_t> &payload)
	{
#if !defined(OMNISIGHT_WEBRTC_WITH_OPUS)
		(void)frame;
		(void)payload;
		last_error_ = "Opus encoder support is not enabled";
		return OutboundMediaStatus::kUnavailable;
#else
		payload.resize(kMaxOpusPacketBytes);
		const int encoded = opus_encode(
			opus_encoder_, frame.samples, static_cast<int>(frame.frames),
			payload.data(), static_cast<opus_int32>(payload.size()));
		if (encoded < 0) {
			last_error_ = opus_strerror(encoded);
			return OutboundMediaStatus::kEncodeError;
		}
		payload.resize(static_cast<size_t>(encoded));
		return OutboundMediaStatus::kOk;
#endif
	}

	OutboundMediaStatus emit(const EncodedMediaPacket &packet)
	{
		if (packet.payload.empty())
			return encode_error("encoded media packet is empty");
		if (!sink_)
			return OutboundMediaStatus::kOk;
		OutboundMediaStatus status = sink_(packet);
		if (status != OutboundMediaStatus::kOk)
			last_error_ = "encoded packet sink rejected outbound media";
		return status;
	}

	OutboundMediaStatus invalid_argument(const char *error)
	{
		last_error_ = error;
		return OutboundMediaStatus::kInvalidArgument;
	}

	OutboundMediaStatus invalid_state(const char *error)
	{
		last_error_ = error;
		return OutboundMediaStatus::kInvalidState;
	}

	OutboundMediaStatus unavailable(const char *error)
	{
		last_error_ = error;
		return OutboundMediaStatus::kUnavailable;
	}

	OutboundMediaStatus encode_error(const char *error)
	{
		last_error_ = error;
		return OutboundMediaStatus::kEncodeError;
	}

	PeerConnection &peer_connection_;
	OutboundMediaConfig config_;
	EncodedPacketSink sink_;
	std::string last_error_;
	bool running_ = false;
#if defined(OMNISIGHT_WEBRTC_WITH_OPUS)
	OpusEncoder *opus_encoder_ = nullptr;
#endif
};

OutboundMediaPipeline::OutboundMediaPipeline(PeerConnection &peer_connection,
					     OutboundMediaConfig config)
	: impl_(std::make_unique<Impl>(peer_connection, std::move(config)))
{
}

OutboundMediaPipeline::~OutboundMediaPipeline() = default;

OutboundMediaPipeline::OutboundMediaPipeline(OutboundMediaPipeline &&) noexcept =
	default;

OutboundMediaPipeline &OutboundMediaPipeline::operator=(
	OutboundMediaPipeline &&) noexcept = default;

OutboundMediaStatus OutboundMediaPipeline::start()
{
	return impl_->start();
}

OutboundMediaStatus OutboundMediaPipeline::pushVideoFrame(const UvcFrame &frame)
{
	return impl_->pushVideoFrame(frame);
}

OutboundMediaStatus OutboundMediaPipeline::pushAudioFrame(const UacFrame &frame)
{
	return impl_->pushAudioFrame(frame);
}

void OutboundMediaPipeline::setPacketSink(EncodedPacketSink sink)
{
	impl_->setPacketSink(std::move(sink));
}

void OutboundMediaPipeline::close()
{
	impl_->close();
}

bool OutboundMediaPipeline::running() const
{
	return impl_->running();
}

bool OutboundMediaPipeline::videoEnabled() const
{
	return impl_->videoEnabled();
}

bool OutboundMediaPipeline::audioEnabled() const
{
	return impl_->audioEnabled();
}

const std::string &OutboundMediaPipeline::lastError() const
{
	return impl_->lastError();
}

const char *toString(OutboundMediaStatus status)
{
	switch (status) {
	case OutboundMediaStatus::kOk:
		return "ok";
	case OutboundMediaStatus::kInvalidArgument:
		return "invalid-argument";
	case OutboundMediaStatus::kUnavailable:
		return "unavailable";
	case OutboundMediaStatus::kInvalidState:
		return "invalid-state";
	case OutboundMediaStatus::kEncodeError:
		return "encode-error";
	case OutboundMediaStatus::kPeerConnectionError:
		return "peer-connection-error";
	}

	return "unknown";
}

const char *toString(OutboundVideoCodec codec)
{
	switch (codec) {
	case OutboundVideoCodec::kH264:
		return "h264";
	case OutboundVideoCodec::kVp8:
		return "vp8";
	}

	return "unknown";
}

const char *toString(OutboundAudioCodec codec)
{
	switch (codec) {
	case OutboundAudioCodec::kOpus:
		return "opus";
	}

	return "unknown";
}

} // namespace omnisight::embedded::conference::webrtc

#if defined(OMNISIGHT_OUTBOUND_MEDIA_SMOKE_MAIN)
#include <iostream>

int main()
{
	using namespace omnisight::embedded::conference::webrtc;

	PeerConnectionConfig peer_config;
	peer_config.enable_audio = false;
	peer_config.enable_video = true;
	PeerConnection peer(peer_config);
	OutboundMediaConfig config;
	config.enable_audio = false;
	config.enable_video = true;

	OutboundMediaPipeline pipeline(peer, config);
	size_t packets = 0;

	pipeline.setPacketSink([&packets](const EncodedMediaPacket &packet) {
		if (packet.kind != OutboundMediaKind::kVideo ||
		    packet.payload_type != 96 || packet.payload.empty())
			return OutboundMediaStatus::kEncodeError;
		++packets;
		return OutboundMediaStatus::kOk;
	});

	OutboundMediaStatus status = pipeline.start();
	if (status == OutboundMediaStatus::kUnavailable) {
		std::cout << "outbound-media smoke skipped: "
			  << pipeline.lastError() << '\n';
		return 0;
	}
	if (status != OutboundMediaStatus::kOk) {
		std::cerr << "outbound-media start failed: "
			  << pipeline.lastError() << '\n';
		return 1;
	}

	const uint8_t h264_idr[] = { 0, 0, 0, 1, 0x65, 0x88, 0x84 };
	status = pipeline.pushVideoFrame({
		OutboundVideoFormat::kH264AnnexB,
		h264_idr,
		sizeof(h264_idr),
		1234,
		true,
	});
	if (status != OutboundMediaStatus::kOk || packets != 1) {
		std::cerr << "outbound-media video push failed: "
			  << pipeline.lastError() << '\n';
		return 1;
	}

	return 0;
}
#endif
