/* SPDX-License-Identifier: MIT
 *
 * Case 6/7 gimbal 3-axis PID controller (OP-2026).
 */
#include "pid-controller.h"

#include <algorithm>
#include <cassert>

namespace omnisight::embedded::uav::gimbal {
namespace {

size_t axis_index(PIDAxis axis)
{
	return static_cast<size_t>(axis);
}

double clamp_value(double value, PIDClamp clamp)
{
	if (clamp.min > clamp.max)
		std::swap(clamp.min, clamp.max);
	return std::clamp(value, clamp.min, clamp.max);
}

PIDAxisConfig normalize_axis_config(PIDAxisConfig config)
{
	if (config.output.min > config.output.max)
		std::swap(config.output.min, config.output.max);
	if (config.integral.min > config.integral.max)
		std::swap(config.integral.min, config.integral.max);
	config.neutral_output = clamp_value(config.neutral_output, config.output);
	return config;
}

PIDControllerConfig normalize_config(PIDControllerConfig config)
{
	config.pitch = normalize_axis_config(config.pitch);
	config.roll = normalize_axis_config(config.roll);
	config.yaw = normalize_axis_config(config.yaw);
	return config;
}

const PIDAxisConfig &axis_config(const PIDControllerConfig &config,
				 PIDAxis axis)
{
	switch (axis) {
	case PIDAxis::kPitch:
		return config.pitch;
	case PIDAxis::kRoll:
		return config.roll;
	case PIDAxis::kYaw:
		return config.yaw;
	}
	return config.pitch;
}

PIDAxisConfig *mutable_axis_config(PIDControllerConfig *config, PIDAxis axis)
{
	switch (axis) {
	case PIDAxis::kPitch:
		return &config->pitch;
	case PIDAxis::kRoll:
		return &config->roll;
	case PIDAxis::kYaw:
		return &config->yaw;
	}
	return &config->pitch;
}

double read_axis(const AxisValues &values, PIDAxis axis)
{
	switch (axis) {
	case PIDAxis::kPitch:
		return values.pitch;
	case PIDAxis::kRoll:
		return values.roll;
	case PIDAxis::kYaw:
		return values.yaw;
	}
	return 0.0;
}

void write_axis(AxisValues *values, PIDAxis axis, double value)
{
	switch (axis) {
	case PIDAxis::kPitch:
		values->pitch = value;
		break;
	case PIDAxis::kRoll:
		values->roll = value;
		break;
	case PIDAxis::kYaw:
		values->yaw = value;
		break;
	}
}

bool drives_out_of_saturation(double unclamped,
			      double clamped,
			      double error)
{
	if (unclamped > clamped)
		return error < 0.0;
	if (unclamped < clamped)
		return error > 0.0;
	return true;
}

} // namespace

PIDController::PIDController() : PIDController(PIDControllerConfig{})
{
}

PIDController::PIDController(PIDControllerConfig config)
	: config_(normalize_config(config))
{
}

AxisValues PIDController::update(const AxisValues &setpoint,
				 const AxisValues &measurement,
				 double dt_seconds)
{
	AxisValues output;

	for (PIDAxis axis : {PIDAxis::kPitch, PIDAxis::kRoll, PIDAxis::kYaw}) {
		const size_t index = axis_index(axis);
		const PIDAxisConfig &axis_cfg = axis_config(config_, axis);
		AxisState &axis_state = state_[index];
		const double error = read_axis(setpoint, axis) -
				     read_axis(measurement, axis);
		const double derivative =
			axis_state.has_previous_error && dt_seconds > 0.0 ?
				(error - axis_state.previous_error) / dt_seconds :
				0.0;
		const double next_integral = dt_seconds > 0.0 ?
			clamp_value(axis_state.integral + error * dt_seconds,
				    axis_cfg.integral) :
			axis_state.integral;
		const double candidate =
			axis_cfg.neutral_output + (axis_cfg.gains.kp * error) +
			(axis_cfg.gains.ki * next_integral) +
			(axis_cfg.gains.kd * derivative);
		const double axis_output = clamp_value(candidate, axis_cfg.output);

		if (drives_out_of_saturation(candidate, axis_output, error))
			axis_state.integral = next_integral;

		axis_state.previous_error = error;
		axis_state.has_previous_error = true;

		write_axis(&output, axis, axis_output);
	}

	return output;
}

void PIDController::reset()
{
	state_ = {};
}

void PIDController::reset(PIDAxis axis)
{
	state_[axis_index(axis)] = {};
}

const PIDControllerConfig &PIDController::config() const
{
	return config_;
}

void PIDController::configure(PIDControllerConfig config)
{
	config_ = normalize_config(config);
	reset();
}

void PIDController::configure(PIDAxis axis, PIDAxisConfig config)
{
	*mutable_axis_config(&config_, axis) = normalize_axis_config(config);
	reset(axis);
}

} // namespace omnisight::embedded::uav::gimbal

#if defined(OMNISIGHT_GIMBAL_PID_SMOKE_MAIN)
int main()
{
	using omnisight::embedded::uav::gimbal::AxisValues;
	using omnisight::embedded::uav::gimbal::PIDController;
	using omnisight::embedded::uav::gimbal::PIDControllerConfig;

	PIDControllerConfig config;
	config.pitch.gains = {2.0, 0.2, 0.1};
	config.roll.gains = {2.0, 0.2, 0.1};
	config.yaw.gains = {1.0, 0.1, 0.05};

	PIDController controller(config);
	const AxisValues output = controller.update(
		AxisValues{1.0, -1.0, 0.5},
		AxisValues{0.0, 0.0, 0.0},
		0.02);

	assert(output.pitch >= 1000.0 && output.pitch <= 2000.0);
	assert(output.roll >= 1000.0 && output.roll <= 2000.0);
	assert(output.yaw >= 1000.0 && output.yaw <= 2000.0);
	assert(output.pitch > 1500.0);
	assert(output.roll < 1500.0);
	assert(output.yaw > 1500.0);
	return 0;
}
#endif
