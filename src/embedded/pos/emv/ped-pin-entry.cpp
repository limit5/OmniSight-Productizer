/* SPDX-License-Identifier: MIT
 *
 * Case 7 EMV PED PIN entry abstraction (OP-2041).
 */
#include "ped-pin-entry.h"

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <utility>

#if defined(OMNISIGHT_PED_PIN_ENTRY_SMOKE_MAIN)
#include <iostream>
#endif

namespace omnisight::embedded::pos::emv {
namespace {

static bool only_digits(const std::string &value)
{
	return !value.empty() &&
	       std::all_of(value.begin(), value.end(), [](unsigned char c) {
		       return c >= '0' && c <= '9';
	       });
}

static bool valid_pin_length(std::size_t pin_length)
{
	return pin_length >= 4 && pin_length <= 12;
}

static bool key_slot_matches(const PedKeySlot &lhs, const PedKeySlot &rhs)
{
	if (lhs.id != 0 && rhs.id != 0)
		return lhs.id == rhs.id;
	if (!lhs.label.empty() && !rhs.label.empty())
		return lhs.label == rhs.label;
	return false;
}

static uint8_t digit_nibble(char digit)
{
	return static_cast<uint8_t>(digit - '0');
}

static void set_nibble(std::array<uint8_t, 8> *block, std::size_t index,
		       uint8_t value)
{
	const std::size_t byte_index = index / 2;

	if ((index % 2) == 0)
		(*block)[byte_index] =
			static_cast<uint8_t>(((*block)[byte_index] & 0x0f) |
					     ((value & 0x0f) << 4));
	else
		(*block)[byte_index] =
			static_cast<uint8_t>(((*block)[byte_index] & 0xf0) |
					     (value & 0x0f));
}

static PedStatus build_pin_field(const std::string &pin,
				 PinBlockFormat format,
				 std::array<uint8_t, 8> *field)
{
	if (!field)
		return PedStatus::kInvalidArgument;
	if (!only_digits(pin) || !valid_pin_length(pin.size()))
		return PedStatus::kInvalidArgument;

	field->fill(format == PinBlockFormat::kIso9564Format3 ? 0xAA : 0xFF);
	set_nibble(field, 0,
		   format == PinBlockFormat::kIso9564Format3 ? 0x03 : 0x00);
	set_nibble(field, 1, static_cast<uint8_t>(pin.size()));

	for (std::size_t i = 0; i < pin.size(); ++i)
		set_nibble(field, i + 2, digit_nibble(pin[i]));

	return PedStatus::kOk;
}

static PedStatus build_pan_field(const std::string &pan,
				 std::array<uint8_t, 8> *field)
{
	if (!field)
		return PedStatus::kInvalidArgument;
	if (!only_digits(pan) || pan.size() < 13)
		return PedStatus::kInvalidArgument;

	field->fill(0x00);

	const std::size_t account_end = pan.size() - 1;
	const std::size_t account_start =
		account_end > 12 ? account_end - 12 : 0;
	const std::string account =
		pan.substr(account_start, account_end - account_start);
	const std::size_t pad_nibbles = 16 - account.size();

	for (std::size_t i = 0; i < account.size(); ++i)
		set_nibble(field, pad_nibbles + i, digit_nibble(account[i]));

	return PedStatus::kOk;
}

static PedStatus default_encrypt_pin_block(
	const std::array<uint8_t, 8> &clear_block,
	const PedKeyInjectionRequest &key,
	std::array<uint8_t, 8> *encrypted_block)
{
	if (!encrypted_block)
		return PedStatus::kInvalidArgument;
	if (key.wrapped_key.empty())
		return PedStatus::kKeyNotInjected;

	for (std::size_t i = 0; i < encrypted_block->size(); ++i) {
		const uint8_t mask =
			key.wrapped_key[i % key.wrapped_key.size()];
		(*encrypted_block)[i] =
			static_cast<uint8_t>(clear_block[i] ^ mask);
	}

	return PedStatus::kOk;
}

} // namespace

InMemoryPedDevice::InMemoryPedDevice(PedPinBlockEncryptFn encrypt)
	: encrypt_(std::move(encrypt))
{
}

PedStatus InMemoryPedDevice::initialize()
{
	initialized_ = true;
	last_error_.clear();
	return PedStatus::kOk;
}

PedStatus InMemoryPedDevice::shutdown()
{
	initialized_ = false;
	injected_keys_.clear();
	clearSeededPinEntry();
	last_error_.clear();
	return PedStatus::kOk;
}

bool InMemoryPedDevice::ready() const
{
	return initialized_;
}

PedStatus InMemoryPedDevice::injectKey(const PedKeyInjectionRequest &request)
{
	if (!initialized_)
		return fail(PedStatus::kInvalidState, "PED is not initialized");
	if (!request.slot || request.wrapped_key.empty())
		return fail(PedStatus::kInvalidArgument,
			    "key injection request is incomplete");

	auto existing = std::find_if(
		injected_keys_.begin(), injected_keys_.end(),
		[&request](const PedKeyInjectionRequest &candidate) {
			return key_slot_matches(candidate.slot, request.slot);
		});

	if (existing == injected_keys_.end())
		injected_keys_.push_back(request);
	else
		*existing = request;

	last_error_.clear();
	return PedStatus::kOk;
}

PedStatus InMemoryPedDevice::deleteKey(const PedKeySlot &slot)
{
	if (!initialized_)
		return fail(PedStatus::kInvalidState, "PED is not initialized");
	if (!slot)
		return fail(PedStatus::kInvalidArgument, "key slot is empty");

	const auto old_size = injected_keys_.size();
	injected_keys_.erase(
		std::remove_if(injected_keys_.begin(), injected_keys_.end(),
			       [&slot](const PedKeyInjectionRequest &candidate) {
				       return key_slot_matches(candidate.slot, slot);
			       }),
		injected_keys_.end());

	if (old_size == injected_keys_.size())
		return fail(PedStatus::kKeyNotInjected, "key slot is not injected");

	last_error_.clear();
	return PedStatus::kOk;
}

PedStatus InMemoryPedDevice::captureEncryptedPinBlock(
	const PedPinEntryRequest &request, PedPinBlock *pin_block)
{
	if (!pin_block)
		return fail(PedStatus::kInvalidArgument,
			    "PIN block destination is null");
	if (!initialized_)
		return fail(PedStatus::kInvalidState, "PED is not initialized");
	if (!request.key_slot)
		return fail(PedStatus::kInvalidArgument, "key slot is empty");
	if (request.min_pin_length < 4 ||
	    request.max_pin_length > 12 ||
	    request.min_pin_length > request.max_pin_length)
		return fail(PedStatus::kInvalidArgument,
			    "PIN length bounds are invalid");
	if (request.timeout_ms == 0)
		return fail(PedStatus::kInvalidArgument,
			    "PIN entry timeout is invalid");
	if (seeded_pin_.empty())
		return fail(PedStatus::kEntryUnavailable,
			    "no in-memory PIN entry is seeded");
	if (seeded_pin_.size() < request.min_pin_length ||
	    seeded_pin_.size() > request.max_pin_length)
		return fail(PedStatus::kInvalidArgument,
			    "seeded PIN length is outside request bounds");

	auto key = std::find_if(
		injected_keys_.begin(), injected_keys_.end(),
		[&request](const PedKeyInjectionRequest &candidate) {
			return key_slot_matches(candidate.slot, request.key_slot);
		});

	if (key == injected_keys_.end())
		return fail(PedStatus::kKeyNotInjected, "key slot is not injected");

	std::array<uint8_t, 8> clear_block = {};
	PedStatus status = buildIso9564PinBlock(seeded_pin_, request.pan,
						request.format, &clear_block);

	if (status != PedStatus::kOk)
		return fail(status, "ISO 9564 PIN block formation failed");

	std::array<uint8_t, 8> encrypted_block = {};
	PedPinBlockEncryptFn encrypt = encrypt_ ? encrypt_ :
						 default_encrypt_pin_block;

	status = encrypt(clear_block, *key, &encrypted_block);
	if (status != PedStatus::kOk)
		return fail(status, "PIN block encryption failed");

	pin_block->key_slot = key->slot;
	pin_block->format = request.format;
	pin_block->pin_length = seeded_pin_.size();
	pin_block->encrypted_block = encrypted_block;
	last_error_.clear();
	return PedStatus::kOk;
}

const std::string &InMemoryPedDevice::lastError() const
{
	return last_error_;
}

void InMemoryPedDevice::setPinBlockEncryptor(PedPinBlockEncryptFn encrypt)
{
	encrypt_ = std::move(encrypt);
}

void InMemoryPedDevice::seedPinEntryForTests(const std::string &pin)
{
	seeded_pin_ = pin;
}

void InMemoryPedDevice::clearSeededPinEntry()
{
	std::fill(seeded_pin_.begin(), seeded_pin_.end(), '\0');
	seeded_pin_.clear();
}

PedStatus InMemoryPedDevice::buildIso9564PinBlock(
	const std::string &pin, const std::string &pan, PinBlockFormat format,
	std::array<uint8_t, 8> *block)
{
	if (!block)
		return PedStatus::kInvalidArgument;
	if (format != PinBlockFormat::kIso9564Format0 &&
	    format != PinBlockFormat::kIso9564Format3)
		return PedStatus::kInvalidArgument;

	std::array<uint8_t, 8> pin_field = {};
	std::array<uint8_t, 8> pan_field = {};
	PedStatus status = build_pin_field(pin, format, &pin_field);

	if (status != PedStatus::kOk)
		return status;

	status = build_pan_field(pan, &pan_field);
	if (status != PedStatus::kOk)
		return status;

	for (std::size_t i = 0; i < block->size(); ++i)
		(*block)[i] = static_cast<uint8_t>(pin_field[i] ^ pan_field[i]);

	return PedStatus::kOk;
}

PedStatus InMemoryPedDevice::fail(PedStatus status,
				  const std::string &error) const
{
	last_error_ = error;
	return status;
}

const char *toString(PedStatus status)
{
	switch (status) {
	case PedStatus::kOk:
		return "ok";
	case PedStatus::kInvalidArgument:
		return "invalid_argument";
	case PedStatus::kInvalidState:
		return "invalid_state";
	case PedStatus::kKeyNotInjected:
		return "key_not_injected";
	case PedStatus::kEntryUnavailable:
		return "entry_unavailable";
	case PedStatus::kCryptoError:
		return "crypto_error";
	}

	return "unknown";
}

const char *toString(PinBlockFormat format)
{
	switch (format) {
	case PinBlockFormat::kIso9564Format0:
		return "iso9564_format_0";
	case PinBlockFormat::kIso9564Format3:
		return "iso9564_format_3";
	}

	return "unknown";
}

const char *toString(PedKeyAlgorithm algorithm)
{
	switch (algorithm) {
	case PedKeyAlgorithm::kTdesDukpt:
		return "tdes_dukpt";
	case PedKeyAlgorithm::kAesDukpt:
		return "aes_dukpt";
	}

	return "unknown";
}

} // namespace omnisight::embedded::pos::emv

