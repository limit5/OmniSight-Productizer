/* SPDX-License-Identifier: MIT
 *
 * Cases 6+7 PX4 / ArduPilot autopilot abstraction layer (OP-2033).
 */
#include "autopilot-abstraction.h"

namespace omnisight::embedded::uav::flight {

std::unique_ptr<AutopilotBackend> createAutopilotBackend(
	AutopilotVendor vendor)
{
	switch (vendor) {
	case AutopilotVendor::kPx4:
		return createPx4Backend();
	case AutopilotVendor::kArduPilot:
		return createArduPilotBackend();
	}

	return nullptr;
}

} // namespace omnisight::embedded::uav::flight
