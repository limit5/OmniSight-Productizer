/* SPDX-License-Identifier: MIT
 *
 * Case 7 Thales nShield HSM client wrapper (OP-2027).
 */
#ifndef OMNISIGHT_EMBEDDED_POS_HSM_THALES_NSHIELD_CLIENT_H_
#define OMNISIGHT_EMBEDDED_POS_HSM_THALES_NSHIELD_CLIENT_H_

#include "hsm-client.h"

#include <functional>
#include <memory>
#include <string>
#include <vector>

namespace omnisight::embedded::pos::hsm {

enum class ThalesNShieldProtocol {
	kPkcs11 = 0,
	kNFast,
};

struct ThalesNShieldConnection {
	ThalesNShieldProtocol protocol = ThalesNShieldProtocol::kPkcs11;
	std::string module_path;
	std::string slot_label;
	std::string operator_card_set;
	std::string world_path;
	std::string pin;
};

using ThalesNShieldConnectFn =
	std::function<HSMStatus(const ThalesNShieldConnection &, void **session)>;
using ThalesNShieldDisconnectFn = std::function<void(void *session)>;
using ThalesNShieldGenerateKeyFn =
	std::function<HSMStatus(void *session, const HSMKeySpec &spec,
				HSMKeyHandle *key)>;
using ThalesNShieldImportKeyFn =
	std::function<HSMStatus(void *session, const HSMKeySpec &spec,
				const std::vector<uint8_t> &wrapped_key,
				HSMKeyHandle *key)>;
using ThalesNShieldOperationFn =
	std::function<HSMStatus(void *session, const HSMOperationRequest &request,
				std::vector<uint8_t> *output)>;
using ThalesNShieldLastErrorFn = std::function<std::string(void *session)>;

struct ThalesNShieldBackend {
	ThalesNShieldConnectFn connect;
	ThalesNShieldDisconnectFn disconnect;
	ThalesNShieldGenerateKeyFn generate_key;
	ThalesNShieldImportKeyFn import_key;
	ThalesNShieldOperationFn encrypt;
	ThalesNShieldOperationFn decrypt;
	ThalesNShieldOperationFn sign;
	ThalesNShieldLastErrorFn last_error;
};

class ThalesNShieldClient final : public HSMClient {
public:
	ThalesNShieldClient(ThalesNShieldConnection connection,
			    ThalesNShieldBackend backend);
	~ThalesNShieldClient() override;

	ThalesNShieldClient(const ThalesNShieldClient &) = delete;
	ThalesNShieldClient &operator=(const ThalesNShieldClient &) = delete;
	ThalesNShieldClient(ThalesNShieldClient &&) noexcept;
	ThalesNShieldClient &operator=(ThalesNShieldClient &&) noexcept;

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

	const ThalesNShieldConnection &connection() const;

private:
	class Impl;

	std::unique_ptr<Impl> impl_;
};

const char *toString(ThalesNShieldProtocol protocol);

} // namespace omnisight::embedded::pos::hsm

#endif // OMNISIGHT_EMBEDDED_POS_HSM_THALES_NSHIELD_CLIENT_H_
