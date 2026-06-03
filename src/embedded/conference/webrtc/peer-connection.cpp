/* SPDX-License-Identifier: MIT
 *
 * Case 5 WebRTC PeerConnection wrapper (OP-1986).
 */
#include "peer-connection.h"

#include <exception>
#include <sstream>
#include <utility>

#if defined(OMNISIGHT_WEBRTC_WITH_LIBDATACHANNEL)
#include <rtc/rtc.hpp>
#endif

namespace omnisight::embedded::conference::webrtc {
namespace {

#if defined(OMNISIGHT_WEBRTC_WITH_LIBDATACHANNEL)
static bool valid_track(const MediaTrackConfig &track)
{
	return !track.mid.empty() && !track.stream_id.empty() &&
	       !track.track_id.empty() && track.ssrc != 0;
}

static const char *sdp_type_name(SdpType type)
{
	switch (type) {
	case SdpType::kOffer:
		return "offer";
	case SdpType::kAnswer:
		return "answer";
	}

	return "offer";
}

static rtc::Description::Type to_rtc_type(SdpType type)
{
	switch (type) {
	case SdpType::kOffer:
		return rtc::Description::Type::Offer;
	case SdpType::kAnswer:
		return rtc::Description::Type::Answer;
	}

	return rtc::Description::Type::Offer;
}

static SdpType from_rtc_type(rtc::Description::Type type)
{
	return type == rtc::Description::Type::Answer ? SdpType::kAnswer :
							SdpType::kOffer;
}
#endif

} // namespace

class PeerConnection::Impl {
public:
	Impl(PeerConnectionConfig config, SignalingAdapter *signaling)
		: config_(std::move(config)), signaling_(signaling)
	{
	}

	PeerConnectionStatus start()
	{
#if !defined(OMNISIGHT_WEBRTC_WITH_LIBDATACHANNEL)
		last_error_ = "libdatachannel support is not enabled";
		return PeerConnectionStatus::kUnavailable;
#else
		if (pc_)
			return PeerConnectionStatus::kOk;

		try {
			rtc::Configuration rtc_config;

			for (const IceServer &server : config_.ice_servers) {
				if (server.url.empty()) {
					last_error_ = "ICE server URL is empty";
					return PeerConnectionStatus::kInvalidArgument;
				}
				if (!server.username.empty() || !server.password.empty()) {
					last_error_ =
						"credentialed ICE servers are configured by C5.D-WebRTC.2";
					return PeerConnectionStatus::kInvalidArgument;
				}
				rtc_config.iceServers.emplace_back(server.url);
			}

			pc_ = std::make_shared<rtc::PeerConnection>(rtc_config);
			pc_->onLocalDescription(
				[this](rtc::Description description) {
					if (!signaling_)
						return;
					signaling_->onLocalDescription({
						from_rtc_type(description.type()),
						std::string(description),
					});
				});
			pc_->onLocalCandidate([this](rtc::Candidate candidate) {
				if (!signaling_)
					return;
				signaling_->onIceCandidate({
					std::string(candidate),
					candidate.mid(),
				});
			});
			pc_->onStateChange([this](rtc::PeerConnection::State state) {
				if (!signaling_)
					return;
				std::ostringstream stream;

				stream << state;
				signaling_->onConnectionState(stream.str());
			});
		} catch (const std::exception &error) {
			last_error_ = error.what();
			return PeerConnectionStatus::kBackendError;
		}

		return PeerConnectionStatus::kOk;
#endif
	}

	PeerConnectionStatus addAudioTrack(const MediaTrackConfig &track)
	{
#if !defined(OMNISIGHT_WEBRTC_WITH_LIBDATACHANNEL)
		(void)track;
		last_error_ = "libdatachannel support is not enabled";
		return PeerConnectionStatus::kUnavailable;
#else
		if (!config_.enable_audio)
			return PeerConnectionStatus::kInvalidState;
		return addTrack(track, true);
#endif
	}

	PeerConnectionStatus addVideoTrack(const MediaTrackConfig &track)
	{
#if !defined(OMNISIGHT_WEBRTC_WITH_LIBDATACHANNEL)
		(void)track;
		last_error_ = "libdatachannel support is not enabled";
		return PeerConnectionStatus::kUnavailable;
#else
		if (!config_.enable_video)
			return PeerConnectionStatus::kInvalidState;
		return addTrack(track, false);
#endif
	}

	PeerConnectionStatus createOffer()
	{
#if !defined(OMNISIGHT_WEBRTC_WITH_LIBDATACHANNEL)
		last_error_ = "libdatachannel support is not enabled";
		return PeerConnectionStatus::kUnavailable;
#else
		if (!pc_)
			return PeerConnectionStatus::kInvalidState;

		try {
			const rtc::Description offer = pc_->createOffer();

			if (signaling_) {
				signaling_->onLocalDescription({
					SdpType::kOffer,
					std::string(offer),
				});
			}
		} catch (const std::exception &error) {
			last_error_ = error.what();
			return PeerConnectionStatus::kBackendError;
		}

		return PeerConnectionStatus::kOk;
#endif
	}

	PeerConnectionStatus setLocalDescription(SdpType type)
	{
#if !defined(OMNISIGHT_WEBRTC_WITH_LIBDATACHANNEL)
		(void)type;
		last_error_ = "libdatachannel support is not enabled";
		return PeerConnectionStatus::kUnavailable;
#else
		if (!pc_)
			return PeerConnectionStatus::kInvalidState;

		try {
			pc_->setLocalDescription(to_rtc_type(type));
		} catch (const std::exception &error) {
			last_error_ = error.what();
			return PeerConnectionStatus::kBackendError;
		}

		return PeerConnectionStatus::kOk;
#endif
	}

