/* SPDX-License-Identifier: MIT
 *
 * Cases 6+7 UAV flight-mode state machine (OP-2051).
 */
#include "flight-mode-fsm.h"

#include <cmath>
#include <utility>

namespace omnisight::embedded::uav::flight {
namespace {

constexpr uint16_t kMavCmdNavReturnToLaunch = 20;
constexpr uint16_t kMavCmdNavLand = 21;
constexpr uint16_t kMavCmdDoSetMode = 176;
constexpr uint16_t kMavCmdComponentArmDisarm = 400;

constexpr uint32_t kPx4CustomMainModeManual = 1;
constexpr uint32_t kPx4CustomMainModeStabilized = 7;
constexpr uint32_t kPx4CustomMainModeAuto = 4;
constexpr uint32_t kPx4CustomSubModeAutoLoiter = 3;
constexpr uint32_t kPx4CustomSubModeAutoRtl = 5;
constexpr uint32_t kPx4CustomSubModeAutoLand = 6;

bool floatEquals(float value, float expected)
{
	return std::fabs(value - expected) < 0.001F;
}

uint32_t px4MainMode(float custom_mode)
{
	return (static_cast<uint32_t>(custom_mode) >> 16U) & 0xffU;
}

uint32_t px4SubMode(float custom_mode)
{
	return (static_cast<uint32_t>(custom_mode) >> 24U) & 0xffU;
}

FlightModeState stateForModeEvent(FlightModeEvent event)
{
	switch (event) {
	case FlightModeEvent::kManual:
		return FlightModeState::kManual;
	case FlightModeEvent::kStabilize:
		return FlightModeState::kStabilize;
	case FlightModeEvent::kLoiter:
		return FlightModeState::kLoiter;
	case FlightModeEvent::kReturnToLaunch:
		return FlightModeState::kReturnToLaunch;
	case FlightModeEvent::kLand:
		return FlightModeState::kLand;
	case FlightModeEvent::kArm:
		return FlightModeState::kArmed;
	case FlightModeEvent::kDisarm:
		return FlightModeState::kDisarmed;
	}

	return FlightModeState::kDisarmed;
}

const char *modeName(FlightModeEvent event)
{
	return FlightModeFsm::stateName(stateForModeEvent(event));
}

} // namespace

FlightModeFsm::FlightModeFsm() = default;

FlightModeFsm::FlightModeFsm(AutopilotBackend *backend) : backend_(backend)
{
}

FlightModeStatus FlightModeFsm::transition(FlightModeEvent event)
{
	switch (event) {
	case FlightModeEvent::kArm:
		if (state_ != FlightModeState::kDisarmed)
			return fail(FlightModeStatus::kInvalidTransition,
				    "aircraft is already armed");
		return dispatch(event, FlightModeState::kArmed);
	case FlightModeEvent::kDisarm:
		if (!armed())
			return fail(FlightModeStatus::kInvalidTransition,
				    "aircraft is already disarmed");
		return dispatch(event, FlightModeState::kDisarmed);
	case FlightModeEvent::kManual:
	case FlightModeEvent::kStabilize:
	case FlightModeEvent::kLoiter:
	case FlightModeEvent::kReturnToLaunch:
	case FlightModeEvent::kLand:
		if (!armed())
			return fail(FlightModeStatus::kInvalidTransition,
				    "flight mode requires armed state");
		return dispatch(event, stateForModeEvent(event));
	}

	return fail(FlightModeStatus::kInvalidTransition,
		    "unknown flight-mode event");
}

FlightModeStatus FlightModeFsm::transition(
	const omnisight::embedded::uav::mavlink::MavlinkCommandLong &command)
{
	switch (command.command) {
	case kMavCmdComponentArmDisarm:
		if (floatEquals(command.param1, 1.0F))
			return transition(FlightModeEvent::kArm);
		if (floatEquals(command.param1, 0.0F))
			return transition(FlightModeEvent::kDisarm);
		return fail(FlightModeStatus::kInvalidCommand,
			    "MAV_CMD_COMPONENT_ARM_DISARM param1 must be 0 or 1");
	case kMavCmdNavReturnToLaunch:
		return transition(FlightModeEvent::kReturnToLaunch);
	case kMavCmdNavLand:
		return transition(FlightModeEvent::kLand);
	case kMavCmdDoSetMode:
		break;
	default:
		return fail(FlightModeStatus::kInvalidCommand,
			    "unsupported MAVLink flight-mode command");
	}

	if (floatEquals(command.param2, 0.0F))
		return transition(FlightModeEvent::kManual);
	if (floatEquals(command.param2, 2.0F))
		return transition(FlightModeEvent::kStabilize);
	if (floatEquals(command.param2, 5.0F))
		return transition(FlightModeEvent::kLoiter);
	if (floatEquals(command.param2, 6.0F))
		return transition(FlightModeEvent::kReturnToLaunch);
	if (floatEquals(command.param2, 9.0F))
		return transition(FlightModeEvent::kLand);

	const uint32_t main_mode = px4MainMode(command.param2);
	const uint32_t sub_mode = px4SubMode(command.param2);

	if (main_mode == kPx4CustomMainModeManual)
		return transition(FlightModeEvent::kManual);
	if (main_mode == kPx4CustomMainModeStabilized)
		return transition(FlightModeEvent::kStabilize);
	if (main_mode == kPx4CustomMainModeAuto) {
		if (sub_mode == kPx4CustomSubModeAutoLoiter)
			return transition(FlightModeEvent::kLoiter);
		if (sub_mode == kPx4CustomSubModeAutoRtl)
			return transition(FlightModeEvent::kReturnToLaunch);
		if (sub_mode == kPx4CustomSubModeAutoLand)
			return transition(FlightModeEvent::kLand);
	}

	return fail(FlightModeStatus::kInvalidCommand,
		    "unsupported MAVLink custom mode");
}

FlightModeState FlightModeFsm::getState() const
{
	return state_;
}

const std::string &FlightModeFsm::lastError() const
{
	return last_error_;
}

const char *FlightModeFsm::stateName(FlightModeState state)
{
	switch (state) {
	case FlightModeState::kDisarmed:
		return "DISARMED";
	case FlightModeState::kArmed:
		return "ARMED";
	case FlightModeState::kManual:
		return "MANUAL";
	case FlightModeState::kStabilize:
		return "STABILIZE";
	case FlightModeState::kLoiter:
		return "LOITER";
	case FlightModeState::kReturnToLaunch:
		return "RTL";
	case FlightModeState::kLand:
		return "LAND";
	}

	return "UNKNOWN";
}

FlightModeStatus FlightModeFsm::fail(FlightModeStatus status, std::string error)
{
	last_error_ = std::move(error);
	return status;
}

FlightModeStatus FlightModeFsm::dispatch(FlightModeEvent event,
					 FlightModeState next_state)
{
	if (backend_ != nullptr) {
		AutopilotStatus status = AutopilotStatus::kOk;

		if (event == FlightModeEvent::kArm)
			status = backend_->arm();
		else if (event == FlightModeEvent::kDisarm)
			status = backend_->disarm();
		else
			status = backend_->setMode(modeName(event));

		if (status != AutopilotStatus::kOk)
			return fail(FlightModeStatus::kBackendError,
				    backend_->lastError());
	}

	state_ = next_state;
	last_error_.clear();
	return FlightModeStatus::kOk;
}

bool FlightModeFsm::armed() const
{
	return state_ != FlightModeState::kDisarmed;
}

} // namespace omnisight::embedded::uav::flight
