/* SPDX-License-Identifier: MIT
 *
 * Case 6/7 gimbal 3-axis PID controller (OP-2026).
 */
#ifndef OMNISIGHT_EMBEDDED_UAV_GIMBAL_PID_CONTROLLER_H_
#define OMNISIGHT_EMBEDDED_UAV_GIMBAL_PID_CONTROLLER_H_

#include <array>
#include <cstddef>

namespace omnisight::embedded::uav::gimbal {

enum class PIDAxis : size_t {
	kPitch = 0,
	kRoll,
	kYaw,
};

struct AxisValues {
	double pitch = 0.0;
	double roll = 0.0;
	double yaw = 0.0;
};

struct PIDGains {
	double kp = 0.0;
	double ki = 0.0;
	double kd = 0.0;
};

struct PIDClamp {
	double min = 0.0;
	double max = 0.0;
};

struct PIDAxisConfig {
	PIDGains gains;
	PIDClamp output{1000.0, 2000.0};
	PIDClamp integral{-500.0, 500.0};
	double neutral_output = 1500.0;
};

struct PIDControllerConfig {
	PIDAxisConfig pitch;
	PIDAxisConfig roll;
	PIDAxisConfig yaw;
};

class PIDController {
public:
	PIDController();
	explicit PIDController(PIDControllerConfig config);

	AxisValues update(const AxisValues &setpoint,
			  const AxisValues &measurement,
			  double dt_seconds);
	void reset();
	void reset(PIDAxis axis);

	const PIDControllerConfig &config() const;
	void configure(PIDControllerConfig config);
	void configure(PIDAxis axis, PIDAxisConfig config);

private:
	struct AxisState {
		double integral = 0.0;
		double previous_error = 0.0;
		bool has_previous_error = false;
	};

	PIDControllerConfig config_;
	std::array<AxisState, 3> state_;
};

} // namespace omnisight::embedded::uav::gimbal

#endif // OMNISIGHT_EMBEDDED_UAV_GIMBAL_PID_CONTROLLER_H_
