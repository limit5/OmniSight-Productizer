/* SPDX-License-Identifier: MIT
 *
 * Cases 6+7 MAVLink transport wrapper (OP-2023).
 */
#ifndef OMNISIGHT_EMBEDDED_UAV_MAVLINK_TRANSPORT_H_
#define OMNISIGHT_EMBEDDED_UAV_MAVLINK_TRANSPORT_H_

#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <vector>

namespace omnisight::embedded::uav::mavlink {

enum class MavlinkStatus {
	kOk = 0,
	kInvalidArgument,
	kInvalidState,
	kUnsupportedMessage,
	kParseError,
	kIoError,
};

enum class MavlinkBackend {
	kSerial = 0,
	kUdp,
};

enum class MavlinkMessageId : uint32_t {
	kHeartbeat = 0,
	kAttitude = 30,
	kGlobalPositionInt = 33,
	kCommandLong = 76,
};

struct MavlinkSerialConfig {
	std::string device;
	uint32_t baud_rate = 57600;
};

struct MavlinkUdpConfig {
	std::string local_address = "0.0.0.0";
	uint16_t local_port = 14550;
	std::string remote_address = "127.0.0.1";
	uint16_t remote_port = 14550;
};

struct MavlinkTransportConfig {
	MavlinkBackend backend = MavlinkBackend::kUdp;
	uint8_t system_id = 1;
	uint8_t component_id = 1;
	MavlinkSerialConfig serial;
	MavlinkUdpConfig udp;
};

struct MavlinkMessage {
	uint32_t message_id = 0;
	uint8_t system_id = 0;
	uint8_t component_id = 0;
	std::vector<uint8_t> payload;
};

struct MavlinkHeartbeat {
	uint32_t custom_mode = 0;
	uint8_t type = 0;
	uint8_t autopilot = 0;
	uint8_t base_mode = 0;
	uint8_t system_status = 0;
	uint8_t mavlink_version = 3;
};

struct MavlinkAttitude {
	uint32_t time_boot_ms = 0;
	float roll = 0.0F;
	float pitch = 0.0F;
	float yaw = 0.0F;
	float rollspeed = 0.0F;
	float pitchspeed = 0.0F;
	float yawspeed = 0.0F;
};

struct MavlinkGlobalPositionInt {
	uint32_t time_boot_ms = 0;
	int32_t lat = 0;
	int32_t lon = 0;
	int32_t alt = 0;
	int32_t relative_alt = 0;
	int16_t vx = 0;
	int16_t vy = 0;
	int16_t vz = 0;
	uint16_t hdg = 0;
};

struct MavlinkCommandLong {
	float param1 = 0.0F;
	float param2 = 0.0F;
	float param3 = 0.0F;
	float param4 = 0.0F;
	float param5 = 0.0F;
	float param6 = 0.0F;
	float param7 = 0.0F;
	uint16_t command = 0;
	uint8_t target_system = 0;
	uint8_t target_component = 0;
	uint8_t confirmation = 0;
};

using MavlinkMessageCallback = std::function<void(const MavlinkMessage &)>;

class MavlinkTransport {
public:
	MavlinkTransport();
	explicit MavlinkTransport(MavlinkTransportConfig config);
	~MavlinkTransport();

	MavlinkTransport(const MavlinkTransport &) = delete;
	MavlinkTransport &operator=(const MavlinkTransport &) = delete;
	MavlinkTransport(MavlinkTransport &&) noexcept;
	MavlinkTransport &operator=(MavlinkTransport &&) noexcept;

	MavlinkStatus open(const MavlinkTransportConfig &config);
	void close();

	MavlinkStatus send(const MavlinkMessage &message);
	MavlinkStatus recv(MavlinkMessage *message);
	MavlinkStatus poll();
	void onMessage(MavlinkMessageCallback callback);

	bool open() const;
	const std::string &lastError() const;

	static MavlinkMessage heartbeat(const MavlinkHeartbeat &heartbeat,
					uint8_t system_id, uint8_t component_id);
	static MavlinkMessage attitude(const MavlinkAttitude &attitude,
				       uint8_t system_id, uint8_t component_id);
	static MavlinkMessage globalPositionInt(const MavlinkGlobalPositionInt &position,
						uint8_t system_id,
						uint8_t component_id);
	static MavlinkMessage commandLong(const MavlinkCommandLong &command,
					  uint8_t system_id, uint8_t component_id);

	static bool parseHeartbeat(const MavlinkMessage &message,
				   MavlinkHeartbeat *heartbeat);
	static bool parseAttitude(const MavlinkMessage &message,
				  MavlinkAttitude *attitude);
	static bool parseGlobalPositionInt(const MavlinkMessage &message,
					   MavlinkGlobalPositionInt *position);
	static bool parseCommandLong(const MavlinkMessage &message,
				     MavlinkCommandLong *command);

private:
	class Impl;

	std::unique_ptr<Impl> impl_;
};

} // namespace omnisight::embedded::uav::mavlink

#endif // OMNISIGHT_EMBEDDED_UAV_MAVLINK_TRANSPORT_H_
