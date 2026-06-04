/* SPDX-License-Identifier: MIT
 *
 * Case 7 EMV ISO 14443 contactless smart-card reader interface (OP-2032).
 */
#ifndef OMNISIGHT_EMBEDDED_POS_EMV_ISO14443_CONTACTLESS_READER_H_
#define OMNISIGHT_EMBEDDED_POS_EMV_ISO14443_CONTACTLESS_READER_H_

#include "iso7816-contact-reader.h"

#include <cstdint>
#include <functional>
#include <string>
#include <vector>

namespace omnisight::embedded::pos::emv {

enum class Iso14443Status {
	kOk = 0,
	kInvalidArgument,
	kParseError,
	kCardAbsent,
	kCollision,
	kTransportError,
};

enum class Iso14443CardType {
	kTypeA = 0,
	kTypeB,
};

struct Iso14443TypeAInfo {
	uint16_t atqa = 0;
	std::vector<uint8_t> uid;
	uint8_t sak = 0;
};

struct Iso14443TypeBInfo {
	std::vector<uint8_t> atqb;
	std::vector<uint8_t> pupi;
	std::vector<uint8_t> application_data;
	std::vector<uint8_t> protocol_info;
};

struct AtsInfo {
	uint8_t format_byte = 0;
	uint8_t max_frame_size = 0;
	bool has_ta = false;
	bool has_tb = false;
	bool has_tc = false;
	uint8_t ta = 0;
	uint8_t tb = 0;
	uint8_t tc = 0;
	std::vector<uint8_t> historical_bytes;
};

struct Iso14443CardInfo {
	Iso14443CardType type = Iso14443CardType::kTypeA;
	Iso14443TypeAInfo type_a;
	Iso14443TypeBInfo type_b;
	bool has_ats = false;
	AtsInfo ats;
};

struct Iso14443Exchange {
	std::vector<uint8_t> command;
	std::vector<uint8_t> response;
};

using Iso14443CardPresentFn = std::function<bool()>;
using Iso14443AntiCollisionFn =
	std::function<Iso14443Status(Iso14443CardInfo *card)>;
using Iso14443RatsFn =
	std::function<Iso14443Status(std::vector<uint8_t> *ats)>;
using Iso14443TransceiveFn =
	std::function<Iso14443Status(const std::vector<uint8_t> &command,
				    std::vector<uint8_t> *response)>;

struct Iso14443ContactlessReaderTransport {
	Iso14443CardPresentFn card_present;
	Iso14443AntiCollisionFn anti_collision;
	Iso14443RatsFn rats;
	Iso14443TransceiveFn transceive;
};

class Iso14443ContactlessReader {
public:
	explicit Iso14443ContactlessReader(
		Iso14443ContactlessReaderTransport transport);

	static Iso14443Status parseTypeBAtqb(const std::vector<uint8_t> &atqb,
					     Iso14443TypeBInfo *info);
	static Iso14443Status parseAts(const std::vector<uint8_t> &ats,
				       AtsInfo *info);
	static Iso14443Status encodeRats(uint8_t fsdi, uint8_t cid,
					 std::vector<uint8_t> *encoded);
	static Iso14443Status encodeApdu(const ApduCommand &command,
					 std::vector<uint8_t> *encoded);
	static Iso14443Status parseApduResponse(const std::vector<uint8_t> &raw,
						ApduResponse *response);

	bool cardPresent() const;
	Iso14443Status activate(Iso14443CardInfo *card);
	Iso14443Status exchangeApdu(const ApduCommand &command,
				    ApduResponse *response);
	const Iso14443Exchange &lastExchange() const;
	const std::string &lastError() const;

private:
	Iso14443Status fail(Iso14443Status status,
			    const std::string &error) const;

	Iso14443ContactlessReaderTransport transport_;
	Iso14443Exchange last_exchange_;
	mutable std::string last_error_;
};

} // namespace omnisight::embedded::pos::emv

#endif // OMNISIGHT_EMBEDDED_POS_EMV_ISO14443_CONTACTLESS_READER_H_
