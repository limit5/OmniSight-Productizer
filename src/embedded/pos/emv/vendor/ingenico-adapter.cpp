/* SPDX-License-Identifier: MIT
 *
 * Case 7 Ingenico payment terminal vendor adapter (OP-2057).
 */
#include "ingenico-adapter.h"

#include <cstdint>
#include <cstdlib>
#include <memory>
#include <string>
#include <utility>
#include <vector>

#if defined(OMNISIGHT_INGENICO_WITH_TELIUM_SDK)
#include <ingenico_telgea.h>
#include <ingenico_telium.h>
#endif

namespace omnisight::embedded::pos::emv::vendor {
namespace {

constexpr uint64_t kFirstTransactionId = 1;
constexpr char kIngenicoTracePrefix[] = "ingenico:";

PaymentTerminalStatus invalid_argument(std::string *last_error,
				       const char *message)
{
	if (last_error)
		*last_error = message;
	return PaymentTerminalStatus::kInvalidArgument;
}

PaymentTerminalStatus invalid_state(std::string *last_error,
				    const char *message)
{
	if (last_error)
		*last_error = message;
	return PaymentTerminalStatus::kInvalidState;
}

PaymentTerminalStatus unavailable(std::string *last_error,
				  const char *message)
{
	if (last_error)
		*last_error = message;
	return PaymentTerminalStatus::kUnavailable;
}

PaymentTerminalStatus map_contact_status(Iso7816Status status)
{
	return status == Iso7816Status::kOk ?
		       PaymentTerminalStatus::kOk :
		       PaymentTerminalStatus::kReaderError;
}

PaymentTerminalStatus map_contactless_status(Iso14443Status status)
{
	return status == Iso14443Status::kOk ?
		       PaymentTerminalStatus::kOk :
		       PaymentTerminalStatus::kReaderError;
}

PaymentTerminalStatus map_ped_status(PedStatus status)
{
	return status == PedStatus::kOk ? PaymentTerminalStatus::kOk :
					 PaymentTerminalStatus::kPedError;
}

PaymentTerminalStatus map_cryptogram_status(EmvCryptogramStatus status)
{
	return status == EmvCryptogramStatus::kOk ?
		       PaymentTerminalStatus::kOk :
		       PaymentTerminalStatus::kCryptogramError;
}

bool valid_family(const std::string &family)
{
	return family == "Telium" || family == "Telium2" ||
	       family == "Tetra" || family == "TelGea";
}

} // namespace

class IngenicoAdapter::Impl {
public:
	explicit Impl(IngenicoAdapterConfig config) : config_(std::move(config))
	{
	}

	~Impl()
	{
		(void)shutdown();
	}

	PaymentTerminalStatus initialize()
	{
		if (!valid_config())
			return invalid_argument(
				&last_error_,
				"Ingenico terminal id, merchant id, PED, and cryptogram generator are required");
		if (!config_.contact_reader && !config_.contactless_reader)
			return invalid_argument(
				&last_error_,
				"at least one EMV reader is required");
		if (tamper_locked_)
			return invalid_state(&last_error_,
					     "Ingenico terminal is tamper locked");

#if defined(OMNISIGHT_INGENICO_WITH_TELIUM_SDK)
		const int open_status = ingenico_telium_open(
			config_.terminal_id.c_str(), config_.merchant_id.c_str(),
			&terminal_);
		if (open_status != 0 || !terminal_)
			return sdk_error(open_status,
					 "Telium terminal open failed");
#endif

		const PedStatus ped_status = config_.ped->initialize();
		if (ped_status != PedStatus::kOk) {
			last_error_ = "PED initialize failed: ";
			last_error_ += config_.ped->lastError();
			return map_ped_status(ped_status);
		}

		initialized_ = true;
		emit(PaymentTerminalStatus::kOk, "Ingenico terminal initialized");
		return PaymentTerminalStatus::kOk;
	}

	PaymentTerminalStatus shutdown()
	{
		if (!initialized_)
			return PaymentTerminalStatus::kOk;

		PaymentTerminalStatus status = PaymentTerminalStatus::kOk;
		const PedStatus ped_status = config_.ped->shutdown();
		if (ped_status != PedStatus::kOk) {
			last_error_ = "PED shutdown failed: ";
			last_error_ += config_.ped->lastError();
			status = map_ped_status(ped_status);
		}

#if defined(OMNISIGHT_INGENICO_WITH_TELIUM_SDK)
		if (terminal_) {
			const int close_status = ingenico_telium_close(terminal_);
			terminal_ = nullptr;
			if (close_status != 0)
				status = sdk_error(close_status,
						   "Telium terminal close failed");
		}
#endif

		initialized_ = false;
		emit(status, "Ingenico terminal shutdown");
		return status;
	}

