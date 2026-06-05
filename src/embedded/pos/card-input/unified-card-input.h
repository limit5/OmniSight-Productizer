/* SPDX-License-Identifier: MIT
 *
 * Cases 6+7 POS unified card-input router (OP-2063).
 */
#ifndef OMNISIGHT_EMBEDDED_POS_CARD_INPUT_UNIFIED_CARD_INPUT_H_
#define OMNISIGHT_EMBEDDED_POS_CARD_INPUT_UNIFIED_CARD_INPUT_H_

#include "iso14443-contactless-reader.h"
#include "iso7816-contact-reader.h"
#include "msr-magstripe-driver.h"
#include "pn532-driver.h"
#include "pn5180-driver.h"

#include <cstdint>
#include <functional>
#include <string>
#include <vector>

namespace omnisight::embedded::pos::card_input {

enum class CardInputStatus {
	kOk = 0,
	kInvalidArgument,
	kCallbackUnavailable,
};

enum class CardInputSource {
	kIso7816Contact = 0,
	kIso14443Contactless,
	kPn532,
	kPn5180,
	kMsr,
};

struct CardInputEvent {
	CardInputSource source = CardInputSource::kIso7816Contact;

	bool has_track_data = false;
	msr::MsrTrackData track_data;

	bool has_atr = false;
	std::vector<uint8_t> atr;
	bool has_atr_info = false;
	emv::AtrInfo atr_info;

	bool has_iso14443_card = false;
	emv::Iso14443CardInfo iso14443_card;

	bool has_pn532_target = false;
	nfc::Pn532TargetInfo pn532_target;

	bool has_pn5180_target = false;
	nfc::Pn5180TargetInfo pn5180_target;
};

class CardInputRouter {
public:
	using CardEventCallback = std::function<void(const CardInputEvent &)>;

	void onCardEvent(CardEventCallback callback);

	CardInputStatus routeIso7816Contact(const std::vector<uint8_t> &atr,
					    const emv::AtrInfo &atr_info);
	CardInputStatus routeIso7816Contact(const emv::AtrInfo &atr_info);
	CardInputStatus routeIso14443Contactless(
		const emv::Iso14443CardInfo &card);
	CardInputStatus routePn532Target(const nfc::Pn532TargetInfo &target);
	CardInputStatus routePn5180Target(const nfc::Pn5180TargetInfo &target);
	CardInputStatus routeMsrSwipe(const msr::MsrTrackData &track_data);

	const CardInputEvent &lastEvent() const;
	const std::string &lastError() const;

private:
	CardInputStatus emit(CardInputEvent event);
	CardInputStatus fail(CardInputStatus status, const std::string &error);

	CardEventCallback callback_;
	CardInputEvent last_event_;
	std::string last_error_;
};

const char *toString(CardInputStatus status);
const char *toString(CardInputSource source);

} // namespace omnisight::embedded::pos::card_input

#endif // OMNISIGHT_EMBEDDED_POS_CARD_INPUT_UNIFIED_CARD_INPUT_H_
