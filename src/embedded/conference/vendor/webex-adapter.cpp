/* SPDX-License-Identifier: MIT
 *
 * Case 5 Cisco Webex Calling SDK adapter (OP-2008).
 */
#include "webex-adapter.h"

#include "webex-sdk-stub.h"

#include <iostream>
#include <utility>

namespace omnisight::embedded::conference::vendor {
namespace {

static bool has_webex_meeting_url(const std::string &target_uri)
{
	return target_uri.rfind("https://", 0) == 0 &&
	       target_uri.find(".webex.com/") != std::string::npos;
}

static ConferenceProviderStatus to_provider_status(
	enum omnisight_webex_sdk_status status)
{
	switch (status) {
	case OMNISIGHT_WEBEX_SDK_OK:
		return ConferenceProviderStatus::kOk;
	case OMNISIGHT_WEBEX_SDK_INVALID_ARGUMENT:
		return ConferenceProviderStatus::kInvalidArgument;
	case OMNISIGHT_WEBEX_SDK_UNAVAILABLE:
		return ConferenceProviderStatus::kUnavailable;
	case OMNISIGHT_WEBEX_SDK_INVALID_STATE:
		return ConferenceProviderStatus::kInvalidState;
	case OMNISIGHT_WEBEX_SDK_BACKEND_ERROR:
		return ConferenceProviderStatus::kBackendError;
	}

	return ConferenceProviderStatus::kBackendError;
}

} // namespace

class WebexAdapter::Impl {
public:
	explicit Impl(WebexSdkConfig config) : config_(std::move(config))
	{
		struct omnisight_webex_sdk_config sdk_config = {
			config_.sdk_root.c_str(),
			config_.glibc_abi.c_str(),
		};

		setStatus(omnisight_webex_sdk_create(&sdk_config, &sdk_));
	}

	~Impl()
	{
		omnisight_webex_sdk_destroy(sdk_);
	}

	Impl(const Impl &) = delete;
	Impl &operator=(const Impl &) = delete;

	SessionHandle startSession(const std::string &target_uri)
	{
		if (!validTarget(target_uri))
			return {};
		if (config_.access_token.empty()) {
			last_error_ = "Webex access token is required";
			emit(ConferenceProviderEventType::kError, {},
			     ConferenceProviderStatus::kInvalidArgument,
			     last_error_);
			return {};
		}
		if (!sdk_) {
			unavailable();
			emit(ConferenceProviderEventType::kError, {},
			     ConferenceProviderStatus::kUnavailable, last_error_);
			return {};
		}

		ConferenceProviderStatus status = authenticate();
		if (status != ConferenceProviderStatus::kOk) {
			emit(ConferenceProviderEventType::kError, {}, status,
			     last_error_);
			return {};
		}

		const struct omnisight_webex_call_options options = {
			target_uri.c_str(),
			config_.display_name.c_str(),
		};
		omnisight_webex_call *call = nullptr;

		status = setStatus(
			omnisight_webex_sdk_start_call(sdk_, &options, &call));
		if (status != ConferenceProviderStatus::kOk) {
			emit(ConferenceProviderEventType::kError, {}, status,
			     last_error_);
			return {};
		}
		if (!call) {
			last_error_ = "Webex SDK returned an empty call handle";
			emit(ConferenceProviderEventType::kError, {},
			     ConferenceProviderStatus::kBackendError,
			     last_error_);
			return {};
		}

		active_session_ = {
			next_session_id_++,
			call,
		};
		emit(ConferenceProviderEventType::kSessionStarted,
		     active_session_, ConferenceProviderStatus::kOk, "");
		return active_session_;
	}

