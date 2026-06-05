/* SPDX-License-Identifier: MIT
 *
 * Cases 6+7 POS unified card-input router (OP-2063).
 */
#include "unified-card-input.h"

#include <utility>

namespace omnisight::embedded::pos::card_input {

void CardInputRouter::onCardEvent(CardEventCallback callback)
{
	callback_ = std::move(callback);
}

CardInputStatus
CardInputRouter::routeIso7816Contact(const std::vector<uint8_t> &atr,
				     const emv::AtrInfo &atr_info)
{
	CardInputEvent event;

	event.source = CardInputSource::kIso7816Contact;
	event.has_atr = true;
	event.atr = atr;
	event.has_atr_info = true;
	event.atr_info = atr_info;
	return emit(std::move(event));
}

CardInputStatus
CardInputRouter::routeIso7816Contact(const emv::AtrInfo &atr_info)
{
	CardInputEvent event;

	event.source = CardInputSource::kIso7816Contact;
	event.has_atr_info = true;
	event.atr_info = atr_info;
	return emit(std::move(event));
}

CardInputStatus CardInputRouter::routeIso14443Contactless(
	const emv::Iso14443CardInfo &card)
{
	CardInputEvent event;

	event.source = CardInputSource::kIso14443Contactless;
	event.has_iso14443_card = true;
	event.iso14443_card = card;
	return emit(std::move(event));
}

CardInputStatus
CardInputRouter::routePn532Target(const nfc::Pn532TargetInfo &target)
{
	if (!target)
		return fail(CardInputStatus::kInvalidArgument,
			    "PN532 target is empty");

	CardInputEvent event;

	event.source = CardInputSource::kPn532;
	event.has_pn532_target = true;
	event.pn532_target = target;
	return emit(std::move(event));
}

CardInputStatus
CardInputRouter::routePn5180Target(const nfc::Pn5180TargetInfo &target)
{
	CardInputEvent event;

	event.source = CardInputSource::kPn5180;
	event.has_pn5180_target = true;
	event.pn5180_target = target;
	return emit(std::move(event));
}

CardInputStatus
CardInputRouter::routeMsrSwipe(const msr::MsrTrackData &track_data)
{
	if (track_data.empty())
		return fail(CardInputStatus::kInvalidArgument,
			    "MSR track data is empty");

	CardInputEvent event;

	event.source = CardInputSource::kMsr;
	event.has_track_data = true;
	event.track_data = track_data;
	return emit(std::move(event));
}

const CardInputEvent &CardInputRouter::lastEvent() const
{
	return last_event_;
}

const std::string &CardInputRouter::lastError() const
{
	return last_error_;
}

CardInputStatus CardInputRouter::emit(CardInputEvent event)
{
	if (!callback_)
		return fail(CardInputStatus::kCallbackUnavailable,
			    "card-input callback is not configured");

	last_event_ = std::move(event);
	last_error_.clear();
	callback_(last_event_);
	return CardInputStatus::kOk;
}

CardInputStatus CardInputRouter::fail(CardInputStatus status,
				      const std::string &error)
{
	last_error_ = error;
	return status;
}

const char *toString(CardInputStatus status)
{
	switch (status) {
	case CardInputStatus::kOk:
		return "ok";
	case CardInputStatus::kInvalidArgument:
		return "invalid-argument";
	case CardInputStatus::kCallbackUnavailable:
		return "callback-unavailable";
	}

	return "unknown";
}

const char *toString(CardInputSource source)
{
	switch (source) {
	case CardInputSource::kIso7816Contact:
		return "iso7816-contact";
	case CardInputSource::kIso14443Contactless:
		return "iso14443-contactless";
	case CardInputSource::kPn532:
		return "pn532";
	case CardInputSource::kPn5180:
		return "pn5180";
	case CardInputSource::kMsr:
		return "msr";
	}

	return "unknown";
}

} // namespace omnisight::embedded::pos::card_input

#if defined(OMNISIGHT_POS_CARD_INPUT_ROUTER_SMOKE_MAIN)
#include <cstdlib>
#include <iostream>

