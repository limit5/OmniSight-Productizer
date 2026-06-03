/* SPDX-License-Identifier: MIT
 *
 * Case 5 Zoom Linux Meeting SDK adapter (OP-2005).
 */
#ifndef OMNISIGHT_EMBEDDED_CONFERENCE_VENDOR_ZOOM_ADAPTER_H_
#define OMNISIGHT_EMBEDDED_CONFERENCE_VENDOR_ZOOM_ADAPTER_H_

#include <memory>
#include <string>

namespace omnisight::embedded::conference::vendor {

enum class ConferenceStatus {
	kOk = 0,
	kInvalidArgument,
	kUnavailable,
	kInvalidState,
	kBackendError,
};

struct ZoomSdkConfig {
	std::string sdk_root;
	std::string app_key;
	std::string app_secret;
	std::string glibc_abi = "glibc-2.31";
};

struct ConferenceTarget {
	std::string meeting_number;
	std::string password;
	std::string display_name;
	std::string zak_token;
	std::string join_token;
};

struct ConferenceMediaConfig {
	bool audio = true;
	bool video = true;
};

class ConferenceProvider {
public:
	virtual ~ConferenceProvider() = default;

	virtual ConferenceStatus startMeeting(const ConferenceTarget &target) = 0;
	virtual ConferenceStatus joinMeeting(const ConferenceTarget &target) = 0;
	virtual ConferenceStatus leaveMeeting() = 0;
	virtual ConferenceStatus startMedia(const ConferenceMediaConfig &media) = 0;
	virtual ConferenceStatus stopMedia(const ConferenceMediaConfig &media) = 0;
	virtual const std::string &lastError() const = 0;
};

class ZoomAdapter final : public ConferenceProvider {
public:
	explicit ZoomAdapter(ZoomSdkConfig config);
	~ZoomAdapter() override;

	ZoomAdapter(const ZoomAdapter &) = delete;
	ZoomAdapter &operator=(const ZoomAdapter &) = delete;
	ZoomAdapter(ZoomAdapter &&) noexcept;
	ZoomAdapter &operator=(ZoomAdapter &&) noexcept;

	ConferenceStatus startMeeting(const ConferenceTarget &target) override;
	ConferenceStatus joinMeeting(const ConferenceTarget &target) override;
	ConferenceStatus leaveMeeting() override;
	ConferenceStatus startMedia(const ConferenceMediaConfig &media) override;
	ConferenceStatus stopMedia(const ConferenceMediaConfig &media) override;

	bool available() const;
	const std::string &lastError() const override;

private:
	class Impl;

	std::unique_ptr<Impl> impl_;
};

} // namespace omnisight::embedded::conference::vendor

#endif // OMNISIGHT_EMBEDDED_CONFERENCE_VENDOR_ZOOM_ADAPTER_H_
