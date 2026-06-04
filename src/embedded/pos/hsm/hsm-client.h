/* SPDX-License-Identifier: MIT
 *
 * Case 7 POS HSM client interface (OP-2027).
 */
#ifndef OMNISIGHT_EMBEDDED_POS_HSM_HSM_CLIENT_H_
#define OMNISIGHT_EMBEDDED_POS_HSM_HSM_CLIENT_H_

#include <cstdint>
#include <string>
#include <vector>

namespace omnisight::embedded::pos::hsm {

enum class HSMStatus {
	kOk = 0,
	kInvalidArgument,
	kUnavailable,
	kInvalidState,
	kBackendError,
};

enum class HSMKeyAlgorithm {
	kAes256 = 0,
	kRsa2048,
	kRsa3072,
	kEcP256,
};

enum class HSMMechanism {
	kAesCbc = 0,
	kRsaOaepSha256,
	kEcdsaSha256,
};

struct HSMKeyHandle {
	uint64_t id = 0;
	std::string label;
	void *vendor_specific_ptr = nullptr;

	explicit operator bool() const
	{
		return id != 0 || vendor_specific_ptr != nullptr;
	}
};

struct HSMKeySpec {
	HSMKeyAlgorithm algorithm = HSMKeyAlgorithm::kAes256;
	std::string label;
	bool extractable = false;
};

struct HSMOperationRequest {
	HSMKeyHandle key;
	HSMMechanism mechanism = HSMMechanism::kAesCbc;
	std::vector<uint8_t> input;
	std::vector<uint8_t> iv;
};

class HSMClient {
public:
	virtual ~HSMClient() = default;

	virtual HSMStatus connect() = 0;
	virtual HSMStatus disconnect() = 0;
	virtual bool connected() const = 0;
	virtual HSMStatus generateKey(const HSMKeySpec &spec,
				      HSMKeyHandle *key) = 0;
	virtual HSMStatus importKey(const HSMKeySpec &spec,
				    const std::vector<uint8_t> &wrapped_key,
				    HSMKeyHandle *key) = 0;
	virtual HSMStatus encrypt(const HSMOperationRequest &request,
				  std::vector<uint8_t> *ciphertext) = 0;
	virtual HSMStatus decrypt(const HSMOperationRequest &request,
				  std::vector<uint8_t> *plaintext) = 0;
	virtual HSMStatus sign(const HSMOperationRequest &request,
			       std::vector<uint8_t> *signature) = 0;
	virtual const std::string &lastError() const = 0;
};

const char *toString(HSMStatus status);
const char *toString(HSMKeyAlgorithm algorithm);
const char *toString(HSMMechanism mechanism);

} // namespace omnisight::embedded::pos::hsm

#endif // OMNISIGHT_EMBEDDED_POS_HSM_HSM_CLIENT_H_
