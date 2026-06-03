/* SPDX-License-Identifier: MIT
 *
 * Case 5 Microsoft ACS Calling SDK adapter (OP-2007).
 */
#include "acs-adapter.h"

#include "acs-sdk-stub.h"

#include <cstdint>
#include <memory>
#include <string>
#include <utility>

namespace omnisight::embedded::conference::vendor {
namespace {

constexpr uint64_t kFirstSessionId = 1;

bool starts_with(const std::string &value, const char *prefix)
{
	const std::string expected(prefix);

	return value.size() >= expected.size() &&
	       value.compare(0, expected.size(), expected) == 0;
}

bool contains(const std::string &value, const char *needle)
{
	return value.find(needle) != std::string::npos;
}

bool valid_teams_meeting_url(const std::string &target_uri)
{
	if (!starts_with(target_uri, "https://"))
		return false;
	return contains(target_uri, "teams.microsoft.com/") ||
	       contains(target_uri, "teams.live.com/");
}

ConferenceProviderStatus invalid_argument(std::string *last_error,
					  const char *message)
{
	if (last_error)
		*last_error = message;
	return ConferenceProviderStatus::kInvalidArgument;
}

ConferenceProviderStatus invalid_state(std::string *last_error,
				       const char *message)
{
	if (last_error)
		*last_error = message;
	return ConferenceProviderStatus::kInvalidState;
}

} // namespace

class AcsAdapter::Impl {
public:
	explicit Impl(AcsAdapterConfig config) : config_(std::move(config))
	{
	}

	~Impl()
	{
		closeActiveSession();
#if defined(OMNISIGHT_ACS_WITH_SDK)
		if (client_)
			omnisight_acs_client_destroy(client_);
#endif
	}

	SessionHandle startSession(const std::string &target_uri)
	{
		if (!valid_config()) {
			last_error_ =
				"ACS user access token and display name are required";
			emit(ConferenceProviderEventType::kError, {},
			     ConferenceProviderStatus::kInvalidArgument,
			     last_error_);
			return {};
		}
		if (!valid_teams_meeting_url(target_uri)) {
			last_error_ =
				"target URI must be a Teams meeting https URL";
			emit(ConferenceProviderEventType::kError, {},
			     ConferenceProviderStatus::kInvalidArgument,
			     last_error_);
			return {};
		}
		if (active_session_) {
			last_error_ = "an ACS session is already active";
			emit(ConferenceProviderEventType::kError, active_handle_,
			     ConferenceProviderStatus::kInvalidState,
			     last_error_);
			return {};
		}

#if !defined(OMNISIGHT_ACS_WITH_SDK)
		(void)target_uri;
		last_error_ = "ACS Calling SDK support is not enabled";
		emit(ConferenceProviderEventType::kError, {},
		     ConferenceProviderStatus::kUnavailable, last_error_);
		return {};
#else
		if (!client_) {
			const int create_status = omnisight_acs_client_create(
				config_.user_access_token.c_str(),
				config_.display_name.c_str(),
				&Impl::handleSdkEvent, this, &client_);
			if (create_status != 0 || !client_)
				return sdkError(create_status,
						"failed to create ACS client");
		}

		active_session_ = std::make_unique<AcsSessionState>();
		active_session_->target_uri = target_uri;
		active_session_->media_started =
			config_.enable_audio || config_.enable_video;
		active_handle_ = {
			next_session_id_++,
			active_session_.get(),
		};

		const int join_status = omnisight_acs_join_teams_meeting(
			client_, target_uri.c_str(), config_.enable_audio,
			config_.enable_video, &active_session_->call);
		if (join_status != 0 || !active_session_->call) {
			active_session_.reset();
			active_handle_ = {};
			return sdkError(join_status,
					"failed to join Teams meeting through ACS");
		}

		emit(ConferenceProviderEventType::kSessionStarted, active_handle_,
		     ConferenceProviderStatus::kOk, "ACS session started");
		return active_handle_;
#endif
	}

