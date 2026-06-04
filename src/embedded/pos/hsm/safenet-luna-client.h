/* SPDX-License-Identifier: MIT
 *
 * Case 7 SafeNet Luna HSM client wrapper (OP-2043).
 */
#ifndef OMNISIGHT_EMBEDDED_POS_HSM_SAFENET_LUNA_CLIENT_H_
#define OMNISIGHT_EMBEDDED_POS_HSM_SAFENET_LUNA_CLIENT_H_

#include "hsm-client.h"

#include <functional>
#include <memory>
#include <string>
#include <vector>

namespace omnisight::embedded::pos::hsm {

enum class SafeNetLunaProtocol {
	kPkcs11 = 0,
	kLunaClient,
};

struct SafeNetLunaConnection {
	SafeNetLunaProtocol protocol = SafeNetLunaProtocol::kPkcs11;
	std::string module_path;
	std::string appliance;
	std::string partition;
	std::string slot_label;
	std::string role = "Crypto Officer";
	std::string pin;
	std::string sdk_root;
	bool prefer_static_sdk = true;
	bool container_fallback_allowed = true;
};

using SafeNetLunaConnectFn =
	std::function<HSMStatus(const SafeNetLunaConnection &, void **session)>;
using SafeNetLunaDisconnectFn = std::function<void(void *session)>;
using SafeNetLunaGenerateKeyFn =
	std::function<HSMStatus(void *session, const HSMKeySpec &spec,
				HSMKeyHandle *key)>;
using SafeNetLunaImportKeyFn =
	std::function<HSMStatus(void *session, const HSMKeySpec &spec,
				const std::vector<uint8_t> &wrapped_key,
				HSMKeyHandle *key)>;
using SafeNetLunaOperationFn =
	std::function<HSMStatus(void *session, const HSMOperationRequest &request,
				std::vector<uint8_t> *output)>;
using SafeNetLunaLastErrorFn = std::function<std::string(void *session)>;

struct SafeNetLunaBackend {
	SafeNetLunaConnectFn connect;
	SafeNetLunaDisconnectFn disconnect;
	SafeNetLunaGenerateKeyFn generate_key;
	SafeNetLunaImportKeyFn import_key;
	SafeNetLunaOperationFn encrypt;
	SafeNetLunaOperationFn decrypt;
	SafeNetLunaOperationFn sign;
	SafeNetLunaLastErrorFn last_error;
};

class SafeNetLunaClient final : public HSMClient {
public:
	SafeNetLunaClient(SafeNetLunaConnection connection,
			  SafeNetLunaBackend backend);
	~SafeNetLunaClient() override;

	SafeNetLunaClient(const SafeNetLunaClient &) = delete;
	SafeNetLunaClient &operator=(const SafeNetLunaClient &) = delete;
	SafeNetLunaClient(SafeNetLunaClient &&) noexcept;
	SafeNetLunaClient &operator=(SafeNetLunaClient &&) noexcept;

	HSMStatus connect() override;
	HSMStatus disconnect() override;
	bool connected() const override;
	HSMStatus generateKey(const HSMKeySpec &spec, HSMKeyHandle *key) override;
	HSMStatus importKey(const HSMKeySpec &spec,
			    const std::vector<uint8_t> &wrapped_key,
			    HSMKeyHandle *key) override;
	HSMStatus encrypt(const HSMOperationRequest &request,
			  std::vector<uint8_t> *ciphertext) override;
	HSMStatus decrypt(const HSMOperationRequest &request,
			  std::vector<uint8_t> *plaintext) override;
	HSMStatus sign(const HSMOperationRequest &request,
		       std::vector<uint8_t> *signature) override;
	const std::string &lastError() const override;

	const SafeNetLunaConnection &connection() const;

private:
	class Impl;

	std::unique_ptr<Impl> impl_;
};

const char *toString(SafeNetLunaProtocol protocol);

} // namespace omnisight::embedded::pos::hsm

#endif // OMNISIGHT_EMBEDDED_POS_HSM_SAFENET_LUNA_CLIENT_H_
