/*
 * [OP-2059] Case 6 RF telemetry integration helper.
 *
 * The shell wrapper owns qemu execution. This helper owns the synthetic
 * MAVLink UDP loopback stream and verifies it can pass through telemetry
 * QoS and LoRaWAN packet framing without requiring real RF hardware.
 */

#include "lora-packet-framing.h"
#include "mavlink-transport-udp.h"
#include "telemetry-qos.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <iostream>
#include <string>
#include <thread>
#include <vector>

namespace {

constexpr uint8_t kSystemId = 42;
constexpr uint8_t kComponentId = 7;
constexpr uint16_t kBaseUdpPort = 14670;
constexpr uint16_t kMaxUdpPort = 14720;
constexpr int kRecvPolls = 50;
constexpr uint8_t kTelemetryFPort = 3;

using Clock = omnisight::embedded::uav::rf::TelemetryQos::Clock;

struct UdpPair {
	omnisight::embedded::uav::mavlink::MavlinkUdpTransport tx;
	omnisight::embedded::uav::mavlink::MavlinkUdpTransport rx;
};

static int require_status(bool ok, const std::string &message)
{
	if (ok)
		return 0;
	std::cerr << "rf-telemetry: " << message << "\n";
	return 1;
}

static std::vector<uint8_t> serialize_telemetry(
	const omnisight::embedded::uav::mavlink::MavlinkMessage &message)
{
	std::vector<uint8_t> out;

	out.reserve(6 + message.payload.size());
	out.push_back(static_cast<uint8_t>(message.message_id & 0xffU));
	out.push_back(static_cast<uint8_t>((message.message_id >> 8U) & 0xffU));
	out.push_back(static_cast<uint8_t>((message.message_id >> 16U) & 0xffU));
	out.push_back(message.system_id);
	out.push_back(message.component_id);
	out.push_back(static_cast<uint8_t>(message.payload.size()));
	out.insert(out.end(), message.payload.begin(), message.payload.end());
	return out;
}

static bool payload_matches_message(
	const std::vector<uint8_t> &payload,
	const omnisight::embedded::uav::mavlink::MavlinkMessage &message)
{
	if (payload.size() != 6 + message.payload.size())
		return false;

	const uint32_t message_id = static_cast<uint32_t>(payload[0]) |
				    (static_cast<uint32_t>(payload[1]) << 8U) |
				    (static_cast<uint32_t>(payload[2]) << 16U);

	if (message_id != message.message_id || payload[3] != message.system_id ||
	    payload[4] != message.component_id || payload[5] != message.payload.size())
		return false;

	return std::equal(message.payload.begin(), message.payload.end(),
			  payload.begin() + 6);
}

static omnisight::embedded::uav::rf::LoraSessionConfig lora_session()
{
	omnisight::embedded::uav::rf::LoraSessionConfig config;

	config.dev_addr = 0x26011bda;
	for (size_t i = 0; i < config.nwk_skey.size(); ++i) {
		config.nwk_skey[i] = static_cast<uint8_t>(0x10U + i);
		config.app_skey[i] = static_cast<uint8_t>(0x80U + i);
	}
	return config;
}

static bool open_udp_pair(UdpPair *pair)
{
	using omnisight::embedded::uav::mavlink::MavlinkStatus;
	using omnisight::embedded::uav::mavlink::MavlinkUdpTransportConfig;

	for (uint16_t port = kBaseUdpPort; port < kMaxUdpPort; port += 2) {
		UdpPair candidate;
		MavlinkUdpTransportConfig tx_config;
		MavlinkUdpTransportConfig rx_config;

		tx_config.local_address = "127.0.0.1";
		tx_config.remote_address = "127.0.0.1";
		tx_config.local_port = port;
		tx_config.remote_port = static_cast<uint16_t>(port + 1);
		tx_config.system_id = kSystemId;
		tx_config.component_id = kComponentId;

		rx_config = tx_config;
		rx_config.local_port = static_cast<uint16_t>(port + 1);
		rx_config.remote_port = port;

		if (candidate.rx.open(rx_config) == MavlinkStatus::kOk &&
		    candidate.tx.open(tx_config) == MavlinkStatus::kOk) {
			*pair = std::move(candidate);
			return true;
		}
	}
	return false;
}

static bool recv_message(
	omnisight::embedded::uav::mavlink::MavlinkUdpTransport *transport,
	omnisight::embedded::uav::mavlink::MavlinkMessage *message)
{
	using omnisight::embedded::uav::mavlink::MavlinkStatus;

	for (int i = 0; i < kRecvPolls; ++i) {
		if (transport->recv(message) == MavlinkStatus::kOk)
			return true;
		std::this_thread::sleep_for(std::chrono::milliseconds(2));
	}
	return false;
}

static omnisight::embedded::uav::rf::TelemetryPriority priority_for(
	const omnisight::embedded::uav::mavlink::MavlinkMessage &message,
	const omnisight::embedded::uav::mavlink::MavlinkUdpTransport &transport)
{
	using omnisight::embedded::uav::mavlink::MavlinkMessageId;
	using omnisight::embedded::uav::mavlink::MavlinkUdpQosHint;
	using omnisight::embedded::uav::rf::TelemetryPriority;

	if (message.message_id == static_cast<uint32_t>(MavlinkMessageId::kCommandLong))
		return TelemetryPriority::kCritical;
	if (transport.qosHint(message) == MavlinkUdpQosHint::kHighRate)
		return TelemetryPriority::kHigh;
	if (message.message_id ==
	    static_cast<uint32_t>(MavlinkMessageId::kGlobalPositionInt))
		return TelemetryPriority::kNormal;
	return TelemetryPriority::kLow;
}

static std::vector<omnisight::embedded::uav::mavlink::MavlinkMessage>
synthetic_mavlink_stream()
{
	using omnisight::embedded::uav::mavlink::MavlinkAttitude;
	using omnisight::embedded::uav::mavlink::MavlinkCommandLong;
	using omnisight::embedded::uav::mavlink::MavlinkGlobalPositionInt;
	using omnisight::embedded::uav::mavlink::MavlinkHeartbeat;
	using omnisight::embedded::uav::mavlink::MavlinkTransport;

	MavlinkHeartbeat heartbeat;
	heartbeat.type = 2;
	heartbeat.autopilot = 3;
	heartbeat.base_mode = 81;
	heartbeat.system_status = 4;

	MavlinkGlobalPositionInt position;
	position.time_boot_ms = 1000;
	position.lat = 374219999;
	position.lon = -1220840575;
	position.alt = 42000;
	position.relative_alt = 40000;
	position.vx = 120;
	position.vy = -30;
	position.vz = 5;
	position.hdg = 1234;

	MavlinkAttitude attitude;
	attitude.time_boot_ms = 1010;
	attitude.roll = 0.01F;
	attitude.pitch = -0.02F;
	attitude.yaw = 1.20F;
	attitude.rollspeed = 0.001F;
	attitude.pitchspeed = 0.002F;
	attitude.yawspeed = 0.003F;

	MavlinkCommandLong command;
	command.target_system = kSystemId;
	command.target_component = kComponentId;
	command.command = 400;
	command.confirmation = 1;
	command.param1 = 1.0F;

	std::vector<omnisight::embedded::uav::mavlink::MavlinkMessage> messages;
	messages.push_back(MavlinkTransport::heartbeat(heartbeat, kSystemId,
						       kComponentId));
	messages.push_back(MavlinkTransport::globalPositionInt(position, kSystemId,
							       kComponentId));
	messages.push_back(MavlinkTransport::attitude(attitude, kSystemId,
						      kComponentId));
	attitude.time_boot_ms = 1015;
	messages.push_back(MavlinkTransport::attitude(attitude, kSystemId,
						      kComponentId));
	messages.push_back(MavlinkTransport::commandLong(command, kSystemId,
							 kComponentId));
	return messages;
}

} // namespace