#if defined(OMNISIGHT_PED_PIN_ENTRY_SMOKE_MAIN)
int main()
{
	using namespace omnisight::embedded::pos::emv;

	InMemoryPedDevice ped;
	PedStatus status = ped.initialize();

	if (status != PedStatus::kOk) {
		std::cerr << "PED initialize failed: " << toString(status)
			  << "\n";
		return 1;
	}

	PedKeyInjectionRequest key;
	key.slot.id = 7;
	key.slot.label = "smoke-pin-key";
	key.wrapped_key = {0x10, 0x32, 0x54, 0x76, 0x98, 0xba, 0xdc, 0xfe};

	status = ped.injectKey(key);
	if (status != PedStatus::kOk) {
		std::cerr << "PED key injection failed: " << ped.lastError()
			  << "\n";
		return 1;
	}

	ped.seedPinEntryForTests("1234");

	PedPinEntryRequest request;
	request.key_slot = key.slot;
	request.pan = "4761739001010010";

	PedPinBlock pin_block;
	status = ped.captureEncryptedPinBlock(request, &pin_block);
	if (status != PedStatus::kOk || pin_block.encrypted_block.size() != 8) {
		std::cerr << "PED PIN block capture failed: " << ped.lastError()
			  << "\n";
		return 1;
	}

	status = ped.deleteKey(key.slot);
	if (status != PedStatus::kOk) {
		std::cerr << "PED key delete failed: " << ped.lastError()
			  << "\n";
		return 1;
	}

	return 0;
}
#endif
