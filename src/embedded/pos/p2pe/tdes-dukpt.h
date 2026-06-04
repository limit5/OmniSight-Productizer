/* SPDX-License-Identifier: MIT
 *
 * Case 7 TDES-DUKPT key derivation interface (OP-2030).
 */
#ifndef OMNISIGHT_EMBEDDED_POS_P2PE_TDES_DUKPT_H_
#define OMNISIGHT_EMBEDDED_POS_P2PE_TDES_DUKPT_H_

#include <array>
#include <cstddef>
#include <cstdint>

namespace omnisight::embedded::pos::p2pe {

class TdesDukptDeriver {
public:
	static constexpr std::size_t kBdkSize = 16;
	static constexpr std::size_t kIpekSize = 16;
	static constexpr std::size_t kKeySize = 16;
	static constexpr std::size_t kKsnSize = 10;
	static constexpr uint32_t kMaxTransactionCounter = 0x1fffff;

	using Key = std::array<uint8_t, kKeySize>;
	using Ksn = std::array<uint8_t, kKsnSize>;

	enum class Status {
		kOk = 0,
		kInvalidArgument,
		kInvalidKey,
		kCounterExhausted,
	};

	static Status deriveIpek(const Key &bdk, const Ksn &ksn, Key *ipek);
	static Status deriveTransactionKey(const Key &ipek, const Ksn &ksn,
					   Key *transaction_key);
	static Status derivePek(const Key &bdk, const Ksn &ksn, Key *pek);
	static Status setTransactionCounter(const Ksn &base_ksn, uint32_t counter,
					    Ksn *ksn);
	static uint32_t transactionCounter(const Ksn &ksn);
};

} // namespace omnisight::embedded::pos::p2pe

#endif // OMNISIGHT_EMBEDDED_POS_P2PE_TDES_DUKPT_H_
