/* SPDX-License-Identifier: MIT
 *
 * Case 7 KSN management implementation (OP-2061).
 */
#include "ksn-management.h"

#include <cerrno>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <fstream>
#include <sstream>
#include <string>
#include <sys/stat.h>
#include <unistd.h>
#include <utility>

#ifdef OMNISIGHT_KSN_MANAGEMENT_SMOKE_MAIN
#include <cstdlib>
#endif

namespace omnisight::embedded::pos::p2pe {

namespace {

constexpr char kStateMagic[] = "OMNISIGHT_KSN_V1";

bool write_all(int fd, const char *data, std::size_t len)
{
	while (len > 0) {
		const ssize_t written = write(fd, data, len);
		if (written < 0) {
			if (errno == EINTR)
				continue;
			return false;
		}

		data += written;
		len -= static_cast<std::size_t>(written);
	}

	return true;
}

} // namespace

KsnManager::KsnManager(Config config)
	: config_(std::move(config)), counter_(0), loaded_(false)
{
}

KsnManager::Status KsnManager::loadOrInitialize()
{
	if (config_.state_path.empty())
		return Status::kInvalidArgument;

	uint32_t persisted_counter = 0;
	const Status status = loadCounter(&persisted_counter);
	if (status == Status::kOk) {
		counter_ = persisted_counter;
		loaded_ = true;
		return Status::kOk;
	}
	if (status != Status::kPersistenceError)
		return status;

	counter_ = transactionCounter(config_.base_ksn);
	if (counter_ > kMaxTransactionCounter)
		return Status::kCounterExhausted;

	const Status persist_status = persistCounter(counter_);
	if (persist_status != Status::kOk)
		return persist_status;

	loaded_ = true;
	return Status::kOk;
}

KsnManager::Status KsnManager::reserveTransaction(Ksn *ksn)
{
	if (!ksn)
		return Status::kInvalidArgument;
	if (!loaded_)
		return Status::kInvalidArgument;
	if (counter_ >= kMaxTransactionCounter)
		return Status::kCounterExhausted;

	const uint32_t next_counter = counter_ + 1;
	const Status ksn_status =
		setTransactionCounter(config_.base_ksn, next_counter, ksn);
	if (ksn_status != Status::kOk)
		return ksn_status;

	const Status persist_status = persistCounter(next_counter);
	if (persist_status != Status::kOk)
		return persist_status;

	counter_ = next_counter;
	return Status::kOk;
}

KsnManager::Status KsnManager::currentKsn(Ksn *ksn) const
{
	if (!ksn)
		return Status::kInvalidArgument;
	if (!loaded_)
		return Status::kInvalidArgument;

	return setTransactionCounter(config_.base_ksn, counter_, ksn);
}

KsnManager::Status KsnManager::persist() const
{
	if (!loaded_)
		return Status::kInvalidArgument;

	return persistCounter(counter_);
}

uint32_t KsnManager::transactionCounter() const
{
	return counter_;
}

uint32_t KsnManager::remainingTransactions() const
{
	if (counter_ >= kMaxTransactionCounter)
		return 0;

	return kMaxTransactionCounter - counter_;
}

bool KsnManager::exhausted() const
{
	return counter_ >= kMaxTransactionCounter;
}

KsnManager::Status
KsnManager::setTransactionCounter(const Ksn &base_ksn, uint32_t counter,
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

uint32_t KsnManager::transactionCounter(const Ksn &ksn)
{
	return ((static_cast<uint32_t>(ksn[7]) & 0x1f) << 16) |
	       (static_cast<uint32_t>(ksn[8]) << 8) |
	       static_cast<uint32_t>(ksn[9]);
}

KsnManager::Status KsnManager::loadCounter(uint32_t *counter) const
{
	if (!counter)
		return Status::kInvalidArgument;

	std::ifstream state(config_.state_path);
	if (!state)
		return Status::kPersistenceError;

	std::string magic;
	uint32_t persisted_counter = 0;
	if (!(state >> magic >> persisted_counter))
		return Status::kCorruptState;
	if (magic != kStateMagic)
		return Status::kCorruptState;
	if (persisted_counter > kMaxTransactionCounter)
		return Status::kCounterExhausted;

	*counter = persisted_counter;
	return Status::kOk;
}

KsnManager::Status KsnManager::persistCounter(uint32_t counter) const
{
	if (config_.state_path.empty())
		return Status::kInvalidArgument;
	if (counter > kMaxTransactionCounter)
		return Status::kCounterExhausted;

	const std::string tmp_path = config_.state_path + ".tmp";
	std::ostringstream body;
	body << kStateMagic << '\n' << counter << '\n';
	const std::string payload = body.str();

	const int fd = open(tmp_path.c_str(), O_CREAT | O_TRUNC | O_WRONLY,
			    S_IRUSR | S_IWUSR);
	if (fd < 0)
		return Status::kPersistenceError;

	bool ok = write_all(fd, payload.c_str(), payload.size());
	if (ok && fsync(fd) != 0)
		ok = false;
	if (close(fd) != 0)
		ok = false;
	if (!ok) {
		unlink(tmp_path.c_str());
		return Status::kPersistenceError;
	}

	if (std::rename(tmp_path.c_str(), config_.state_path.c_str()) != 0) {
		unlink(tmp_path.c_str());
		return Status::kPersistenceError;
	}

	return Status::kOk;
}

} // namespace omnisight::embedded::pos::p2pe

#ifdef OMNISIGHT_KSN_MANAGEMENT_SMOKE_MAIN
int main()
{
	using omnisight::embedded::pos::p2pe::KsnManager;

	char state_path[] = "/tmp/omnisight-ksn-smoke-XXXXXX";
	const int fd = mkstemp(state_path);
	if (fd < 0)
		return EXIT_FAILURE;
	close(fd);
	unlink(state_path);

	const KsnManager::Ksn base_ksn = {
		0xff, 0xff, 0x98, 0x76, 0x54, 0x32, 0x10, 0xe0, 0x00, 0x00,
	};
	KsnManager manager({base_ksn, state_path});
	if (manager.loadOrInitialize() != KsnManager::Status::kOk)
		return EXIT_FAILURE;
	if (manager.transactionCounter() != 0)
		return EXIT_FAILURE;

	KsnManager::Ksn first_ksn = {};
	if (manager.reserveTransaction(&first_ksn) != KsnManager::Status::kOk)
		return EXIT_FAILURE;
	if (KsnManager::transactionCounter(first_ksn) != 1)
		return EXIT_FAILURE;

	KsnManager reloaded({base_ksn, state_path});
	if (reloaded.loadOrInitialize() != KsnManager::Status::kOk)
		return EXIT_FAILURE;
	if (reloaded.transactionCounter() != 1)
		return EXIT_FAILURE;

	KsnManager::Ksn exhausted_base = {};
	if (KsnManager::setTransactionCounter(base_ksn,
					      KsnManager::kMaxTransactionCounter,
					      &exhausted_base) !=
	    KsnManager::Status::kOk)
		return EXIT_FAILURE;
	unlink(state_path);

	KsnManager exhausted({exhausted_base, state_path});
	if (exhausted.loadOrInitialize() != KsnManager::Status::kOk)
		return EXIT_FAILURE;
	if (exhausted.reserveTransaction(&first_ksn) !=
	    KsnManager::Status::kCounterExhausted)
		return EXIT_FAILURE;

	unlink(state_path);
	return EXIT_SUCCESS;
}
#endif
