/* SPDX-License-Identifier: MIT
 *
 * Case 7 EMV ISO 7816 contact smart-card reader interface (OP-2024).
 */
#ifndef OMNISIGHT_EMBEDDED_POS_EMV_ISO7816_CONTACT_READER_H_
#define OMNISIGHT_EMBEDDED_POS_EMV_ISO7816_CONTACT_READER_H_

#include <cstddef>
#include <cstdint>
#include <functional>
#include <string>
#include <vector>

namespace omnisight::embedded::pos::emv {

enum class Iso7816Status {
	kOk = 0,
	kInvalidArgument,
	kParseError,
	kCardAbsent,
	kTransportError,
};

enum class Iso7816Protocol {
	kT0 = 0,
	kT1,
};

struct AtrInterfaceByteGroup {
	bool has_ta = false;
	bool has_tb = false;
	bool has_tc = false;
	bool has_td = false;
	uint8_t ta = 0;
	uint8_t tb = 0;
	uint8_t tc = 0;
	uint8_t td = 0;
};

struct AtrInfo {
	uint8_t ts = 0;
	uint8_t t0 = 0;
	std::vector<AtrInterfaceByteGroup> interface_bytes;
	std::vector<uint8_t> historical_bytes;
	std::vector<Iso7816Protocol> protocols;
	bool has_tck = false;
	uint8_t tck = 0;
};

struct ApduCommand {
	uint8_t cla = 0;
	uint8_t ins = 0;
	uint8_t p1 = 0;
	uint8_t p2 = 0;
	std::vector<uint8_t> data;
	int le = -1;
};

struct ApduResponse {
	std::vector<uint8_t> data;
	uint8_t sw1 = 0;
	uint8_t sw2 = 0;
};

struct Iso7816Exchange {
	std::vector<uint8_t> command;
	std::vector<uint8_t> response;
};

using Iso7816CardPresentFn = std::function<bool()>;
using Iso7816ResetFn = std::function<Iso7816Status(std::vector<uint8_t> *atr)>;
using Iso7816TransceiveFn =
	std::function<Iso7816Status(const std::vector<uint8_t> &command,
				    std::vector<uint8_t> *response)>;

struct Iso7816ContactReaderTransport {
	Iso7816CardPresentFn card_present;
	Iso7816ResetFn reset;
	Iso7816TransceiveFn transceive;
};

class Iso7816ContactReader {
public:
	explicit Iso7816ContactReader(Iso7816ContactReaderTransport transport);

	static Iso7816Status parseAtr(const std::vector<uint8_t> &atr,
				      AtrInfo *info);
	static Iso7816Status encodeApdu(const ApduCommand &command,
					std::vector<uint8_t> *encoded);
	static Iso7816Status parseApduResponse(const std::vector<uint8_t> &raw,
					       ApduResponse *response);

	bool cardPresent() const;
	Iso7816Status reset(AtrInfo *atr);
	Iso7816Status exchangeApdu(const ApduCommand &command,
				   ApduResponse *response);
	const Iso7816Exchange &lastExchange() const;
	const std::string &lastError() const;

private:
	Iso7816Status fail(Iso7816Status status, const std::string &error) const;

	Iso7816ContactReaderTransport transport_;
	Iso7816Exchange last_exchange_;
	mutable std::string last_error_;
};

} // namespace omnisight::embedded::pos::emv

#endif // OMNISIGHT_EMBEDDED_POS_EMV_ISO7816_CONTACT_READER_H_
