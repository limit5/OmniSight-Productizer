/* SPDX-License-Identifier: MIT
 *
 * Case 5 SIP signaling wrapper (OP-2001).
 */
#include "signaling.h"

#include <exception>
#include <utility>

#if defined(OMNISIGHT_SIP_WITH_PJSIP)
#include <pjsua-lib/pjsua.h>
#endif

namespace omnisight::embedded::conference::sip {
namespace {

static bool empty_registration_field(const SipEndpointConfig &config)
{
	return config.local_uri.empty() || config.registrar_uri.empty() ||
	       config.username.empty();
}

static bool valid_invite(const SipInviteConfig &invite)
{
	return !invite.remote_uri.empty();
}

#if defined(OMNISIGHT_SIP_WITH_PJSIP)
static pj_str_t pj_string(const std::string &value)
{
	return pj_str(const_cast<char *>(value.c_str()));
}

static std::string to_string(const pj_str_t &value)
{
	if (!value.ptr || value.slen <= 0)
		return {};
	return std::string(value.ptr, static_cast<size_t>(value.slen));
}

static SipSignalingStatus map_status(pj_status_t status, std::string *last_error)
{
	if (status == PJ_SUCCESS)
		return SipSignalingStatus::kOk;

	char buffer[PJ_ERR_MSG_SIZE] = {};

	pj_strerror(status, buffer, sizeof(buffer));
	if (last_error)
		*last_error = buffer;
	return SipSignalingStatus::kBackendError;
}
#endif

} // namespace

class SipSignaling::Impl {
public:
	~Impl()
	{
		close();
	}

	SipSignalingStatus start(const SipEndpointConfig &config)
	{
		if (empty_registration_field(config)) {
			last_error_ =
				"local URI, registrar URI, and username are required";
			return SipSignalingStatus::kInvalidArgument;
		}
		if (config.transport != "udp") {
			last_error_ = "only UDP SIP transport is supported";
			return SipSignalingStatus::kInvalidArgument;
		}

#if !defined(OMNISIGHT_SIP_WITH_PJSIP)
		(void)config;
		last_error_ = "PJSIP support is not enabled";
		return SipSignalingStatus::kUnavailable;
#else
		if (started_)
			return SipSignalingStatus::kOk;
		if (active_instance_ && active_instance_ != this) {
			last_error_ = "another SIP signaling instance is active";
			return SipSignalingStatus::kInvalidState;
		}

		config_ = config;
		pj_status_t status = pjsua_create();

		if (status != PJ_SUCCESS)
			return map_status(status, &last_error_);

		pjsua_config ua_config;
		pjsua_logging_config logging_config;
		pjsua_media_config media_config;

		pjsua_config_default(&ua_config);
		pjsua_logging_config_default(&logging_config);
		pjsua_media_config_default(&media_config);

		ua_config.cb.on_incoming_call = &Impl::handleIncomingCall;
		ua_config.cb.on_call_state = &Impl::handleCallState;
		ua_config.cb.on_reg_state = &Impl::handleRegistrationState;
		media_config.has_ioqueue = PJ_FALSE;

		status = pjsua_init(&ua_config, &logging_config, &media_config);
		if (status != PJ_SUCCESS) {
			pjsua_destroy();
			return map_status(status, &last_error_);
		}

		pjsua_transport_config transport_config;

		pjsua_transport_config_default(&transport_config);
		transport_config.port = config.local_port;
		status = pjsua_transport_create(PJSIP_TRANSPORT_UDP,
						&transport_config, nullptr);
		if (status != PJ_SUCCESS) {
			pjsua_destroy();
			return map_status(status, &last_error_);
		}

		status = pjsua_start();
		if (status != PJ_SUCCESS) {
			pjsua_destroy();
			return map_status(status, &last_error_);
		}

		active_instance_ = this;
		started_ = true;
		state_ = SipSignalingState::kStarted;
		return SipSignalingStatus::kOk;
#endif
	}

