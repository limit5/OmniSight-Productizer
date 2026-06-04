/* SPDX-License-Identifier: MIT
 *
 * Case 7 Thales nShield HSM client wrapper (OP-2027).
 */
#include "thales-nshield-client.h"

#include "hsm-client-registry.h"

#include <cstdint>
#include <iostream>
#include <utility>

namespace omnisight::embedded::pos::hsm {
namespace {

bool valid_connection(const ThalesNShieldConnection &connection)
{
	if (connection.protocol == ThalesNShieldProtocol::kPkcs11)
		return !connection.module_path.empty() &&
		       (!connection.pin.empty() ||
			!connection.operator_card_set.empty());

	return !connection.world_path.empty() &&
	       (!connection.pin.empty() || !connection.operator_card_set.empty());
}

bool valid_key_spec(const HSMKeySpec &spec)
{
	return !spec.label.empty();
}

bool valid_operation_request(const HSMOperationRequest &request)
{
	return request.key && !request.input.empty();
}

HSMStatus invalid_argument(std::string *last_error, const char *message)
{
	if (last_error)
		*last_error = message;
	return HSMStatus::kInvalidArgument;
}

} // namespace

class ThalesNShieldClient::Impl {
public:
	Impl(ThalesNShieldConnection connection, ThalesNShieldBackend backend)
		: connection_(std::move(connection)), backend_(std::move(backend))
	{
	}

	~Impl()
	{
		(void)disconnect();
	}

	Impl(const Impl &) = delete;
	Impl &operator=(const Impl &) = delete;

	HSMStatus connect()
	{
		if (session_) {
			last_error_.clear();
			return HSMStatus::kOk;
		}
		if (!valid_connection(connection_))
			return invalid_argument(
				&last_error_,
				"Thales nShield connection is incomplete");
		if (!backend_.connect)
			return unavailable("Thales nShield bridge is not linked");

		void *session = nullptr;
		HSMStatus status = backend_.connect(connection_, &session);

		if (status != HSMStatus::kOk || !session)
			return backendError(status,
					    "failed to open Thales nShield session");

		session_ = session;
		last_error_.clear();
		return HSMStatus::kOk;
	}

	HSMStatus disconnect()
	{
		if (!session_) {
			last_error_.clear();
			return HSMStatus::kOk;
		}
		if (backend_.disconnect)
			backend_.disconnect(session_);
		session_ = nullptr;
		last_error_.clear();
		return HSMStatus::kOk;
	}

	bool connected() const
	{
		return session_ != nullptr;
	}

	HSMStatus generateKey(const HSMKeySpec &spec, HSMKeyHandle *key)
	{
		if (!key)
			return invalid_argument(&last_error_,
						"key handle destination is null");
		if (!valid_key_spec(spec))
			return invalid_argument(&last_error_,
						"key label is required");
		if (!backend_.generate_key)
			return unavailable("Thales nShield key generation is not linked");
		return withSession([&]() {
			return backend_.generate_key(session_, spec, key);
		}, "Thales nShield key generation failed");
	}

	HSMStatus importKey(const HSMKeySpec &spec,
			    const std::vector<uint8_t> &wrapped_key,
			    HSMKeyHandle *key)
	{
		if (!key)
			return invalid_argument(&last_error_,
						"key handle destination is null");
		if (!valid_key_spec(spec) || wrapped_key.empty())
			return invalid_argument(
				&last_error_,
				"key label and wrapped key material are required");
		if (!backend_.import_key)
			return unavailable("Thales nShield key import is not linked");
		return withSession([&]() {
			return backend_.import_key(session_, spec, wrapped_key, key);
		}, "Thales nShield key import failed");
	}

	HSMStatus encrypt(const HSMOperationRequest &request,
			  std::vector<uint8_t> *ciphertext)
	{
		return runOperation(request, ciphertext, backend_.encrypt,
				    "Thales nShield encrypt is not linked",
				    "Thales nShield encrypt failed");
	}

	HSMStatus decrypt(const HSMOperationRequest &request,
			  std::vector<uint8_t> *plaintext)
	{
		return runOperation(request, plaintext, backend_.decrypt,
				    "Thales nShield decrypt is not linked",
				    "Thales nShield decrypt failed");
	}

	HSMStatus sign(const HSMOperationRequest &request,
		       std::vector<uint8_t> *signature)
	{
		return runOperation(request, signature, backend_.sign,
				    "Thales nShield sign is not linked",
				    "Thales nShield sign failed");
	}

	const std::string &lastError() const
	{
		return last_error_;
	}

