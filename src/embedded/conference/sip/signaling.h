/* SPDX-License-Identifier: MIT
 *
 * Case 5 SIP signaling wrapper (OP-2001).
 */
#ifndef OMNISIGHT_EMBEDDED_CONFERENCE_SIP_SIGNALING_H_
#define OMNISIGHT_EMBEDDED_CONFERENCE_SIP_SIGNALING_H_

#include <cstdint>
#include <functional>
#include <memory>
#include <string>

namespace omnisight::embedded::conference::sip {

enum class SipSignalingStatus {
	kOk = 0,
	kInvalidArgument,
	kUnavailable,
	kInvalidState,
	kBackendError,
};

enum class SipSignalingState {
	kIdle = 0,
	kStarted,
	kRegistering,
	kRegistered,
	kInviting,
	kInCall,
	kTerminating,
	kTerminated,
};

struct SipEndpointConfig {
	std::string local_uri;
	std::string registrar_uri;
	std::string username;
	std::string password;
	std::string realm = "*";
	std::string transport = "udp";
	uint16_t local_port = 5060;
};

struct SipInviteConfig {
	std::string remote_uri;
	std::string local_sdp;
};

struct SipIncomingInvite {
	std::string remote_uri;
	std::string local_uri;
	std::string sdp;
};

using IncomingInviteHandler = std::function<void(const SipIncomingInvite &)>;

class SipSignaling {
public:
	SipSignaling();
	~SipSignaling();

	SipSignaling(const SipSignaling &) = delete;
	SipSignaling &operator=(const SipSignaling &) = delete;
	SipSignaling(SipSignaling &&) noexcept;
	SipSignaling &operator=(SipSignaling &&) noexcept;

	SipSignalingStatus start(const SipEndpointConfig &config);
	SipSignalingStatus registerEndpoint();
	SipSignalingStatus invite(const SipInviteConfig &invite);
	SipSignalingStatus bye();
	void onIncoming(IncomingInviteHandler handler);
	void close();

	bool available() const;
	SipSignalingState state() const;
	const std::string &lastError() const;

private:
	class Impl;

	std::unique_ptr<Impl> impl_;
};

} // namespace omnisight::embedded::conference::sip

#endif // OMNISIGHT_EMBEDDED_CONFERENCE_SIP_SIGNALING_H_
