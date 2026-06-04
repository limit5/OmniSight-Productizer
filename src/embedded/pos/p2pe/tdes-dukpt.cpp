/* SPDX-License-Identifier: MIT
 *
 * Case 7 TDES-DUKPT key derivation implementation (OP-2030).
 */
#include "tdes-dukpt.h"

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>

#ifdef OMNISIGHT_TDES_DUKPT_SMOKE_MAIN
#include <cstdlib>
#endif

namespace omnisight::embedded::pos::p2pe {
namespace {

using Block = std::array<uint8_t, 8>;
using DesSubkeys = std::array<uint64_t, 16>;

constexpr std::array<uint8_t, 64> kInitialPermutation = {
	58, 50, 42, 34, 26, 18, 10, 2,  60, 52, 44, 36, 28, 20, 12,
	4,  62, 54, 46, 38, 30, 22, 14, 6,  64, 56, 48, 40, 32, 24,
	16, 8,  57, 49, 41, 33, 25, 17, 9,  1,  59, 51, 43, 35, 27,
	19, 11, 3,  61, 53, 45, 37, 29, 21, 13, 5,  63, 55, 47, 39,
	31, 23, 15, 7,
};

constexpr std::array<uint8_t, 64> kFinalPermutation = {
	40, 8, 48, 16, 56, 24, 64, 32, 39, 7, 47, 15, 55, 23, 63, 31,
	38, 6, 46, 14, 54, 22, 62, 30, 37, 5, 45, 13, 53, 21, 61, 29,
	36, 4, 44, 12, 52, 20, 60, 28, 35, 3, 43, 11, 51, 19, 59, 27,
	34, 2, 42, 10, 50, 18, 58, 26, 33, 1, 41, 9,  49, 17, 57, 25,
};

constexpr std::array<uint8_t, 48> kExpansionPermutation = {
	32, 1,  2,  3,  4,  5,  4,  5,  6,  7,  8,  9,
	8,  9,  10, 11, 12, 13, 12, 13, 14, 15, 16, 17,
	16, 17, 18, 19, 20, 21, 20, 21, 22, 23, 24, 25,
	24, 25, 26, 27, 28, 29, 28, 29, 30, 31, 32, 1,
};

constexpr std::array<uint8_t, 32> kPBoxPermutation = {
	16, 7, 20, 21, 29, 12, 28, 17, 1,  15, 23, 26, 5,  18, 31, 10,
	2,  8, 24, 14, 32, 27, 3,  9,  19, 13, 30, 6,  22, 11, 4,  25,
};

constexpr std::array<uint8_t, 56> kPermutedChoice1 = {
	57, 49, 41, 33, 25, 17, 9,  1,  58, 50, 42, 34, 26, 18,
	10, 2,  59, 51, 43, 35, 27, 19, 11, 3,  60, 52, 44, 36,
	63, 55, 47, 39, 31, 23, 15, 7,  62, 54, 46, 38, 30, 22,
	14, 6,  61, 53, 45, 37, 29, 21, 13, 5,  28, 20, 12, 4,
};

constexpr std::array<uint8_t, 48> kPermutedChoice2 = {
	14, 17, 11, 24, 1,  5,  3,  28, 15, 6,  21, 10,
	23, 19, 12, 4,  26, 8,  16, 7,  27, 20, 13, 2,
	41, 52, 31, 37, 47, 55, 30, 40, 51, 45, 33, 48,
	44, 49, 39, 56, 34, 53, 46, 42, 50, 36, 29, 32,
};

constexpr std::array<uint8_t, 16> kKeyShifts = {
	1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1,
};

constexpr uint8_t kSBoxes[8][64] = {
	{ 14, 4,  13, 1,  2,  15, 11, 8,  3,  10, 6,  12, 5,
	  9,  0,  7,  0,  15, 7,  4,  14, 2,  13, 1,  10, 6,
	  12, 11, 9,  5,  3,  8,  4,  1,  14, 8,  13, 6,  2,
	  11, 15, 12, 9,  7,  3,  10, 5,  0,  15, 12, 8,  2,
	  4,  9,  1,  7,  5,  11, 3,  14, 10, 0,  6,  13 },
	{ 15, 1,  8,  14, 6,  11, 3,  4,  9,  7,  2,  13, 12,
	  0,  5,  10, 3,  13, 4,  7,  15, 2,  8,  14, 12, 0,
	  1,  10, 6,  9,  11, 5,  0,  14, 7,  11, 10, 4,  13,
	  1,  5,  8,  12, 6,  9,  3,  2,  15, 13, 8,  10, 1,
	  3,  15, 4,  2,  11, 6,  7,  12, 0,  5,  14, 9 },
	{ 10, 0,  9,  14, 6,  3,  15, 5,  1,  13, 12, 7,  11,
	  4,  2,  8,  13, 7,  0,  9,  3,  4,  6,  10, 2,  8,
	  5,  14, 12, 11, 15, 1,  13, 6,  4,  9,  8,  15, 3,
	  0,  11, 1,  2,  12, 5,  10, 14, 7,  1,  10, 13, 0,
	  6,  9,  8,  7,  4,  15, 14, 3,  11, 5,  2,  12 },
	{ 7,  13, 14, 3,  0,  6,  9,  10, 1,  2,  8,  5,  11,
	  12, 4,  15, 13, 8,  11, 5,  6,  15, 0,  3,  4,  7,
	  2,  12, 1,  10, 14, 9,  10, 6,  9,  0,  12, 11, 7,
	  13, 15, 1,  3,  14, 5,  2,  8,  4,  3,  15, 0,  6,
	  10, 1,  13, 8,  9,  4,  5,  11, 12, 7,  2,  14 },
	{ 2,  12, 4,  1,  7,  10, 11, 6,  8,  5,  3,  15, 13,
	  0,  14, 9,  14, 11, 2,  12, 4,  7,  13, 1,  5,  0,
	  15, 10, 3,  9,  8,  6,  4,  2,  1,  11, 10, 13, 7,
	  8,  15, 9,  12, 5,  6,  3,  0,  14, 11, 8,  12, 7,
	  1,  14, 2,  13, 6,  15, 0,  9,  10, 4,  5,  3 },
	{ 12, 1,  10, 15, 9,  2,  6,  8,  0,  13, 3,  4,  14,
	  7,  5,  11, 10, 15, 4,  2,  7,  12, 9,  5,  6,  1,
	  13, 14, 0,  11, 3,  8,  9,  14, 15, 5,  2,  8,  12,
	  3,  7,  0,  4,  10, 1,  13, 11, 6,  4,  3,  2,  12,
	  9,  5,  15, 10, 11, 14, 1,  7,  6,  0,  8,  13 },
	{ 4,  11, 2,  14, 15, 0,  8,  13, 3,  12, 9,  7,  5,
	  10, 6,  1,  13, 0,  11, 7,  4,  9,  1,  10, 14, 3,
	  5,  12, 2,  15, 8,  6,  1,  4,  11, 13, 12, 3,  7,
	  14, 10, 15, 6,  8,  0,  5,  9,  2,  6,  11, 13, 8,
	  1,  4,  10, 7,  9,  5,  0,  15, 14, 2,  3,  12 },
	{ 13, 2,  8,  4,  6,  15, 11, 1,  10, 9,  3,  14, 5,
	  0,  12, 7,  1,  15, 13, 8,  10, 3,  7,  4,  12, 5,
	  6,  11, 0,  14, 9,  2,  7,  11, 4,  1,  9,  12, 14,
	  2,  0,  6,  10, 13, 15, 3,  5,  8,  2,  1,  14, 7,
	  4,  10, 8,  13, 15, 12, 9,  0,  3,  5,  6,  11 },
};

constexpr TdesDukptDeriver::Key kDukptKeyMask = {
	0xc0, 0xc0, 0xc0, 0xc0, 0x00, 0x00, 0x00, 0x00,
	0xc0, 0xc0, 0xc0, 0xc0, 0x00, 0x00, 0x00, 0x00,
};

constexpr TdesDukptDeriver::Key kPekVariantMask = {
	0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0xff,
	0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0xff,
};

template <std::size_t N>
static uint64_t pack_bits(const std::array<uint8_t, N> &bytes)
{
	uint64_t value = 0;

	for (uint8_t byte : bytes)
		value = (value << 8) | byte;

	return value;
}

template <std::size_t N>
static uint64_t permute(uint64_t input, std::size_t input_bits,
			const std::array<uint8_t, N> &table)
{
	uint64_t output = 0;

	for (uint8_t position : table) {
		output <<= 1;
		output |= (input >> (input_bits - position)) & 0x1;
	}

	return output;
}

static uint32_t rotate_left_28(uint32_t value, uint8_t shift)
{
	return ((value << shift) | (value >> (28 - shift))) & 0x0fffffff;
}

static DesSubkeys make_des_subkeys(const Block &key)
{
	const uint64_t permuted = permute(pack_bits(key), 64, kPermutedChoice1);
	uint32_t c = static_cast<uint32_t>((permuted >> 28) & 0x0fffffff);
	uint32_t d = static_cast<uint32_t>(permuted & 0x0fffffff);
	DesSubkeys subkeys = {};

	for (std::size_t i = 0; i < subkeys.size(); ++i) {
		c = rotate_left_28(c, kKeyShifts[i]);
		d = rotate_left_28(d, kKeyShifts[i]);
		subkeys[i] =
			permute((static_cast<uint64_t>(c) << 28) | d, 56,
				kPermutedChoice2);
	}

	return subkeys;
}

static uint32_t des_round(uint32_t right, uint64_t subkey)
{
	const uint64_t expanded =
		permute(static_cast<uint64_t>(right) << 32, 64,
			kExpansionPermutation) ^
		subkey;
	uint32_t substituted = 0;

	for (std::size_t i = 0; i < 8; ++i) {
		const uint8_t chunk =
			static_cast<uint8_t>((expanded >> (42 - 6 * i)) & 0x3f);
		const uint8_t row = ((chunk & 0x20) >> 4) | (chunk & 0x01);
		const uint8_t column = (chunk >> 1) & 0x0f;
		substituted =
			(substituted << 4) | kSBoxes[i][row * 16 + column];
	}

	return static_cast<uint32_t>(
		permute(static_cast<uint64_t>(substituted) << 32, 64,
			kPBoxPermutation));
}

static Block des_crypt(const Block &key, const Block &input, bool decrypt)
{
	const DesSubkeys subkeys = make_des_subkeys(key);
	const uint64_t permuted =
		permute(pack_bits(input), 64, kInitialPermutation);
	uint32_t left = static_cast<uint32_t>(permuted >> 32);
	uint32_t right = static_cast<uint32_t>(permuted & 0xffffffff);

	for (std::size_t i = 0; i < subkeys.size(); ++i) {
		const uint64_t subkey =
			decrypt ? subkeys[subkeys.size() - 1 - i] : subkeys[i];
		const uint32_t next_left = right;
		right = left ^ des_round(right, subkey);
		left = next_left;
	}

	const uint64_t preoutput =
		(static_cast<uint64_t>(right) << 32) | static_cast<uint64_t>(left);
	const uint64_t output = permute(preoutput, 64, kFinalPermutation);
	Block block = {};

	for (std::size_t i = 0; i < block.size(); ++i)
		block[i] = static_cast<uint8_t>(output >> (56 - 8 * i));

	return block;
}

static Block tdes_encrypt_2key(const TdesDukptDeriver::Key &key,
			       const Block &input)
{
	Block key_left = {};
	Block key_right = {};
	std::copy_n(key.begin(), key_left.size(), key_left.begin());
	std::copy_n(key.begin() + key_left.size(), key_right.size(),
		    key_right.begin());

	return des_crypt(key_left,
			 des_crypt(key_right, des_crypt(key_left, input, false),
				   true),
			 false);
}

static Block ipek_register_from_ksn(const TdesDukptDeriver::Ksn &ksn)
{
	Block reg = {};

	std::copy_n(ksn.begin(), reg.size(), reg.begin());
	reg[7] &= 0xe0;
	return reg;
}

static Block derivation_register_from_ksn(const TdesDukptDeriver::Ksn &ksn)
{
	Block reg = {};

	std::copy_n(ksn.begin() + 2, reg.size(), reg.begin());
	reg[5] &= 0xe0;
	reg[6] = 0x00;
	reg[7] = 0x00;
	return reg;
}

static void apply_counter_to_register(Block *reg, uint32_t counter)
{
	(*reg)[5] = static_cast<uint8_t>(((*reg)[5] & 0xe0) |
					 ((counter >> 16) & 0x1f));
	(*reg)[6] = static_cast<uint8_t>((counter >> 8) & 0xff);
	(*reg)[7] = static_cast<uint8_t>(counter & 0xff);
}

static bool valid_des_component(const TdesDukptDeriver::Key &key,
				std::size_t offset)
{
	bool nonzero = false;

	for (std::size_t i = 0; i < 8; ++i)
		nonzero = nonzero || key[offset + i] != 0;

	return nonzero;
}

static bool valid_tdes_key(const TdesDukptDeriver::Key &key)
{
	return valid_des_component(key, 0) && valid_des_component(key, 8) &&
	       !std::equal(key.begin(), key.begin() + 8, key.begin() + 8);
}

static TdesDukptDeriver::Key xor_key(const TdesDukptDeriver::Key &key,
				     const TdesDukptDeriver::Key &mask)
{
	TdesDukptDeriver::Key out = {};

	for (std::size_t i = 0; i < out.size(); ++i)
		out[i] = key[i] ^ mask[i];

	return out;
}

static Block xor_right_half_with_register(const TdesDukptDeriver::Key &key,
					  const Block &ksn_reg)
{
	Block out = {};

	for (std::size_t i = 0; i < out.size(); ++i)
		out[i] = key[i + 8] ^ ksn_reg[i];

	return out;
}

static Block des_left_half_then_xor_right(const TdesDukptDeriver::Key &key,
					  const Block &ksn_reg)
{
	Block left = {};
	std::copy_n(key.begin(), left.size(), left.begin());

	Block out = des_crypt(left, xor_right_half_with_register(key, ksn_reg),
			      false);
	for (std::size_t i = 0; i < out.size(); ++i)
		out[i] ^= key[i + 8];

	return out;
}

static TdesDukptDeriver::Key derive_next_key(const TdesDukptDeriver::Key &key,
					     const Block &ksn_reg)
{
	const TdesDukptDeriver::Key masked_key = xor_key(key, kDukptKeyMask);
	const Block left = des_left_half_then_xor_right(masked_key, ksn_reg);
	const Block right = des_left_half_then_xor_right(key, ksn_reg);
	TdesDukptDeriver::Key out = {};

	std::copy(left.begin(), left.end(), out.begin());
	std::copy(right.begin(), right.end(), out.begin() + left.size());
	return out;
}

} // namespace

TdesDukptDeriver::Status TdesDukptDeriver::deriveIpek(const Key &bdk,
						      const Ksn &ksn,
						      Key *ipek)
{
	if (!ipek)
		return Status::kInvalidArgument;
	if (!valid_tdes_key(bdk))
		return Status::kInvalidKey;

	const Block initial_ksn = ipek_register_from_ksn(ksn);
	const Block left = tdes_encrypt_2key(bdk, initial_ksn);
	const Key masked_bdk = xor_key(bdk, kDukptKeyMask);
	const Block right = tdes_encrypt_2key(masked_bdk, initial_ksn);

	std::copy(left.begin(), left.end(), ipek->begin());
	std::copy(right.begin(), right.end(), ipek->begin() + left.size());
	return Status::kOk;
}

TdesDukptDeriver::Status
TdesDukptDeriver::deriveTransactionKey(const Key &ipek, const Ksn &ksn,
				       Key *transaction_key)
{
	if (!transaction_key)
		return Status::kInvalidArgument;
	if (!valid_tdes_key(ipek))
		return Status::kInvalidKey;

	const uint32_t counter = transactionCounter(ksn);

	if (counter > kMaxTransactionCounter)
		return Status::kCounterExhausted;

	Key current_key = ipek;
	Block ksn_reg = derivation_register_from_ksn(ksn);
	uint32_t active_counter = 0;

	for (uint32_t shift = 0x100000; shift != 0; shift >>= 1) {
		if ((counter & shift) == 0)
			continue;
		active_counter |= shift;
		apply_counter_to_register(&ksn_reg, active_counter);
		current_key = derive_next_key(current_key, ksn_reg);
	}

	*transaction_key = current_key;
	return Status::kOk;
}

TdesDukptDeriver::Status TdesDukptDeriver::derivePek(const Key &bdk,
						     const Ksn &ksn, Key *pek)
{
	if (!pek)
		return Status::kInvalidArgument;

	Key ipek = {};
	Status status = deriveIpek(bdk, ksn, &ipek);

	if (status != Status::kOk)
		return status;

	status = deriveTransactionKey(ipek, ksn, pek);
	if (status != Status::kOk)
		return status;

	*pek = xor_key(*pek, kPekVariantMask);
	return Status::kOk;
}

TdesDukptDeriver::Status
TdesDukptDeriver::setTransactionCounter(const Ksn &base_ksn, uint32_t counter,
					Ksn *ksn)
{
	if (!ksn)
		return Status::kInvalidArgument;
	if (counter > kMaxTransactionCounter)
		return Status::kCounterExhausted;

	*ksn = base_ksn;
	(*ksn)[7] = static_cast<uint8_t>(((*ksn)[7] & 0xe0) |
					 ((counter >> 16) & 0x1f));
	(*ksn)[8] = static_cast<uint8_t>((counter >> 8) & 0xff);
	(*ksn)[9] = static_cast<uint8_t>(counter & 0xff);
	return Status::kOk;
}

uint32_t TdesDukptDeriver::transactionCounter(const Ksn &ksn)
{
	return ((static_cast<uint32_t>(ksn[7]) & 0x1f) << 16) |
	       (static_cast<uint32_t>(ksn[8]) << 8) |
	       static_cast<uint32_t>(ksn[9]);
}

} // namespace omnisight::embedded::pos::p2pe

