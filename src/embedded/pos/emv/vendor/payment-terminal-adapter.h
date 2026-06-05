/* SPDX-License-Identifier: MIT
 *
 * Case 7 EMV payment terminal vendor adapter interface (OP-2054).
 */
#ifndef OMNISIGHT_EMBEDDED_POS_EMV_VENDOR_PAYMENT_TERMINAL_ADAPTER_H_
#define OMNISIGHT_EMBEDDED_POS_EMV_VENDOR_PAYMENT_TERMINAL_ADAPTER_H_

#include "../cryptogram-generator.h"
#include "../iso14443-contactless-reader.h"
#include "../iso7816-contact-reader.h"
#include "../ped-pin-entry.h"

#include <cstdint>
#include <functional>
#include <string>
#include <vector>

namespace omnisight::embedded::pos::emv::vendor {

enum class PaymentTerminalStatus {
	kOk = 0,
	kInvalidArgument,
	kUnavailable,
	kInvalidState,
	kReaderError,
	kPedError,
	kCryptogramError,
	kTamperDetected,
	kBackendError,
};

enum class PaymentTerminalCardInterface {
	kContact = 0,
	kContactless,
};

enum class PaymentTerminalTamperAction {
	kReportOnly = 0,
	kLockTerminal,
	kZeroizeKeys,
};

struct PaymentTerminalHandle {
	uint64_t id = 0;
	void *vendor_specific_ptr = nullptr;

	explicit operator bool() const
	{
		return id != 0 || vendor_specific_ptr != nullptr;
	}
};

struct PaymentTerminalRequest {
	PaymentTerminalCardInterface card_interface =
		PaymentTerminalCardInterface::kContact;
	ApduCommand select_application;
	PedPinEntryRequest pin_entry;
	EmvCryptogramRequest cryptogram;
	bool require_pin_block = true;
};

struct PaymentTerminalResult {
	PaymentTerminalHandle handle;
	PaymentTerminalCardInterface card_interface =
		PaymentTerminalCardInterface::kContact;
	ApduResponse application_response;
	PedPinBlock pin_block;
	EmvCryptogramResult cryptogram;
	bool tamper_detected = false;
	std::string vendor_trace;
};

struct PaymentTerminalKeyCeremony {
	PedKeyInjectionRequest ped_key;
	std::string family_id;
	std::vector<uint8_t> ceremony_token;
	bool require_vendor_confirmation = true;
};

struct PaymentTerminalTamperEvent {
	PaymentTerminalTamperAction action =
		PaymentTerminalTamperAction::kLockTerminal;
	std::string reason;
	std::vector<uint8_t> vendor_evidence;
};

using PaymentTerminalEventCallback =
	std::function<void(PaymentTerminalStatus status,
			   const std::string &detail)>;

class PaymentTerminalAdapter {
public:
	virtual ~PaymentTerminalAdapter() = default;

	virtual PaymentTerminalStatus initialize() = 0;
	virtual PaymentTerminalStatus shutdown() = 0;
	virtual bool available() const = 0;
	virtual PaymentTerminalStatus injectKey(
		const PaymentTerminalKeyCeremony &ceremony) = 0;
	virtual PaymentTerminalStatus authorize(
		const PaymentTerminalRequest &request,
		PaymentTerminalResult *result) = 0;
	virtual PaymentTerminalStatus handleTamper(
		const PaymentTerminalTamperEvent &event) = 0;
	virtual void onEvent(PaymentTerminalEventCallback callback) = 0;
	virtual const std::string &lastError() const = 0;
};

const char *toString(PaymentTerminalStatus status);
const char *toString(PaymentTerminalCardInterface card_interface);
const char *toString(PaymentTerminalTamperAction action);

} // namespace omnisight::embedded::pos::emv::vendor

#endif // OMNISIGHT_EMBEDDED_POS_EMV_VENDOR_PAYMENT_TERMINAL_ADAPTER_H_