	ConferenceProviderStatus stopSession(const SessionHandle &handle)
	{
		if (!handle)
			return invalid_argument(&last_error_,
						"ACS session handle is empty");
		if (!active_session_ || handle.id != active_handle_.id)
			return invalid_state(&last_error_,
					     "ACS session handle is not active");

		closeActiveSession();
		return ConferenceProviderStatus::kOk;
	}

	void onEvent(ConferenceProviderEventCallback callback)
	{
		callback_ = std::move(callback);
	}

	bool available() const
	{
#if defined(OMNISIGHT_ACS_WITH_SDK)
		return true;
#else
		return false;
#endif
	}

	const std::string &lastError() const
	{
		return last_error_;
	}

private:
	struct AcsSessionState {
		std::string target_uri;
		bool media_started = false;
#if defined(OMNISIGHT_ACS_WITH_SDK)
		OmnisightAcsCall *call = nullptr;
#endif
	};

	bool valid_config() const
	{
		return !config_.user_access_token.empty() &&
		       !config_.display_name.empty() &&
		       (config_.enable_audio || config_.enable_video);
	}

	void closeActiveSession()
	{
		if (!active_session_)
			return;

#if defined(OMNISIGHT_ACS_WITH_SDK)
		if (active_session_->call)
			(void)omnisight_acs_call_hangup(active_session_->call);
#endif
		if (active_session_->media_started) {
			active_session_->media_started = false;
			emit(ConferenceProviderEventType::kMediaChanged,
			     active_handle_, ConferenceProviderStatus::kOk,
			     "ACS media stopped");
		}
		emit(ConferenceProviderEventType::kSessionStopped, active_handle_,
		     ConferenceProviderStatus::kOk, "ACS session stopped");
		active_session_.reset();
		active_handle_ = {};
	}

	void emit(ConferenceProviderEventType type, const SessionHandle &handle,
		  ConferenceProviderStatus status, const std::string &detail)
	{
		if (!callback_)
			return;

		callback_({
			type,
			handle,
			status,
			detail,
			handle.vendor_specific_ptr,
		});
	}

#if defined(OMNISIGHT_ACS_WITH_SDK)
	static void handleSdkEvent(OmnisightAcsEventType type, int status,
				   const char *detail, void *user_data)
	{
		auto *self = static_cast<Impl *>(user_data);

		if (!self)
			return;
		self->onSdkEvent(type, status, detail);
	}

	void onSdkEvent(OmnisightAcsEventType type, int status, const char *detail)
	{
		ConferenceProviderStatus mapped_status =
			status == 0 ? ConferenceProviderStatus::kOk :
				      ConferenceProviderStatus::kBackendError;
		const std::string event_detail = detail ? detail : "";

		if (mapped_status != ConferenceProviderStatus::kOk)
			last_error_ = event_detail;
		if (type == OMNISIGHT_ACS_EVENT_MEDIA_CHANGED &&
		    active_session_) {
			active_session_->media_started =
				mapped_status == ConferenceProviderStatus::kOk;
		}
		emit(toProviderEvent(type), active_handle_, mapped_status,
		     event_detail);
	}

	ConferenceProviderEventType toProviderEvent(OmnisightAcsEventType type)
	{
		switch (type) {
		case OMNISIGHT_ACS_EVENT_SESSION_STARTED:
			return ConferenceProviderEventType::kSessionStarted;
		case OMNISIGHT_ACS_EVENT_SESSION_STOPPED:
			return ConferenceProviderEventType::kSessionStopped;
		case OMNISIGHT_ACS_EVENT_PARTICIPANT_CHANGED:
			return ConferenceProviderEventType::kParticipantChanged;
		case OMNISIGHT_ACS_EVENT_MEDIA_CHANGED:
			return ConferenceProviderEventType::kMediaChanged;
		case OMNISIGHT_ACS_EVENT_ERROR:
			return ConferenceProviderEventType::kError;
		}

		return ConferenceProviderEventType::kError;
	}

	SessionHandle sdkError(int status, const char *message)
	{
		last_error_ = message;
		if (status != 0)
			last_error_ += " status=" + std::to_string(status);
		emit(ConferenceProviderEventType::kError, {},
		     ConferenceProviderStatus::kBackendError, last_error_);
		return {};
	}
#endif

