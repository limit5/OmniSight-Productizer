/*
 * [OP-2060] Case 7 unified HSM client integration helper.
 *
 * The shell wrapper owns qemu execution. This helper walks the test-local
 * HsmClientRegistry and verifies the key-generate/encrypt/decrypt contract
 * across Thales nShield, Utimaco SecurityServer, and SafeNet Luna adapters
 * without requiring real HSM hardware.
 */

#include "hsm-sdk-mock.h"

#include <algorithm>
#include <iostream>
#include <string>
#include <vector>

namespace {

using omnisight::embedded::pos::hsm::HSMKeyAlgorithm;
using omnisight::embedded::pos::hsm::HSMKeyHandle;
using omnisight::embedded::pos::hsm::HSMKeySpec;
using omnisight::embedded::pos::hsm::HSMMechanism;
using omnisight::embedded::pos::hsm::HSMOperationRequest;
using omnisight::embedded::pos::hsm::HSMStatus;
using omnisight::embedded::pos::hsm::mock::HsmClientRegistry;

constexpr int kExpectedVendorCount = 3;

static int require_status(bool ok, const std::string &vendor,
			  const char *message)
{
	if (ok)
		return 0;

	std::cerr << "phase7-hsm-test: " << vendor << ": " << message << "\n";
	return 1;
}

static bool has_vendor(const std::vector<std::string> &vendors,
		       const char *vendor)
{
	return std::find(vendors.begin(), vendors.end(), vendor) != vendors.end();
}

static int exercise_vendor(const char *registry_vendor)
{
	const auto &registry = HsmClientRegistry::registeredVendors();
	const auto it = std::find_if(registry.begin(), registry.end(),
				     [registry_vendor](const auto &entry) {
					     return entry.vendor == std::string(registry_vendor);
				     });

	if (it == registry.end())
		return require_status(false, registry_vendor,
				      "vendor is missing from HsmClientRegistry");

	auto client = it->create();
	const std::string vendor = client->vendor();
	const std::vector<uint8_t> plaintext = {
		0x10, 0x20, 0x30, 0x40, 0x50, 0x60,
	};
	const std::vector<uint8_t> iv = {
		0x01, 0x02, 0x03, 0x04, 0x05, 0x06,
		0x07, 0x08, 0x09, 0x0a, 0x0b, 0x0c,
		0x0d, 0x0e, 0x0f, 0x10,
	};

	HSMKeySpec spec;
	spec.algorithm = HSMKeyAlgorithm::kAes256;
	spec.label = vendor + "-op2060-round-trip";
	spec.extractable = false;

	HSMKeyHandle key;
	std::vector<uint8_t> ciphertext;
	std::vector<uint8_t> decrypted;

	if (client->connect() != HSMStatus::kOk || !client->connected())
		return require_status(false, vendor, "connect failed");
	if (client->generateKey(spec, &key) != HSMStatus::kOk || !key)
		return require_status(false, vendor, "generateKey failed");
	if (key.label != spec.label)
		return require_status(false, vendor,
				      "generateKey did not preserve key label");

	HSMOperationRequest encrypt_request;
	encrypt_request.key = key;
	encrypt_request.mechanism = HSMMechanism::kAesCbc;
	encrypt_request.input = plaintext;
	encrypt_request.iv = iv;

	if (client->encrypt(encrypt_request, &ciphertext) != HSMStatus::kOk)
		return require_status(false, vendor, "encrypt failed");
	if (ciphertext.empty())
		return require_status(false, vendor, "encrypt returned no data");
	if (ciphertext == plaintext)
		return require_status(false, vendor,
				      "encrypt returned plaintext unchanged");

	HSMOperationRequest decrypt_request;
	decrypt_request.key = key;
	decrypt_request.mechanism = HSMMechanism::kAesCbc;
	decrypt_request.input = ciphertext;
	decrypt_request.iv = iv;

	if (client->decrypt(decrypt_request, &decrypted) != HSMStatus::kOk)
		return require_status(false, vendor, "decrypt failed");
	if (decrypted != plaintext)
		return require_status(false, vendor,
				      "decrypt did not restore original plaintext");
	if (client->disconnect() != HSMStatus::kOk || client->connected())
		return require_status(false, vendor, "disconnect failed");

	std::cout << "phase7-hsm-test: " << vendor
		  << " key-generate/encrypt/decrypt contract passed\n";
	return 0;
}

} // namespace

int main()
{
	const auto &registry = HsmClientRegistry::registeredVendors();
	std::vector<std::string> vendors;

	for (const auto &entry : registry)
		vendors.emplace_back(entry.vendor);

	if (require_status(static_cast<int>(registry.size()) == kExpectedVendorCount,
			   "registry", "expected exactly 3 registered HSM vendors") ||
	    require_status(has_vendor(vendors, "thales"), "registry",
			   "Thales vendor is not registered") ||
	    require_status(has_vendor(vendors, "utimaco"), "registry",
			   "Utimaco vendor is not registered") ||
	    require_status(has_vendor(vendors, "safenet"), "registry",
			   "SafeNet vendor is not registered"))
		return 1;

	for (const char *vendor : { "thales", "utimaco", "safenet" }) {
		if (exercise_vendor(vendor))
			return 1;
	}

	std::cout << "phase7-hsm-test: unified HSM registry contract passed for "
		  << registry.size() << " vendors\n";
	return 0;
}
