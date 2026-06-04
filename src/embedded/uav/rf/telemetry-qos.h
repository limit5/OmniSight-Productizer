/* SPDX-License-Identifier: MIT
 *
 * Cases 6+7 telemetry QoS layer (OP-2044).
 */
#ifndef OMNISIGHT_EMBEDDED_UAV_RF_TELEMETRY_QOS_H_
#define OMNISIGHT_EMBEDDED_UAV_RF_TELEMETRY_QOS_H_

#include "../mavlink/mavlink-transport.h"

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace omnisight::embedded::uav::rf {

enum class TelemetryQosStatus {
	kOk = 0,
	kInvalidArgument,
	kRateLimited,
	kBackpressure,
	kQueueFull,
	kEmpty,
};

enum class TelemetryPriority {
	kLow = 0,
	kNormal,
	kHigh,
	kCritical,
};

struct TelemetryMessageBudget {
	uint32_t message_id = 0;
	uint32_t max_rate_hz = 0;
};

struct TelemetryQosConfig {
	std::vector<TelemetryMessageBudget> per_message_budget;
	size_t max_queue_messages = 128;
	size_t transport_buffer_capacity_bytes = 4096;
	size_t backpressure_high_watermark_bytes = 3072;
};

struct QueuedTelemetryMessage {
	mavlink::MavlinkMessage message;
	TelemetryPriority priority = TelemetryPriority::kNormal;
};

class TelemetryQos {
public:
	using Clock = std::chrono::steady_clock;

	TelemetryQos();
	explicit TelemetryQos(TelemetryQosConfig config);
	~TelemetryQos();

	TelemetryQos(const TelemetryQos &) = delete;
	TelemetryQos &operator=(const TelemetryQos &) = delete;
	TelemetryQos(TelemetryQos &&) noexcept;
	TelemetryQos &operator=(TelemetryQos &&) noexcept;

	TelemetryQosStatus configure(TelemetryQosConfig config);
	const TelemetryQosConfig &config() const;

	TelemetryQosStatus enqueue(const mavlink::MavlinkMessage &message,
				   TelemetryPriority priority);
	TelemetryQosStatus enqueue(const mavlink::MavlinkMessage &message,
				   TelemetryPriority priority,
				   Clock::time_point now);
	TelemetryQosStatus dequeue(QueuedTelemetryMessage *message);

	void setTransportBufferFill(size_t bytes_used);
	void clear();

	size_t size() const;
	bool empty() const;
	bool backpressureActive() const;
	const std::string &lastError() const;

	static TelemetryQosConfig defaultConfig();
	static size_t estimateFrameBytes(const mavlink::MavlinkMessage &message);

private:
	class Impl;

	std::unique_ptr<Impl> impl_;
};

} // namespace omnisight::embedded::uav::rf

#endif // OMNISIGHT_EMBEDDED_UAV_RF_TELEMETRY_QOS_H_
