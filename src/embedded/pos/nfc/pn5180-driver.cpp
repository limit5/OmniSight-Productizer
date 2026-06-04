/* SPDX-License-Identifier: MIT
 *
 * Cases 6+7 NXP PN5180 high-performance NFC reader driver (OP-2049).
 */
#include "pn5180-driver.h"

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <thread>
#include <utility>

#include <fcntl.h>
#include <linux/spi/spidev.h>
#include <sys/ioctl.h>
#include <unistd.h>

namespace omnisight::embedded::pos::nfc {
namespace {

constexpr uint8_t kCmdWriteRegister = 0x00;
constexpr uint8_t kCmdReadRegister = 0x04;
constexpr uint8_t kCmdSendData = 0x09;
constexpr uint8_t kCmdReadData = 0x0a;
constexpr uint8_t kCmdLoadRfConfig = 0x11;
constexpr uint8_t kCmdRfOn = 0x16;
constexpr uint8_t kCmdRfOff = 0x17;

constexpr uint8_t kSystemConfigRegister = 0x00;
constexpr uint8_t kIrqClearRegister = 0x03;
constexpr uint32_t kSystemConfigIdle = 0x00000000;
constexpr uint32_t kIrqClearAll = 0xffffffff;

constexpr uint8_t kIso14443ATxConfig = 0x00;
constexpr uint8_t kIso14443ARxConfig = 0x80;
constexpr uint8_t kIso14443BTxConfig = 0x03;
constexpr uint8_t kIso14443BRxConfig = 0x83;
constexpr uint8_t kIso15693TxConfig = 0x0d;
constexpr uint8_t kIso15693RxConfig = 0x8d;

static int close_fd(int fd)
{
	if (fd >= 0)
		(void)::close(fd);
	return -1;
}

static void append_le32(std::vector<uint8_t> *data, uint32_t value)
{
	data->push_back(static_cast<uint8_t>(value & 0xff));
	data->push_back(static_cast<uint8_t>((value >> 8) & 0xff));
	data->push_back(static_cast<uint8_t>((value >> 16) & 0xff));
	data->push_back(static_cast<uint8_t>((value >> 24) & 0xff));
}

static uint16_t le_u16(const uint8_t *data)
{
	return static_cast<uint16_t>(data[0]) |
	       (static_cast<uint16_t>(data[1]) << 8);
}

struct RfConfig {
	uint8_t tx;
	uint8_t rx;
};

static RfConfig rf_config_for(Pn5180Protocol protocol)
{
	switch (protocol) {
	case Pn5180Protocol::kIso14443A:
		return { kIso14443ATxConfig, kIso14443ARxConfig };
	case Pn5180Protocol::kIso14443B:
		return { kIso14443BTxConfig, kIso14443BRxConfig };
	case Pn5180Protocol::kIso15693:
		return { kIso15693TxConfig, kIso15693RxConfig };
	}

	return { kIso14443ATxConfig, kIso14443ARxConfig };
}

static std::vector<uint8_t> activation_command_for(Pn5180Protocol protocol)
{
	switch (protocol) {
	case Pn5180Protocol::kIso14443A:
		return { 0x26 };
	case Pn5180Protocol::kIso14443B:
		return { 0x05, 0x00, 0x08 };
	case Pn5180Protocol::kIso15693:
		return { 0x26, 0x01, 0x00 };
	}

	return {};
}

} // namespace

class Pn5180Driver::Impl {
public:
	Impl() = default;
	explicit Impl(Pn5180Config config) : config_(std::move(config)) {}
	Impl(Pn5180Config config, Pn5180Transport transport)
		: config_(std::move(config)), transport_(std::move(transport))
	{
	}
	~Impl()
	{
		close();
	}

	Impl(const Impl &) = delete;
	Impl &operator=(const Impl &) = delete;

	Pn5180Status open(const Pn5180Config &config)
	{
		if (config.spi_device.empty() && !transport_.transfer) {
			last_error_ = "spi_device is required";
			return Pn5180Status::kInvalidArgument;
		}
		if (config.spi_speed_hz == 0) {
			last_error_ = "spi_speed_hz must be greater than zero";
			return Pn5180Status::kInvalidArgument;
		}
		if (config.spi_bits_per_word == 0) {
			last_error_ = "spi_bits_per_word must be greater than zero";
			return Pn5180Status::kInvalidArgument;
		}
		if (config.max_response_size == 0) {
			last_error_ = "max_response_size must be greater than zero";
			return Pn5180Status::kInvalidArgument;
		}

		close();
		config_ = config;
		if (transport_.transfer) {
			opened_ = true;
			last_error_.clear();
			return Pn5180Status::kOk;
		}

		fd_ = ::open(config_.spi_device.c_str(), O_RDWR | O_CLOEXEC);
		if (fd_ < 0) {
			last_error_ = std::strerror(errno);
			return Pn5180Status::kIoError;
		}
		if (::ioctl(fd_, SPI_IOC_WR_MODE, &config_.spi_mode) < 0 ||
		    ::ioctl(fd_, SPI_IOC_WR_BITS_PER_WORD,
			    &config_.spi_bits_per_word) < 0 ||
		    ::ioctl(fd_, SPI_IOC_WR_MAX_SPEED_HZ,
			    &config_.spi_speed_hz) < 0) {
			last_error_ = std::strerror(errno);
			fd_ = close_fd(fd_);
			return Pn5180Status::kIoError;
		}

		opened_ = true;
		last_error_.clear();
		return Pn5180Status::kOk;
	}