	ConferenceProviderStatus stopSession(const SessionHandle &handle)
	{
		if (!validHandle(handle))
			return invalidState("Webex session handle is not active");
		if (!sdk_)
			return unavailable();

		auto *call = static_cast<omnisight_webex_call *>(
			active_session_.vendor_specific_ptr);
		ConferenceProviderStatus status =
			setStatus(omnisight_webex_sdk_stop_call(sdk_, call));
		if (status == ConferenceProviderStatus::kOk) {
			SessionHandle stopped = active_session_;

			active_session_ = {};
			emit(ConferenceProviderEventType::kSessionStopped, stopped,
			     status, "");
		} else {
			emit(ConferenceProviderEventType::kError, active_session_,
			     status, last_error_);
		}
		return status;
	}

	void onEvent(ConferenceProviderEventCallback callback)
	{
		callback_ = std::move(callback);
	}

	ConferenceProviderStatus startMedia(const SessionHandle &handle,
					    const WebexMediaConfig &media)
	{
		if (!validMedia(media))
			return ConferenceProviderStatus::kInvalidArgument;
		if (!validHandle(handle))
			return invalidState("Webex session handle is not active");
		if (!sdk_)
			return unavailable();

		auto *call = static_cast<omnisight_webex_call *>(
			active_session_.vendor_specific_ptr);
		ConferenceProviderStatus status =
			setStatus(omnisight_webex_sdk_start_media(
				sdk_, call, media.audio ? 1 : 0,
				media.video ? 1 : 0));
		emitMediaStatus(status);
		return status;
	}

