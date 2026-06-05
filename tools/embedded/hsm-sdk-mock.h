/* SPDX-License-Identifier: MIT
 *
 * [OP-2060] Case 7 HSM SDK mock registry for qemu smoke coverage.
 */
#ifndef OMNISIGHT_TOOLS_EMBEDDED_HSM_SDK_MOCK_H_
#define OMNISIGHT_TOOLS_EMBEDDED_HSM_SDK_MOCK_H_

#include "hsm-client.h"
#include "safenet-luna-client.h"
#include "thales-nshield-client.h"
#include "utimaco-client.h"

#include <algorithm>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <utility>
#include <vector>

namespace omnisight::embedded::pos::hsm::mock {

class UnifiedHsmClient {
public:
	virtual ~UnifiedHsmClient() = default;

	virtual const char *vendor() const = 0;
	virtual HSMStatus connect() = 0;
	virtual HSMStatus disconnect() = 0;
	virtual bool connected() const = 0;
	virtual HSMStatus generateKey(const HSMKeySpec &spec,
				      HSMKeyHandle *key) = 0;
	virtual HSMStatus encrypt(const HSMOperationRequest &request,
				  std::vector<uint8_t> *ciphertext) = 0;
	virtual HSMStatus decrypt(const HSMOperationRequest &request,
				  std::vector<uint8_t> *plaintext) = 0;
	virtual const std::string &lastError() const = 0;
};

struct HsmClientRegistryEntry {
	const char *vendor;
	std::function<std::unique_ptr<UnifiedHsmClient>()> create;
};

namespace detail {

struct MockSdkState {
	explicit MockSdkState(uint8_t tag) : vendor_tag(tag) {}

	uint8_t vendor_tag;
	uint64_t next_key_id = 1;
	std::string last_error;
};

inline HSMStatus mock_generate_key(MockSdkState *state,
				   const HSMKeySpec &spec,
				   HSMKeyHandle *key)
{
	if (!state || !key || spec.label.empty()) {
		if (state)
			state->last_error = "mock generate-key input is invalid";
		return HSMStatus::kInvalidArgument;
	}

	*key = { state->next_key_id++, spec.label, state };
	state->last_error.clear();
	return HSMStatus::kOk;
}

inline HSMStatus mock_encrypt(MockSdkState *state,
			      const HSMOperationRequest &request,
			      std::vector<uint8_t> *output)
{
	if (!state || !output || !request.key || request.input.empty()) {
		if (state)
			state->last_error = "mock encrypt input is invalid";
		return HSMStatus::kInvalidArgument;
	}

	*output = request.input;
	std::reverse(output->begin(), output->end());
	for (uint8_t &value : *output)
		value ^= state->vendor_tag;
	output->insert(output->begin(), state->vendor_tag);
	state->last_error.clear();
	return HSMStatus::kOk;
}

inline HSMStatus mock_decrypt(MockSdkState *state,
			      const HSMOperationRequest &request,
			      std::vector<uint8_t> *output)
{
	if (!state || !output || !request.key || request.input.size() < 2 ||
	    request.input.front() != state->vendor_tag) {
		if (state)
			state->last_error = "mock decrypt input is invalid";
		return HSMStatus::kInvalidArgument;
	}

	output->assign(request.input.begin() + 1, request.input.end());
	for (uint8_t &value : *output)
		value ^= state->vendor_tag;
	std::reverse(output->begin(), output->end());
	state->last_error.clear();
	return HSMStatus::kOk;
}

inline HSMStatus mock_sign(MockSdkState *state,
			   const HSMOperationRequest &request,
			   std::vector<uint8_t> *output)
{
	if (!state || !output || request.input.empty()) {
		if (state)
			state->last_error = "mock sign input is invalid";
		return HSMStatus::kInvalidArgument;
	}

	*output = { 0x30, state->vendor_tag, request.input.front() };
	state->last_error.clear();
	return HSMStatus::kOk;
}

inline HsmStatus to_utimaco_status(HSMStatus status)
{
	switch (status) {
	case HSMStatus::kOk:
		return HsmStatus::kOk;
	case HSMStatus::kInvalidArgument:
		return HsmStatus::kInvalidArgument;
	case HSMStatus::kUnavailable:
		return HsmStatus::kUnavailable;
	case HSMStatus::kInvalidState:
		return HsmStatus::kInvalidState;
	case HSMStatus::kBackendError:
		return HsmStatus::kBackendError;
	}

	return HsmStatus::kBackendError;
}

inline HSMStatus from_utimaco_status(HsmStatus status)
{
	switch (status) {
	case HsmStatus::kOk:
		return HSMStatus::kOk;
	case HsmStatus::kInvalidArgument:
		return HSMStatus::kInvalidArgument;
	case HsmStatus::kUnavailable:
		return HSMStatus::kUnavailable;
	case HsmStatus::kInvalidState:
		return HSMStatus::kInvalidState;
	case HsmStatus::kBackendError:
		return HSMStatus::kBackendError;
	}

	return HSMStatus::kBackendError;
}

inline HsmKeySpec to_utimaco_key_spec(const HSMKeySpec &spec)
{
	HsmKeySpec out;

	out.algorithm = HsmKeyAlgorithm::kAes;
	out.bits = spec.algorithm == HSMKeyAlgorithm::kAes256 ? 256 : 2048;
	out.extractable = spec.extractable;
	out.label = spec.label;
	return out;
}

inline HSMKeyHandle from_utimaco_key_handle(const HsmKeyHandle &key)
{
	return { key.id, key.label, key.vendor_specific_ptr };
}

inline HsmKeyHandle to_utimaco_key_handle(const HSMKeyHandle &key)
{
	return { key.id, key.label, key.vendor_specific_ptr };
}

inline HsmCipherRequest to_utimaco_cipher_request(
	const HSMOperationRequest &request)
{
	HsmCipherRequest out;

	out.key = to_utimaco_key_handle(request.key);
	out.mode = HsmCipherMode::kCbc;
	out.iv = request.iv;
	out.input = request.input;
	return out;
}

class HSMClientAdapter final : public UnifiedHsmClient {
public:
	HSMClientAdapter(const char *vendor, std::unique_ptr<HSMClient> client)
		: vendor_(vendor), client_(std::move(client))
	{
	}

