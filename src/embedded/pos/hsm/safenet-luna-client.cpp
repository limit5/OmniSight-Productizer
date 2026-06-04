/* SPDX-License-Identifier: MIT
 *
 * Case 7 SafeNet Luna HSM client wrapper (OP-2043).
 */
#include "safenet-luna-client.h"

#include <cstdint>
#include <iostream>
#include <utility>

namespace omnisight::embedded::pos::hsm {
namespace {

bool valid_connection(const SafeNetLunaConnection &connection)
{
	if (connection.pin.empty())
		return false;

	if (connection.protocol == SafeNetLunaProtocol::kPkcs11)
		return !connection.module_path.empty() &&
		       (!connection.slot_label.empty() ||
			!connection.partition.empty());

	return !connection.appliance.empty() && !connection.partition.empty();
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

class SafeNetLunaClient::Impl {
public:
	Impl(SafeNetLunaConnection connection, SafeNetLunaBackend backend)
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
				"SafeNet Luna connection is incomplete");
		if (!backend_.connect)
			return unavailable("SafeNet Luna bridge is not linked");

		void *session = nullptr;
		HSMStatus status = backend_.connect(connection_, &session);

		if (status != HSMStatus::kOk || !session)
			return backendError(status,
					    "failed to open SafeNet Luna session");

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
			return unavailable("SafeNet Luna key generation is not linked");
		return withSession([&]() {
			return backend_.generate_key(session_, spec, key);
		}, "SafeNet Luna key generation failed");
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
			return unavailable("SafeNet Luna key import is not linked");
		return withSession([&]() {
			return backend_.import_key(session_, spec, wrapped_key, key);
		}, "SafeNet Luna key import failed");
	}

	HSMStatus encrypt(const HSMOperationRequest &request,
			  std::vector<uint8_t> *ciphertext)
	{
		return runOperation(request, ciphertext, backend_.encrypt,
				    "SafeNet Luna encrypt is not linked",
				    "SafeNet Luna encrypt failed");
	}

	HSMStatus decrypt(const HSMOperationRequest &request,
			  std::vector<uint8_t> *plaintext)
	{
		return runOperation(request, plaintext, backend_.decrypt,
				    "SafeNet Luna decrypt is not linked",
				    "SafeNet Luna decrypt failed");
	}

	HSMStatus sign(const HSMOperationRequest &request,
		       std::vector<uint8_t> *signature)
	{
		return runOperation(request, signature, backend_.sign,
				    "SafeNet Luna sign is not linked",
				    "SafeNet Luna sign failed");
	}

	const std::string &lastError() const
	{
		return last_error_;
	}

	const SafeNetLunaConnection &connection() const
	{
		return connection_;
	}

private:
	template <typename Callable>
	HSMStatus withSession(Callable callable, const char *fallback_error)
	{
		if (!session_)
			return invalidState("SafeNet Luna session is not open");

		HSMStatus status = callable();

		if (status == HSMStatus::kOk) {
			last_error_.clear();
			return status;
		}
		return backendError(status, fallback_error);
	}