	void close()
	{
		fd_ = close_fd(fd_);
		opened_ = false;
	}

	Pn5180Status init()
	{
		if (!opened_)
			return fail(Pn5180Status::kInvalidState,
				    "PN5180 SPI transport is not open");

		Pn5180Status status = write_register(kSystemConfigRegister,
						     kSystemConfigIdle);
		if (status != Pn5180Status::kOk)
			return status;
		status = write_register(kIrqClearRegister, kIrqClearAll);
		if (status != Pn5180Status::kOk)
			return status;
		status = load_rf_config(Pn5180Protocol::kIso14443A);
		if (status != Pn5180Status::kOk)
			return status;

		last_error_.clear();
		return Pn5180Status::kOk;
	}

	Pn5180Status load_rf_config(Pn5180Protocol protocol)
	{
		if (!opened_)
			return fail(Pn5180Status::kInvalidState,
				    "PN5180 SPI transport is not open");

		const RfConfig config = rf_config_for(protocol);
		Pn5180Status status = command({ kCmdRfOff }, 0, nullptr);
		if (status != Pn5180Status::kOk)
			return status;
		status = command({ kCmdLoadRfConfig, config.tx, config.rx }, 0,
				 nullptr);
		if (status != Pn5180Status::kOk)
			return status;
		status = command({ kCmdRfOn }, 0, nullptr);
		if (status != Pn5180Status::kOk)
			return status;

		last_protocol_ = protocol;
		last_error_.clear();
		return Pn5180Status::kOk;
	}

	Pn5180Status activate(Pn5180Protocol protocol, Pn5180TargetInfo *target)
	{
		if (!target)
			return fail(Pn5180Status::kInvalidArgument,
				    "target destination is null");

		std::vector<uint8_t> response;
		Pn5180Status status = exchange(protocol,
					       activation_command_for(protocol),
					       &response);
		if (status != Pn5180Status::kOk)
			return status;
		if (response.empty())
			return fail(Pn5180Status::kCardAbsent,
				    "activation response is empty");

		return parse_activation(protocol, response, target);
	}

	Pn5180Status exchange(Pn5180Protocol protocol,
			      const std::vector<uint8_t> &command_data,
			      std::vector<uint8_t> *response)
	{
		if (!response)
			return fail(Pn5180Status::kInvalidArgument,
				    "response destination is null");
		if (command_data.empty())
			return fail(Pn5180Status::kInvalidArgument,
				    "command payload is empty");
		if (!opened_)
			return fail(Pn5180Status::kInvalidState,
				    "PN5180 SPI transport is not open");

		if (protocol != last_protocol_) {
			const Pn5180Status status = load_rf_config(protocol);
			if (status != Pn5180Status::kOk)
				return status;
		}

		std::vector<uint8_t> send = { kCmdSendData, 0x00 };
		send.insert(send.end(), command_data.begin(), command_data.end());
		Pn5180Status status = command(send, 0, nullptr);
		if (status != Pn5180Status::kOk)
			return status;

		std::this_thread::sleep_for(
			std::chrono::microseconds(config_.exchange_delay_us));

		std::vector<uint8_t> raw;
		status = command({ kCmdReadData }, config_.max_response_size, &raw);
		if (status != Pn5180Status::kOk)
			return status;

		while (!raw.empty() && raw.back() == 0x00)
			raw.pop_back();
		*response = std::move(raw);
		last_error_.clear();
		return Pn5180Status::kOk;
	}

	bool open() const
	{
		return opened_;
	}

	const Pn5180Config &config() const
	{
		return config_;
	}

	const std::string &lastError() const
	{
		return last_error_;
	}

private:
	Pn5180Status fail(Pn5180Status status, const std::string &error)
	{
		last_error_ = error;
		return status;
	}

	Pn5180Status write_register(uint8_t reg, uint32_t value)
	{
		std::vector<uint8_t> payload = { kCmdWriteRegister, reg };

		append_le32(&payload, value);
		return command(payload, 0, nullptr);
	}