	SipSignalingStatus registerEndpoint()
	{
#if !defined(OMNISIGHT_SIP_WITH_PJSIP)
		last_error_ = "PJSIP support is not enabled";
		return SipSignalingStatus::kUnavailable;
#else
		if (!started_)
			return SipSignalingStatus::kInvalidState;

		pjsua_acc_config account_config;

		pjsua_acc_config_default(&account_config);
		account_config.id = pj_string(config_.local_uri);
		account_config.reg_uri = pj_string(config_.registrar_uri);
		account_config.cred_count = 1;
		account_config.cred_info[0].scheme = pj_str(const_cast<char *>("digest"));
		account_config.cred_info[0].realm = pj_string(config_.realm);
		account_config.cred_info[0].username = pj_string(config_.username);
		account_config.cred_info[0].data_type = PJSIP_CRED_DATA_PLAIN_PASSWD;
		account_config.cred_info[0].data = pj_string(config_.password);

		state_ = SipSignalingState::kRegistering;
		const pj_status_t status =
			pjsua_acc_add(&account_config, PJ_TRUE, &account_id_);
		return map_status(status, &last_error_);
#endif
	}

	SipSignalingStatus invite(const SipInviteConfig &invite)
	{
		if (!valid_invite(invite)) {
			last_error_ = "remote URI is required";
			return SipSignalingStatus::kInvalidArgument;
		}

#if !defined(OMNISIGHT_SIP_WITH_PJSIP)
		(void)invite;
		last_error_ = "PJSIP support is not enabled";
		return SipSignalingStatus::kUnavailable;
#else
		if (state_ != SipSignalingState::kRegistered &&
		    state_ != SipSignalingState::kTerminated)
			return SipSignalingStatus::kInvalidState;

		pj_str_t remote = pj_string(invite.remote_uri);
		pj_str_t sdp = pj_string(invite.local_sdp);
		pjsua_msg_data message_data;
		pjsua_call_setting call_setting;

		pjsua_msg_data_init(&message_data);
		pjsua_call_setting_default(&call_setting);
		call_setting.aud_cnt = 0;
		call_setting.vid_cnt = 0;
		if (!invite.local_sdp.empty()) {
			message_data.content_type =
				pj_str(const_cast<char *>("application/sdp"));
			message_data.content = sdp;
		}

		state_ = SipSignalingState::kInviting;
		const pj_status_t status = pjsua_call_make_call(
			account_id_, &remote, &call_setting, nullptr, &message_data,
			&call_id_);
		return map_status(status, &last_error_);
#endif
	}

	SipSignalingStatus bye()
	{
#if !defined(OMNISIGHT_SIP_WITH_PJSIP)
		last_error_ = "PJSIP support is not enabled";
		return SipSignalingStatus::kUnavailable;
#else
		if (call_id_ == PJSUA_INVALID_ID ||
		    (state_ != SipSignalingState::kInviting &&
		     state_ != SipSignalingState::kInCall))
			return SipSignalingStatus::kInvalidState;

		state_ = SipSignalingState::kTerminating;
		const pj_status_t status =
			pjsua_call_hangup(call_id_, 0, nullptr, nullptr);
		return map_status(status, &last_error_);
#endif
	}

	void onIncoming(IncomingInviteHandler handler)
	{
		incoming_handler_ = std::move(handler);
	}

	void close()
	{
#if defined(OMNISIGHT_SIP_WITH_PJSIP)
		if (started_) {
			if (call_id_ != PJSUA_INVALID_ID)
				pjsua_call_hangup(call_id_, 0, nullptr, nullptr);
			pjsua_destroy();
		}
		if (active_instance_ == this)
			active_instance_ = nullptr;
		started_ = false;
		account_id_ = PJSUA_INVALID_ID;
		call_id_ = PJSUA_INVALID_ID;
#endif
		state_ = SipSignalingState::kIdle;
	}

	bool available() const
	{
#if defined(OMNISIGHT_SIP_WITH_PJSIP)
		return true;
#else
		return false;
#endif
	}

	SipSignalingState state() const
	{
		return state_;
	}