	HSMStatus runOperation(const HSMOperationRequest &request,
			       std::vector<uint8_t> *output,
			       const SafeNetLunaOperationFn &operation,
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

	SafeNetLunaConnection connection_;
	SafeNetLunaBackend backend_;
	void *session_ = nullptr;
	std::string last_error_;
};

SafeNetLunaClient::SafeNetLunaClient(SafeNetLunaConnection connection,
				     SafeNetLunaBackend backend)
	: impl_(std::make_unique<Impl>(std::move(connection), std::move(backend)))
{
}

SafeNetLunaClient::~SafeNetLunaClient() = default;

SafeNetLunaClient::SafeNetLunaClient(SafeNetLunaClient &&) noexcept = default;

SafeNetLunaClient &SafeNetLunaClient::operator=(
	SafeNetLunaClient &&) noexcept = default;

HSMStatus SafeNetLunaClient::connect()
{
	return impl_->connect();
}

HSMStatus SafeNetLunaClient::disconnect()
{
	return impl_->disconnect();
}

bool SafeNetLunaClient::connected() const
{
	return impl_->connected();
}

HSMStatus SafeNetLunaClient::generateKey(const HSMKeySpec &spec,
					 HSMKeyHandle *key)
{
	return impl_->generateKey(spec, key);
}

HSMStatus SafeNetLunaClient::importKey(
	const HSMKeySpec &spec, const std::vector<uint8_t> &wrapped_key,
	HSMKeyHandle *key)
{
	return impl_->importKey(spec, wrapped_key, key);
}

HSMStatus SafeNetLunaClient::encrypt(const HSMOperationRequest &request,
				     std::vector<uint8_t> *ciphertext)
{
	return impl_->encrypt(request, ciphertext);
}

HSMStatus SafeNetLunaClient::decrypt(const HSMOperationRequest &request,
				     std::vector<uint8_t> *plaintext)
{
	return impl_->decrypt(request, plaintext);
}

HSMStatus SafeNetLunaClient::sign(const HSMOperationRequest &request,
				  std::vector<uint8_t> *signature)
{
	return impl_->sign(request, signature);
}

const std::string &SafeNetLunaClient::lastError() const
{
	return impl_->lastError();
}

const SafeNetLunaConnection &SafeNetLunaClient::connection() const
{
	return impl_->connection();
}

const char *toString(SafeNetLunaProtocol protocol)
{
	switch (protocol) {
	case SafeNetLunaProtocol::kPkcs11:
		return "pkcs11";
	case SafeNetLunaProtocol::kLunaClient:
		return "luna-client";
	}

	return "unknown";
}

} // namespace omnisight::embedded::pos::hsm

#if defined(OMNISIGHT_SAFENET_LUNA_CLIENT_SMOKE_MAIN)
int main()
{
	using namespace omnisight::embedded::pos::hsm;

	int session_token = 7;
	SafeNetLunaBackend backend;

	backend.connect = [&session_token](const SafeNetLunaConnection &connection,
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
	backend.import_key = [](void *session, const HSMKeySpec &spec,
				const std::vector<uint8_t> &wrapped_key,
				HSMKeyHandle *key) {
		if (!session || !key || spec.label.empty() || wrapped_key.empty())
			return HSMStatus::kInvalidArgument;
		*key = {2, spec.label, session};
		return HSMStatus::kOk;
	};
	backend.encrypt = [](void *session, const HSMOperationRequest &request,
			     std::vector<uint8_t> *output) {
		if (!session || !output || !request.key)
			return HSMStatus::kInvalidArgument;
		*output = request.input;
		output->push_back(0x5a);
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

	SafeNetLunaClient client({
					  SafeNetLunaProtocol::kPkcs11,
					  "/usr/safenet/lunaclient/lib/libCryptoki2_64.so",
					  "luna-a700.internal",
					  "omnisight-pos",
					  "omnisight-rk3588",
					  "Crypto Officer",
					  "smoke-pin",
					  "",
					  true,
					  true,
				  },
				  std::move(backend));
	HSMKeyHandle key;
	HSMKeyHandle imported_key;
	std::vector<uint8_t> ciphertext;
	std::vector<uint8_t> plaintext;
	std::vector<uint8_t> signature;

	if (client.connect() != HSMStatus::kOk || !client.connected()) {
		std::cerr << "SafeNet Luna smoke connect failed: "
			  << client.lastError() << '\n';
		return 1;
	}
	if (client.generateKey({HSMKeyAlgorithm::kAes256, "smoke-key", false},
			       &key) != HSMStatus::kOk || !key) {
		std::cerr << "SafeNet Luna smoke key generation failed: "
			  << client.lastError() << '\n';
		return 1;
	}
	if (client.importKey({HSMKeyAlgorithm::kAes256, "import-key", false},
			     {0xaa, 0xbb}, &imported_key) != HSMStatus::kOk ||
	    !imported_key) {
		std::cerr << "SafeNet Luna smoke key import failed: "
			  << client.lastError() << '\n';
		return 1;
	}
	if (client.encrypt({key, HSMMechanism::kAesCbc, {1, 2, 3}, {4, 5, 6}},
			   &ciphertext) != HSMStatus::kOk) {
		std::cerr << "SafeNet Luna smoke encrypt failed: "
			  << client.lastError() << '\n';
		return 1;
	}
	if (client.decrypt({key, HSMMechanism::kAesCbc, ciphertext, {4, 5, 6}},
			   &plaintext) != HSMStatus::kOk ||
	    plaintext != std::vector<uint8_t>({1, 2, 3})) {
		std::cerr << "SafeNet Luna smoke decrypt failed: "
			  << client.lastError() << '\n';
		return 1;
	}
	if (client.sign({key, HSMMechanism::kEcdsaSha256, {9}, {}},
			&signature) != HSMStatus::kOk ||
	    signature.empty()) {
		std::cerr << "SafeNet Luna smoke sign failed: "
			  << client.lastError() << '\n';
		return 1;
	}

	return client.disconnect() == HSMStatus::kOk && !client.connected() ?
		       0 :
		       1;
}
#endif
