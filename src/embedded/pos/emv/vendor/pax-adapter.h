/* SPDX-License-Identifier: MIT
 *
 * Case 7 PAX payment terminal vendor adapter (OP-2057).
 */
#ifndef OMNISIGHT_EMBEDDED_POS_EMV_VENDOR_PAX_ADAPTER_H_
#define OMNISIGHT_EMBEDDED_POS_EMV_VENDOR_PAX_ADAPTER_H_

#include "payment-terminal-adapter.h"

#include <memory>
#include <string>

namespace omnisight::embedded::pos::emv::vendor {

struct PaxAdapterConfig {
	std::string terminal_family = "Prolin";
	std::string merchant_id;
	std::string terminal_id;
	Iso7816ContactReader *contact_reader = nullptr;
	Iso14443ContactlessReader *contactless_reader = nullptr;
	PedDevice *ped = nullptr;
	EmvCryptogramGenerator *cryptogram_generator = nullptr;
};

class PaxAdapter : public PaymentTerminalAdapter {
public:
	explicit PaxAdapter(PaxAdapterConfig config);
	~PaxAdapter() override;

	PaxAdapter(const PaxAdapter &) = delete;
	PaxAdapter &operator=(const PaxAdapter &) = delete;
	PaxAdapter(PaxAdapter &&) noexcept;
	PaxAdapter &operator=(PaxAdapter &&) noexcept;

	PaymentTerminalStatus initialize() override;
	PaymentTerminalStatus shutdown() override;
	bool available() const override;
	PaymentTerminalStatus injectKey(
		const PaymentTerminalKeyCeremony &ceremony) override;
	PaymentTerminalStatus authorize(const PaymentTerminalRequest &request,
					PaymentTerminalResult *result) override;
	PaymentTerminalStatus handleTamper(
		const PaymentTerminalTamperEvent &event) override;
	void onEvent(PaymentTerminalEventCallback callback) override;
	const std::string &lastError() const override;

private:
	class Impl;

	std::unique_ptr<Impl> impl_;
};

} // namespace omnisight::embedded::pos::emv::vendor

#endif // OMNISIGHT_EMBEDDED_POS_EMV_VENDOR_PAX_ADAPTER_H_
