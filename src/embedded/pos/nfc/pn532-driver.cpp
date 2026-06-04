/* SPDX-License-Identifier: MIT
 *
 * Case 7 NXP PN532 NFC reader driver implementation (OP-2040).
 */
#include "pn532-driver.h"

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <utility>

#ifdef OMNISIGHT_PN532_DRIVER_SMOKE_MAIN
#include <cstdlib>
#include <iostream>
#endif

namespace omnisight::embedded::pos::nfc {
namespace {

constexpr uint8_t kPreamble = 0x00;
constexpr uint8_t kStartCode1 = 0x00;
constexpr uint8_t kStartCode2 = 0xff;
constexpr uint8_t kPostamble = 0x00;
constexpr uint8_t kHostToPn532 = 0xd4;
constexpr uint8_t kPn532ToHost = 0xd5;
constexpr uint8_t kI2cReady = 0x01;
constexpr uint8_t kSamConfiguration = 0x14;
constexpr uint8_t kInListPassiveTarget = 0x4a;
constexpr uint8_t kInDataExchange = 0x40;
constexpr uint8_t kIso14443ABrty = 0x00;
constexpr uint8_t kIso14443BBrty = 0x03;

static uint8_t checksum_byte(uint8_t value)
{
	return static_cast<uint8_t>(0x100 - value);
}

static uint8_t checksum(const std::vector<uint8_t> &bytes)
{
	uint8_t sum = 0;

	for (uint8_t byte : bytes)
		sum = static_cast<uint8_t>(sum + byte);

	return checksum_byte(sum);
}

static uint8_t byte_sum(const std::vector<uint8_t> &bytes)
{
	uint8_t sum = 0;

	for (uint8_t byte : bytes)
		sum = static_cast<uint8_t>(sum + byte);

	return sum;
}

static bool has_normal_frame_prefix(const std::vector<uint8_t> &frame)
{
	return frame.size() >= 8 && frame[0] == kPreamble &&
	       frame[1] == kStartCode1 && frame[2] == kStartCode2;
}

static uint8_t target_type_brty(Pn532TargetType type)
{
	return type == Pn532TargetType::kIso14443A ? kIso14443ABrty :
						     kIso14443BBrty;
}

} // namespace

Pn532Driver::Pn532Driver(Pn532Transport transport)
	: transport_(std::move(transport))
{
}

Pn532Status Pn532Driver::buildCommandFrame(
	const std::vector<uint8_t> &command, std::vector<uint8_t> *frame)
{
	if (!frame)
		return Pn532Status::kInvalidArgument;
	if (command.empty() || command.size() > 254)
		return Pn532Status::kInvalidArgument;

	const uint8_t len = static_cast<uint8_t>(command.size() + 1);
	std::vector<uint8_t> data;
	data.reserve(command.size() + 1);
	data.push_back(kHostToPn532);
	data.insert(data.end(), command.begin(), command.end());

	frame->clear();
	frame->reserve(data.size() + 7);
	frame->push_back(kPreamble);
	frame->push_back(kStartCode1);
	frame->push_back(kStartCode2);
	frame->push_back(len);
	frame->push_back(checksum_byte(len));
	frame->insert(frame->end(), data.begin(), data.end());
	frame->push_back(checksum(data));
	frame->push_back(kPostamble);
	return Pn532Status::kOk;
}

Pn532Status Pn532Driver::parseResponseFrame(
	const std::vector<uint8_t> &frame, uint8_t expected_command,
	std::vector<uint8_t> *payload)
{
	if (!payload)
		return Pn532Status::kInvalidArgument;
	if (!has_normal_frame_prefix(frame))
		return Pn532Status::kInvalidFrame;

	const uint8_t len = frame[3];
	const uint8_t lcs = frame[4];
	const std::size_t data_begin = 5;
	const std::size_t dcs_offset = data_begin + len;
	const std::size_t postamble_offset = dcs_offset + 1;

	if (len < 2 || static_cast<uint8_t>(len + lcs) != 0)
		return Pn532Status::kInvalidFrame;
	if (frame.size() != postamble_offset + 1)
		return Pn532Status::kInvalidFrame;
	if (frame[postamble_offset] != kPostamble)
		return Pn532Status::kInvalidFrame;

	std::vector<uint8_t> data(frame.begin() + data_begin,
				  frame.begin() + static_cast<std::ptrdiff_t>(
							  dcs_offset));
	if (static_cast<uint8_t>(byte_sum(data) + frame[dcs_offset]) != 0)
		return Pn532Status::kInvalidFrame;
	if (data[0] != kPn532ToHost ||
	    data[1] != static_cast<uint8_t>(expected_command + 1))
		return Pn532Status::kInvalidFrame;

	payload->assign(data.begin() + 2, data.end());
	return Pn532Status::kOk;
}

Pn532Status Pn532Driver::wrapForTransport(Pn532TransportMode mode,
					   const std::vector<uint8_t> &frame,
					   std::vector<uint8_t> *wrapped)
{
	if (!wrapped)
		return Pn532Status::kInvalidArgument;

	*wrapped = frame;
	if (mode == Pn532TransportMode::kSpi)
		wrapped->insert(wrapped->begin(), 0x01);

	return Pn532Status::kOk;
}

Pn532Status Pn532Driver::unwrapFromTransport(Pn532TransportMode mode,
					     const std::vector<uint8_t> &raw,
					     std::vector<uint8_t> *frame)
{
	if (!frame)
		return Pn532Status::kInvalidArgument;
	if (mode == Pn532TransportMode::kI2c) {
		if (raw.empty() || raw[0] != kI2cReady)
			return Pn532Status::kTransportError;
		frame->assign(raw.begin() + 1, raw.end());
		return Pn532Status::kOk;
	}
	if (mode == Pn532TransportMode::kSpi) {
		if (raw.empty())
			return Pn532Status::kTransportError;
		frame->assign(raw.begin() + 1, raw.end());
		return Pn532Status::kOk;
	}

	*frame = raw;
	return Pn532Status::kOk;
}

Pn532Status Pn532Driver::init()
{
	std::vector<uint8_t> payload;
	const Pn532Status status = transceiveCommand(
		{kSamConfiguration, 0x01, 0x14, 0x01}, &payload);

	if (status != Pn532Status::kOk)
		return status;
	if (!payload.empty())
		return fail(Pn532Status::kChipError,
			    "SAMConfiguration returned unexpected data");

	last_error_.clear();
	return Pn532Status::kOk;
}

Pn532Status Pn532Driver::detect(Pn532TargetType type, Pn532TargetInfo *target)
{
	if (!target)
		return fail(Pn532Status::kInvalidArgument,
			    "target destination is null");

	std::vector<uint8_t> payload;
	Pn532Status status = transceiveCommand(
		{kInListPassiveTarget, 0x01, target_type_brty(type)}, &payload);

	if (status != Pn532Status::kOk)
		return status;

	status = parseDetectedTarget(type, payload, target);
	if (status != Pn532Status::kOk)
		return status;

	active_target_ = *target;
	last_error_.clear();
	return Pn532Status::kOk;
}

Pn532Status Pn532Driver::detect(Pn532TargetInfo *target)
{
	Pn532Status status = detect(Pn532TargetType::kIso14443A, target);

	if (status == Pn532Status::kCardAbsent)
		status = detect(Pn532TargetType::kIso14443B, target);

	return status;
}

Pn532Status Pn532Driver::read(const std::vector<uint8_t> &command,
			      std::vector<uint8_t> *response)
{
	return write(command, response);
}

Pn532Status Pn532Driver::write(const std::vector<uint8_t> &command,
			       std::vector<uint8_t> *response)
{
	if (!response)
		return fail(Pn532Status::kInvalidArgument,
			    "response destination is null");
	if (command.empty())
		return fail(Pn532Status::kInvalidArgument, "command is empty");
	if (!active_target_)
		return fail(Pn532Status::kCardAbsent, "target is not active");

	std::vector<uint8_t> pn532_command = {kInDataExchange,
					      active_target_.target_number};
	pn532_command.insert(pn532_command.end(), command.begin(), command.end());

	std::vector<uint8_t> payload;
	Pn532Status status = transceiveCommand(pn532_command, &payload);

	if (status != Pn532Status::kOk)
		return status;
	if (payload.empty())
		return fail(Pn532Status::kInvalidFrame,
			    "InDataExchange response is empty");
	if (payload[0] != 0x00)
		return fail(Pn532Status::kChipError,
			    "InDataExchange reported target error");

	response->assign(payload.begin() + 1, payload.end());
	last_error_.clear();
	return Pn532Status::kOk;
}

const Pn532Exchange &Pn532Driver::lastExchange() const
{
	return last_exchange_;
}

const std::string &Pn532Driver::lastError() const
{
	return last_error_;
}

Pn532Status Pn532Driver::transceiveCommand(
	const std::vector<uint8_t> &command, std::vector<uint8_t> *payload)
{
	if (!payload)
		return fail(Pn532Status::kInvalidArgument,
			    "payload destination is null");
	if (!transport_.exchange)
		return fail(Pn532Status::kInvalidArgument,
			    "transport exchange is not configured");

	std::vector<uint8_t> frame;
	Pn532Status status = buildCommandFrame(command, &frame);

	if (status != Pn532Status::kOk)
		return fail(status, "command frame build failed");

	std::vector<uint8_t> wrapped_request;
	status = wrapForTransport(transport_.mode, frame, &wrapped_request);
	if (status != Pn532Status::kOk)
		return fail(status, "transport frame wrap failed");

	std::vector<uint8_t> raw_response;
	status = transport_.exchange(wrapped_request, &raw_response);
	if (status != Pn532Status::kOk)
		return fail(status, "transport exchange failed");

	std::vector<uint8_t> response_frame;
	status = unwrapFromTransport(transport_.mode, raw_response,
				     &response_frame);
	if (status != Pn532Status::kOk)
		return fail(status, "transport response unwrap failed");

	status = parseResponseFrame(response_frame, command[0], payload);
	if (status != Pn532Status::kOk)
		return fail(status, "response frame parse failed");

	last_exchange_ = {std::move(frame), std::move(response_frame)};
	return Pn532Status::kOk;
}

Pn532Status Pn532Driver::parseDetectedTarget(
	Pn532TargetType type, const std::vector<uint8_t> &payload,
	Pn532TargetInfo *target) const
{
	if (!target)
		return fail(Pn532Status::kInvalidArgument,
			    "target destination is null");
	if (payload.empty())
		return fail(Pn532Status::kInvalidFrame,
			    "InListPassiveTarget response is empty");
	if (payload[0] == 0)
		return fail(Pn532Status::kCardAbsent, "card is absent");
	if (payload[0] > 1)
		return fail(Pn532Status::kInvalidFrame,
			    "multiple targets are not supported");

	Pn532TargetInfo parsed;
	parsed.type = type;

	if (type == Pn532TargetType::kIso14443A) {
		if (payload.size() < 7)
			return fail(Pn532Status::kInvalidFrame,
				    "Type A target response is too short");
		const uint8_t uid_len = payload[5];
		if (uid_len == 0 || payload.size() < 6U + uid_len)
			return fail(Pn532Status::kInvalidFrame,
				    "Type A UID length is invalid");

		parsed.target_number = payload[1];
		parsed.activation_data.assign(payload.begin() + 2,
					      payload.begin() + 5);
		parsed.uid.assign(payload.begin() + 6,
				  payload.begin() + 6 + uid_len);
	} else {
		if (payload.size() < 14)
			return fail(Pn532Status::kInvalidFrame,
				    "Type B target response is too short");
		const uint8_t attrib_res_len = payload[13];
		if (payload.size() < 14U + attrib_res_len)
			return fail(Pn532Status::kInvalidFrame,
				    "Type B ATTRIB response length is invalid");

		parsed.target_number = payload[1];
		parsed.uid.assign(payload.begin() + 2, payload.begin() + 6);
		parsed.activation_data.assign(payload.begin() + 2,
					      payload.begin() + 14 +
						      attrib_res_len);
	}

	*target = std::move(parsed);
	return Pn532Status::kOk;
}

Pn532Status Pn532Driver::fail(Pn532Status status,
			      const std::string &error) const
{
	last_error_ = error;
	return status;
}

const char *toString(Pn532Status status)
{
	switch (status) {
	case Pn532Status::kOk:
		return "ok";
	case Pn532Status::kInvalidArgument:
		return "invalid-argument";
	case Pn532Status::kInvalidFrame:
		return "invalid-frame";
	case Pn532Status::kCardAbsent:
		return "card-absent";
	case Pn532Status::kTransportError:
		return "transport-error";
	case Pn532Status::kChipError:
		return "chip-error";
	}

	return "unknown";
}

const char *toString(Pn532TransportMode mode)
{
	switch (mode) {
	case Pn532TransportMode::kI2c:
		return "i2c";
	case Pn532TransportMode::kSpi:
		return "spi";
	case Pn532TransportMode::kUart:
		return "uart";
	}

	return "unknown";
}

const char *toString(Pn532TargetType type)
{
	switch (type) {
	case Pn532TargetType::kIso14443A:
		return "iso14443-a";
	case Pn532TargetType::kIso14443B:
		return "iso14443-b";
	}

	return "unknown";
}

#ifdef OMNISIGHT_PN532_DRIVER_SMOKE_MAIN
namespace {

static bool expect_status(Pn532Status status, Pn532Status expected,
			  const char *name)
{
	if (status == expected)
		return true;

	std::cerr << name << ": got " << toString(status) << ", expected "
		  << toString(expected) << '\n';
	return false;
}

static bool expect_bytes(const std::vector<uint8_t> &got,
			 const std::vector<uint8_t> &expected, const char *name)
{
	if (got == expected)
		return true;

	std::cerr << name << ": byte vector mismatch\n";
	return false;
}

static std::vector<uint8_t> make_response(uint8_t command,
					  const std::vector<uint8_t> &payload)
{
	std::vector<uint8_t> body = {static_cast<uint8_t>(command + 1)};
	std::vector<uint8_t> frame;

	body.insert(body.end(), payload.begin(), payload.end());
	(void)Pn532Driver::buildCommandFrame(body, &frame);
	frame[5] = kPn532ToHost;
	frame[6] = static_cast<uint8_t>(command + 1);
	frame[frame.size() - 2] = checksum(std::vector<uint8_t>(
		frame.begin() + 5, frame.end() - 2));
	return frame;
}

} // namespace

int pn532_driver_smoke_main()
{
	std::vector<uint8_t> frame;
	if (!expect_status(Pn532Driver::buildCommandFrame({0x14, 0x01}, &frame),
			   Pn532Status::kOk, "buildCommandFrame"))
		return EXIT_FAILURE;
	if (!expect_bytes(frame, {0x00, 0x00, 0xff, 0x03, 0xfd, 0xd4,
				  0x14, 0x01, 0x17, 0x00},
			  "SAMConfiguration command frame"))
		return EXIT_FAILURE;

	std::vector<uint8_t> payload;
	if (!expect_status(Pn532Driver::parseResponseFrame(
				   make_response(kInDataExchange,
						 {0x00, 0x90, 0x00}),
				   kInDataExchange, &payload),
			   Pn532Status::kOk, "parseResponseFrame"))
		return EXIT_FAILURE;
	if (!expect_bytes(payload, {0x00, 0x90, 0x00}, "parsed payload"))
		return EXIT_FAILURE;

	std::vector<uint8_t> spi_wrapped;
	if (!expect_status(Pn532Driver::wrapForTransport(
				   Pn532TransportMode::kSpi, frame, &spi_wrapped),
			   Pn532Status::kOk, "wrapForTransport"))
		return EXIT_FAILURE;
	if (spi_wrapped.empty() || spi_wrapped[0] != 0x01)
		return EXIT_FAILURE;

	std::vector<std::vector<uint8_t>> responses = {
		make_response(kSamConfiguration, {}),
		make_response(kInListPassiveTarget,
			      {0x01, 0x01, 0x04, 0x00, 0x08, 0x04, 0xde,
			       0xad, 0xbe, 0xef}),
		make_response(kInDataExchange, {0x00, 0x90, 0x00}),
	};
	std::size_t response_index = 0;
	Pn532Driver driver({Pn532TransportMode::kUart,
			    [&responses, &response_index](
				    const std::vector<uint8_t> &request,
				    std::vector<uint8_t> *response) {
				    if (request.empty() ||
					response_index >= responses.size())
					    return Pn532Status::kTransportError;
				    *response = responses[response_index++];
				    return Pn532Status::kOk;
			    }});

	Pn532TargetInfo target;
	std::vector<uint8_t> exchange_response;
	if (!expect_status(driver.init(), Pn532Status::kOk, "init"))
		return EXIT_FAILURE;
	if (!expect_status(driver.detect(Pn532TargetType::kIso14443A, &target),
			   Pn532Status::kOk, "detect Type A"))
		return EXIT_FAILURE;
	if (!target || !expect_bytes(target.uid, {0xde, 0xad, 0xbe, 0xef},
				     "target UID"))
		return EXIT_FAILURE;
	if (!expect_status(driver.write({0x00, 0xa4, 0x04, 0x00},
					&exchange_response),
			   Pn532Status::kOk, "InDataExchange"))
		return EXIT_FAILURE;
	if (!expect_bytes(exchange_response, {0x90, 0x00},
			  "InDataExchange response"))
		return EXIT_FAILURE;

	return EXIT_SUCCESS;
}
#endif

} // namespace omnisight::embedded::pos::nfc

#ifdef OMNISIGHT_PN532_DRIVER_SMOKE_MAIN
int main()
{
	return omnisight::embedded::pos::nfc::pn532_driver_smoke_main();
}
#endif