	const ThalesNShieldConnection &connection() const
	{
		return connection_;
	}

private:
	template <typename Callable>
	HSMStatus withSession(Callable callable, const char *fallback_error)
	{
		if (!session_)
			return invalidState("Thales nShield session is not open");

		HSMStatus status = callable();

		if (status == HSMStatus::kOk) {
			last_error_.clear();
			return status;
		}
		return backendError(status, fallback_error);
	}

	HSMStatus runOperation(const HSMOperationRequest &request,
			       std::vector<uint8_t> *output,
			       const ThalesNShieldOperationFn &operation,
			       const char *unavailable_error,
			       const char *fallback_error)
	{
		if (!output)
			return invalid_argument(&last_error_,
						"operation output destination is null");
		if (!valid_operation_request(request))
			return invalid_argument(
				&last_error_,
				"operation key and input data are required");
		if (!operation)
			return unavailable(unavailable_error);
		return withSession([&]() {
			return operation(session_, request, output);
		}, fallback_error);
	}

	HSMStatus invalidState(const char *message)
	{
		last_error_ = message;
		return HSMStatus::kInvalidState;
	}

	HSMStatus unavailable(const char *message)
	{
		last_error_ = message;
		return HSMStatus::kUnavailable;
	}

	HSMStatus backendError(HSMStatus status, const char *fallback_error)
	{
		last_error_ = fallback_error;
		if (backend_.last_error) {
			std::string vendor_error = backend_.last_error(session_);

			if (!vendor_error.empty())
				last_error_ = std::move(vendor_error);
		}
		return status == HSMStatus::kOk ? HSMStatus::kBackendError :
						 status;
	}

	ThalesNShieldConnection connection_;
	ThalesNShieldBackend backend_;
	void *session_ = nullptr;
	std::string last_error_;
};

ThalesNShieldClient::ThalesNShieldClient(ThalesNShieldConnection connection,
					 ThalesNShieldBackend backend)
	: impl_(std::make_unique<Impl>(std::move(connection), std::move(backend)))
{
}

ThalesNShieldClient::~ThalesNShieldClient() = default;

ThalesNShieldClient::ThalesNShieldClient(ThalesNShieldClient &&) noexcept =
	default;

ThalesNShieldClient &ThalesNShieldClient::operator=(
	ThalesNShieldClient &&) noexcept = default;

HSMStatus ThalesNShieldClient::connect()
{
	return impl_->connect();
}

HSMStatus ThalesNShieldClient::disconnect()
{
	return impl_->disconnect();
}

bool ThalesNShieldClient::connected() const
{
	return impl_->connected();
}

HSMStatus ThalesNShieldClient::generateKey(const HSMKeySpec &spec,
					   HSMKeyHandle *key)
{
	return impl_->generateKey(spec, key);
}

HSMStatus ThalesNShieldClient::importKey(
	const HSMKeySpec &spec, const std::vector<uint8_t> &wrapped_key,
	HSMKeyHandle *key)
{
	return impl_->importKey(spec, wrapped_key, key);
}

HSMStatus ThalesNShieldClient::encrypt(const HSMOperationRequest &request,
				       std::vector<uint8_t> *ciphertext)
{
	return impl_->encrypt(request, ciphertext);
}

HSMStatus ThalesNShieldClient::decrypt(const HSMOperationRequest &request,
				       std::vector<uint8_t> *plaintext)
{
	return impl_->decrypt(request, plaintext);
}

HSMStatus ThalesNShieldClient::sign(const HSMOperationRequest &request,
				    std::vector<uint8_t> *signature)
{
	return impl_->sign(request, signature);
}

const std::string &ThalesNShieldClient::lastError() const
{
	return impl_->lastError();
}

const ThalesNShieldConnection &ThalesNShieldClient::connection() const
{
	return impl_->connection();
}

const char *toString(HSMStatus status)
{
	switch (status) {
	case HSMStatus::kOk:
		return "ok";
	case HSMStatus::kInvalidArgument:
		return "invalid-argument";
	case HSMStatus::kUnavailable:
		return "unavailable";
	case HSMStatus::kInvalidState:
		return "invalid-state";
	case HSMStatus::kBackendError:
		return "backend-error";
	}

	return "unknown";
}

const char *toString(HSMKeyAlgorithm algorithm)
{
	switch (algorithm) {
	case HSMKeyAlgorithm::kAes256:
		return "aes-256";
	case HSMKeyAlgorithm::kRsa2048:
		return "rsa-2048";
	case HSMKeyAlgorithm::kRsa3072:
		return "rsa-3072";
	case HSMKeyAlgorithm::kEcP256:
		return "ec-p256";
	}

	return "unknown";
}

const char *toString(HSMMechanism mechanism)
{
	switch (mechanism) {
	case HSMMechanism::kAesCbc:
		return "aes-cbc";
	case HSMMechanism::kRsaOaepSha256:
		return "rsa-oaep-sha256";
	case HSMMechanism::kEcdsaSha256:
		return "ecdsa-sha256";
	}

	return "unknown";
}

const char *toString(ThalesNShieldProtocol protocol)
{
	switch (protocol) {
	case ThalesNShieldProtocol::kPkcs11:
		return "pkcs11";
	case ThalesNShieldProtocol::kNFast:
		return "nfast";
	}

	return "unknown";
}

namespace {

ThalesNShieldClient thales_nshield_registry_client({}, {});
[[maybe_unused]] const bool thales_nshield_registered =
	HsmClientRegistry::instance().registerClient("thales-nshield",
						    &thales_nshield_registry_client);

} // namespace

} // namespace omnisight::embedded::pos::hsm

