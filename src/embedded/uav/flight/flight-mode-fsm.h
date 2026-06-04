/* SPDX-License-Identifier: MIT
 *
 * Cases 6+7 UAV flight-mode state machine (OP-2051).
 */
#ifndef OMNISIGHT_EMBEDDED_UAV_FLIGHT_FLIGHT_MODE_FSM_H_
#define OMNISIGHT_EMBEDDED_UAV_FLIGHT_FLIGHT_MODE_FSM_H_

#include "autopilot-abstraction.h"
#include "../mavlink/mavlink-transport.h"

#include <string>

namespace omnisight::embedded::uav::flight {

enum class FlightModeState {
	kDisarmed = 0,
	kArmed,
	kManual,
	kStabilize,
	kLoiter,
	kReturnToLaunch,
	kLand,
};

enum class FlightModeEvent {
	kArm = 0,
	kDisarm,
	kManual,
	kStabilize,
	kLoiter,
	kReturnToLaunch,
	kLand,
};

enum class FlightModeStatus {
	kOk = 0,
	kInvalidTransition,
	kInvalidCommand,
	kBackendError,
};

class FlightModeFsm {
public:
	FlightModeFsm();
	explicit FlightModeFsm(AutopilotBackend *backend);

	FlightModeStatus transition(FlightModeEvent event);
	FlightModeStatus transition(
		const omnisight::embedded::uav::mavlink::MavlinkCommandLong &command);

	FlightModeState getState() const;
	const std::string &lastError() const;

	static const char *stateName(FlightModeState state);

private:
	FlightModeStatus fail(FlightModeStatus status, std::string error);
	FlightModeStatus dispatch(FlightModeEvent event, FlightModeState next_state);
	bool armed() const;

	FlightModeState state_ = FlightModeState::kDisarmed;
	AutopilotBackend *backend_ = nullptr;
	std::string last_error_;
};

} // namespace omnisight::embedded::uav::flight

#endif // OMNISIGHT_EMBEDDED_UAV_FLIGHT_FLIGHT_MODE_FSM_H_
