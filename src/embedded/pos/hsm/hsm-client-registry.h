/* SPDX-License-Identifier: MIT
 *
 * Case 7 POS HSM client registry (OP-2050).
 */
#ifndef OMNISIGHT_EMBEDDED_POS_HSM_HSM_CLIENT_REGISTRY_H_
#define OMNISIGHT_EMBEDDED_POS_HSM_HSM_CLIENT_REGISTRY_H_

#include "hsm-client.h"

#include <mutex>
#include <string>
#include <unordered_map>

namespace omnisight::embedded::pos::hsm {

class HsmClientRegistry final {
public:
	static HsmClientRegistry &instance();

	HsmClientRegistry(const HsmClientRegistry &) = delete;
	HsmClientRegistry &operator=(const HsmClientRegistry &) = delete;

	bool registerClient(const char *vendor_id, HSMClient *client);
	HSMClient *findByVendor(const std::string &vendor_id) const;

private:
	HsmClientRegistry() = default;

	mutable std::mutex lock_;
	std::unordered_map<std::string, HSMClient *> clients_;
};

} // namespace omnisight::embedded::pos::hsm

#endif // OMNISIGHT_EMBEDDED_POS_HSM_HSM_CLIENT_REGISTRY_H_
