/* SPDX-License-Identifier: MIT
 *
 * Cases 6+7 telemetry QoS layer (OP-2044).
 */
#include "telemetry-qos.h"

#include <array>
#include <cassert>
#include <chrono>
#include <deque>
#include <unordered_map>
#include <utility>

namespace omnisight::embedded::uav::rf {
namespace {

constexpr uint32_t kMavlinkGpsRawIntMessageId = 24;
constexpr uint32_t kMavlinkStatusTextMessageId = 253;
constexpr size_t kMavlinkV2FrameOverheadBytes = 12;

size_t priority_index(TelemetryPriority priority)
{
	switch (priority) {
	case TelemetryPriority::kLow:
		return 0;
	case TelemetryPriority::kNormal:
		return 1;
	case TelemetryPriority::kHigh:
		return 2;
	case TelemetryPriority::kCritical:
		return 3;
	}
	return 1;
}

bool priority_is_backpressure_exempt(TelemetryPriority priority)
{
	return priority == TelemetryPriority::kCritical ||
	       priority == TelemetryPriority::kHigh;
}

TelemetryQosConfig normalized_config(TelemetryQosConfig config)
{
	if (config.per_message_budget.empty())
		config.per_message_budget =
			TelemetryQos::defaultConfig().per_message_budget;
	if (config.backpressure_high_watermark_bytes >
	    config.transport_buffer_capacity_bytes)
		config.backpressure_high_watermark_bytes =
			config.transport_buffer_capacity_bytes;
	return config;
}

} // namespace

class TelemetryQos::Impl {
public:
	Impl() : config_(TelemetryQos::defaultConfig()) {}
	explicit Impl(TelemetryQosConfig config)
		: config_(normalized_config(std::move(config)))
	{
	}

	TelemetryQosStatus configure(TelemetryQosConfig config)
	{
		if (config.max_queue_messages == 0) {
			last_error_ = "max_queue_messages must be greater than zero";
			return TelemetryQosStatus::kInvalidArgument;
		}
		if (config.transport_buffer_capacity_bytes == 0) {
			last_error_ =
				"transport_buffer_capacity_bytes must be greater than zero";
			return TelemetryQosStatus::kInvalidArgument;
		}

		config_ = normalized_config(std::move(config));
		clear();
		return TelemetryQosStatus::kOk;
	}

	const TelemetryQosConfig &config() const { return config_; }

	TelemetryQosStatus enqueue(const mavlink::MavlinkMessage &message,
				   TelemetryPriority priority,
				   Clock::time_point now)
	{
		if (size_ >= config_.max_queue_messages) {
			last_error_ = "telemetry QoS queue is full";
			return TelemetryQosStatus::kQueueFull;
		}
		if (backpressure_active() &&
		    !priority_is_backpressure_exempt(priority)) {
			last_error_ = "transport buffer backpressure active";
			return TelemetryQosStatus::kBackpressure;
		}
		if (!consume_budget(message.message_id, now)) {
			last_error_ = "message rate limit exceeded";
			return TelemetryQosStatus::kRateLimited;
		}

		queues_[priority_index(priority)].push_back({message, priority});
		++size_;
		queued_bytes_ += TelemetryQos::estimateFrameBytes(message);
		return TelemetryQosStatus::kOk;
	}

	TelemetryQosStatus dequeue(QueuedTelemetryMessage *message)
	{
		if (message == nullptr) {
			last_error_ = "message output is required";
			return TelemetryQosStatus::kInvalidArgument;
		}

		for (size_t i = queues_.size(); i > 0; --i) {
			auto &queue = queues_[i - 1];

			if (queue.empty())
				continue;
			*message = queue.front();
			queue.pop_front();
			--size_;
			const size_t frame_bytes =
				TelemetryQos::estimateFrameBytes(message->message);
			queued_bytes_ = frame_bytes > queued_bytes_ ?
				0 :
				queued_bytes_ - frame_bytes;
			return TelemetryQosStatus::kOk;
		}

		last_error_ = "telemetry QoS queue is empty";
		return TelemetryQosStatus::kEmpty;
	}

	void set_transport_buffer_fill(size_t bytes_used)
	{
		transport_buffer_fill_bytes_ =
			bytes_used > config_.transport_buffer_capacity_bytes ?
				config_.transport_buffer_capacity_bytes :
				bytes_used;
	}

	void clear()
	{
		for (auto &queue : queues_)
			queue.clear();
		size_ = 0;
		queued_bytes_ = 0;
		last_sent_by_message_id_.clear();
	}

	size_t size() const { return size_; }
	bool empty() const { return size_ == 0; }
	bool backpressure_active() const
	{
		return transport_buffer_fill_bytes_ + queued_bytes_ >=
		       config_.backpressure_high_watermark_bytes;
	}
	const std::string &last_error() const { return last_error_; }

private:
	bool consume_budget(uint32_t message_id, Clock::time_point now)
	{
		const uint32_t max_rate_hz = max_rate_for(message_id);

		if (max_rate_hz == 0)
			return true;

		const auto min_interval =
			std::chrono::nanoseconds(1000000000ULL / max_rate_hz);
		const auto found = last_sent_by_message_id_.find(message_id);

		if (found != last_sent_by_message_id_.end() &&
		    now - found->second < min_interval)
			return false;

		last_sent_by_message_id_[message_id] = now;
		return true;
	}

