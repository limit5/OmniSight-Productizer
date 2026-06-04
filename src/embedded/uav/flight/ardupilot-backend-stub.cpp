/* SPDX-License-Identifier: MIT
 *
 * Cases 6+7 ArduPilot autopilot backend stub (OP-2033).
 */
#include "autopilot-abstraction.h"

#include <utility>

namespace omnisight::embedded::uav::flight {
namespace {

class ArduPilotBackendStub : public AutopilotBackend {
public:
	AutopilotStatus arm() override
	{
		return unsupported("ArduPilot backend integration is not linked");
	}

	AutopilotStatus disarm() override
	{
		return unsupported("ArduPilot backend integration is not linked");
	}

	AutopilotStatus setMode(const std::string &mode) override
	{
		if (mode.empty())
			return fail(AutopilotStatus::kInvalidArgument,
				    "ArduPilot mode is required");
		mode_ = mode;
		return unsupported("ArduPilot mode dispatch is not linked");
	}

	AutopilotStatus uploadMission(
		const std::vector<MissionItem> &mission) override
	{
		if (mission.empty())
			return fail(AutopilotStatus::kInvalidArgument,
				    "ArduPilot mission must contain at least one item");
		mission_ = mission;
		return unsupported("ArduPilot mission upload is not linked");
	}

	AutopilotTelemetry getStatus() const override
	{
		return {false, false, mode_, "ArduPilot backend stub only"};
	}

	const std::string &lastError() const override
	{
		return last_error_;
	}

private:
	AutopilotStatus fail(AutopilotStatus status, std::string error)
	{
		last_error_ = std::move(error);
		return status;
	}

	AutopilotStatus unsupported(std::string error)
	{
		return fail(AutopilotStatus::kUnsupported, std::move(error));
	}

	std::string mode_;
	std::vector<MissionItem> mission_;
	std::string last_error_;
};

} // namespace

std::unique_ptr<AutopilotBackend> createArduPilotBackend()
{
	return std::make_unique<ArduPilotBackendStub>();
}

} // namespace omnisight::embedded::uav::flight