	bool available() const
	{
		return true;
	}

	PaymentTerminalStatus injectKey(
		const PaymentTerminalKeyCeremony &ceremony)
	{
		if (!initialized_)
			return invalid_state(&last_error_,
					     "Ingenico terminal is not initialized");
		if (tamper_locked_)
			return invalid_state(&last_error_,
					     "Ingenico terminal is tamper locked");
		if (!ceremony.ped_key.slot)
			return invalid_argument(&last_error_,
						"Ingenico key slot is required");
		if (!valid_family(ceremony.family_id))
			return invalid_argument(
				&last_error_,
				"Ingenico key ceremony family is not supported");
		if (ceremony.ceremony_token.empty())
			return invalid_argument(
				&last_error_,
				"Ingenico key ceremony token is required");

#if defined(OMNISIGHT_INGENICO_WITH_TELIUM_SDK)
		if (ceremony.require_vendor_confirmation) {
			const int confirm_status = ingenico_telgea_confirm_key_injection(
				terminal_, ceremony.family_id.c_str(),
				ceremony.ceremony_token.data(),
				ceremony.ceremony_token.size());
			if (confirm_status != 0)
				return sdk_error(
					confirm_status,
					"TelGea key ceremony confirmation failed");
		}
#endif

		const PedStatus ped_status = config_.ped->injectKey(ceremony.ped_key);
		if (ped_status != PedStatus::kOk) {
			last_error_ = "Ingenico PED key injection failed: ";
			last_error_ += config_.ped->lastError();
			return map_ped_status(ped_status);
		}

		emit(PaymentTerminalStatus::kOk,
		     "Ingenico family key ceremony completed");
		return PaymentTerminalStatus::kOk;
	}

	PaymentTerminalStatus authorize(const PaymentTerminalRequest &request,
					PaymentTerminalResult *result)
	{
		if (!result)
			return invalid_argument(&last_error_,
						"authorization result is null");
		if (!initialized_)
			return invalid_state(&last_error_,
					     "Ingenico terminal is not initialized");
		if (tamper_locked_)
			return invalid_state(&last_error_,
					     "Ingenico terminal is tamper locked");

		PaymentTerminalResult pending;
		pending.handle = {next_transaction_id_++, this};
		pending.card_interface = request.card_interface;

		PaymentTerminalStatus status = exchange_application_select(
			request, &pending.application_response);
		if (status != PaymentTerminalStatus::kOk)
			return status;

		if (request.require_pin_block) {
			const PedStatus ped_status =
				config_.ped->captureEncryptedPinBlock(
					request.pin_entry, &pending.pin_block);
			if (ped_status != PedStatus::kOk) {
				last_error_ = "Ingenico PED PIN capture failed: ";
				last_error_ += config_.ped->lastError();
				return map_ped_status(ped_status);
			}
		}

		const EmvCryptogramStatus cryptogram_status =
			config_.cryptogram_generator->generate(request.cryptogram,
							       &pending.cryptogram);
		status = map_cryptogram_status(cryptogram_status);
		if (status != PaymentTerminalStatus::kOk) {
			last_error_ = "Ingenico cryptogram generation failed: ";
			last_error_ += config_.cryptogram_generator->lastError();
			return status;
		}

		pending.vendor_trace = kIngenicoTracePrefix;
		pending.vendor_trace += config_.terminal_family;
		pending.vendor_trace += ":";
		pending.vendor_trace += config_.terminal_id;
		*result = std::move(pending);
		emit(PaymentTerminalStatus::kOk,
		     "Ingenico authorization path completed");
		return PaymentTerminalStatus::kOk;
	}