	PeerConnectionStatus setRemoteDescription(
		const SessionDescription &description)
	{
		if (description.sdp.empty()) {
			last_error_ = "remote SDP is empty";
			return PeerConnectionStatus::kInvalidArgument;
		}

#if !defined(OMNISIGHT_WEBRTC_WITH_LIBDATACHANNEL)
		(void)description;
		last_error_ = "libdatachannel support is not enabled";
		return PeerConnectionStatus::kUnavailable;
#else
		if (!pc_)
			return PeerConnectionStatus::kInvalidState;

		try {
			pc_->setRemoteDescription(rtc::Description(
				description.sdp, sdp_type_name(description.type)));
		} catch (const std::exception &error) {
			last_error_ = error.what();
			return PeerConnectionStatus::kBackendError;
		}

		return PeerConnectionStatus::kOk;
#endif
	}

	PeerConnectionStatus addRemoteCandidate(const IceCandidate &candidate)
	{
		if (candidate.candidate.empty() || candidate.mid.empty()) {
			last_error_ = "remote ICE candidate or mid is empty";
			return PeerConnectionStatus::kInvalidArgument;
		}

#if !defined(OMNISIGHT_WEBRTC_WITH_LIBDATACHANNEL)
		(void)candidate;
		last_error_ = "libdatachannel support is not enabled";
		return PeerConnectionStatus::kUnavailable;
#else
		if (!pc_)
			return PeerConnectionStatus::kInvalidState;

		try {
			pc_->addRemoteCandidate(
				rtc::Candidate(candidate.candidate, candidate.mid));
		} catch (const std::exception &error) {
			last_error_ = error.what();
			return PeerConnectionStatus::kBackendError;
		}

		return PeerConnectionStatus::kOk;
#endif
	}

	void close()
	{
#if defined(OMNISIGHT_WEBRTC_WITH_LIBDATACHANNEL)
		if (pc_)
			pc_->close();
		pc_.reset();
		audio_track_.reset();
		video_track_.reset();
#endif
	}

	bool available() const
	{
#if defined(OMNISIGHT_WEBRTC_WITH_LIBDATACHANNEL)
		return true;
#else
		return false;
#endif
	}

	const std::string &lastError() const
	{
		return last_error_;
	}

private:
#if defined(OMNISIGHT_WEBRTC_WITH_LIBDATACHANNEL)
	PeerConnectionStatus addTrack(const MediaTrackConfig &track, bool audio)
	{
		if (!pc_)
			return PeerConnectionStatus::kInvalidState;
		if (!valid_track(track)) {
			last_error_ = "media track configuration is incomplete";
			return PeerConnectionStatus::kInvalidArgument;
		}

		try {
			rtc::Description::Media media =
				audio ? rtc::Description::Media(
						rtc::Description::Audio(
							track.mid,
							rtc::Description::Direction::SendOnly)) :
					rtc::Description::Media(
						rtc::Description::Video(
							track.mid,
							rtc::Description::Direction::SendOnly));

			if (audio) {
				media.addOpusCodec(111);
			} else {
				media.addH264Codec(96);
			}
			media.addSSRC(track.ssrc, track.track_id, track.stream_id,
				      track.track_id);

			std::shared_ptr<rtc::Track> rtc_track = pc_->addTrack(media);
			if (audio)
				audio_track_ = std::move(rtc_track);
			else
				video_track_ = std::move(rtc_track);
		} catch (const std::exception &error) {
			last_error_ = error.what();
			return PeerConnectionStatus::kBackendError;
		}

		return PeerConnectionStatus::kOk;
	}
#endif

	PeerConnectionConfig config_;
	SignalingAdapter *signaling_ = nullptr;
	std::string last_error_;
#if defined(OMNISIGHT_WEBRTC_WITH_LIBDATACHANNEL)
	std::shared_ptr<rtc::PeerConnection> pc_;
	std::shared_ptr<rtc::Track> audio_track_;
	std::shared_ptr<rtc::Track> video_track_;
#endif
};

PeerConnection::PeerConnection(PeerConnectionConfig config,
			       SignalingAdapter *signaling)
	: impl_(std::make_unique<Impl>(std::move(config), signaling))
{
}

PeerConnection::~PeerConnection() = default;

PeerConnection::PeerConnection(PeerConnection &&) noexcept = default;

PeerConnection &PeerConnection::operator=(PeerConnection &&) noexcept = default;

PeerConnectionStatus PeerConnection::start()
{
	return impl_->start();
}

PeerConnectionStatus PeerConnection::addAudioTrack(const MediaTrackConfig &track)
{
	return impl_->addAudioTrack(track);
}

PeerConnectionStatus PeerConnection::addVideoTrack(const MediaTrackConfig &track)
{
	return impl_->addVideoTrack(track);
}

PeerConnectionStatus PeerConnection::createOffer()
{
	return impl_->createOffer();
}

PeerConnectionStatus PeerConnection::setLocalDescription(SdpType type)
{
	return impl_->setLocalDescription(type);
}

PeerConnectionStatus PeerConnection::setRemoteDescription(
	const SessionDescription &description)
{
	return impl_->setRemoteDescription(description);
}

PeerConnectionStatus PeerConnection::addRemoteCandidate(
	const IceCandidate &candidate)
{
	return impl_->addRemoteCandidate(candidate);
}

void PeerConnection::close()
{
	impl_->close();
}

bool PeerConnection::available() const
{
	return impl_->available();
}

const std::string &PeerConnection::lastError() const
{
	return impl_->lastError();
}

} // namespace omnisight::embedded::conference::webrtc