#ifdef OMNISIGHT_TDES_DUKPT_SMOKE_MAIN
int main()
{
	using omnisight::embedded::pos::p2pe::TdesDukptDeriver;

	const TdesDukptDeriver::Key bdk = {
		0x01, 0x23, 0x45, 0x67, 0x89, 0xab, 0xcd, 0xef,
		0xfe, 0xdc, 0xba, 0x98, 0x76, 0x54, 0x32, 0x10,
	};
	const TdesDukptDeriver::Ksn initial_ksn = {
		0xff, 0xff, 0x98, 0x76, 0x54, 0x32, 0x10, 0xe0, 0x00, 0x00,
	};
	const TdesDukptDeriver::Key expected_ipek = {
		0x6a, 0xc2, 0x92, 0xfa, 0xa1, 0x31, 0x5b, 0x4d,
		0x85, 0x8a, 0xb3, 0xa3, 0xd7, 0xd5, 0x93, 0x3a,
	};

	TdesDukptDeriver::Key ipek = {};
	if (TdesDukptDeriver::deriveIpek(bdk, initial_ksn, &ipek) !=
	    TdesDukptDeriver::Status::kOk)
		return EXIT_FAILURE;
	if (ipek != expected_ipek)
		return EXIT_FAILURE;

	TdesDukptDeriver::Ksn transaction_ksn = {};
	TdesDukptDeriver::Key pek = {};
	if (TdesDukptDeriver::setTransactionCounter(initial_ksn, 8,
						    &transaction_ksn) !=
	    TdesDukptDeriver::Status::kOk)
		return EXIT_FAILURE;
	if (TdesDukptDeriver::derivePek(bdk, transaction_ksn, &pek) !=
	    TdesDukptDeriver::Status::kOk)
		return EXIT_FAILURE;
	if (pek == ipek)
		return EXIT_FAILURE;
	if (TdesDukptDeriver::transactionCounter(transaction_ksn) != 8)
		return EXIT_FAILURE;

	return EXIT_SUCCESS;
}
#endif
