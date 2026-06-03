/* SPDX-License-Identifier: MIT
 *
 * Case 5 Zoom Linux Meeting SDK adapter (OP-2005).
 */
#include "zoom-adapter.h"

#include "zoom-sdk-stub.h"

#include <utility>

namespace omnisight::embedded::conference::vendor {
namespace {

static bool has_meeting_number(const ConferenceTarget &target)
{
	return !target.meeting_number.empty();
}

static bool has_display_name(const ConferenceTarget &target)
{
	return !target.display_name.empty();
}

static ConferenceStatus to_conference_status(
	enum omnisight_zoom_sdk_status status)
{
	switch (status) {
	case OMNISIGHT_ZOOM_SDK_OK:
		return ConferenceStatus::kOk;
	case OMNISIGHT_ZOOM_SDK_INVALID_ARGUMENT:
		return ConferenceStatus::kInvalidArgument;
	case OMNISIGHT_ZOOM_SDK_UNAVAILABLE:
		return ConferenceStatus::kUnavailable;
	case OMNISIGHT_ZOOM_SDK_INVALID_STATE:
		return ConferenceStatus::kInvalidState;
	case OMNISIGHT_ZOOM_SDK_BACKEND_ERROR:
		return ConferenceStatus::kBackendError;
	}

	return ConferenceStatus::kBackendError;
}

} // namespace

class ZoomAdapter::Impl {
public:
	explicit Impl(ZoomSdkConfig config) : config_(std::move(config))
	{
		struct omnisight_zoom_sdk_config sdk_config = {
			config_.sdk_root.c_str(),
			config_.app_key.c_str(),
			config_.app_secret.c_str(),
			config_.glibc_abi.c_str(),
		};

		setStatus(omnisight_zoom_sdk_create(&sdk_config, &sdk_));
	}

	~Impl()
	{
		omnisight_zoom_sdk_destroy(sdk_);
	}

	Impl(const Impl &) = delete;
	Impl &operator=(const Impl &) = delete;

	ConferenceStatus startMeeting(const ConferenceTarget &target)
	{
		if (!valid_target(target, true))
			return ConferenceStatus::kInvalidArgument;

		return callMeeting(target, true);
	}

	ConferenceStatus joinMeeting(const ConferenceTarget &target)
	{
		if (!valid_target(target, false))
			return ConferenceStatus::kInvalidArgument;

		return callMeeting(target, false);
	}

	ConferenceStatus leaveMeeting()
	{
		if (!sdk_)
			return unavailable();

		return setStatus(omnisight_zoom_sdk_leave_meeting(sdk_));
	}

	ConferenceStatus startMedia(const ConferenceMediaConfig &media)
	{
		if (!media.audio && !media.video) {
			last_error_ = "at least one Zoom media stream is required";
			return ConferenceStatus::kInvalidArgument;
		}
		if (!sdk_)
			return unavailable();

		return setStatus(omnisight_zoom_sdk_start_media(
			sdk_, media.audio ? 1 : 0, media.video ? 1 : 0));
	}

	ConferenceStatus stopMedia(const ConferenceMediaConfig &media)
	{
		if (!media.audio && !media.video) {
			last_error_ = "at least one Zoom media stream is required";
			return ConferenceStatus::kInvalidArgument;
		}
		if (!sdk_)
			return unavailable();

		return setStatus(omnisight_zoom_sdk_stop_media(
			sdk_, media.audio ? 1 : 0, media.video ? 1 : 0));
	}

	bool available() const
	{
		return sdk_ != nullptr;
	}

	const std::string &lastError() const
	{
		return last_error_;
	}

private:
	ConferenceStatus callMeeting(const ConferenceTarget &target, bool host)
	{
		if (!sdk_)
			return unavailable();

		const struct omnisight_zoom_meeting_options options = {
			target.meeting_number.c_str(),
			target.password.c_str(),
			target.display_name.c_str(),
			target.zak_token.c_str(),
			target.join_token.c_str(),
		};

		if (host)
			return setStatus(
				omnisight_zoom_sdk_start_meeting(sdk_, &options));
		return setStatus(omnisight_zoom_sdk_join_meeting(sdk_, &options));
	}

	bool valid_target(const ConferenceTarget &target, bool host)
	{
		if (!has_meeting_number(target)) {
			last_error_ = "Zoom meeting number is required";
			return false;
		}
		if (!has_display_name(target)) {
			last_error_ = "Zoom display name is required";
			return false;
		}
		if (host && target.zak_token.empty()) {
			last_error_ = "Zoom host ZAK token is required";
			return false;
		}

		last_error_.clear();
		return true;
	}

	ConferenceStatus unavailable()
	{
		last_error_ = omnisight_zoom_sdk_last_error(sdk_);
		return ConferenceStatus::kUnavailable;
	}

	ConferenceStatus setStatus(enum omnisight_zoom_sdk_status status)
	{
		if (status == OMNISIGHT_ZOOM_SDK_OK) {
			last_error_.clear();
			return ConferenceStatus::kOk;
		}

		last_error_ = omnisight_zoom_sdk_last_error(sdk_);
		return to_conference_status(status);
	}

	ZoomSdkConfig config_;
	omnisight_zoom_sdk *sdk_ = nullptr;
	std::string last_error_;
};

ZoomAdapter::ZoomAdapter(ZoomSdkConfig config)
	: impl_(std::make_unique<Impl>(std::move(config)))
{
}

ZoomAdapter::~ZoomAdapter() = default;

ZoomAdapter::ZoomAdapter(ZoomAdapter &&) noexcept = default;

ZoomAdapter &ZoomAdapter::operator=(ZoomAdapter &&) noexcept = default;

ConferenceStatus ZoomAdapter::startMeeting(const ConferenceTarget &target)
{
	return impl_->startMeeting(target);
}

ConferenceStatus ZoomAdapter::joinMeeting(const ConferenceTarget &target)
{
	return impl_->joinMeeting(target);
}

ConferenceStatus ZoomAdapter::leaveMeeting()
{
	return impl_->leaveMeeting();
}

ConferenceStatus ZoomAdapter::startMedia(const ConferenceMediaConfig &media)
{
	return impl_->startMedia(media);
}

ConferenceStatus ZoomAdapter::stopMedia(const ConferenceMediaConfig &media)
{
	return impl_->stopMedia(media);
}

bool ZoomAdapter::available() const
{
	return impl_->available();
}

const std::string &ZoomAdapter::lastError() const
{
	return impl_->lastError();
}

} // namespace omnisight::embedded::conference::vendor