	PaymentTerminalStatus handleTamper(
		const PaymentTerminalTamperEvent &event)
	{
		if (event.reason.empty())
			return invalid_argument(&last_error_,
						"Ingenico tamper reason is required");

#if defined(OMNISIGHT_INGENICO_WITH_TELIUM_SDK)
		const int tamper_status = ingenico_telium_report_tamper(
			terminal_, static_cast<int>(event.action),
			event.reason.c_str(), event.vendor_evidence.data(),
			event.vendor_evidence.size());
		if (tamper_status != 0)
			return sdk_error(tamper_status,
					 "Telium tamper report failed");
#endif

		if (event.action == PaymentTerminalTamperAction::kLockTerminal ||
		    event.action == PaymentTerminalTamperAction::kZeroizeKeys) {
			tamper_locked_ = true;
			(void)config_.ped->shutdown();
		}

		last_error_ = event.reason;
		emit(PaymentTerminalStatus::kTamperDetected, event.reason);
		return PaymentTerminalStatus::kTamperDetected;
	}

	void onEvent(PaymentTerminalEventCallback callback)
	{
		callback_ = std::move(callback);
	}

	const std::string &lastError() const
	{
		return last_error_;
	}

private:
	bool valid_config() const
	{
		return !config_.terminal_id.empty() &&
		       !config_.merchant_id.empty() &&
		       valid_family(config_.terminal_family) && config_.ped &&
		       config_.cryptogram_generator;
	}

	PaymentTerminalStatus exchange_application_select(
		const PaymentTerminalRequest &request, ApduResponse *response)
	{
		if (request.card_interface == PaymentTerminalCardInterface::kContact) {
			if (!config_.contact_reader)
				return unavailable(&last_error_,
						   "Ingenico contact reader is not configured");
			AtrInfo atr;
			const Iso7816Status reset_status =
				config_.contact_reader->reset(&atr);
			if (reset_status != Iso7816Status::kOk) {
				last_error_ = "Ingenico contact reset failed: ";
				last_error_ += config_.contact_reader->lastError();
				return map_contact_status(reset_status);
			}
			const Iso7816Status exchange_status =
				config_.contact_reader->exchangeApdu(
					request.select_application, response);
			if (exchange_status != Iso7816Status::kOk) {
				last_error_ = "Ingenico contact APDU failed: ";
				last_error_ += config_.contact_reader->lastError();
			}
			return map_contact_status(exchange_status);
		}

		if (!config_.contactless_reader)
			return unavailable(&last_error_,
					   "Ingenico contactless reader is not configured");
		Iso14443CardInfo card;
		const Iso14443Status activate_status =
			config_.contactless_reader->activate(&card);
		if (activate_status != Iso14443Status::kOk) {
			last_error_ = "Ingenico contactless activation failed: ";
			last_error_ += config_.contactless_reader->lastError();
			return map_contactless_status(activate_status);
		}
		const Iso14443Status exchange_status =
			config_.contactless_reader->exchangeApdu(
				request.select_application, response);
		if (exchange_status != Iso14443Status::kOk) {
			last_error_ = "Ingenico contactless APDU failed: ";
			last_error_ += config_.contactless_reader->lastError();
		}
		return map_contactless_status(exchange_status);
	}

	void emit(PaymentTerminalStatus status, const std::string &detail)
	{
		if (callback_)
			callback_(status, detail);
	}

#if defined(OMNISIGHT_INGENICO_WITH_TELIUM_SDK)
	PaymentTerminalStatus sdk_error(int status, const char *message)
	{
		last_error_ = message;
		if (status != 0)
			last_error_ += " status=" + std::to_string(status);
		emit(PaymentTerminalStatus::kBackendError, last_error_);
		return PaymentTerminalStatus::kBackendError;
	}
#endif

	IngenicoAdapterConfig config_;
	PaymentTerminalEventCallback callback_;
	std::string last_error_;
	bool initialized_ = false;
	bool tamper_locked_ = false;
	uint64_t next_transaction_id_ = kFirstTransactionId;
#if defined(OMNISIGHT_INGENICO_WITH_TELIUM_SDK)
	IngenicoTeliumTerminal *terminal_ = nullptr;
#endif
};

IngenicoAdapter::IngenicoAdapter(IngenicoAdapterConfig config)
	: impl_(std::make_unique<Impl>(std::move(config)))
{
}

IngenicoAdapter::~IngenicoAdapter() = default;

IngenicoAdapter::IngenicoAdapter(IngenicoAdapter &&) noexcept = default;

IngenicoAdapter &IngenicoAdapter::operator=(IngenicoAdapter &&) noexcept =
	default;

PaymentTerminalStatus IngenicoAdapter::initialize()
{
	return impl_->initialize();
}

PaymentTerminalStatus IngenicoAdapter::shutdown()
{
	return impl_->shutdown();
}

bool IngenicoAdapter::available() const
{
	return impl_->available();
}

PaymentTerminalStatus IngenicoAdapter::injectKey(
	const PaymentTerminalKeyCeremony &ceremony)
{
	return impl_->injectKey(ceremony);
}

PaymentTerminalStatus IngenicoAdapter::authorize(
	const PaymentTerminalRequest &request, PaymentTerminalResult *result)
{
	return impl_->authorize(request, result);
}

PaymentTerminalStatus IngenicoAdapter::handleTamper(
	const PaymentTerminalTamperEvent &event)
{
	return impl_->handleTamper(event);
}

void IngenicoAdapter::onEvent(PaymentTerminalEventCallback callback)
{
	impl_->onEvent(std::move(callback));
}

const std::string &IngenicoAdapter::lastError() const
{
	return impl_->lastError();
}

} // namespace omnisight::embedded::pos::emv::vendor

