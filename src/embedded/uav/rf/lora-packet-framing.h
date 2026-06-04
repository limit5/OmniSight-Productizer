/* SPDX-License-Identifier: MIT
 *
 * Cases 6+7 LoRaWAN Class A packet framing (OP-2039).
 */
#ifndef OMNISIGHT_EMBEDDED_UAV_RF_LORA_PACKET_FRAMING_H_
#define OMNISIGHT_EMBEDDED_UAV_RF_LORA_PACKET_FRAMING_H_

#include <array>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace omnisight::embedded::uav::rf {

enum class LoraStatus {
	kOk = 0,
	kInvalidArgument,
	kFrameTooShort,
	kUnsupportedFrame,
	kMicMismatch,
};

enum class LoraDirection : uint8_t {
	kUplink = 0,
	kDownlink = 1,
};

enum class LoraFrameType : uint8_t {
	kUnconfirmedDataUp = 2,
	kUnconfirmedDataDown = 3,
	kConfirmedDataUp = 4,
	kConfirmedDataDown = 5,
};

struct LoraSessionConfig {
	uint32_t dev_addr = 0;
	std::array<uint8_t, 16> nwk_skey {};
	std::array<uint8_t, 16> app_skey {};
	uint32_t uplink_frame_counter = 0;
	uint32_t downlink_frame_counter = 0;
};

struct LoraFrameRequest {
	LoraDirection direction = LoraDirection::kUplink;
	bool confirmed = false;
	uint8_t fctrl = 0;
	std::vector<uint8_t> fopts;
	uint8_t fport = 1;
	std::vector<uint8_t> payload;
};

struct LoraFrame {
	LoraDirection direction = LoraDirection::kUplink;
	LoraFrameType type = LoraFrameType::kUnconfirmedDataUp;
	uint32_t dev_addr = 0;
	uint32_t frame_counter = 0;
	uint8_t fctrl = 0;
	std::vector<uint8_t> fopts;
	uint8_t fport = 0;
	bool has_fport = false;
	std::vector<uint8_t> payload;
};

class LoraFramer {
public:
	LoraFramer();
	explicit LoraFramer(LoraSessionConfig config);
	~LoraFramer();

	LoraFramer(const LoraFramer &) = delete;
	LoraFramer &operator=(const LoraFramer &) = delete;
	LoraFramer(LoraFramer &&) noexcept;
	LoraFramer &operator=(LoraFramer &&) noexcept;

	LoraStatus frame(const LoraFrameRequest &request,
			 std::vector<uint8_t> *phy_payload);
	LoraStatus parse(const std::vector<uint8_t> &phy_payload,
			 LoraFrame *frame);

	const LoraSessionConfig &config() const;
	const std::string &lastError() const;

private:
	class Impl;

	std::unique_ptr<Impl> impl_;
};

const char *toString(LoraStatus status);

} // namespace omnisight::embedded::uav::rf

#endif // OMNISIGHT_EMBEDDED_UAV_RF_LORA_PACKET_FRAMING_H_
