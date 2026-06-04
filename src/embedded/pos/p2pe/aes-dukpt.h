/* SPDX-License-Identifier: MIT
 *
 * Case 7 AES-DUKPT key derivation interface (OP-2038).
 */
#ifndef OMNISIGHT_EMBEDDED_POS_P2PE_AES_DUKPT_H_
#define OMNISIGHT_EMBEDDED_POS_P2PE_AES_DUKPT_H_

#include <array>
#include <cstddef>
#include <cstdint>

namespace omnisight::embedded::pos::p2pe {

class AesDukptDeriver {
public:
	static constexpr std::size_t kBlockSize = 16;
	static constexpr std::size_t kMaxKeySize = 32;
	static constexpr std::size_t kInitialKeyIdSize = 8;
	static constexpr std::size_t kTransactionIdSize = 8;

	using Key = std::array<uint8_t, kMaxKeySize>;
	using InitialKeyId = std::array<uint8_t, kInitialKeyIdSize>;
	using TransactionId = std::array<uint8_t, kTransactionIdSize>;

	enum class KeySize : std::size_t {
		kAes128 = 16,
		kAes192 = 24,
		kAes256 = 32,
	};

	enum class Status {
		kOk = 0,
		kInvalidArgument,
		kInvalidKey,
	};

	static Status deriveInitialKey(const Key &bdk, KeySize bdk_size,
				       const InitialKeyId &initial_key_id,
				       KeySize output_size, Key *initial_key);
	static Status deriveTransactionKey(const Key &initial_key,
					   KeySize initial_key_size,
					   const TransactionId &transaction_id,
					   KeySize output_size,
					   Key *transaction_key);
	static Status derivePek(const Key &bdk, KeySize bdk_size,
				const InitialKeyId &initial_key_id,
				const TransactionId &transaction_id,
				KeySize output_size, Key *pek);
};

} // namespace omnisight::embedded::pos::p2pe

#endif // OMNISIGHT_EMBEDDED_POS_P2PE_AES_DUKPT_H_
