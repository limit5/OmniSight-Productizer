/* SPDX-License-Identifier: MIT
 *
 * Case 7 EMV PED PIN entry abstraction (OP-2041).
 */
#ifndef OMNISIGHT_EMBEDDED_POS_EMV_PED_PIN_ENTRY_H_
#define OMNISIGHT_EMBEDDED_POS_EMV_PED_PIN_ENTRY_H_

#include <array>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <string>
#include <vector>

namespace omnisight::embedded::pos::emv {

enum class PedStatus {
	kOk = 0,
	kInvalidArgument,
	kInvalidState,
	kKeyNotInjected,
	kEntryUnavailable,
	kCryptoError,
};

enum class PinBlockFormat {
	kIso9564Format0 = 0,
	kIso9564Format3,
};

enum class PedKeyAlgorithm {
	kTdesDukpt = 0,
	kAesDukpt,
};

struct PedKeySlot {
	uint32_t id = 0;
	std::string label;

	explicit operator bool() const
	{
		return id != 0 || !label.empty();
	}
};

struct PedKeyInjectionRequest {
	PedKeySlot slot;
	PedKeyAlgorithm algorithm = PedKeyAlgorithm::kTdesDukpt;
	std::vector<uint8_t> wrapped_key;
	std::vector<uint8_t> key_serial_number;
};

struct PedPinEntryRequest {
	PedKeySlot key_slot;
	PinBlockFormat format = PinBlockFormat::kIso9564Format0;
	std::string pan;
	std::size_t min_pin_length = 4;
	std::size_t max_pin_length = 12;
	uint32_t timeout_ms = 30000;
};

struct PedPinBlock {
	PedKeySlot key_slot;
	PinBlockFormat format = PinBlockFormat::kIso9564Format0;
	std::size_t pin_length = 0;
	std::array<uint8_t, 8> encrypted_block = {};
};

using PedPinBlockEncryptFn =
	std::function<PedStatus(const std::array<uint8_t, 8> &clear_block,
				const PedKeyInjectionRequest &key,
				std::array<uint8_t, 8> *encrypted_block)>;

class PedDevice {
public:
	virtual ~PedDevice() = default;

	virtual PedStatus initialize() = 0;
	virtual PedStatus shutdown() = 0;
	virtual bool ready() const = 0;
	virtual PedStatus injectKey(const PedKeyInjectionRequest &request) = 0;
	virtual PedStatus deleteKey(const PedKeySlot &slot) = 0;
	virtual PedStatus captureEncryptedPinBlock(
		const PedPinEntryRequest &request, PedPinBlock *pin_block) = 0;
	virtual const std::string &lastError() const = 0;
};

class InMemoryPedDevice final : public PedDevice {
public:
	InMemoryPedDevice() = default;
	explicit InMemoryPedDevice(PedPinBlockEncryptFn encrypt);

	PedStatus initialize() override;
	PedStatus shutdown() override;
	bool ready() const override;
	PedStatus injectKey(const PedKeyInjectionRequest &request) override;
	PedStatus deleteKey(const PedKeySlot &slot) override;
	PedStatus captureEncryptedPinBlock(const PedPinEntryRequest &request,
					   PedPinBlock *pin_block) override;
	const std::string &lastError() const override;

	void setPinBlockEncryptor(PedPinBlockEncryptFn encrypt);
	void seedPinEntryForTests(const std::string &pin);
	void clearSeededPinEntry();

	static PedStatus buildIso9564PinBlock(const std::string &pin,
					      const std::string &pan,
					      PinBlockFormat format,
					      std::array<uint8_t, 8> *block);

private:
	PedStatus fail(PedStatus status, const std::string &error) const;

	bool initialized_ = false;
	std::vector<PedKeyInjectionRequest> injected_keys_;
	std::string seeded_pin_;
	PedPinBlockEncryptFn encrypt_;
	mutable std::string last_error_;
};

const char *toString(PedStatus status);
const char *toString(PinBlockFormat format);
const char *toString(PedKeyAlgorithm algorithm);

} // namespace omnisight::embedded::pos::emv

#endif // OMNISIGHT_EMBEDDED_POS_EMV_PED_PIN_ENTRY_H_
