/* SPDX-License-Identifier: MIT
 *
 * Case 5 Cisco Webex Calling SDK adapter (OP-2008).
 */
#ifndef OMNISIGHT_EMBEDDED_CONFERENCE_VENDOR_WEBEX_ADAPTER_H_
#define OMNISIGHT_EMBEDDED_CONFERENCE_VENDOR_WEBEX_ADAPTER_H_

#include "conference-provider.h"

#include <memory>
#include <string>

namespace omnisight::embedded::conference::vendor {

struct WebexSdkConfig {
	std::string sdk_root;
	std::string access_token;
	std::string display_name;
	std::string glibc_abi = "glibc-2.31";
};

struct WebexMediaConfig {
	bool audio = true;
	bool video = true;
};

class WebexAdapter final : public ConferenceProvider {
public:
	explicit WebexAdapter(WebexSdkConfig config);
	~WebexAdapter() override;

	WebexAdapter(const WebexAdapter &) = delete;
	WebexAdapter &operator=(const WebexAdapter &) = delete;
	WebexAdapter(WebexAdapter &&) noexcept;
	WebexAdapter &operator=(WebexAdapter &&) noexcept;

	SessionHandle startSession(const std::string &target_uri) override;
	ConferenceProviderStatus stopSession(
		const SessionHandle &handle) override;
	void onEvent(ConferenceProviderEventCallback callback) override;

	ConferenceProviderStatus startMedia(const SessionHandle &handle,
					    const WebexMediaConfig &media);
	ConferenceProviderStatus stopMedia(const SessionHandle &handle,
					   const WebexMediaConfig &media);

	bool available() const;
	const std::string &lastError() const;

private:
	class Impl;

	std::unique_ptr<Impl> impl_;
};

} // namespace omnisight::embedded::conference::vendor

#endif // OMNISIGHT_EMBEDDED_CONFERENCE_VENDOR_WEBEX_ADAPTER_H_
