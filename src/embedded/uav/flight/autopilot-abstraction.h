/* SPDX-License-Identifier: MIT
 *
 * Cases 6+7 PX4 / ArduPilot autopilot abstraction layer (OP-2033).
 */
#ifndef OMNISIGHT_EMBEDDED_UAV_FLIGHT_AUTOPILOT_ABSTRACTION_H_
#define OMNISIGHT_EMBEDDED_UAV_FLIGHT_AUTOPILOT_ABSTRACTION_H_

#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace omnisight::embedded::uav::flight {

enum class AutopilotStatus {
	kOk = 0,
	kInvalidArgument,
	kUnavailable,
	kUnsupported,
	kBackendError,
};

enum class AutopilotVendor {
	kPx4 = 0,
	kArduPilot,
};

struct MissionItem {
	double latitude_deg = 0.0;
	double longitude_deg = 0.0;
	float altitude_m = 0.0F;
	uint16_t command = 0;
};

struct AutopilotTelemetry {
	bool connected = false;
	bool armed = false;
	std::string mode;
	std::string detail;
};

class AutopilotBackend {
public:
	virtual ~AutopilotBackend() = default;

	virtual AutopilotStatus arm() = 0;
	virtual AutopilotStatus disarm() = 0;
	virtual AutopilotStatus setMode(const std::string &mode) = 0;
	virtual AutopilotStatus uploadMission(
		const std::vector<MissionItem> &mission) = 0;
	virtual AutopilotTelemetry getStatus() const = 0;
	virtual const std::string &lastError() const = 0;
};

std::unique_ptr<AutopilotBackend> createAutopilotBackend(
	AutopilotVendor vendor);
std::unique_ptr<AutopilotBackend> createPx4Backend();
std::unique_ptr<AutopilotBackend> createArduPilotBackend();

} // namespace omnisight::embedded::uav::flight

#endif // OMNISIGHT_EMBEDDED_UAV_FLIGHT_AUTOPILOT_ABSTRACTION_H_
