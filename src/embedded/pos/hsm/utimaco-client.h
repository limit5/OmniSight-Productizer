/* SPDX-License-Identifier: MIT
 *
 * Case 7 Utimaco SecurityServer HSM client wrapper (OP-2036).
 */
#ifndef OMNISIGHT_EMBEDDED_POS_HSM_UTIMACO_CLIENT_H_
#define OMNISIGHT_EMBEDDED_POS_HSM_UTIMACO_CLIENT_H_

#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <vector>

namespace omnisight::embedded::pos::hsm {

enum class HsmStatus {
	kOk = 0,
	kInvalidArgument,
	kUnavailable,
	kInvalidState,
	kBackendError,
};

enum class UtimacoBackend {
	kCxi = 0,
	kPkcs11,
};

enum class HsmKeyAlgorithm {
	kAes = 0,
	kTdes,
	kRsa,
};

enum class HsmCipherMode {
	kEcb = 0,
	kCbc,
	kGcm,
};

struct UtimacoClientConfig {
	UtimacoBackend backend = UtimacoBackend::kPkcs11;
	std::string endpoint;
	std::string slot;
	std::string partition;
	std::string user_pin;
	std::string sdk_root;
	std::string glibc_abi = "glibc-2.31";
	bool prefer_static_sdk = true;
	bool container_fallback_allowed = true;
};

struct HsmKeySpec {
	HsmKeyAlgorithm algorithm = HsmKeyAlgorithm::kAes;
	uint16_t bits = 256;
	bool extractable = false;
	std::string label;
};

struct HsmKeyHandle {
	uint64_t id = 0;
	std::string label;
	void *vendor_specific_ptr = nullptr;

	explicit operator bool() const
	{
		return id != 0 || vendor_specific_ptr != nullptr;
	}
};

struct HsmCipherRequest {
	HsmKeyHandle key;
	HsmCipherMode mode = HsmCipherMode::kCbc;
	std::vector<uint8_t> iv;
	std::vector<uint8_t> aad;
	std::vector<uint8_t> input;
};

struct UtimacoTransport {
	std::function<HsmStatus(const UtimacoClientConfig &)> connect;
	std::function<void()> disconnect;
	std::function<HsmStatus(const HsmKeySpec &, HsmKeyHandle *)> generate_key;
	std::function<HsmStatus(const HsmCipherRequest &, std::vector<uint8_t> *)>
		encrypt;
	std::function<HsmStatus(const HsmCipherRequest &, std::vector<uint8_t> *)>
		decrypt;
	std::function<const char *()> last_error;
};

class UtimacoClient final {
public:
	explicit UtimacoClient(UtimacoClientConfig config);
	UtimacoClient(UtimacoClientConfig config, UtimacoTransport transport);
	~UtimacoClient();

	UtimacoClient(const UtimacoClient &) = delete;
	UtimacoClient &operator=(const UtimacoClient &) = delete;
	UtimacoClient(UtimacoClient &&) noexcept;
	UtimacoClient &operator=(UtimacoClient &&) noexcept;

	HsmStatus connect();
	void disconnect();
	HsmStatus generateKey(const HsmKeySpec &spec, HsmKeyHandle *key);
	HsmStatus encrypt(const HsmCipherRequest &request,
			  std::vector<uint8_t> *ciphertext);
	HsmStatus decrypt(const HsmCipherRequest &request,
			  std::vector<uint8_t> *plaintext);

	bool connected() const;
	const std::string &lastError() const;
	const UtimacoClientConfig &config() const;

private:
	class Impl;

	std::unique_ptr<Impl> impl_;
};

} // namespace omnisight::embedded::pos::hsm

#endif // OMNISIGHT_EMBEDDED_POS_HSM_UTIMACO_CLIENT_H_
