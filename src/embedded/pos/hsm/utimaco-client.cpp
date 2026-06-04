/* SPDX-License-Identifier: MIT
 *
 * Case 7 Utimaco SecurityServer HSM client wrapper (OP-2036).
 */
#include "utimaco-client.h"

#include <algorithm>
#include <iostream>
#include <utility>

namespace omnisight::embedded::pos::hsm {
namespace {

static bool valid_endpoint(const UtimacoClientConfig &config)
{
	if (config.backend == UtimacoBackend::kCxi)
		return !config.endpoint.empty();

	return !config.slot.empty();
}

static bool valid_key_spec(const HsmKeySpec &spec)
{
	if (spec.label.empty())
		return false;

	switch (spec.algorithm) {
	case HsmKeyAlgorithm::kAes:
		return spec.bits == 128 || spec.bits == 192 || spec.bits == 256;
	case HsmKeyAlgorithm::kTdes:
		return spec.bits == 112 || spec.bits == 168;
	case HsmKeyAlgorithm::kRsa:
		return spec.bits == 2048 || spec.bits == 3072 ||
		       spec.bits == 4096;
	}

	return false;
}

static bool valid_cipher_request(const HsmCipherRequest &request)
{
	return request.key && !request.input.empty();
}

static std::string transport_error(const UtimacoTransport &transport)
{
	if (!transport.last_error)
		return "Utimaco SDK operation failed";

	const char *error = transport.last_error();
	if (!error || error[0] == '\0')
		return "Utimaco SDK operation failed";

	return error;
}

} // namespace

class UtimacoClient::Impl {
public:
	Impl(UtimacoClientConfig config, UtimacoTransport transport)
		: config_(std::move(config)), transport_(std::move(transport))
	{
	}

	~Impl()
	{
		disconnect();
	}

	Impl(const Impl &) = delete;
	Impl &operator=(const Impl &) = delete;

	HsmStatus connect()
	{
		if (!valid_endpoint(config_)) {
			last_error_ = config_.backend == UtimacoBackend::kCxi ?
				      "Utimaco CXI endpoint is required" :
				      "Utimaco PKCS#11 slot is required";
			return HsmStatus::kInvalidArgument;
		}
		if (config_.user_pin.empty()) {
			last_error_ = "Utimaco user PIN is required";
			return HsmStatus::kInvalidArgument;
		}
		if (!transport_.connect) {
			last_error_ = "Utimaco SDK transport is not configured";
			return HsmStatus::kUnavailable;
		}

		HsmStatus status = transport_.connect(config_);
		if (status != HsmStatus::kOk) {
			last_error_ = transport_error(transport_);
			connected_ = false;
			return status;
		}

		last_error_.clear();
		connected_ = true;
		return HsmStatus::kOk;
	}

	void disconnect()
	{
		if (!connected_)
			return;

		if (transport_.disconnect)
			transport_.disconnect();
		connected_ = false;
	}

	HsmStatus generateKey(const HsmKeySpec &spec, HsmKeyHandle *key)
	{
		if (!key)
			return fail(HsmStatus::kInvalidArgument,
				    "Utimaco key destination is null");
		if (!valid_key_spec(spec))
			return fail(HsmStatus::kInvalidArgument,
				    "Utimaco key spec is invalid");
		if (!connected_)
			return fail(HsmStatus::kInvalidState,
				    "Utimaco session is not connected");
		if (!transport_.generate_key)
			return fail(HsmStatus::kUnavailable,
				    "Utimaco generate-key transport is not configured");

		HsmKeyHandle generated;
		HsmStatus status = transport_.generate_key(spec, &generated);

		if (status != HsmStatus::kOk)
			return fail(status, transport_error(transport_));
		if (!generated)
			return fail(HsmStatus::kBackendError,
				    "Utimaco SDK returned an empty key handle");

		*key = generated;
		last_error_.clear();
		return HsmStatus::kOk;
	}

	HsmStatus encrypt(const HsmCipherRequest &request,
			  std::vector<uint8_t> *ciphertext)
	{
		return crypt(request, ciphertext, true);
	}

	HsmStatus decrypt(const HsmCipherRequest &request,
			  std::vector<uint8_t> *plaintext)
	{
		return crypt(request, plaintext, false);
	}

	bool connected() const
	{
		return connected_;
	}

	const std::string &lastError() const
	{
		return last_error_;
	}

