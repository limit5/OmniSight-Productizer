/* SPDX-License-Identifier: MIT
 *
 * Case 7 AES-DUKPT key derivation implementation (OP-2038).
 */
#include "aes-dukpt.h"

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>

#ifdef OMNISIGHT_AES_DUKPT_SMOKE_MAIN
#include <cstdlib>
#endif

namespace omnisight::embedded::pos::p2pe {
namespace {

using Block = std::array<uint8_t, AesDukptDeriver::kBlockSize>;
using RoundKeys = std::array<uint8_t, 240>;

constexpr std::array<uint8_t, 256> kSBox = {
	0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67,
	0x2b, 0xfe, 0xd7, 0xab, 0x76, 0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59,
	0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0, 0xb7,
	0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1,
	0x71, 0xd8, 0x31, 0x15, 0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05,
	0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75, 0x09, 0x83,
	0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29,
	0xe3, 0x2f, 0x84, 0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b,
	0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf, 0xd0, 0xef, 0xaa,
	0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c,
	0x9f, 0xa8, 0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc,
	0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2, 0xcd, 0x0c, 0x13, 0xec,
	0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19,
	0x73, 0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee,
	0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb, 0xe0, 0x32, 0x3a, 0x0a, 0x49,
	0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
	0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4,
	0xea, 0x65, 0x7a, 0xae, 0x08, 0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6,
	0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a, 0x70,
	0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9,
	0x86, 0xc1, 0x1d, 0x9e, 0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e,
	0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf, 0x8c, 0xa1,
	0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0,
	0x54, 0xbb, 0x16,
};

constexpr std::array<uint8_t, 15> kRcon = {
	0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40,
	0x80, 0x1b, 0x36, 0x6c, 0xd8, 0xab, 0x4d,
};

constexpr uint16_t kUsageInitialKey = 0x8001;
constexpr uint16_t kUsageKeyDerivation = 0x8002;
constexpr uint16_t kUsagePek = 0x1000;
constexpr uint16_t kAlgorithmAes = 0x0002;

static std::size_t key_size_bytes(AesDukptDeriver::KeySize key_size)
{
	return static_cast<std::size_t>(key_size);
}

static bool valid_key_size(AesDukptDeriver::KeySize key_size)
{
	return key_size == AesDukptDeriver::KeySize::kAes128 ||
	       key_size == AesDukptDeriver::KeySize::kAes192 ||
	       key_size == AesDukptDeriver::KeySize::kAes256;
}

static bool nonzero_key(const AesDukptDeriver::Key &key, std::size_t key_size)
{
	bool nonzero = false;

	for (std::size_t i = 0; i < key_size; ++i)
		nonzero = nonzero || key[i] != 0;

	return nonzero;
}

static uint8_t xtime(uint8_t value)
{
	return static_cast<uint8_t>((value << 1) ^
				    ((value & 0x80) ? 0x1b : 0x00));
}

static void sub_word(uint8_t *word)
{
	for (std::size_t i = 0; i < 4; ++i)
		word[i] = kSBox[word[i]];
}

static void rot_word(uint8_t *word)
{
	const uint8_t first = word[0];

	word[0] = word[1];
	word[1] = word[2];
	word[2] = word[3];
	word[3] = first;
}

static unsigned rounds_for_key_size(std::size_t key_size)
{
	return static_cast<unsigned>(key_size / 4 + 6);
}

static RoundKeys expand_key(const AesDukptDeriver::Key &key,
			    std::size_t key_size)
{
	const std::size_t key_words = key_size / 4;
	const std::size_t round_key_bytes =
		AesDukptDeriver::kBlockSize * (rounds_for_key_size(key_size) + 1);
	RoundKeys round_keys = {};

	std::copy_n(key.begin(), key_size, round_keys.begin());

	for (std::size_t offset = key_size; offset < round_key_bytes;
	     offset += 4) {
		uint8_t temp[4] = {
			round_keys[offset - 4],
			round_keys[offset - 3],
			round_keys[offset - 2],
			round_keys[offset - 1],
		};
		const std::size_t word_index = offset / 4;

		if ((word_index % key_words) == 0) {
			rot_word(temp);
			sub_word(temp);
			temp[0] ^= kRcon[word_index / key_words];
		} else if (key_words > 6 && (word_index % key_words) == 4) {
			sub_word(temp);
		}

		for (std::size_t i = 0; i < 4; ++i)
			round_keys[offset + i] =
				round_keys[offset - key_size + i] ^ temp[i];
	}

	return round_keys;
}

static void add_round_key(Block *state, const RoundKeys &round_keys,
			  unsigned round)
{
	const std::size_t offset = round * AesDukptDeriver::kBlockSize;

	for (std::size_t i = 0; i < state->size(); ++i)
		(*state)[i] ^= round_keys[offset + i];
}

static void sub_bytes(Block *state)
{
	for (uint8_t &byte : *state)
		byte = kSBox[byte];
}

static void shift_rows(Block *state)
{
	Block out = *state;

	for (std::size_t row = 0; row < 4; ++row) {
		for (std::size_t column = 0; column < 4; ++column)
			out[column * 4 + row] =
				(*state)[((column + row) % 4) * 4 + row];
	}

	*state = out;
}

static void mix_columns(Block *state)
{
	for (std::size_t column = 0; column < 4; ++column) {
		const std::size_t offset = column * 4;
		const uint8_t a0 = (*state)[offset];
		const uint8_t a1 = (*state)[offset + 1];
		const uint8_t a2 = (*state)[offset + 2];
		const uint8_t a3 = (*state)[offset + 3];
		const uint8_t all = a0 ^ a1 ^ a2 ^ a3;

		(*state)[offset] ^= all ^ xtime(a0 ^ a1);
		(*state)[offset + 1] ^= all ^ xtime(a1 ^ a2);
		(*state)[offset + 2] ^= all ^ xtime(a2 ^ a3);
		(*state)[offset + 3] ^= all ^ xtime(a3 ^ a0);
	}
}

static Block aes_encrypt_block(const AesDukptDeriver::Key &key,
			       std::size_t key_size, const Block &input)
{
	const RoundKeys round_keys = expand_key(key, key_size);
	const unsigned rounds = rounds_for_key_size(key_size);
	Block state = input;

	add_round_key(&state, round_keys, 0);
	for (unsigned round = 1; round < rounds; ++round) {
		sub_bytes(&state);
		shift_rows(&state);
		mix_columns(&state);
		add_round_key(&state, round_keys, round);
	}

	sub_bytes(&state);
	shift_rows(&state);
	add_round_key(&state, round_keys, rounds);
	return state;
}

static void store_be16(uint16_t value, Block *block, std::size_t offset)
{
	(*block)[offset] = static_cast<uint8_t>(value >> 8);
	(*block)[offset + 1] = static_cast<uint8_t>(value & 0xff);
}

template <std::size_t N>
static Block make_derivation_data(uint8_t counter, uint16_t usage,
				  AesDukptDeriver::KeySize output_size,
				  const std::array<uint8_t, N> &data)
{
	Block block = {};

	block[0] = 0x01;
	block[1] = counter;
	store_be16(usage, &block, 2);
	store_be16(kAlgorithmAes, &block, 4);
	store_be16(static_cast<uint16_t>(key_size_bytes(output_size) * 8),
		   &block, 6);
	static_assert(N == 8, "AES-DUKPT derivation data is 64 bits");
	std::copy(data.begin(), data.end(), block.begin() + 8);
	return block;
}

template <std::size_t N>
static AesDukptDeriver::Status
derive_key(const AesDukptDeriver::Key &derivation_key,
	   AesDukptDeriver::KeySize derivation_key_size, uint16_t usage,
	   const std::array<uint8_t, N> &derivation_data,
	   AesDukptDeriver::KeySize output_size, AesDukptDeriver::Key *out)
{
	if (!out)
		return AesDukptDeriver::Status::kInvalidArgument;
	if (!valid_key_size(derivation_key_size) || !valid_key_size(output_size))
		return AesDukptDeriver::Status::kInvalidArgument;

	const std::size_t derivation_bytes = key_size_bytes(derivation_key_size);
	const std::size_t output_bytes = key_size_bytes(output_size);

	if (!nonzero_key(derivation_key, derivation_bytes))
		return AesDukptDeriver::Status::kInvalidKey;

	*out = {};
	for (std::size_t offset = 0; offset < output_bytes;
	     offset += AesDukptDeriver::kBlockSize) {
		const uint8_t counter =
			static_cast<uint8_t>(offset / AesDukptDeriver::kBlockSize +
					     1);
		const Block request =
			make_derivation_data(counter, usage, output_size,
					     derivation_data);
		const Block block =
			aes_encrypt_block(derivation_key, derivation_bytes,
					  request);
		const std::size_t remaining = output_bytes - offset;

		std::copy_n(block.begin(),
			    std::min(block.size(), remaining),
			    out->begin() + offset);
	}

	return AesDukptDeriver::Status::kOk;
}

} // namespace

AesDukptDeriver::Status
AesDukptDeriver::deriveInitialKey(const Key &bdk, KeySize bdk_size,
				  const InitialKeyId &initial_key_id,
				  KeySize output_size, Key *initial_key)
{
	return derive_key(bdk, bdk_size, kUsageInitialKey, initial_key_id,
			  output_size, initial_key);
}

AesDukptDeriver::Status
AesDukptDeriver::deriveTransactionKey(const Key &initial_key,
				      KeySize initial_key_size,
				      const TransactionId &transaction_id,
				      KeySize output_size, Key *transaction_key)
{
	return derive_key(initial_key, initial_key_size, kUsageKeyDerivation,
			  transaction_id, output_size, transaction_key);
}

AesDukptDeriver::Status
AesDukptDeriver::derivePek(const Key &bdk, KeySize bdk_size,
			   const InitialKeyId &initial_key_id,
			   const TransactionId &transaction_id,
			   KeySize output_size, Key *pek)
{
	if (!pek)
		return Status::kInvalidArgument;

	Key initial_key = {};
	Status status = deriveInitialKey(bdk, bdk_size, initial_key_id,
					 output_size, &initial_key);

	if (status != Status::kOk)
		return status;

	Key transaction_key = {};
	status = deriveTransactionKey(initial_key, output_size, transaction_id,
				      output_size, &transaction_key);
	if (status != Status::kOk)
		return status;

	return derive_key(transaction_key, output_size, kUsagePek,
			  transaction_id, output_size, pek);
}

} // namespace omnisight::embedded::pos::p2pe