	const char *vendor() const override
	{
		return vendor_;
	}

	HSMStatus connect() override
	{
		return client_->connect();
	}

	HSMStatus disconnect() override
	{
		return client_->disconnect();
	}

	bool connected() const override
	{
		return client_->connected();
	}

	HSMStatus generateKey(const HSMKeySpec &spec, HSMKeyHandle *key) override
	{
		return client_->generateKey(spec, key);
	}

	HSMStatus encrypt(const HSMOperationRequest &request,
			  std::vector<uint8_t> *ciphertext) override
	{
		return client_->encrypt(request, ciphertext);
	}

	HSMStatus decrypt(const HSMOperationRequest &request,
			  std::vector<uint8_t> *plaintext) override
	{
		return client_->decrypt(request, plaintext);
	}

	const std::string &lastError() const override
	{
		return client_->lastError();
	}

private:
	const char *vendor_;
	std::unique_ptr<HSMClient> client_;
};

class UtimacoUnifiedClient final : public UnifiedHsmClient {
public:
	explicit UtimacoUnifiedClient(UtimacoClient client)
		: client_(std::move(client))
	{
	}

	const char *vendor() const override
	{
		return "utimaco";
	}

	HSMStatus connect() override
	{
		return from_utimaco_status(client_.connect());
	}

	HSMStatus disconnect() override
	{
		client_.disconnect();
		return HSMStatus::kOk;
	}

	bool connected() const override
	{
		return client_.connected();
	}

	HSMStatus generateKey(const HSMKeySpec &spec, HSMKeyHandle *key) override
	{
		if (!key)
			return HSMStatus::kInvalidArgument;

		HsmKeyHandle utimaco_key;
		HsmStatus status = client_.generateKey(to_utimaco_key_spec(spec),
						       &utimaco_key);

		if (status == HsmStatus::kOk)
			*key = from_utimaco_key_handle(utimaco_key);
		return from_utimaco_status(status);
	}