namespace {

using omnisight::embedded::pos::card_input::CardInputEvent;
using omnisight::embedded::pos::card_input::CardInputRouter;
using omnisight::embedded::pos::card_input::CardInputSource;
using omnisight::embedded::pos::card_input::CardInputStatus;
using omnisight::embedded::pos::emv::AtrInfo;
using omnisight::embedded::pos::emv::Iso14443CardInfo;
using omnisight::embedded::pos::emv::Iso14443CardType;
using omnisight::embedded::pos::msr::MsrTrack;
using omnisight::embedded::pos::msr::MsrTrackData;
using omnisight::embedded::pos::msr::MsrTrackNumber;
using omnisight::embedded::pos::nfc::Pn532TargetInfo;
using omnisight::embedded::pos::nfc::Pn532TargetType;
using omnisight::embedded::pos::nfc::Pn5180Protocol;
using omnisight::embedded::pos::nfc::Pn5180TargetInfo;

bool expect(bool value, const char *message)
{
	if (value)
		return true;

	std::cerr << message << '\n';
	return false;
}

} // namespace

int main()
{
	CardInputRouter router;
	CardInputEvent last_event;
	int callback_count = 0;

	if (!expect(router.routeMsrSwipe(MsrTrackData()) ==
			    CardInputStatus::kInvalidArgument,
		    "empty MSR swipe should be rejected"))
		return EXIT_FAILURE;

	router.onCardEvent([&last_event, &callback_count](
				   const CardInputEvent &event) {
		last_event = event;
		++callback_count;
	});

	AtrInfo atr_info;
	atr_info.ts = 0x3b;
	if (router.routeIso7816Contact({0x3b, 0x00}, atr_info) !=
	    CardInputStatus::kOk)
		return EXIT_FAILURE;
	if (!expect(last_event.source == CardInputSource::kIso7816Contact &&
			    last_event.has_atr && last_event.atr.size() == 2 &&
			    last_event.has_atr_info,
		    "ISO7816 event was not routed"))
		return EXIT_FAILURE;

	Iso14443CardInfo card;
	card.type = Iso14443CardType::kTypeA;
	card.type_a.uid = {0x04, 0xde, 0xad, 0xbe};
	if (router.routeIso14443Contactless(card) != CardInputStatus::kOk)
		return EXIT_FAILURE;
	if (!expect(last_event.source == CardInputSource::kIso14443Contactless &&
			    last_event.has_iso14443_card &&
			    last_event.iso14443_card.type_a.uid.size() == 4,
		    "ISO14443 event was not routed"))
		return EXIT_FAILURE;

	Pn532TargetInfo pn532;
	pn532.type = Pn532TargetType::kIso14443A;
	pn532.target_number = 1;
	pn532.uid = {0xde, 0xad, 0xbe, 0xef};
	if (router.routePn532Target(pn532) != CardInputStatus::kOk)
		return EXIT_FAILURE;
	if (!expect(last_event.source == CardInputSource::kPn532 &&
			    last_event.has_pn532_target &&
			    last_event.pn532_target.uid.size() == 4,
		    "PN532 event was not routed"))
		return EXIT_FAILURE;

	Pn5180TargetInfo pn5180;
	pn5180.protocol = Pn5180Protocol::kIso14443A;
	pn5180.uid = {0x04, 0xa1};
	if (router.routePn5180Target(pn5180) != CardInputStatus::kOk)
		return EXIT_FAILURE;
	if (!expect(last_event.source == CardInputSource::kPn5180 &&
			    last_event.has_pn5180_target &&
			    last_event.pn5180_target.uid.size() == 2,
		    "PN5180 event was not routed"))
		return EXIT_FAILURE;

	MsrTrackData swipe;
	swipe.raw =
		"%B4111111111111111^CARDHOLDER/TEST^25121010000000000000?";
	MsrTrack track;
	track.number = MsrTrackNumber::kTrack1;
	track.raw = swipe.raw;
	track.payload =
		"B4111111111111111^CARDHOLDER/TEST^25121010000000000000";
	swipe.tracks.push_back(track);
	if (router.routeMsrSwipe(swipe) != CardInputStatus::kOk)
		return EXIT_FAILURE;
	if (!expect(last_event.source == CardInputSource::kMsr &&
			    last_event.has_track_data &&
			    last_event.track_data.tracks.size() == 1,
		    "MSR event was not routed"))
		return EXIT_FAILURE;

	if (!expect(callback_count == 5, "unexpected callback count"))
		return EXIT_FAILURE;

	return EXIT_SUCCESS;
}
#endif