	uint32_t max_rate_for(uint32_t message_id) const
	{
		for (const auto &budget : config_.per_message_budget) {
			if (budget.message_id == message_id)
				return budget.max_rate_hz;
		}
		return 0;
	}

	TelemetryQosConfig config_;
	std::array<std::deque<QueuedTelemetryMessage>, 4> queues_;
	std::unordered_map<uint32_t, Clock::time_point> last_sent_by_message_id_;
	size_t size_ = 0;
	size_t queued_bytes_ = 0;
	size_t transport_buffer_fill_bytes_ = 0;
	std::string last_error_;
};

TelemetryQos::TelemetryQos() : impl_(std::make_unique<Impl>())
{
}

TelemetryQos::TelemetryQos(TelemetryQosConfig config)
	: impl_(std::make_unique<Impl>(std::move(config)))
{
}

TelemetryQos::~TelemetryQos() = default;
TelemetryQos::TelemetryQos(TelemetryQos &&) noexcept = default;
TelemetryQos &TelemetryQos::operator=(TelemetryQos &&) noexcept = default;

TelemetryQosStatus TelemetryQos::configure(TelemetryQosConfig config)
{
	return impl_->configure(std::move(config));
}

const TelemetryQosConfig &TelemetryQos::config() const
{
	return impl_->config();
}

TelemetryQosStatus TelemetryQos::enqueue(
	const mavlink::MavlinkMessage &message, TelemetryPriority priority)
{
	return impl_->enqueue(message, priority, Clock::now());
}

TelemetryQosStatus TelemetryQos::enqueue(
	const mavlink::MavlinkMessage &message, TelemetryPriority priority,
	Clock::time_point now)
{
	return impl_->enqueue(message, priority, now);
}

TelemetryQosStatus TelemetryQos::dequeue(QueuedTelemetryMessage *message)
{
	return impl_->dequeue(message);
}

void TelemetryQos::setTransportBufferFill(size_t bytes_used)
{
	impl_->set_transport_buffer_fill(bytes_used);
}

void TelemetryQos::clear()
{
	impl_->clear();
}

size_t TelemetryQos::size() const
{
	return impl_->size();
}

bool TelemetryQos::empty() const
{
	return impl_->empty();
}

bool TelemetryQos::backpressureActive() const
{
	return impl_->backpressure_active();
}

const std::string &TelemetryQos::lastError() const
{
	return impl_->last_error();
}

TelemetryQosConfig TelemetryQos::defaultConfig()
{
	TelemetryQosConfig config;

	config.per_message_budget = {
		{static_cast<uint32_t>(mavlink::MavlinkMessageId::kAttitude), 50},
		{kMavlinkGpsRawIntMessageId, 10},
		{kMavlinkStatusTextMessageId, 0},
	};
	return config;
}

size_t TelemetryQos::estimateFrameBytes(const mavlink::MavlinkMessage &message)
{
	return kMavlinkV2FrameOverheadBytes + message.payload.size();
}

} // namespace omnisight::embedded::uav::rf

#if defined(OMNISIGHT_UAV_RF_TELEMETRY_QOS_SMOKE_MAIN)
int main()
{
	using omnisight::embedded::uav::mavlink::MavlinkMessage;
	using omnisight::embedded::uav::mavlink::MavlinkMessageId;
	using omnisight::embedded::uav::rf::QueuedTelemetryMessage;
	using omnisight::embedded::uav::rf::TelemetryPriority;
	using omnisight::embedded::uav::rf::TelemetryQos;
	using omnisight::embedded::uav::rf::TelemetryQosStatus;

	TelemetryQos qos;
	const auto now = TelemetryQos::Clock::now();
	MavlinkMessage attitude;
	attitude.message_id = static_cast<uint32_t>(MavlinkMessageId::kAttitude);

	assert(qos.enqueue(attitude, TelemetryPriority::kNormal, now) ==
	       TelemetryQosStatus::kOk);
	assert(qos.enqueue(attitude, TelemetryPriority::kNormal,
			   now + std::chrono::milliseconds(5)) ==
	       TelemetryQosStatus::kRateLimited);
	assert(qos.enqueue(attitude, TelemetryPriority::kNormal,
			   now + std::chrono::milliseconds(20)) ==
	       TelemetryQosStatus::kOk);

	MavlinkMessage statustext;
	statustext.message_id = 253;
	assert(qos.enqueue(statustext, TelemetryPriority::kCritical, now) ==
	       TelemetryQosStatus::kOk);

	QueuedTelemetryMessage out;
	assert(qos.dequeue(&out) == TelemetryQosStatus::kOk);
	assert(out.priority == TelemetryPriority::kCritical);

	qos.clear();
	qos.setTransportBufferFill(qos.config().backpressure_high_watermark_bytes);
	assert(qos.backpressureActive());
	assert(qos.enqueue(statustext, TelemetryPriority::kLow, now) ==
	       TelemetryQosStatus::kBackpressure);
	assert(qos.enqueue(statustext, TelemetryPriority::kHigh, now) ==
	       TelemetryQosStatus::kOk);
	return 0;
}
#endif
