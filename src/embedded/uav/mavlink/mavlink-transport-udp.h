/* SPDX-License-Identifier: MIT
 *
 * Cases 6+7 MAVLink-over-UDP transport adapter (OP-2028).
 */
#ifndef OMNISIGHT_EMBEDDED_UAV_MAVLINK_TRANSPORT_UDP_H_
#define OMNISIGHT_EMBEDDED_UAV_MAVLINK_TRANSPORT_UDP_H_

#include "mavlink-transport.h"

#include <cstdint>
#include <memory>
#include <string>

namespace omnisight::embedded::uav::mavlink {

enum class MavlinkUdpQosHint {
	kLowRate = 0,
	kHighRate,
};

struct MavlinkUdpTransportConfig {
	std::string local_address = "0.0.0.0";
	uint16_t local_port = 14555;
	std::string remote_address = "127.0.0.1";
	uint16_t remote_port = 14550;
	uint8_t system_id = 1;
	uint8_t component_id = 1;
	MavlinkUdpQosHint default_qos = MavlinkUdpQosHint::kLowRate;
	MavlinkUdpQosHint attitude_qos = MavlinkUdpQosHint::kHighRate;
	MavlinkUdpQosHint statustext_qos = MavlinkUdpQosHint::kLowRate;
};

class MavlinkUdpTransport final {
public:
	MavlinkUdpTransport();
	explicit MavlinkUdpTransport(MavlinkUdpTransportConfig config);
	~MavlinkUdpTransport();

	MavlinkUdpTransport(const MavlinkUdpTransport &) = delete;
	MavlinkUdpTransport &operator=(const MavlinkUdpTransport &) = delete;
	MavlinkUdpTransport(MavlinkUdpTransport &&) noexcept;
	MavlinkUdpTransport &operator=(MavlinkUdpTransport &&) noexcept;

	MavlinkStatus open(const MavlinkUdpTransportConfig &config);
	void close();

	MavlinkStatus send(const MavlinkMessage &message);
	MavlinkStatus recv(MavlinkMessage *message);
	MavlinkStatus poll();
	void onMessage(MavlinkMessageCallback callback);

	bool open() const;
	const std::string &lastError() const;
	const MavlinkUdpTransportConfig &config() const;
	MavlinkUdpQosHint qosHint(const MavlinkMessage &message) const;

	static MavlinkUdpQosHint qosForMessage(
		const MavlinkMessage &message,
		const MavlinkUdpTransportConfig &config);
	static MavlinkTransportConfig transportConfig(
		const MavlinkUdpTransportConfig &config);

private:
	class Impl;

	std::unique_ptr<Impl> impl_;
};

} // namespace omnisight::embedded::uav::mavlink

#endif // OMNISIGHT_EMBEDDED_UAV_MAVLINK_TRANSPORT_UDP_H_
