/* SPDX-License-Identifier: MIT
 *
 * Case 7 EMV ISO 14443 contactless smart-card reader interface (OP-2032).
 */
#include "iso14443-contactless-reader.h"

#include <cstddef>
#include <limits>
#include <utility>

namespace omnisight::embedded::pos::emv {
namespace {

static bool valid_uid_size(std::size_t size)
{
	return size == 4 || size == 7 || size == 10;
}

static bool valid_fsdi(uint8_t fsdi)
{
	return fsdi <= 8;
}

static bool valid_cid(uint8_t cid)
{
	return cid <= 14;
}

static Iso14443Status from_iso7816_status(Iso7816Status status)
{
	switch (status) {
	case Iso7816Status::kOk:
		return Iso14443Status::kOk;
	case Iso7816Status::kInvalidArgument:
		return Iso14443Status::kInvalidArgument;
	case Iso7816Status::kParseError:
		return Iso14443Status::kParseError;
	case Iso7816Status::kCardAbsent:
		return Iso14443Status::kCardAbsent;
	case Iso7816Status::kTransportError:
		return Iso14443Status::kTransportError;
	}

	return Iso14443Status::kTransportError;
}

} // namespace

Iso14443ContactlessReader::Iso14443ContactlessReader(
	Iso14443ContactlessReaderTransport transport)
	: transport_(std::move(transport))
{
}

Iso14443Status
Iso14443ContactlessReader::parseTypeBAtqb(const std::vector<uint8_t> &atqb,
					  Iso14443TypeBInfo *info)
{
	if (!info)
		return Iso14443Status::kInvalidArgument;
	if (atqb.size() < 12 || atqb[0] != 0x50)
		return Iso14443Status::kParseError;

	Iso14443TypeBInfo parsed;
	parsed.atqb = atqb;
	parsed.pupi.assign(atqb.begin() + 1, atqb.begin() + 5);
	parsed.application_data.assign(atqb.begin() + 5, atqb.begin() + 9);
	parsed.protocol_info.assign(atqb.begin() + 9, atqb.begin() + 12);
	*info = std::move(parsed);
	return Iso14443Status::kOk;
}

Iso14443Status
Iso14443ContactlessReader::parseAts(const std::vector<uint8_t> &ats,
				    AtsInfo *info)
{
	if (!info)
		return Iso14443Status::kInvalidArgument;
	if (ats.size() < 2 || ats.size() > std::numeric_limits<uint8_t>::max())
		return Iso14443Status::kParseError;
	if (ats[0] != ats.size())
		return Iso14443Status::kParseError;

	AtsInfo parsed;
	parsed.format_byte = ats[1];
	parsed.max_frame_size = parsed.format_byte & 0x0f;

	std::size_t offset = 2;

	if ((parsed.format_byte & 0x10) != 0) {
		if (offset >= ats.size())
			return Iso14443Status::kParseError;
		parsed.has_ta = true;
		parsed.ta = ats[offset++];
	}
	if ((parsed.format_byte & 0x20) != 0) {
		if (offset >= ats.size())
			return Iso14443Status::kParseError;
		parsed.has_tb = true;
		parsed.tb = ats[offset++];
	}
	if ((parsed.format_byte & 0x40) != 0) {
		if (offset >= ats.size())
			return Iso14443Status::kParseError;
		parsed.has_tc = true;
		parsed.tc = ats[offset++];
	}
	if ((parsed.format_byte & 0x80) != 0)
		return Iso14443Status::kParseError;

	parsed.historical_bytes.assign(
		ats.begin() + static_cast<std::ptrdiff_t>(offset), ats.end());
	*info = std::move(parsed);
	return Iso14443Status::kOk;
}

Iso14443Status Iso14443ContactlessReader::encodeRats(
	uint8_t fsdi, uint8_t cid, std::vector<uint8_t> *encoded)
{
	if (!encoded)
		return Iso14443Status::kInvalidArgument;
	if (!valid_fsdi(fsdi) || !valid_cid(cid))
		return Iso14443Status::kInvalidArgument;

	*encoded = {0xe0, static_cast<uint8_t>((fsdi << 4) | cid)};
	return Iso14443Status::kOk;
}

Iso14443Status Iso14443ContactlessReader::encodeApdu(
	const ApduCommand &command, std::vector<uint8_t> *encoded)
{
	return from_iso7816_status(
		Iso7816ContactReader::encodeApdu(command, encoded));
}

Iso14443Status Iso14443ContactlessReader::parseApduResponse(
	const std::vector<uint8_t> &raw, ApduResponse *response)
{
	return from_iso7816_status(
		Iso7816ContactReader::parseApduResponse(raw, response));
}

bool Iso14443ContactlessReader::cardPresent() const
{
	return transport_.card_present && transport_.card_present();
}

Iso14443Status Iso14443ContactlessReader::activate(Iso14443CardInfo *card)
{
	if (!card)
		return fail(Iso14443Status::kInvalidArgument,
			    "card destination is null");
	if (!transport_.anti_collision)
		return fail(Iso14443Status::kInvalidArgument,
			    "anti-collision transport is not configured");
	if (!cardPresent())
		return fail(Iso14443Status::kCardAbsent, "card is absent");

	Iso14443CardInfo detected;
	Iso14443Status status = transport_.anti_collision(&detected);

	if (status != Iso14443Status::kOk)
		return fail(status, "anti-collision failed");

	if (detected.type == Iso14443CardType::kTypeA &&
	    !valid_uid_size(detected.type_a.uid.size()))
		return fail(Iso14443Status::kParseError,
			    "Type A UID length is invalid");

	if (detected.type == Iso14443CardType::kTypeB) {
		Iso14443TypeBInfo parsed;

		status = parseTypeBAtqb(detected.type_b.atqb, &parsed);
		if (status != Iso14443Status::kOk)
			return fail(status, "ATQB parse failed");
		detected.type_b = std::move(parsed);
	}

	if (detected.type == Iso14443CardType::kTypeA && transport_.rats) {
		std::vector<uint8_t> raw_ats;

		status = transport_.rats(&raw_ats);
		if (status != Iso14443Status::kOk)
			return fail(status, "RATS failed");
		status = parseAts(raw_ats, &detected.ats);
		if (status != Iso14443Status::kOk)
			return fail(status, "ATS parse failed");
		detected.has_ats = true;
	}

	*card = std::move(detected);
	last_error_.clear();
	return Iso14443Status::kOk;
}

Iso14443Status
Iso14443ContactlessReader::exchangeApdu(const ApduCommand &command,
					ApduResponse *response)
{
	if (!response)
		return fail(Iso14443Status::kInvalidArgument,
			    "APDU response destination is null");
	if (!transport_.transceive)
		return fail(Iso14443Status::kInvalidArgument,
			    "transceive transport is not configured");
	if (!cardPresent())
		return fail(Iso14443Status::kCardAbsent, "card is absent");

	std::vector<uint8_t> encoded;
	Iso14443Status status = encodeApdu(command, &encoded);

	if (status != Iso14443Status::kOk)
		return fail(status, "APDU command encode failed");

	std::vector<uint8_t> raw_response;
	status = transport_.transceive(encoded, &raw_response);
	if (status != Iso14443Status::kOk)
		return fail(status, "APDU transceive failed");

	status = parseApduResponse(raw_response, response);
	if (status != Iso14443Status::kOk)
		return fail(status, "APDU response parse failed");

	last_exchange_ = {std::move(encoded), std::move(raw_response)};
	last_error_.clear();
	return Iso14443Status::kOk;
}

const Iso14443Exchange &Iso14443ContactlessReader::lastExchange() const
{
	return last_exchange_;
}

const std::string &Iso14443ContactlessReader::lastError() const
{
	return last_error_;
}

Iso14443Status
Iso14443ContactlessReader::fail(Iso14443Status status,
				const std::string &error) const
{
	last_error_ = error;
	return status;
}

} // namespace omnisight::embedded::pos::emv