#ifdef OMNISIGHT_AES_DUKPT_SMOKE_MAIN
int main()
{
	using omnisight::embedded::pos::p2pe::AesDukptDeriver;

	const AesDukptDeriver::Key bdk = {
		0x60, 0x3d, 0xeb, 0x10, 0x15, 0xca, 0x71, 0xbe,
		0x2b, 0x73, 0xae, 0xf0, 0x85, 0x7d, 0x77, 0x81,
		0x1f, 0x35, 0x2c, 0x07, 0x3b, 0x61, 0x08, 0xd7,
		0x2d, 0x98, 0x10, 0xa3, 0x09, 0x14, 0xdf, 0xf4,
	};
	const AesDukptDeriver::InitialKeyId initial_key_id = {
		0x12, 0x34, 0x56, 0x78, 0x90, 0xab, 0xcd, 0xef,
	};
	const AesDukptDeriver::TransactionId transaction_id = {
		0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x08,
	};
	const AesDukptDeriver::Key expected_initial_key = {
		0x85, 0x3f, 0xbc, 0x74, 0x8d, 0xa5, 0xc3, 0xea,
		0x7f, 0x70, 0x18, 0x99, 0x37, 0xc3, 0x1e, 0x7a,
	};

	AesDukptDeriver::Key initial_key = {};
	if (AesDukptDeriver::deriveInitialKey(
		    bdk, AesDukptDeriver::KeySize::kAes256, initial_key_id,
		    AesDukptDeriver::KeySize::kAes128, &initial_key) !=
	    AesDukptDeriver::Status::kOk)
		return EXIT_FAILURE;
	if (!std::equal(initial_key.begin(), initial_key.begin() + 16,
			expected_initial_key.begin()))
		return EXIT_FAILURE;

	AesDukptDeriver::Key pek128 = {};
	AesDukptDeriver::Key pek192 = {};
	AesDukptDeriver::Key pek256 = {};
	if (AesDukptDeriver::derivePek(
		    bdk, AesDukptDeriver::KeySize::kAes256, initial_key_id,
		    transaction_id, AesDukptDeriver::KeySize::kAes128,
		    &pek128) != AesDukptDeriver::Status::kOk)
		return EXIT_FAILURE;
	if (AesDukptDeriver::derivePek(
		    bdk, AesDukptDeriver::KeySize::kAes256, initial_key_id,
		    transaction_id, AesDukptDeriver::KeySize::kAes192,
		    &pek192) != AesDukptDeriver::Status::kOk)
		return EXIT_FAILURE;
	if (AesDukptDeriver::derivePek(
		    bdk, AesDukptDeriver::KeySize::kAes256, initial_key_id,
		    transaction_id, AesDukptDeriver::KeySize::kAes256,
		    &pek256) != AesDukptDeriver::Status::kOk)
		return EXIT_FAILURE;
	if (std::equal(pek128.begin(), pek128.begin() + 16,
		       initial_key.begin()))
		return EXIT_FAILURE;
	if (std::equal(pek192.begin(), pek192.begin() + 16, pek256.begin()))
		return EXIT_FAILURE;

	return EXIT_SUCCESS;
}
#endif