	const UtimacoClientConfig &config() const
	{
		return config_;
	}

private:
	HsmStatus crypt(const HsmCipherRequest &request,
		       std::vector<uint8_t> *output, bool encrypt)
	{
		if (!output)
			return fail(HsmStatus::kInvalidArgument,
				    "Utimaco cipher destination is null");
		if (!valid_cipher_request(request))
			return fail(HsmStatus::kInvalidArgument,
				    "Utimaco cipher request is invalid");
		if (!connected_)
			return fail(HsmStatus::kInvalidState,
				    "Utimaco session is not connected");

		auto &operation = encrypt ? transport_.encrypt : transport_.decrypt;
		if (!operation)
			return fail(HsmStatus::kUnavailable,
				    encrypt ?
					    "Utimaco encrypt transport is not configured" :
					    "Utimaco decrypt transport is not configured");

		std::vector<uint8_t> result;
		HsmStatus status = operation(request, &result);
		if (status != HsmStatus::kOk)
			return fail(status, transport_error(transport_));

		*output = std::move(result);
		last_error_.clear();
		return HsmStatus::kOk;
	}

	HsmStatus fail(HsmStatus status, const std::string &error)
	{
		last_error_ = error;
		return status;
	}

	UtimacoClientConfig config_;
	UtimacoTransport transport_;
	bool connected_ = false;
	std::string last_error_;
};

UtimacoClient::UtimacoClient(UtimacoClientConfig config)
	: UtimacoClient(std::move(config), {})
{
}

UtimacoClient::UtimacoClient(UtimacoClientConfig config,
			     UtimacoTransport transport)
	: impl_(std::make_unique<Impl>(std::move(config), std::move(transport)))
{
}

UtimacoClient::~UtimacoClient() = default;

UtimacoClient::UtimacoClient(UtimacoClient &&) noexcept = default;

UtimacoClient &UtimacoClient::operator=(UtimacoClient &&) noexcept = default;

HsmStatus UtimacoClient::connect()
{
	return impl_->connect();
}

void UtimacoClient::disconnect()
{
	impl_->disconnect();
}

HsmStatus UtimacoClient::generateKey(const HsmKeySpec &spec, HsmKeyHandle *key)
{
	return impl_->generateKey(spec, key);
}

HsmStatus UtimacoClient::encrypt(const HsmCipherRequest &request,
				 std::vector<uint8_t> *ciphertext)
{
	return impl_->encrypt(request, ciphertext);
}

HsmStatus UtimacoClient::decrypt(const HsmCipherRequest &request,
				 std::vector<uint8_t> *plaintext)
{
	return impl_->decrypt(request, plaintext);
}

bool UtimacoClient::connected() const
{
	return impl_->connected();
}

const std::string &UtimacoClient::lastError() const
{
	return impl_->lastError();
}

const UtimacoClientConfig &UtimacoClient::config() const
{
	return impl_->config();
}

} // namespace omnisight::embedded::pos::hsm

#if defined(OMNISIGHT_UTIMACO_CLIENT_SMOKE_MAIN)
int main()
{
	using namespace omnisight::embedded::pos::hsm;

	UtimacoClientConfig config;
	config.backend = UtimacoBackend::kPkcs11;
	config.slot = "0";
	config.user_pin = "smoke-pin";

	std::string transport_error;
	UtimacoTransport transport;
	transport.connect = [](const UtimacoClientConfig &) {
		return HsmStatus::kOk;
	};
	transport.disconnect = []() {};
	transport.generate_key = [](const HsmKeySpec &spec, HsmKeyHandle *key) {
		key->id = 1;
		key->label = spec.label;
		return HsmStatus::kOk;
	};
	transport.encrypt = [](const HsmCipherRequest &request,
			       std::vector<uint8_t> *output) {
		*output = request.input;
		std::reverse(output->begin(), output->end());
		return HsmStatus::kOk;
	};
	transport.decrypt = transport.encrypt;
	transport.last_error = [&transport_error]() {
		return transport_error.c_str();
	};

	UtimacoClient client(config, transport);
	if (client.connect() != HsmStatus::kOk) {
		std::cerr << "utimaco-client connect failed: "
			  << client.lastError() << '\n';
		return 1;
	}

	HsmKeySpec spec;
	spec.label = "smoke-aes";
	HsmKeyHandle key;
	if (client.generateKey(spec, &key) != HsmStatus::kOk || !key) {
		std::cerr << "utimaco-client key generation failed: "
			  << client.lastError() << '\n';
		return 1;
	}

	HsmCipherRequest request;
	request.key = key;
	request.input = { 0x01, 0x02, 0x03, 0x04 };

	std::vector<uint8_t> ciphertext;
	if (client.encrypt(request, &ciphertext) != HsmStatus::kOk ||
	    ciphertext != std::vector<uint8_t>({ 0x04, 0x03, 0x02, 0x01 })) {
		std::cerr << "utimaco-client encrypt smoke failed: "
			  << client.lastError() << '\n';
		return 1;
	}

	request.input = ciphertext;
	std::vector<uint8_t> plaintext;
	if (client.decrypt(request, &plaintext) != HsmStatus::kOk ||
	    plaintext != std::vector<uint8_t>({ 0x01, 0x02, 0x03, 0x04 })) {
		std::cerr << "utimaco-client decrypt smoke failed: "
			  << client.lastError() << '\n';
		return 1;
	}

	client.disconnect();
	return client.connected() ? 1 : 0;
}
#endif
