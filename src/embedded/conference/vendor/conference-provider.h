/* SPDX-License-Identifier: MIT
 *
 * Case 5 conference vendor provider shell (OP-2002).
 *
 * This interface intentionally stays thin. Zoom, ACS, Webex, and later
 * adapters have different protocol shapes; this shell only gives application
 * code a common spawn, teardown, and event subscription surface. Vendor
 * adapters keep protocol-specific state behind vendor_specific_ptr and expose
 * richer controls through their own SDK-facing layers.
 */
#ifndef OMNISIGHT_EMBEDDED_CONFERENCE_VENDOR_CONFERENCE_PROVIDER_H_
#define OMNISIGHT_EMBEDDED_CONFERENCE_VENDOR_CONFERENCE_PROVIDER_H_

#include <cstdint>
#include <functional>
#include <string>

namespace omnisight::embedded::conference::vendor {

enum class ConferenceProviderStatus {
	kOk = 0,
	kInvalidArgument,
	kUnavailable,
	kInvalidState,
	kBackendError,
};

enum class ConferenceProviderEventType {
	kSessionStarted = 0,
	kSessionStopped,
	kParticipantChanged,
	kMediaChanged,
	kError,
};

struct SessionHandle {
	uint64_t id = 0;
	void *vendor_specific_ptr = nullptr;

	explicit operator bool() const
	{
		return id != 0 || vendor_specific_ptr != nullptr;
	}
};

struct ConferenceProviderEvent {
	ConferenceProviderEventType type = ConferenceProviderEventType::kError;
	SessionHandle session;
	ConferenceProviderStatus status = ConferenceProviderStatus::kOk;
	std::string detail;
	void *vendor_specific_ptr = nullptr;
};

using ConferenceProviderEventCallback =
	std::function<void(const ConferenceProviderEvent &event)>;

class ConferenceProvider {
public:
	virtual ~ConferenceProvider() = default;

	virtual SessionHandle startSession(const std::string &target_uri) = 0;
	virtual ConferenceProviderStatus stopSession(
		const SessionHandle &handle) = 0;
	virtual void onEvent(ConferenceProviderEventCallback callback) = 0;
};

} // namespace omnisight::embedded::conference::vendor

#endif // OMNISIGHT_EMBEDDED_CONFERENCE_VENDOR_CONFERENCE_PROVIDER_H_