	ConferenceProviderStatus stopMedia(const SessionHandle &handle,
					   const WebexMediaConfig &media)
	{
		if (!validMedia(media))
			return ConferenceProviderStatus::kInvalidArgument;
		if (!validHandle(handle))
			return invalidState("Webex session handle is not active");
		if (!sdk_)
			return unavailable();

		auto *call = static_cast<omnisight_webex_call *>(
			active_session_.vendor_specific_ptr);
		ConferenceProviderStatus status =
			setStatus(omnisight_webex_sdk_stop_media(
				sdk_, call, media.audio ? 1 : 0,
				media.video ? 1 : 0));
		emitMediaStatus(status);
		return status;
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
	bool validTarget(const std::string &target_uri)
	{
		if (!has_webex_meeting_url(target_uri)) {
			last_error_ = "Webex meeting URL is required";
			emit(ConferenceProviderEventType::kError, {},
			     ConferenceProviderStatus::kInvalidArgument,
			     last_error_);
			return false;
		}

		last_error_.clear();
		return true;
	}

	bool validHandle(const SessionHandle &handle) const
	{
		return handle.id != 0 && handle.id == active_session_.id &&
		       handle.vendor_specific_ptr ==
			       active_session_.vendor_specific_ptr;
	}

	bool validMedia(const WebexMediaConfig &media)
	{
		if (media.audio || media.video) {
			last_error_.clear();
			return true;
		}

		last_error_ = "at least one Webex media stream is required";
		emit(ConferenceProviderEventType::kError, active_session_,
		     ConferenceProviderStatus::kInvalidArgument, last_error_);
		return false;
	}

	ConferenceProviderStatus authenticate()
	{
		return setStatus(omnisight_webex_sdk_authenticate(
			sdk_, config_.access_token.c_str()));
	}

	ConferenceProviderStatus invalidState(const std::string &error)
	{
		last_error_ = error;
		emit(ConferenceProviderEventType::kError, active_session_,
		     ConferenceProviderStatus::kInvalidState, last_error_);
		return ConferenceProviderStatus::kInvalidState;
	}

	ConferenceProviderStatus unavailable()
	{
		last_error_ = omnisight_webex_sdk_last_error(sdk_);
		return ConferenceProviderStatus::kUnavailable;
	}

	ConferenceProviderStatus setStatus(enum omnisight_webex_sdk_status status)
	{
		if (status == OMNISIGHT_WEBEX_SDK_OK) {
			last_error_.clear();
			return ConferenceProviderStatus::kOk;
		}

		last_error_ = omnisight_webex_sdk_last_error(sdk_);
		return to_provider_status(status);
	}

	void emitMediaStatus(ConferenceProviderStatus status)
	{
		if (status == ConferenceProviderStatus::kOk) {
			emit(ConferenceProviderEventType::kMediaChanged,
			     active_session_, status, "");
			return;
		}

		emit(ConferenceProviderEventType::kError, active_session_, status,
		     last_error_);
	}

	void emit(ConferenceProviderEventType type, const SessionHandle &session,
		  ConferenceProviderStatus status, const std::string &detail)
	{
		if (!callback_)
			return;

		callback_({
			type,
			session,
			status,
			detail,
			session.vendor_specific_ptr,
		});
	}

	WebexSdkConfig config_;
	omnisight_webex_sdk *sdk_ = nullptr;
	SessionHandle active_session_;
	uint64_t next_session_id_ = 1;
	ConferenceProviderEventCallback callback_;
	std::string last_error_;
};

WebexAdapter::WebexAdapter(WebexSdkConfig config)
	: impl_(std::make_unique<Impl>(std::move(config)))
{
}

WebexAdapter::~WebexAdapter() = default;

WebexAdapter::WebexAdapter(WebexAdapter &&) noexcept = default;

WebexAdapter &WebexAdapter::operator=(WebexAdapter &&) noexcept = default;

SessionHandle WebexAdapter::startSession(const std::string &target_uri)
{
	return impl_->startSession(target_uri);
}

ConferenceProviderStatus WebexAdapter::stopSession(const SessionHandle &handle)
{
	return impl_->stopSession(handle);
}

void WebexAdapter::onEvent(ConferenceProviderEventCallback callback)
{
	impl_->onEvent(std::move(callback));
}

ConferenceProviderStatus WebexAdapter::startMedia(
	const SessionHandle &handle, const WebexMediaConfig &media)
{
	return impl_->startMedia(handle, media);
}

ConferenceProviderStatus WebexAdapter::stopMedia(
	const SessionHandle &handle, const WebexMediaConfig &media)
{
	return impl_->stopMedia(handle, media);
}

bool WebexAdapter::available() const
{
	return impl_->available();
}

const std::string &WebexAdapter::lastError() const
{
	return impl_->lastError();
}

} // namespace omnisight::embedded::conference::vendor

#if defined(OMNISIGHT_WEBEX_ADAPTER_SMOKE_MAIN)
int main()
{
	using namespace omnisight::embedded::conference::vendor;

	WebexSdkConfig config;
	config.access_token = "smoke-token";
	config.display_name = "OmniSight Smoke";

	WebexAdapter adapter(config);
	size_t events = 0;

	adapter.onEvent([&events](const ConferenceProviderEvent &event) {
		if (event.type == ConferenceProviderEventType::kError)
			++events;
	});

	SessionHandle invalid = adapter.startSession("https://example.invalid/");
	if (invalid || adapter.lastError() != "Webex meeting URL is required" ||
	    events != 1) {
		std::cerr << "webex-adapter invalid target check failed\n";
		return 1;
	}

	SessionHandle handle =
		adapter.startSession("https://omnisight.webex.com/meet/smoke");
	if (!handle && !adapter.available()) {
		std::cout << "webex-adapter smoke skipped: "
			  << adapter.lastError() << '\n';
		return 0;
	}
	if (!handle) {
		std::cerr << "webex-adapter start failed: "
			  << adapter.lastError() << '\n';
		return 1;
	}

	WebexMediaConfig media;
	ConferenceProviderStatus status = adapter.startMedia(handle, media);
	if (status != ConferenceProviderStatus::kOk) {
		std::cerr << "webex-adapter media start failed: "
			  << adapter.lastError() << '\n';
		return 1;
	}

	status = adapter.stopMedia(handle, media);
	if (status != ConferenceProviderStatus::kOk) {
		std::cerr << "webex-adapter media stop failed: "
			  << adapter.lastError() << '\n';
		return 1;
	}

	status = adapter.stopSession(handle);
	if (status != ConferenceProviderStatus::kOk) {
		std::cerr << "webex-adapter stop failed: "
			  << adapter.lastError() << '\n';
		return 1;
	}

	return 0;
}
#endif
