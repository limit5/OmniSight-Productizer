/* SPDX-License-Identifier: MIT
 *
 * Case 7 EMV ISO 7816 contact smart-card reader interface (OP-2024).
 */
#include "iso7816-contact-reader.h"

#include <algorithm>
#include <cstddef>
#include <limits>
#include <utility>

namespace omnisight::embedded::pos::emv {
namespace {

static bool valid_ts(uint8_t ts)
{
	return ts == 0x3b || ts == 0x3f;
}

static bool protocol_seen(const std::vector<Iso7816Protocol> &protocols,
			  Iso7816Protocol protocol)
{
	return std::find(protocols.begin(), protocols.end(), protocol) !=
	       protocols.end();
}

static bool append_protocol(std::vector<Iso7816Protocol> *protocols,
			    uint8_t encoded_protocol)
{
	Iso7816Protocol protocol = Iso7816Protocol::kT0;
	const uint8_t value = encoded_protocol & 0x0f;

	if (value == 1)
		protocol = Iso7816Protocol::kT1;
	else if (value != 0)
		return false;

	if (!protocol_seen(*protocols, protocol))
		protocols->push_back(protocol);
	return true;
}

static bool requires_tck(const std::vector<Iso7816Protocol> &protocols)
{
	return std::any_of(protocols.begin(), protocols.end(),
			   [](Iso7816Protocol protocol) {
				   return protocol != Iso7816Protocol::kT0;
			   });
}

static uint8_t atr_xor_from_t0(const std::vector<uint8_t> &atr)
{
	uint8_t value = 0;

	for (std::size_t i = 1; i < atr.size(); ++i)
		value ^= atr[i];

	return value;
}

static bool valid_le(int le)
{
	return le >= -1 && le <= std::numeric_limits<uint8_t>::max();
}

} // namespace

Iso7816ContactReader::Iso7816ContactReader(
	Iso7816ContactReaderTransport transport)
	: transport_(std::move(transport))
{
}

Iso7816Status Iso7816ContactReader::parseAtr(const std::vector<uint8_t> &atr,
					     AtrInfo *info)
{
	if (!info)
		return Iso7816Status::kInvalidArgument;
	if (atr.size() < 2 || atr.size() > 33)
		return Iso7816Status::kParseError;
	if (!valid_ts(atr[0]))
		return Iso7816Status::kParseError;

	AtrInfo parsed;
	parsed.ts = atr[0];
	parsed.t0 = atr[1];

	std::size_t offset = 2;
	uint8_t presence = parsed.t0 >> 4;
	const std::size_t historical_len = parsed.t0 & 0x0f;

	if (!append_protocol(&parsed.protocols, 0))
		return Iso7816Status::kParseError;

	while (presence != 0) {
		if (offset >= atr.size())
			return Iso7816Status::kParseError;

		AtrInterfaceByteGroup group;

		if ((presence & 0x1) != 0) {
			if (offset >= atr.size())
				return Iso7816Status::kParseError;
			group.has_ta = true;
			group.ta = atr[offset++];
		}
		if ((presence & 0x2) != 0) {
			if (offset >= atr.size())
				return Iso7816Status::kParseError;
			group.has_tb = true;
			group.tb = atr[offset++];
		}
		if ((presence & 0x4) != 0) {
			if (offset >= atr.size())
				return Iso7816Status::kParseError;
			group.has_tc = true;
			group.tc = atr[offset++];
		}
		if ((presence & 0x8) != 0) {
			if (offset >= atr.size())
				return Iso7816Status::kParseError;
			group.has_td = true;
			group.td = atr[offset++];
			if (!append_protocol(&parsed.protocols, group.td))
				return Iso7816Status::kParseError;
			presence = group.td >> 4;
		} else {
			presence = 0;
		}

		parsed.interface_bytes.push_back(group);
	}

	if (offset + historical_len > atr.size())
		return Iso7816Status::kParseError;

	parsed.historical_bytes.assign(
		atr.begin() + static_cast<std::ptrdiff_t>(offset),
		atr.begin() +
			static_cast<std::ptrdiff_t>(offset + historical_len));
	offset += historical_len;

	if (requires_tck(parsed.protocols)) {
		if (offset >= atr.size())
			return Iso7816Status::kParseError;
		parsed.has_tck = true;
		parsed.tck = atr[offset++];
		if (atr_xor_from_t0(atr) != 0)
			return Iso7816Status::kParseError;
	}

	if (offset != atr.size())
		return Iso7816Status::kParseError;

	*info = std::move(parsed);
	return Iso7816Status::kOk;
}

Iso7816Status Iso7816ContactReader::encodeApdu(const ApduCommand &command,
					       std::vector<uint8_t> *encoded)
{
	if (!encoded)
		return Iso7816Status::kInvalidArgument;
	if (!valid_le(command.le) ||
	    command.data.size() > std::numeric_limits<uint8_t>::max())
		return Iso7816Status::kInvalidArgument;

	std::vector<uint8_t> frame = {
		command.cla,
		command.ins,
		command.p1,
		command.p2,
	};

	if (!command.data.empty()) {
		frame.push_back(static_cast<uint8_t>(command.data.size()));
		frame.insert(frame.end(), command.data.begin(), command.data.end());
		if (command.le >= 0)
			frame.push_back(static_cast<uint8_t>(command.le));
	} else if (command.le >= 0) {
		frame.push_back(static_cast<uint8_t>(command.le));
	}

	*encoded = std::move(frame);
	return Iso7816Status::kOk;
}

Iso7816Status Iso7816ContactReader::parseApduResponse(
	const std::vector<uint8_t> &raw, ApduResponse *response)
{
	if (!response)
		return Iso7816Status::kInvalidArgument;
	if (raw.size() < 2)
		return Iso7816Status::kParseError;

	ApduResponse parsed;
	parsed.data.assign(raw.begin(), raw.end() - 2);
	parsed.sw1 = raw[raw.size() - 2];
	parsed.sw2 = raw[raw.size() - 1];
	*response = std::move(parsed);
	return Iso7816Status::kOk;
}

bool Iso7816ContactReader::cardPresent() const
{
	return transport_.card_present && transport_.card_present();
}

Iso7816Status Iso7816ContactReader::reset(AtrInfo *atr)
{
	if (!atr)
		return fail(Iso7816Status::kInvalidArgument,
			    "ATR destination is null");
	if (!transport_.reset)
		return fail(Iso7816Status::kInvalidArgument,
			    "reset transport is not configured");
	if (!cardPresent())
		return fail(Iso7816Status::kCardAbsent, "card is absent");

	std::vector<uint8_t> raw_atr;
	Iso7816Status status = transport_.reset(&raw_atr);

	if (status != Iso7816Status::kOk)
		return fail(status, "card reset failed");

	status = parseAtr(raw_atr, atr);
	if (status != Iso7816Status::kOk)
		return fail(status, "ATR parse failed");

	last_error_.clear();
	return Iso7816Status::kOk;
}

Iso7816Status Iso7816ContactReader::exchangeApdu(const ApduCommand &command,
						  ApduResponse *response)
{
	if (!response)
		return fail(Iso7816Status::kInvalidArgument,
			    "APDU response destination is null");
	if (!transport_.transceive)
		return fail(Iso7816Status::kInvalidArgument,
			    "transceive transport is not configured");
	if (!cardPresent())
		return fail(Iso7816Status::kCardAbsent, "card is absent");

	std::vector<uint8_t> encoded;
	Iso7816Status status = encodeApdu(command, &encoded);

	if (status != Iso7816Status::kOk)
		return fail(status, "APDU command encode failed");

	std::vector<uint8_t> raw_response;
	status = transport_.transceive(encoded, &raw_response);
	if (status != Iso7816Status::kOk)
		return fail(status, "APDU transceive failed");

	status = parseApduResponse(raw_response, response);
	if (status != Iso7816Status::kOk)
		return fail(status, "APDU response parse failed");

	last_exchange_ = {std::move(encoded), std::move(raw_response)};
	last_error_.clear();
	return Iso7816Status::kOk;
}

const Iso7816Exchange &Iso7816ContactReader::lastExchange() const
{
	return last_exchange_;
}

const std::string &Iso7816ContactReader::lastError() const
{
	return last_error_;
}

Iso7816Status Iso7816ContactReader::fail(Iso7816Status status,
					 const std::string &error) const
{
	last_error_ = error;
	return status;
}

} // namespace omnisight::embedded::pos::emv
