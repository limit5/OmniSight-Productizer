/* SPDX-License-Identifier: MIT
 *
 * Case 5 WebRTC PeerConnection wrapper (OP-1986).
 */
#ifndef OMNISIGHT_EMBEDDED_CONFERENCE_WEBRTC_PEER_CONNECTION_H_
#define OMNISIGHT_EMBEDDED_CONFERENCE_WEBRTC_PEER_CONNECTION_H_

#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace omnisight::embedded::conference::webrtc {

enum class PeerConnectionStatus {
	kOk = 0,
	kInvalidArgument,
	kUnavailable,
	kInvalidState,
	kBackendError,
};

enum class SdpType {
	kOffer = 0,
	kAnswer,
};

struct IceServer {
	std::string url;
	std::string username;
	std::string password;
};

struct PeerConnectionConfig {
	std::vector<IceServer> ice_servers;
	bool enable_audio = true;
	bool enable_video = true;
};

struct SessionDescription {
	SdpType type = SdpType::kOffer;
	std::string sdp;
};

struct IceCandidate {
	std::string candidate;
	std::string mid;
};

struct MediaTrackConfig {
	std::string mid;
	std::string stream_id;
	std::string track_id;
	uint32_t ssrc = 0;
};

class SignalingAdapter {
public:
	virtual ~SignalingAdapter() = default;

	virtual void onLocalDescription(const SessionDescription &description) = 0;
	virtual void onIceCandidate(const IceCandidate &candidate) = 0;
	virtual void onConnectionState(const std::string &state) = 0;
};

class PeerConnection {
public:
	explicit PeerConnection(PeerConnectionConfig config,
				SignalingAdapter *signaling = nullptr);
	~PeerConnection();

	PeerConnection(const PeerConnection &) = delete;
	PeerConnection &operator=(const PeerConnection &) = delete;
	PeerConnection(PeerConnection &&) noexcept;
	PeerConnection &operator=(PeerConnection &&) noexcept;

	PeerConnectionStatus start();
	PeerConnectionStatus addAudioTrack(const MediaTrackConfig &track);
	PeerConnectionStatus addVideoTrack(const MediaTrackConfig &track);
	PeerConnectionStatus createOffer();
	PeerConnectionStatus setLocalDescription(SdpType type);
	PeerConnectionStatus setRemoteDescription(
		const SessionDescription &description);
	PeerConnectionStatus addRemoteCandidate(const IceCandidate &candidate);
	void close();

	bool available() const;
	const std::string &lastError() const;

private:
	class Impl;

	std::unique_ptr<Impl> impl_;
};

} // namespace omnisight::embedded::conference::webrtc

#endif // OMNISIGHT_EMBEDDED_CONFERENCE_WEBRTC_PEER_CONNECTION_H_