	Pn5180Status read_register(uint8_t reg, uint32_t *value)
	{
		if (!value)
			return fail(Pn5180Status::kInvalidArgument,
				    "register destination is null");

		std::vector<uint8_t> response;
		Pn5180Status status = command({ kCmdReadRegister, reg }, 4,
					      &response);
		if (status != Pn5180Status::kOk)
			return status;
		if (response.size() < 4)
			return fail(Pn5180Status::kProtocolError,
				    "register response is short");

		*value = static_cast<uint32_t>(response[0]) |
			 (static_cast<uint32_t>(response[1]) << 8) |
			 (static_cast<uint32_t>(response[2]) << 16) |
			 (static_cast<uint32_t>(response[3]) << 24);
		return Pn5180Status::kOk;
	}

	Pn5180Status command(const std::vector<uint8_t> &tx, std::size_t rx_len,
			     std::vector<uint8_t> *rx)
	{
		if (transport_.transfer)
			return transport_.transfer(tx, rx_len, rx);
		if (fd_ < 0)
			return fail(Pn5180Status::kInvalidState,
				    "PN5180 SPI fd is closed");

		std::vector<uint8_t> local_rx(tx.size() + rx_len);
		std::vector<uint8_t> local_tx = tx;
		local_tx.resize(local_rx.size(), 0x00);

		spi_ioc_transfer transfer {};
		transfer.tx_buf = reinterpret_cast<unsigned long>(local_tx.data());
		transfer.rx_buf = reinterpret_cast<unsigned long>(local_rx.data());
		transfer.len = static_cast<uint32_t>(local_tx.size());
		transfer.speed_hz = config_.spi_speed_hz;
		transfer.bits_per_word = config_.spi_bits_per_word;

		if (::ioctl(fd_, SPI_IOC_MESSAGE(1), &transfer) < 0) {
			last_error_ = std::strerror(errno);
			return Pn5180Status::kIoError;
		}

		if (rx) {
			rx->assign(local_rx.begin() +
					   static_cast<std::ptrdiff_t>(tx.size()),
				   local_rx.end());
		}
		return Pn5180Status::kOk;
	}

	Pn5180Status parse_activation(Pn5180Protocol protocol,
				      const std::vector<uint8_t> &response,
				      Pn5180TargetInfo *target)
	{
		Pn5180TargetInfo parsed;
		parsed.protocol = protocol;

		switch (protocol) {
		case Pn5180Protocol::kIso14443A:
			if (response.size() < 2)
				return fail(Pn5180Status::kProtocolError,
					    "ISO 14443-A ATQA response is short");
			parsed.atqa = le_u16(response.data());
			if (response.size() > 2)
				parsed.uid.assign(response.begin() + 2,
						  response.end());
			break;
		case Pn5180Protocol::kIso14443B:
			if (response.size() < 12 || response[0] != 0x50)
				return fail(Pn5180Status::kProtocolError,
					    "ISO 14443-B ATQB response is invalid");
			parsed.uid.assign(response.begin() + 1,
					  response.begin() + 5);
			parsed.application_data.assign(response.begin() + 5,
						       response.begin() + 9);
			parsed.protocol_info.assign(response.begin() + 9,
						    response.begin() + 12);
			break;
		case Pn5180Protocol::kIso15693:
			if (response.size() < 10 || response[0] != 0x00)
				return fail(Pn5180Status::kProtocolError,
					    "ISO 15693 inventory response is invalid");
			parsed.dsfid = response[1];
			parsed.uid.assign(response.begin() + 2,
					  response.begin() + 10);
			break;
		}

		*target = std::move(parsed);
		last_error_.clear();
		return Pn5180Status::kOk;
	}

