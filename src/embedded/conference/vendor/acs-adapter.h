/* SPDX-License-Identifier: MIT
 *
 * Case 5 Microsoft ACS Calling SDK adapter (OP-2007).
 */
#ifndef OMNISIGHT_EMBEDDED_CONFERENCE_VENDOR_ACS_ADAPTER_H_
#define OMNISIGHT_EMBEDDED_CONFERENCE_VENDOR_ACS_ADAPTER_H_

#include "conference-provider.h"

#include <memory>
#include <string>

namespace omnisight::embedded::conference::vendor {

struct AcsAdapterConfig {
	std::string user_access_token;
	std::string display_name = "OmniSight embedded conference device";
	bool enable_audio = true;
	bool enable_video = true;
};

class AcsAdapter : public ConferenceProvider {
public:
	explicit AcsAdapter(AcsAdapterConfig config);
	~AcsAdapter() override;

	AcsAdapter(const AcsAdapter &) = delete;
	AcsAdapter &operator=(const AcsAdapter &) = delete;
	AcsAdapter(AcsAdapter &&) noexcept;
	AcsAdapter &operator=(AcsAdapter &&) noexcept;

	SessionHandle startSession(const std::string &target_uri) override;
	ConferenceProviderStatus stopSession(const SessionHandle &handle) override;
	void onEvent(ConferenceProviderEventCallback callback) override;

	bool available() const;
	const std::string &lastError() const;

private:
	class Impl;

	std::unique_ptr<Impl> impl_;
};

const char *toString(ConferenceProviderStatus status);
const char *toString(ConferenceProviderEventType type);

} // namespace omnisight::embedded::conference::vendor

#endif // OMNISIGHT_EMBEDDED_CONFERENCE_VENDOR_ACS_ADAPTER_H_