	AcsAdapterConfig config_;
	ConferenceProviderEventCallback callback_;
	std::unique_ptr<AcsSessionState> active_session_;
	SessionHandle active_handle_;
	std::string last_error_;
	uint64_t next_session_id_ = kFirstSessionId;
#if defined(OMNISIGHT_ACS_WITH_SDK)
	OmnisightAcsClient *client_ = nullptr;
#endif
};

AcsAdapter::AcsAdapter(AcsAdapterConfig config)
	: impl_(std::make_unique<Impl>(std::move(config)))
{
}

AcsAdapter::~AcsAdapter() = default;

AcsAdapter::AcsAdapter(AcsAdapter &&) noexcept = default;

AcsAdapter &AcsAdapter::operator=(AcsAdapter &&) noexcept = default;

SessionHandle AcsAdapter::startSession(const std::string &target_uri)
{
	return impl_->startSession(target_uri);
}

ConferenceProviderStatus AcsAdapter::stopSession(const SessionHandle &handle)
{
	return impl_->stopSession(handle);
}

void AcsAdapter::onEvent(ConferenceProviderEventCallback callback)
{
	impl_->onEvent(std::move(callback));
}

bool AcsAdapter::available() const
{
	return impl_->available();
}

const std::string &AcsAdapter::lastError() const
{
	return impl_->lastError();
}

const char *toString(ConferenceProviderStatus status)
{
	switch (status) {
	case ConferenceProviderStatus::kOk:
		return "ok";
	case ConferenceProviderStatus::kInvalidArgument:
		return "invalid-argument";
	case ConferenceProviderStatus::kUnavailable:
		return "unavailable";
	case ConferenceProviderStatus::kInvalidState:
		return "invalid-state";
	case ConferenceProviderStatus::kBackendError:
		return "backend-error";
	}

	return "unknown";
}

const char *toString(ConferenceProviderEventType type)
{
	switch (type) {
	case ConferenceProviderEventType::kSessionStarted:
		return "session-started";
	case ConferenceProviderEventType::kSessionStopped:
		return "session-stopped";
	case ConferenceProviderEventType::kParticipantChanged:
		return "participant-changed";
	case ConferenceProviderEventType::kMediaChanged:
		return "media-changed";
	case ConferenceProviderEventType::kError:
		return "error";
	}

	return "unknown";
}

} // namespace omnisight::embedded::conference::vendor

#if defined(OMNISIGHT_ACS_ADAPTER_SMOKE_MAIN)
#include <iostream>
#include <vector>

int main()
{
	using namespace omnisight::embedded::conference::vendor;

	AcsAdapter adapter({
		"stub-token",
		"OmniSight smoke",
		true,
		true,
	});
	std::vector<ConferenceProviderEventType> events;

	adapter.onEvent([&events](const ConferenceProviderEvent &event) {
		events.push_back(event.type);
	});

	SessionHandle invalid = adapter.startSession("https://example.com/not-teams");
	if (invalid || events.empty() ||
	    events.back() != ConferenceProviderEventType::kError) {
		std::cerr << "ACS adapter accepted a non-Teams target\n";
		return 1;
	}

	SessionHandle handle = adapter.startSession(
		"https://teams.microsoft.com/l/meetup-join/19%3ameeting");
	if (!adapter.available()) {
		if (handle) {
			std::cerr << "ACS adapter returned a handle without SDK\n";
			return 1;
		}
		std::cout << "ACS adapter smoke skipped: "
			  << adapter.lastError() << '\n';
		return 0;
	}
	if (!handle) {
		std::cerr << "ACS adapter failed to start an SDK-backed session: "
			  << adapter.lastError() << '\n';
		return 1;
	}
	if (adapter.stopSession(handle) != ConferenceProviderStatus::kOk) {
		std::cerr << "ACS adapter stop failed: " << adapter.lastError()
			  << '\n';
		return 1;
	}

	return 0;
}
#endif
