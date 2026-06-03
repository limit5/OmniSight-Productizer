/* SPDX-License-Identifier: MIT
 *
 * Case 5 WebRTC ICE/STUN/TURN + SDP wrapper (OP-1992).
 */
#ifndef OMNISIGHT_EMBEDDED_CONFERENCE_WEBRTC_ICE_CLIENT_H_
#define OMNISIGHT_EMBEDDED_CONFERENCE_WEBRTC_ICE_CLIENT_H_

#include "peer-connection.h"

#include <cstdint>
#include <string>
#include <vector>

namespace omnisight::embedded::conference::webrtc {

enum class IceClientStatus {
	kOk = 0,
	kInvalidArgument,
	kParseError,
};

enum class SdpDirection {
	kSendRecv = 0,
	kSendOnly,
	kRecvOnly,
	kInactive,
};

struct IceClientConfig {
	std::vector<std::string> stun_servers;
	std::vector<IceServer> turn_servers;
};

struct SdpMediaSection {
	std::string kind;
	std::string mid;
	std::string protocol = "UDP/TLS/RTP/SAVPF";
	std::vector<uint16_t> payload_types;
	SdpDirection direction = SdpDirection::kRecvOnly;
};

struct LocalSdpOfferConfig {
	std::string origin_username = "-";
	std::string session_id;
	std::string session_version = "0";
	std::string ice_ufrag;
	std::string ice_pwd;
	std::string dtls_fingerprint;
	std::vector<SdpMediaSection> media;
};

struct RemoteSdpMediaSection {
	std::string kind;
	std::string mid;
	std::string ice_ufrag;
	std::string ice_pwd;
	std::string dtls_fingerprint;
	SdpDirection direction = SdpDirection::kSendRecv;
};

struct RemoteSdpAnswer {
	bool ice_lite = false;
	bool trickle_ice = false;
	std::vector<RemoteSdpMediaSection> media;
};

class IceClient {
public:
	IceClient();
	explicit IceClient(IceClientConfig config);

	static IceClientConfig defaultConfig();

	IceClientStatus configure(IceClientConfig config);
	IceClientStatus createLocalOffer(const LocalSdpOfferConfig &config,
					 SessionDescription *description) const;
	IceClientStatus parseRemoteAnswer(const SessionDescription &description,
					  RemoteSdpAnswer *answer) const;

	const std::vector<IceServer> &iceServers() const;
	const std::string &lastError() const;

private:
	IceClientStatus fail(IceClientStatus status, const std::string &error) const;

	std::vector<IceServer> ice_servers_;
	mutable std::string last_error_;
};

} // namespace omnisight::embedded::conference::webrtc

#endif // OMNISIGHT_EMBEDDED_CONFERENCE_WEBRTC_ICE_CLIENT_H_