	HSMStatus encrypt(const HSMOperationRequest &request,
			  std::vector<uint8_t> *ciphertext) override
	{
		return from_utimaco_status(
			client_.encrypt(to_utimaco_cipher_request(request),
					ciphertext));
	}

	HSMStatus decrypt(const HSMOperationRequest &request,
			  std::vector<uint8_t> *plaintext) override
	{
		return from_utimaco_status(
			client_.decrypt(to_utimaco_cipher_request(request),
					plaintext));
	}

	const std::string &lastError() const override
	{
		return client_.lastError();
	}

private:
	UtimacoClient client_;
};

inline ThalesNShieldBackend make_thales_backend(
	const std::shared_ptr<MockSdkState> &state)
{
	ThalesNShieldBackend backend;

	backend.connect = [state](const ThalesNShieldConnection &connection,
				  void **session) {
		if (!session || connection.module_path.empty())
			return HSMStatus::kInvalidArgument;
		*session = state.get();
		return HSMStatus::kOk;
	};
	backend.disconnect = [](void *session) {
		(void)session;
	};
	backend.generate_key = [](void *session, const HSMKeySpec &spec,
				  HSMKeyHandle *key) {
		return mock_generate_key(static_cast<MockSdkState *>(session),
					 spec, key);
	};
	backend.encrypt = [](void *session, const HSMOperationRequest &request,
			     std::vector<uint8_t> *output) {
		return mock_encrypt(static_cast<MockSdkState *>(session), request,
				    output);
	};
	backend.decrypt = [](void *session, const HSMOperationRequest &request,
			     std::vector<uint8_t> *output) {
		return mock_decrypt(static_cast<MockSdkState *>(session), request,
				    output);
	};
	backend.sign = [](void *session, const HSMOperationRequest &request,
			  std::vector<uint8_t> *output) {
		return mock_sign(static_cast<MockSdkState *>(session), request,
				 output);
	};
	backend.last_error = [](void *session) {
		auto *state = static_cast<MockSdkState *>(session);
		return state ? state->last_error : std::string {};
	};
	return backend;
}

inline SafeNetLunaBackend make_safenet_backend(
	const std::shared_ptr<MockSdkState> &state)
{
	SafeNetLunaBackend backend;

	backend.connect = [state](const SafeNetLunaConnection &connection,
				  void **session) {
		if (!session || connection.module_path.empty())
			return HSMStatus::kInvalidArgument;
		*session = state.get();
		return HSMStatus::kOk;
	};
	backend.disconnect = [](void *session) {
		(void)session;
	};
	backend.generate_key = [](void *session, const HSMKeySpec &spec,
				  HSMKeyHandle *key) {
		return mock_generate_key(static_cast<MockSdkState *>(session),
					 spec, key);
	};
	backend.import_key = [](void *session, const HSMKeySpec &spec,
				const std::vector<uint8_t> &wrapped_key,
				HSMKeyHandle *key) {
		if (wrapped_key.empty())
			return HSMStatus::kInvalidArgument;
		return mock_generate_key(static_cast<MockSdkState *>(session),
					 spec, key);
	};
	backend.encrypt = [](void *session, const HSMOperationRequest &request,
			     std::vector<uint8_t> *output) {
		return mock_encrypt(static_cast<MockSdkState *>(session), request,
				    output);
	};
	backend.decrypt = [](void *session, const HSMOperationRequest &request,
			     std::vector<uint8_t> *output) {
		return mock_decrypt(static_cast<MockSdkState *>(session), request,
				    output);
	};
	backend.sign = [](void *session, const HSMOperationRequest &request,
			  std::vector<uint8_t> *output) {
		return mock_sign(static_cast<MockSdkState *>(session), request,
				 output);
	};
	backend.last_error = [](void *session) {
		auto *state = static_cast<MockSdkState *>(session);
		return state ? state->last_error : std::string {};
	};
	return backend;
}

inline UtimacoTransport make_utimaco_transport(
	const std::shared_ptr<MockSdkState> &state)
{
	UtimacoTransport transport;

	transport.connect = [state](const UtimacoClientConfig &config) {
		(void)state;
		if (config.slot.empty() || config.user_pin.empty())
			return HsmStatus::kInvalidArgument;
		return HsmStatus::kOk;
	};
	transport.disconnect = []() {};
	transport.generate_key = [state](const HsmKeySpec &spec,
					 HsmKeyHandle *key) {
		HSMKeySpec hsm_spec;
		HSMKeyHandle hsm_key;
		HSMStatus status;

		hsm_spec.algorithm = HSMKeyAlgorithm::kAes256;
		hsm_spec.label = spec.label;
		hsm_spec.extractable = spec.extractable;
		status = mock_generate_key(state.get(), hsm_spec, &hsm_key);
		if (status == HSMStatus::kOk)
			*key = to_utimaco_key_handle(hsm_key);
		return to_utimaco_status(status);
	};
	transport.encrypt = [state](const HsmCipherRequest &request,
				    std::vector<uint8_t> *output) {
		HSMOperationRequest hsm_request;

		hsm_request.key = from_utimaco_key_handle(request.key);
		hsm_request.mechanism = HSMMechanism::kAesCbc;
		hsm_request.input = request.input;
		hsm_request.iv = request.iv;
		return to_utimaco_status(
			mock_encrypt(state.get(), hsm_request, output));
	};
	transport.decrypt = [state](const HsmCipherRequest &request,
				    std::vector<uint8_t> *output) {
		HSMOperationRequest hsm_request;

		hsm_request.key = from_utimaco_key_handle(request.key);
		hsm_request.mechanism = HSMMechanism::kAesCbc;
		hsm_request.input = request.input;
		hsm_request.iv = request.iv;
		return to_utimaco_status(
			mock_decrypt(state.get(), hsm_request, output));
	};
	transport.last_error = [state]() {
		return state->last_error.c_str();
	};
	return transport;
}

} // namespace detail

inline std::unique_ptr<UnifiedHsmClient> make_thales_nshield_client()
{
	auto state = std::make_shared<detail::MockSdkState>(0xa5);
	ThalesNShieldConnection connection;

	connection.protocol = ThalesNShieldProtocol::kPkcs11;
	connection.module_path = "/mock/thales/libcknfast.so";
	connection.slot_label = "omnisight-rk3588";
	connection.operator_card_set = "mock-operator-card-set";
	return std::make_unique<detail::HSMClientAdapter>(
		"thales", std::make_unique<ThalesNShieldClient>(
				  std::move(connection),
				  detail::make_thales_backend(state)));
}

inline std::unique_ptr<UnifiedHsmClient> make_utimaco_client()
{
	auto state = std::make_shared<detail::MockSdkState>(0x3c);
	UtimacoClientConfig config;

	config.backend = UtimacoBackend::kPkcs11;
	config.slot = "0";
	config.partition = "omnisight-pos";
	config.user_pin = "mock-pin";
	return std::make_unique<detail::UtimacoUnifiedClient>(
		UtimacoClient(std::move(config),
			      detail::make_utimaco_transport(state)));
}

inline std::unique_ptr<UnifiedHsmClient> make_safenet_luna_client()
{
	auto state = std::make_shared<detail::MockSdkState>(0x5a);
	SafeNetLunaConnection connection;

	connection.protocol = SafeNetLunaProtocol::kPkcs11;
	connection.module_path = "/mock/safenet/libCryptoki2_64.so";
	connection.partition = "omnisight-pos";
	connection.slot_label = "omnisight-rk3588";
	connection.pin = "mock-pin";
	return std::make_unique<detail::HSMClientAdapter>(
		"safenet", std::make_unique<SafeNetLunaClient>(
				   std::move(connection),
				   detail::make_safenet_backend(state)));
}

class HsmClientRegistry {
public:
	static const std::vector<HsmClientRegistryEntry> &registeredVendors()
	{
		static const std::vector<HsmClientRegistryEntry> vendors = {
			{ "thales", make_thales_nshield_client },
			{ "utimaco", make_utimaco_client },
			{ "safenet", make_safenet_luna_client },
		};

		return vendors;
	}
};

} // namespace omnisight::embedded::pos::hsm::mock

#endif // OMNISIGHT_TOOLS_EMBEDDED_HSM_SDK_MOCK_H_
