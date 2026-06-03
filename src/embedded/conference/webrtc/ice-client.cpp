/* SPDX-License-Identifier: MIT
 *
 * Case 5 WebRTC ICE/STUN/TURN + SDP wrapper (OP-1992).
 */
#include "ice-client.h"

#include <sstream>
#include <utility>

namespace omnisight::embedded::conference::webrtc {
namespace {

static bool starts_with(const std::string &value, const char *prefix)
{
	return value.rfind(prefix, 0) == 0;
}

static bool valid_stun_url(const std::string &url)
{
	return starts_with(url, "stun:") || starts_with(url, "stuns:");
}

static bool valid_turn_url(const std::string &url)
{
	return starts_with(url, "turn:") || starts_with(url, "turns:");
}

static const char *direction_name(SdpDirection direction)
{
	switch (direction) {
	case SdpDirection::kSendRecv:
		return "sendrecv";
	case SdpDirection::kSendOnly:
		return "sendonly";
	case SdpDirection::kRecvOnly:
		return "recvonly";
	case SdpDirection::kInactive:
		return "inactive";
	}

	return "sendrecv";
}

static bool parse_direction(const std::string &line, SdpDirection *direction)
{
	if (line == "a=sendrecv") {
		*direction = SdpDirection::kSendRecv;
		return true;
	}
	if (line == "a=sendonly") {
		*direction = SdpDirection::kSendOnly;
		return true;
	}
	if (line == "a=recvonly") {
		*direction = SdpDirection::kRecvOnly;
		return true;
	}
	if (line == "a=inactive") {
		*direction = SdpDirection::kInactive;
		return true;
	}

	return false;
}

static std::vector<std::string> sdp_lines(const std::string &sdp)
{
	std::vector<std::string> lines;
	std::istringstream stream(sdp);
	std::string line;

	while (std::getline(stream, line)) {
		if (!line.empty() && line.back() == '\r')
			line.pop_back();
		if (!line.empty())
			lines.push_back(line);
	}

	return lines;
}

static std::string value_after(const std::string &line, const char *prefix)
{
	const std::string key(prefix);

	if (line.rfind(key, 0) != 0)
		return {};
	return line.substr(key.size());
}

static std::string parse_media_kind(const std::string &line)
{
	const std::string media = value_after(line, "m=");
	const std::string::size_type space = media.find(' ');

	if (space == std::string::npos)
		return {};
	return media.substr(0, space);
}

static bool valid_media(const SdpMediaSection &media)
{
	return !media.kind.empty() && !media.mid.empty() &&
	       !media.protocol.empty() && !media.payload_types.empty();
}

static void append_rtpmap(std::ostringstream &sdp, uint16_t payload_type)
{
	switch (payload_type) {
	case 96:
		sdp << "a=rtpmap:96 H264/90000\r\n";
		break;
	case 111:
		sdp << "a=rtpmap:111 opus/48000/2\r\n";
		break;
	default:
		break;
	}
}

} // namespace

IceClient::IceClient() : IceClient(defaultConfig())
{
}

IceClient::IceClient(IceClientConfig config)
{
	(void)configure(std::move(config));
}

IceClientConfig IceClient::defaultConfig()
{
	return {
		{
			"stun:stun.l.google.com:19302",
			"stun:stun1.l.google.com:19302",
			"stun:stun.cloudflare.com:3478",
		},
		{},
	};
}

IceClientStatus IceClient::configure(IceClientConfig config)
{
	std::vector<IceServer> next;

	if (config.stun_servers.empty() && config.turn_servers.empty())
		config = defaultConfig();

	for (const std::string &server : config.stun_servers) {
		if (!valid_stun_url(server))
			return fail(IceClientStatus::kInvalidArgument,
				    "STUN server URL must use stun: or stuns:");
		next.push_back({server, {}, {}});
	}

	for (const IceServer &server : config.turn_servers) {
		if (!valid_turn_url(server.url))
			return fail(IceClientStatus::kInvalidArgument,
				    "TURN server URL must use turn: or turns:");
		if (server.username.empty() || server.password.empty())
			return fail(IceClientStatus::kInvalidArgument,
				    "TURN username and password are required");
		next.push_back(server);
	}

	ice_servers_ = std::move(next);
	last_error_.clear();
	return IceClientStatus::kOk;
}

IceClientStatus IceClient::createLocalOffer(
	const LocalSdpOfferConfig &config, SessionDescription *description) const
{
	if (!description)
		return fail(IceClientStatus::kInvalidArgument,
			    "local SDP destination is null");
	if (config.session_id.empty() || config.ice_ufrag.empty() ||
	    config.ice_pwd.empty() || config.dtls_fingerprint.empty())
		return fail(IceClientStatus::kInvalidArgument,
			    "local SDP identity, ICE, and DTLS fields are required");
	if (config.media.empty())
		return fail(IceClientStatus::kInvalidArgument,
			    "local SDP requires at least one media section");

	std::ostringstream bundle;

	for (const SdpMediaSection &media : config.media) {
		if (!valid_media(media))
			return fail(IceClientStatus::kInvalidArgument,
				    "local SDP media section is incomplete");
		if (bundle.tellp() > 0)
			bundle << ' ';
		bundle << media.mid;
	}

	std::ostringstream sdp;

	sdp << "v=0\r\n";
	sdp << "o=" << config.origin_username << ' ' << config.session_id << ' '
	    << config.session_version << " IN IP4 0.0.0.0\r\n";
	sdp << "s=OmniSight Conference\r\n";
	sdp << "t=0 0\r\n";
	sdp << "a=group:BUNDLE " << bundle.str() << "\r\n";
	sdp << "a=ice-options:trickle\r\n";

	for (const SdpMediaSection &media : config.media) {
		sdp << "m=" << media.kind << " 9 " << media.protocol;
		for (uint16_t payload_type : media.payload_types)
			sdp << ' ' << payload_type;
		sdp << "\r\n";
		sdp << "c=IN IP4 0.0.0.0\r\n";
		sdp << "a=mid:" << media.mid << "\r\n";
		sdp << "a=" << direction_name(media.direction) << "\r\n";
		sdp << "a=ice-ufrag:" << config.ice_ufrag << "\r\n";
		sdp << "a=ice-pwd:" << config.ice_pwd << "\r\n";
		sdp << "a=fingerprint:" << config.dtls_fingerprint << "\r\n";
		sdp << "a=setup:actpass\r\n";
		sdp << "a=rtcp-mux\r\n";
		for (uint16_t payload_type : media.payload_types)
			append_rtpmap(sdp, payload_type);
	}

	*description = {SdpType::kOffer, sdp.str()};
	last_error_.clear();
	return IceClientStatus::kOk;
}

IceClientStatus IceClient::parseRemoteAnswer(const SessionDescription &description,
					     RemoteSdpAnswer *answer) const
{
	if (!answer)
		return fail(IceClientStatus::kInvalidArgument,
			    "remote SDP answer destination is null");
	if (description.type != SdpType::kAnswer)
		return fail(IceClientStatus::kInvalidArgument,
			    "remote SDP must be an answer");
	if (description.sdp.empty())
		return fail(IceClientStatus::kInvalidArgument,
			    "remote SDP is empty");

	RemoteSdpAnswer parsed;
	RemoteSdpMediaSection *current = nullptr;
	std::string session_ufrag;
	std::string session_pwd;
	std::string session_fingerprint;
	SdpDirection session_direction = SdpDirection::kSendRecv;

	for (const std::string &line : sdp_lines(description.sdp)) {
		if (starts_with(line, "m=")) {
			parsed.media.push_back({});
			current = &parsed.media.back();
			current->kind = parse_media_kind(line);
			current->direction = session_direction;
			current->ice_ufrag = session_ufrag;
			current->ice_pwd = session_pwd;
			current->dtls_fingerprint = session_fingerprint;
			continue;
		}

		if (line == "a=ice-lite") {
			parsed.ice_lite = true;
			continue;
		}
		if (line == "a=ice-options:trickle") {
			parsed.trickle_ice = true;
			continue;
		}

		SdpDirection direction = SdpDirection::kSendRecv;
		if (parse_direction(line, &direction)) {
			if (current)
				current->direction = direction;
			else
				session_direction = direction;
			continue;
		}

		const std::string mid = value_after(line, "a=mid:");
		if (!mid.empty() && current) {
			current->mid = mid;
			continue;
		}

		const std::string ufrag = value_after(line, "a=ice-ufrag:");
		if (!ufrag.empty()) {
			if (current)
				current->ice_ufrag = ufrag;
			else
				session_ufrag = ufrag;
			continue;
		}

		const std::string pwd = value_after(line, "a=ice-pwd:");
		if (!pwd.empty()) {
			if (current)
				current->ice_pwd = pwd;
			else
				session_pwd = pwd;
			continue;
		}

		const std::string fingerprint = value_after(line, "a=fingerprint:");
		if (!fingerprint.empty()) {
			if (current)
				current->dtls_fingerprint = fingerprint;
			else
				session_fingerprint = fingerprint;
			continue;
		}
	}

	if (parsed.media.empty())
		return fail(IceClientStatus::kParseError,
			    "remote SDP answer has no media sections");

	for (const RemoteSdpMediaSection &media : parsed.media) {
		if (media.kind.empty() || media.mid.empty() ||
		    media.ice_ufrag.empty() || media.ice_pwd.empty() ||
		    media.dtls_fingerprint.empty())
			return fail(IceClientStatus::kParseError,
				    "remote SDP answer media section is incomplete");
	}

	*answer = std::move(parsed);
	last_error_.clear();
	return IceClientStatus::kOk;
}

const std::vector<IceServer> &IceClient::iceServers() const
{
	return ice_servers_;
}

const std::string &IceClient::lastError() const
{
	return last_error_;
}

IceClientStatus IceClient::fail(IceClientStatus status,
				const std::string &error) const
{
	last_error_ = error;
	return status;
}

} // namespace omnisight::embedded::conference::webrtc
