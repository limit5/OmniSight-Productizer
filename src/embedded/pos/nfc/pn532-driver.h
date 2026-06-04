/* SPDX-License-Identifier: MIT
 *
 * Case 7 NXP PN532 NFC reader driver interface (OP-2040).
 */
#ifndef OMNISIGHT_EMBEDDED_POS_NFC_PN532_DRIVER_H_
#define OMNISIGHT_EMBEDDED_POS_NFC_PN532_DRIVER_H_

#include <cstddef>
#include <cstdint>
#include <functional>
#include <string>
#include <vector>

namespace omnisight::embedded::pos::nfc {

enum class Pn532Status {
	kOk = 0,
	kInvalidArgument,
	kInvalidFrame,
	kCardAbsent,
	kTransportError,
	kChipError,
};

enum class Pn532TransportMode {
	kI2c = 0,
	kSpi,
	kUart,
};

enum class Pn532TargetType {
	kIso14443A = 0,
	kIso14443B,
};

struct Pn532TargetInfo {
	Pn532TargetType type = Pn532TargetType::kIso14443A;
	uint8_t target_number = 0;
	std::vector<uint8_t> uid;
	std::vector<uint8_t> activation_data;

	explicit operator bool() const
	{
		return target_number != 0;
	}
};

struct Pn532Exchange {
	std::vector<uint8_t> command;
	std::vector<uint8_t> response;
};

using Pn532TransportExchangeFn =
	std::function<Pn532Status(const std::vector<uint8_t> &request_frame,
				  std::vector<uint8_t> *response_frame)>;

struct Pn532Transport {
	Pn532TransportMode mode = Pn532TransportMode::kI2c;
	Pn532TransportExchangeFn exchange;
};

class Pn532Driver {
public:
	explicit Pn532Driver(Pn532Transport transport);

	static Pn532Status buildCommandFrame(
		const std::vector<uint8_t> &command,
		std::vector<uint8_t> *frame);
	static Pn532Status parseResponseFrame(
		const std::vector<uint8_t> &frame, uint8_t expected_command,
		std::vector<uint8_t> *payload);
	static Pn532Status wrapForTransport(Pn532TransportMode mode,
					    const std::vector<uint8_t> &frame,
					    std::vector<uint8_t> *wrapped);
	static Pn532Status unwrapFromTransport(Pn532TransportMode mode,
					      const std::vector<uint8_t> &raw,
					      std::vector<uint8_t> *frame);

	Pn532Status init();
	Pn532Status detect(Pn532TargetType type, Pn532TargetInfo *target);
	Pn532Status detect(Pn532TargetInfo *target);
	Pn532Status read(const std::vector<uint8_t> &command,
			 std::vector<uint8_t> *response);
	Pn532Status write(const std::vector<uint8_t> &command,
			  std::vector<uint8_t> *response);
	const Pn532Exchange &lastExchange() const;
	const std::string &lastError() const;

private:
	Pn532Status transceiveCommand(const std::vector<uint8_t> &command,
				      std::vector<uint8_t> *payload);
	Pn532Status parseDetectedTarget(Pn532TargetType type,
					const std::vector<uint8_t> &payload,
					Pn532TargetInfo *target) const;
	Pn532Status fail(Pn532Status status, const std::string &error) const;

	Pn532Transport transport_;
	Pn532TargetInfo active_target_;
	Pn532Exchange last_exchange_;
	mutable std::string last_error_;
};

const char *toString(Pn532Status status);
const char *toString(Pn532TransportMode mode);
const char *toString(Pn532TargetType type);

} // namespace omnisight::embedded::pos::nfc

#endif // OMNISIGHT_EMBEDDED_POS_NFC_PN532_DRIVER_H_