#if defined(OMNISIGHT_THALES_NSHIELD_CLIENT_SMOKE_MAIN)
int main()
{
	using namespace omnisight::embedded::pos::hsm;

	int session_token = 7;
	ThalesNShieldBackend backend;

	backend.connect = [&session_token](const ThalesNShieldConnection &connection,
					   void **session) {
		if (!session || connection.module_path.empty())
			return HSMStatus::kInvalidArgument;
		*session = &session_token;
		return HSMStatus::kOk;
	};
	backend.disconnect = [](void *session) {
		(void)session;
	};
	backend.generate_key = [](void *session, const HSMKeySpec &spec,
				  HSMKeyHandle *key) {
		if (!session || !key || spec.label.empty())
			return HSMStatus::kInvalidArgument;
		*key = {1, spec.label, session};
		return HSMStatus::kOk;
	};
	backend.encrypt = [](void *session, const HSMOperationRequest &request,
			     std::vector<uint8_t> *output) {
		if (!session || !output || !request.key)
			return HSMStatus::kInvalidArgument;
		*output = request.input;
		output->push_back(0xa5);
		return HSMStatus::kOk;
	};
	backend.decrypt = [](void *session, const HSMOperationRequest &request,
			     std::vector<uint8_t> *output) {
		if (!session || !output || request.input.empty())
			return HSMStatus::kInvalidArgument;
		*output = request.input;
		output->pop_back();
		return HSMStatus::kOk;
	};
	backend.sign = [](void *session, const HSMOperationRequest &request,
			  std::vector<uint8_t> *output) {
		if (!session || !output || request.input.empty())
			return HSMStatus::kInvalidArgument;
		*output = {0x30, 0x03, request.input.front()};
		return HSMStatus::kOk;
	};

	ThalesNShieldClient client({
					    ThalesNShieldProtocol::kPkcs11,
					    "/opt/nfast/toolkits/pkcs11/libcknfast.so",
					    "omnisight-rk3588",
					    "operator-card-set",
					    "",
					    "",
				    },
				    std::move(backend));
	HSMKeyHandle key;
	std::vector<uint8_t> ciphertext;
	std::vector<uint8_t> plaintext;
	std::vector<uint8_t> signature;

	if (client.connect() != HSMStatus::kOk || !client.connected()) {
		std::cerr << "nShield smoke connect failed: "
			  << client.lastError() << '\n';
		return 1;
	}
	if (client.generateKey({HSMKeyAlgorithm::kAes256, "smoke-key", false},
			       &key) != HSMStatus::kOk || !key) {
		std::cerr << "nShield smoke key generation failed: "
			  << client.lastError() << '\n';
		return 1;
	}
	if (client.encrypt({key, HSMMechanism::kAesCbc, {1, 2, 3}, {4, 5, 6}},
			   &ciphertext) != HSMStatus::kOk) {
		std::cerr << "nShield smoke encrypt failed: "
			  << client.lastError() << '\n';
		return 1;
	}
	if (client.decrypt({key, HSMMechanism::kAesCbc, ciphertext, {4, 5, 6}},
			   &plaintext) != HSMStatus::kOk ||
	    plaintext != std::vector<uint8_t>({1, 2, 3})) {
		std::cerr << "nShield smoke decrypt failed: "
			  << client.lastError() << '\n';
		return 1;
	}
	if (client.sign({key, HSMMechanism::kEcdsaSha256, {9}, {}},
			&signature) != HSMStatus::kOk ||
	    signature.empty()) {
		std::cerr << "nShield smoke sign failed: " << client.lastError()
			  << '\n';
		return 1;
	}

	return 0;
}
#endif