int main()
{
	using omnisight::embedded::uav::mavlink::MavlinkStatus;
	using omnisight::embedded::uav::rf::LoraFrame;
	using omnisight::embedded::uav::rf::LoraFrameRequest;
	using omnisight::embedded::uav::rf::LoraFramer;
	using omnisight::embedded::uav::rf::LoraStatus;
	using omnisight::embedded::uav::rf::QueuedTelemetryMessage;
	using omnisight::embedded::uav::rf::TelemetryPriority;
	using omnisight::embedded::uav::rf::TelemetryQos;
	using omnisight::embedded::uav::rf::TelemetryQosConfig;
	using omnisight::embedded::uav::rf::TelemetryQosStatus;

	UdpPair udp;
	if (require_status(open_udp_pair(&udp),
			   "could not open UDP loopback transport pair"))
		return 1;

	TelemetryQosConfig qos_config;
	qos_config.per_message_budget = {
		{30, 50},
		{33, 10},
	};
	qos_config.max_queue_messages = 8;
	TelemetryQos qos(qos_config);
	LoraFramer framer(lora_session());
	LoraFramer parser(lora_session());
	const auto base_time = Clock::now();
	const auto messages = synthetic_mavlink_stream();
	size_t accepted_count = 0;
	size_t rate_limited = 0;

	for (size_t i = 0; i < messages.size(); ++i) {
		if (udp.tx.send(messages[i]) != MavlinkStatus::kOk)
			return require_status(false, "UDP transport send failed");

		omnisight::embedded::uav::mavlink::MavlinkMessage received;
		if (require_status(recv_message(&udp.rx, &received),
				   "UDP transport did not receive MAVLink frame"))
			return 1;

		const Clock::time_point enqueue_time =
			i == 3 ? base_time + std::chrono::milliseconds(10) :
			       base_time + std::chrono::milliseconds(25 * i);
		const TelemetryPriority priority = priority_for(received, udp.rx);
		const TelemetryQosStatus status =
			qos.enqueue(received, priority, enqueue_time);

		if (i == 3) {
			if (require_status(status == TelemetryQosStatus::kRateLimited,
					   "attitude burst was not rate-limited"))
				return 1;
			++rate_limited;
			continue;
		}
		if (require_status(status == TelemetryQosStatus::kOk,
				   "QoS rejected an in-budget MAVLink message"))
			return 1;

		++accepted_count;
	}

	const std::array<TelemetryPriority, 4> expected_order {
		TelemetryPriority::kCritical,
		TelemetryPriority::kHigh,
		TelemetryPriority::kNormal,
		TelemetryPriority::kLow,
	};

	for (TelemetryPriority expected : expected_order) {
		QueuedTelemetryMessage queued;
		if (require_status(qos.dequeue(&queued) == TelemetryQosStatus::kOk,
				   "QoS queue emptied before priority order completed"))
			return 1;
		if (require_status(queued.priority == expected,
				   "QoS priority queue ordering mismatch"))
			return 1;

		LoraFrameRequest frame_request;
		frame_request.fport = kTelemetryFPort;
		frame_request.payload = serialize_telemetry(queued.message);

		std::vector<uint8_t> phy_payload;
		if (require_status(framer.frame(frame_request, &phy_payload) ==
					   LoraStatus::kOk,
				   "LoRa framer rejected queued telemetry"))
			return 1;

		LoraFrame parsed;
		if (require_status(parser.parse(phy_payload, &parsed) ==
					   LoraStatus::kOk,
				   "LoRa parser rejected framed telemetry"))
			return 1;
		if (require_status(parsed.has_fport &&
					   parsed.fport == kTelemetryFPort,
				   "LoRa frame did not preserve telemetry fport"))
			return 1;
		if (require_status(payload_matches_message(parsed.payload,
							   queued.message),
				   "LoRa payload did not preserve MAVLink message"))
			return 1;
	}

	if (require_status(qos.empty(), "QoS queue retained unexpected telemetry") ||
	    require_status(rate_limited == 1, "unexpected rate-limit count"))
		return 1;

	std::cout << "rf-telemetry: UDP MAVLink -> QoS -> LoRa smoke passed "
		  << "accepted=" << accepted_count
		  << " rate_limited=" << rate_limited << "\n";
	return 0;
}
