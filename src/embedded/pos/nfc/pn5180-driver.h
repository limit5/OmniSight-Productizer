/* SPDX-License-Identifier: MIT
 *
 * Cases 6+7 NXP PN5180 high-performance NFC reader driver (OP-2049).
 */
#ifndef OMNISIGHT_EMBEDDED_POS_NFC_PN5180_DRIVER_H_
#define OMNISIGHT_EMBEDDED_POS_NFC_PN5180_DRIVER_H_

#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <vector>

namespace omnisight::embedded::pos::nfc {

enum class Pn5180Status {
	kOk = 0,
	kInvalidArgument,
	kInvalidState,
	kCardAbsent,
	kCollision,
	kTimeout,
	kIoError,
	kProtocolError,
};

enum class Pn5180Protocol {
	kIso14443A = 0,
	kIso14443B,
	kIso15693,
};

struct Pn5180Config {
	std::string spi_device = "/dev/spidev0.0";
	uint32_t spi_speed_hz = 7000000;
	uint8_t spi_mode = 0;
	uint8_t spi_bits_per_word = 8;
	uint32_t exchange_delay_us = 5000;
	std::size_t max_response_size = 256;
};

struct Pn5180TargetInfo {
	Pn5180Protocol protocol = Pn5180Protocol::kIso14443A;
	std::vector<uint8_t> uid;
	uint16_t atqa = 0;
	uint8_t sak = 0;
	std::vector<uint8_t> application_data;
	std::vector<uint8_t> protocol_info;
	uint8_t dsfid = 0;
};

using Pn5180SpiTransferFn =
	std::function<Pn5180Status(const std::vector<uint8_t> &tx,
				  std::size_t rx_len,
				  std::vector<uint8_t> *rx)>;

struct Pn5180Transport {
	Pn5180SpiTransferFn transfer;
};

class Pn5180Driver {
public:
	Pn5180Driver();
	explicit Pn5180Driver(Pn5180Config config);
	Pn5180Driver(Pn5180Config config, Pn5180Transport transport);
	~Pn5180Driver();

	Pn5180Driver(const Pn5180Driver &) = delete;
	Pn5180Driver &operator=(const Pn5180Driver &) = delete;
	Pn5180Driver(Pn5180Driver &&) noexcept;
	Pn5180Driver &operator=(Pn5180Driver &&) noexcept;

	Pn5180Status open(const Pn5180Config &config);
	void close();

	Pn5180Status init();
	Pn5180Status loadRfConfig(Pn5180Protocol protocol);
	Pn5180Status activate(Pn5180Protocol protocol, Pn5180TargetInfo *target);
	Pn5180Status exchange(Pn5180Protocol protocol,
			      const std::vector<uint8_t> &command,
			      std::vector<uint8_t> *response);

	bool open() const;
	const Pn5180Config &config() const;
	const std::string &lastError() const;

private:
	class Impl;

	std::unique_ptr<Impl> impl_;
};

const char *toString(Pn5180Status status);
const char *toString(Pn5180Protocol protocol);

} // namespace omnisight::embedded::pos::nfc

#endif // OMNISIGHT_EMBEDDED_POS_NFC_PN5180_DRIVER_H_
