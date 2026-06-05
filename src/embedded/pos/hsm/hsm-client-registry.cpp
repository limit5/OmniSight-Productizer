/* SPDX-License-Identifier: MIT
 *
 * Case 7 POS HSM client registry (OP-2050).
 */
#include "hsm-client-registry.h"

namespace omnisight::embedded::pos::hsm {

HsmClientRegistry &HsmClientRegistry::instance()
{
	static HsmClientRegistry registry;

	return registry;
}

bool HsmClientRegistry::registerClient(const char *vendor_id, HSMClient *client)
{
	if (!vendor_id || vendor_id[0] == '\0' || !client)
		return false;

	std::lock_guard<std::mutex> guard(lock_);

	return clients_.emplace(vendor_id, client).second;
}

HSMClient *HsmClientRegistry::findByVendor(const std::string &vendor_id) const
{
	std::lock_guard<std::mutex> guard(lock_);
	auto it = clients_.find(vendor_id);

	return it == clients_.end() ? nullptr : it->second;
}

} // namespace omnisight::embedded::pos::hsm