	const std::string &lastError() const
	{
		return last_error_;
	}

private:
#if defined(OMNISIGHT_SIP_WITH_PJSIP)
	static void handleIncomingCall(pjsua_acc_id account_id,
				       pjsua_call_id call_id,
				       pjsip_rx_data *rx_data)
	{
		if (!active_instance_)
			return;
		active_instance_->onIncomingCall(account_id, call_id, rx_data);
	}

	static void handleCallState(pjsua_call_id call_id, pjsip_event *event)
	{
		(void)event;
		if (!active_instance_)
			return;
		active_instance_->onCallState(call_id);
	}

	static void handleRegistrationState(pjsua_acc_id account_id)
	{
		if (!active_instance_)
			return;
		active_instance_->onRegistrationState(account_id);
	}

	void onIncomingCall(pjsua_acc_id account_id, pjsua_call_id call_id,
			    pjsip_rx_data *rx_data)
	{
		(void)account_id;
		call_id_ = call_id;
		state_ = SipSignalingState::kInviting;

		if (!incoming_handler_)
			return;

		SipIncomingInvite invite;
		pjsua_call_info info;

		if (pjsua_call_get_info(call_id, &info) == PJ_SUCCESS) {
			invite.remote_uri = to_string(info.remote_info);
			invite.local_uri = to_string(info.local_info);
		}
		if (rx_data && rx_data->msg_info.msg &&
		    rx_data->msg_info.msg->body) {
			const pjsip_msg_body *body = rx_data->msg_info.msg->body;
			if (body->data && body->len > 0) {
				invite.sdp = std::string(
					static_cast<const char *>(body->data),
					body->len);
			}
		}

		incoming_handler_(invite);
	}

	void onCallState(pjsua_call_id call_id)
	{
		if (call_id != call_id_)
			return;

		pjsua_call_info info;

		if (pjsua_call_get_info(call_id, &info) != PJ_SUCCESS)
			return;

		switch (info.state) {
		case PJSIP_INV_STATE_CONFIRMED:
			state_ = SipSignalingState::kInCall;
			break;
		case PJSIP_INV_STATE_DISCONNECTED:
			state_ = SipSignalingState::kTerminated;
			call_id_ = PJSUA_INVALID_ID;
			break;
		default:
			break;
		}
	}

	void onRegistrationState(pjsua_acc_id account_id)
	{
		if (account_id != account_id_)
			return;

		pjsua_acc_info info;

		if (pjsua_acc_get_info(account_id, &info) != PJ_SUCCESS)
			return;

		state_ = info.status == PJSIP_SC_OK ?
				 SipSignalingState::kRegistered :
				 SipSignalingState::kRegistering;
	}

	inline static Impl *active_instance_ = nullptr;
	SipEndpointConfig config_;
	bool started_ = false;
	pjsua_acc_id account_id_ = PJSUA_INVALID_ID;
	pjsua_call_id call_id_ = PJSUA_INVALID_ID;
#endif
	IncomingInviteHandler incoming_handler_;
	SipSignalingState state_ = SipSignalingState::kIdle;
	std::string last_error_;
};

SipSignaling::SipSignaling() : impl_(std::make_unique<Impl>())
{
}

SipSignaling::~SipSignaling() = default;

SipSignaling::SipSignaling(SipSignaling &&) noexcept = default;

SipSignaling &SipSignaling::operator=(SipSignaling &&) noexcept = default;

SipSignalingStatus SipSignaling::start(const SipEndpointConfig &config)
{
	return impl_->start(config);
}

SipSignalingStatus SipSignaling::registerEndpoint()
{
	return impl_->registerEndpoint();
}

SipSignalingStatus SipSignaling::invite(const SipInviteConfig &invite)
{
	return impl_->invite(invite);
}

SipSignalingStatus SipSignaling::bye()
{
	return impl_->bye();
}

void SipSignaling::onIncoming(IncomingInviteHandler handler)
{
	impl_->onIncoming(std::move(handler));
}

void SipSignaling::close()
{
	impl_->close();
}

bool SipSignaling::available() const
{
	return impl_->available();
}

SipSignalingState SipSignaling::state() const
{
	return impl_->state();
}

const std::string &SipSignaling::lastError() const
{
	return impl_->lastError();
}

} // namespace omnisight::embedded::conference::sip
