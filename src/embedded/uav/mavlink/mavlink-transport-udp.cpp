/* SPDX-License-Identifier: MIT
 *
 * Cases 6+7 MAVLink-over-UDP transport adapter (OP-2028).
 */
#include "mavlink-transport-udp.h"

#include <utility>

namespace omnisight::embedded::uav::mavlink {
namespace {

constexpr uint32_t kMavlinkStatusTextMessageId = 253;

} // namespace

class MavlinkUdpTransport::Impl {
public:
	explicit Impl(MavlinkUdpTransportConfig config) : config_(std::move(config)) {}
	~Impl() { close(); }

	MavlinkStatus open(const MavlinkUdpTransportConfig &config)
	{
		config_ = config;
		return transport_.open(MavlinkUdpTransport::transportConfig(config_));
	}

	void close()
	{
		transport_.close();
	}

	MavlinkStatus send(const MavlinkMessage &message)
	{
		return transport_.send(message);
	}

	MavlinkStatus recv(MavlinkMessage *message)
	{
		return transport_.recv(message);
	}

	MavlinkStatus poll()
	{
		return transport_.poll();
	}

	void onMessage(MavlinkMessageCallback callback)
	{
		transport_.onMessage(std::move(callback));
	}

	bool open() const { return transport_.open(); }
	const std::string &lastError() const { return transport_.lastError(); }
	const MavlinkUdpTransportConfig &config() const { return config_; }
	MavlinkUdpQosHint qosHint(const MavlinkMessage &message) const
	{
		return MavlinkUdpTransport::qosForMessage(message, config_);
	}

private:
	MavlinkUdpTransportConfig config_;
	MavlinkTransport transport_;
};

MavlinkUdpTransport::MavlinkUdpTransport()
	: impl_(std::make_unique<Impl>(MavlinkUdpTransportConfig {}))
{
}

MavlinkUdpTransport::MavlinkUdpTransport(MavlinkUdpTransportConfig config)
	: impl_(std::make_unique<Impl>(std::move(config)))
{
}

MavlinkUdpTransport::~MavlinkUdpTransport() = default;
MavlinkUdpTransport::MavlinkUdpTransport(MavlinkUdpTransport &&) noexcept =
	default;
MavlinkUdpTransport &MavlinkUdpTransport::operator=(
	MavlinkUdpTransport &&) noexcept = default;

MavlinkStatus MavlinkUdpTransport::open(
	const MavlinkUdpTransportConfig &config)
{
	return impl_->open(config);
}

void MavlinkUdpTransport::close()
{
	impl_->close();
}

MavlinkStatus MavlinkUdpTransport::send(const MavlinkMessage &message)
{
	return impl_->send(message);
}

MavlinkStatus MavlinkUdpTransport::recv(MavlinkMessage *message)
{
	return impl_->recv(message);
}

MavlinkStatus MavlinkUdpTransport::poll()
{
	return impl_->poll();
}

void MavlinkUdpTransport::onMessage(MavlinkMessageCallback callback)
{
	impl_->onMessage(std::move(callback));
}

bool MavlinkUdpTransport::open() const
{
	return impl_->open();
}

const std::string &MavlinkUdpTransport::lastError() const
{
	return impl_->lastError();
}

const MavlinkUdpTransportConfig &MavlinkUdpTransport::config() const
{
	return impl_->config();
}

MavlinkUdpQosHint MavlinkUdpTransport::qosHint(
	const MavlinkMessage &message) const
{
	return impl_->qosHint(message);
}

MavlinkUdpQosHint MavlinkUdpTransport::qosForMessage(
	const MavlinkMessage &message, const MavlinkUdpTransportConfig &config)
{
	if (message.message_id ==
	    static_cast<uint32_t>(MavlinkMessageId::kAttitude))
		return config.attitude_qos;
	if (message.message_id == kMavlinkStatusTextMessageId)
		return config.statustext_qos;
	return config.default_qos;
}

MavlinkTransportConfig MavlinkUdpTransport::transportConfig(
	const MavlinkUdpTransportConfig &config)
{
	MavlinkTransportConfig transport_config;

	transport_config.backend = MavlinkBackend::kUdp;
	transport_config.system_id = config.system_id;
	transport_config.component_id = config.component_id;
	transport_config.udp.local_address = config.local_address;
	transport_config.udp.local_port = config.local_port;
	transport_config.udp.remote_address = config.remote_address;
	transport_config.udp.remote_port = config.remote_port;
	return transport_config;
}

} // namespace omnisight::embedded::uav::mavlink
