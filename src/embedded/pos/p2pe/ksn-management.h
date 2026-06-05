/* SPDX-License-Identifier: MIT
 *
 * Case 7 KSN management interface (OP-2061).
 */
#ifndef OMNISIGHT_EMBEDDED_POS_P2PE_KSN_MANAGEMENT_H_
#define OMNISIGHT_EMBEDDED_POS_P2PE_KSN_MANAGEMENT_H_

#include <array>
#include <cstddef>
#include <cstdint>
#include <string>

namespace omnisight::embedded::pos::p2pe {

class KsnManager {
public:
	static constexpr std::size_t kKsnSize = 10;
	static constexpr uint32_t kMaxTransactionCounter = 0x1fffff;

	using Ksn = std::array<uint8_t, kKsnSize>;

	enum class Status {
		kOk = 0,
		kInvalidArgument,
		kPersistenceError,
		kCorruptState,
		kCounterExhausted,
	};

	struct Config {
		Ksn base_ksn = {};
		std::string state_path;
	};

	explicit KsnManager(Config config);

	Status loadOrInitialize();
	Status reserveTransaction(Ksn *ksn);
	Status currentKsn(Ksn *ksn) const;
	Status persist() const;

	uint32_t transactionCounter() const;
	uint32_t remainingTransactions() const;
	bool exhausted() const;

	static Status setTransactionCounter(const Ksn &base_ksn, uint32_t counter,
					    Ksn *ksn);
	static uint32_t transactionCounter(const Ksn &ksn);

private:
	Status loadCounter(uint32_t *counter) const;
	Status persistCounter(uint32_t counter) const;

	Config config_;
	uint32_t counter_;
	bool loaded_;
};

} // namespace omnisight::embedded::pos::p2pe

#endif // OMNISIGHT_EMBEDDED_POS_P2PE_KSN_MANAGEMENT_H_