#if defined(OMNISIGHT_INGENICO_ADAPTER_SMOKE_MAIN)
int main()
{
	using namespace omnisight::embedded::pos::emv;
	using namespace omnisight::embedded::pos::emv::vendor;

	Iso7816ContactReader contact({
		[]() { return true; },
		[](std::vector<uint8_t> *atr) {
			*atr = {0x3b, 0x00};
			return Iso7816Status::kOk;
		},
		[](const std::vector<uint8_t> &, std::vector<uint8_t> *response) {
			*response = {0x6f, 0x02, 0x84, 0x00, 0x90, 0x00};
			return Iso7816Status::kOk;
		},
	});
	InMemoryPedDevice ped;
	EmvCryptogramGenerator generator(
		[](const EmvCryptogramSignRequest &request,
		   std::vector<uint8_t> *signature) {
			if (!signature || request.message.empty())
				return EmvCryptogramStatus::kInvalidArgument;
			signature->assign(16, request.message.back());
			(*signature)[0] = 0x3d;
			return EmvCryptogramStatus::kOk;
		});

	IngenicoAdapter adapter({
		"Telium",
		"merchant-smoke",
		"ingenico-smoke",
		&contact,
		nullptr,
		&ped,
		&generator,
	});

	if (adapter.initialize() != PaymentTerminalStatus::kOk)
		return EXIT_FAILURE;

	PaymentTerminalKeyCeremony ceremony;
	ceremony.family_id = "Telium";
	ceremony.ceremony_token = {0x01, 0x02, 0x03, 0x04};
	ceremony.require_vendor_confirmation = false;
	ceremony.ped_key.slot.id = 7;
	ceremony.ped_key.slot.label = "ingenico-smoke-key";
	ceremony.ped_key.wrapped_key = {0x10, 0x32, 0x54, 0x76,
					0x98, 0xba, 0xdc, 0xfe};
	if (adapter.injectKey(ceremony) != PaymentTerminalStatus::kOk)
		return EXIT_FAILURE;

	ped.seedPinEntryForTests("1234");

	PaymentTerminalRequest request;
	request.select_application = {0x00, 0xa4, 0x04, 0x00,
				      {0xa0, 0x00, 0x00, 0x00, 0x03}, 0};
	request.pin_entry.key_slot = ceremony.ped_key.slot;
	request.pin_entry.pan = "4761739001010010";
	request.cryptogram.application_transaction_counter = 0x1234;
	request.cryptogram.cdol_data = {0x00, 0x00, 0x00, 0x00,
					0x10, 0x00, 0x00, 0x00};
	request.cryptogram.icc_data = {{0x9f37, {0xde, 0xad, 0xbe, 0xef}}};

	PaymentTerminalResult result;
	if (adapter.authorize(request, &result) != PaymentTerminalStatus::kOk)
		return EXIT_FAILURE;
	if (!result.handle || result.application_response.sw1 != 0x90 ||
	    result.cryptogram.cryptogram.empty())
		return EXIT_FAILURE;

	PaymentTerminalTamperEvent tamper;
	tamper.action = PaymentTerminalTamperAction::kReportOnly;
	tamper.reason = "smoke tamper report";
	if (adapter.handleTamper(tamper) !=
	    PaymentTerminalStatus::kTamperDetected)
		return EXIT_FAILURE;

	if (adapter.shutdown() != PaymentTerminalStatus::kOk)
		return EXIT_FAILURE;

	return EXIT_SUCCESS;
}
#endif