	Pn5180Config config_;
	Pn5180Transport transport_;
	Pn5180Protocol last_protocol_ = Pn5180Protocol::kIso14443A;
	int fd_ = -1;
	bool opened_ = false;
	std::string last_error_;
};

Pn5180Driver::Pn5180Driver() : impl_(std::make_unique<Impl>()) {}

Pn5180Driver::Pn5180Driver(Pn5180Config config)
	: impl_(std::make_unique<Impl>(std::move(config)))
{
}

Pn5180Driver::Pn5180Driver(Pn5180Config config, Pn5180Transport transport)
	: impl_(std::make_unique<Impl>(std::move(config), std::move(transport)))
{
}

Pn5180Driver::~Pn5180Driver() = default;
Pn5180Driver::Pn5180Driver(Pn5180Driver &&) noexcept = default;
Pn5180Driver &
Pn5180Driver::operator=(Pn5180Driver &&) noexcept = default;

Pn5180Status Pn5180Driver::open(const Pn5180Config &config)
{
	return impl_->open(config);
}

void Pn5180Driver::close()
{
	impl_->close();
}

Pn5180Status Pn5180Driver::init()
{
	return impl_->init();
}

Pn5180Status Pn5180Driver::loadRfConfig(Pn5180Protocol protocol)
{
	return impl_->load_rf_config(protocol);
}

Pn5180Status Pn5180Driver::activate(Pn5180Protocol protocol,
				    Pn5180TargetInfo *target)
{
	return impl_->activate(protocol, target);
}

Pn5180Status Pn5180Driver::exchange(Pn5180Protocol protocol,
				    const std::vector<uint8_t> &command,
				    std::vector<uint8_t> *response)
{
	return impl_->exchange(protocol, command, response);
}

bool Pn5180Driver::open() const
{
	return impl_->open();
}

const Pn5180Config &Pn5180Driver::config() const
{
	return impl_->config();
}

const std::string &Pn5180Driver::lastError() const
{
	return impl_->lastError();
}

const char *toString(Pn5180Status status)
{
	switch (status) {
	case Pn5180Status::kOk:
		return "ok";
	case Pn5180Status::kInvalidArgument:
		return "invalid-argument";
	case Pn5180Status::kInvalidState:
		return "invalid-state";
	case Pn5180Status::kCardAbsent:
		return "card-absent";
	case Pn5180Status::kCollision:
		return "collision";
	case Pn5180Status::kTimeout:
		return "timeout";
	case Pn5180Status::kIoError:
		return "io-error";
	case Pn5180Status::kProtocolError:
		return "protocol-error";
	}

	return "unknown";
}

const char *toString(Pn5180Protocol protocol)
{
	switch (protocol) {
	case Pn5180Protocol::kIso14443A:
		return "iso14443-a";
	case Pn5180Protocol::kIso14443B:
		return "iso14443-b";
	case Pn5180Protocol::kIso15693:
		return "iso15693";
	}

	return "unknown";
}

} // namespace omnisight::embedded::pos::nfc

#if defined(OMNISIGHT_PN5180_DRIVER_SMOKE_MAIN)
#include <iostream>

int main()
{
	using namespace omnisight::embedded::pos::nfc;

	std::vector<std::vector<uint8_t>> commands;
	Pn5180Transport transport;
	transport.transfer = [&commands](const std::vector<uint8_t> &tx,
					 std::size_t rx_len,
					 std::vector<uint8_t> *rx) {
		commands.push_back(tx);
		if (!rx)
			return Pn5180Status::kOk;
		rx->assign(rx_len, 0x00);
		if (!tx.empty() && tx[0] == kCmdReadData && rx_len >= 4)
			*rx = { 0x04, 0x00, 0x04, 0xa1 };
		return Pn5180Status::kOk;
	};

	Pn5180Config config;
	config.spi_device.clear();
	config.exchange_delay_us = 0;
	config.max_response_size = 16;

	Pn5180Driver driver(config, transport);
	if (driver.open(config) != Pn5180Status::kOk) {
		std::cerr << "PN5180 smoke open failed: "
			  << driver.lastError() << '\n';
		return 1;
	}
	if (driver.init() != Pn5180Status::kOk) {
		std::cerr << "PN5180 smoke init failed: "
			  << driver.lastError() << '\n';
		return 1;
	}

	Pn5180TargetInfo target;
	if (driver.activate(Pn5180Protocol::kIso14443A, &target) !=
	    Pn5180Status::kOk) {
		std::cerr << "PN5180 smoke activation failed: "
			  << driver.lastError() << '\n';
		return 1;
	}
	if (target.protocol != Pn5180Protocol::kIso14443A ||
	    target.atqa != 0x0004 ||
	    target.uid != std::vector<uint8_t>({ 0x04, 0xa1 })) {
		std::cerr << "PN5180 smoke parsed unexpected Type A target\n";
		return 1;
	}

	std::vector<uint8_t> response;
	if (driver.exchange(Pn5180Protocol::kIso14443A, { 0x30, 0x04 },
			    &response) != Pn5180Status::kOk ||
	    response.empty()) {
		std::cerr << "PN5180 smoke exchange failed: "
			  << driver.lastError() << '\n';
		return 1;
	}

	const bool saw_load_rf = std::any_of(
		commands.begin(), commands.end(), [](const std::vector<uint8_t> &cmd) {
			return cmd.size() == 3 && cmd[0] == kCmdLoadRfConfig;
		});
	if (!saw_load_rf) {
		std::cerr << "PN5180 smoke did not issue LoadRfConfig\n";
		return 1;
	}

	return 0;
}
#endif
