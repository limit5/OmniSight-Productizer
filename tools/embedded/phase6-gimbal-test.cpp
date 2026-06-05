/*
 * [OP-2053] Case 6/7 gimbal integration helper.
 *
 * The shell wrapper owns qemu execution. This helper owns the synthetic IMU
 * orientation stream and verifies that PID outputs dispatch through the PWM
 * and Hall abstractions without requiring real BLDC motor hardware.
 */

#include "pid-controller.h"
#include "pwm-output-abstraction.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <iostream>

namespace {

constexpr double kDtS = 0.02;
constexpr int kStepsPerSetpoint = 220;
constexpr double kNeutralPwmUs = 1500.0;
constexpr double kMinPwmUs = 1000.0;
constexpr double kMaxPwmUs = 2000.0;
constexpr double kMinDutyCycle = 0.05;
constexpr double kMaxDutyCycle = 0.95;
constexpr double kAngleToleranceDeg = 0.75;
constexpr double kSettledDutyTolerance = 0.08;
constexpr double kMaxInitialDuty = 0.90;
constexpr double kPlantRateDegPerS = 65.0;
constexpr double kMaxSyntheticRpm = 4200.0;

struct SyntheticIMUFrame {
	omnisight::embedded::uav::gimbal::AxisValues orientation_deg;
};

struct SetpointCase {
	omnisight::embedded::uav::gimbal::AxisValues setpoint_deg;
	const char *name;
};

struct DispatchSnapshot {
	std::array<double, 3> duty_cycle;
	std::array<double, 3> rpm;
};

static double read_axis(const omnisight::embedded::uav::gimbal::AxisValues &values,
			size_t channel)
{
	switch (channel) {
	case 0:
		return values.pitch;
	case 1:
		return values.roll;
	case 2:
		return values.yaw;
	default:
		return 0.0;
	}
}

static void write_axis(omnisight::embedded::uav::gimbal::AxisValues *values,
		       size_t channel,
		       double value)
{
	switch (channel) {
	case 0:
		values->pitch = value;
		break;
	case 1:
		values->roll = value;
		break;
	case 2:
		values->yaw = value;
		break;
	default:
		break;
	}
}

static double pwm_us_to_duty_cycle(double pwm_us)
{
	const double normalized = (pwm_us - kMinPwmUs) / (kMaxPwmUs - kMinPwmUs);

	return kMinDutyCycle + std::clamp(normalized, 0.0, 1.0) *
				       (kMaxDutyCycle - kMinDutyCycle);
}

static double duty_cycle_to_axis_rate(double duty_cycle)
{
	const double normalized = (duty_cycle - 0.5) / (kMaxDutyCycle - 0.5);

	return std::clamp(normalized, -1.0, 1.0) * kPlantRateDegPerS;
}

static double duty_cycle_to_synthetic_rpm(double duty_cycle)
{
	return std::fabs(duty_cycle - 0.5) / (kMaxDutyCycle - 0.5) *
	       kMaxSyntheticRpm;
}

static int require_status(bool ok, const char *message)
{
	if (ok)
		return 0;
	std::cerr << "gimbal: " << message << "\n";
	return 1;
}

static DispatchSnapshot dispatch_pwm(
	const omnisight::embedded::uav::gimbal::AxisValues &pid_output,
	omnisight::embedded::uav::gimbal::PwmOutput *pwm,
	omnisight::embedded::uav::gimbal::HallSensorInput *hall)
{
	using omnisight::embedded::uav::gimbal::GimbalIoStatus;

	DispatchSnapshot snapshot{};

	for (size_t channel = 0; channel < 3; ++channel) {
		const double duty_cycle = pwm_us_to_duty_cycle(read_axis(pid_output, channel));

		if (pwm->setDutyCycle(channel, duty_cycle) != GimbalIoStatus::kOk)
			return {};
		if (hall->setStubRPM(channel, duty_cycle_to_synthetic_rpm(duty_cycle)) !=
		    GimbalIoStatus::kOk)
			return {};

		snapshot.duty_cycle[channel] = pwm->dutyCycle(channel);
		snapshot.rpm[channel] = hall->readRPM(channel);
	}

	return snapshot;
}

static SyntheticIMUFrame advance_synthetic_imu(
	const SyntheticIMUFrame &frame,
	const DispatchSnapshot &snapshot)
{
	SyntheticIMUFrame next = frame;

	for (size_t channel = 0; channel < 3; ++channel) {
		const double angle = read_axis(frame.orientation_deg, channel);
		const double rate = duty_cycle_to_axis_rate(snapshot.duty_cycle[channel]);

		write_axis(&next.orientation_deg, channel, angle + rate * kDtS);
	}

	return next;
}

static double max_error_deg(
	const omnisight::embedded::uav::gimbal::AxisValues &setpoint,
	const omnisight::embedded::uav::gimbal::AxisValues &measurement)
{
	double max_error = 0.0;

	for (size_t channel = 0; channel < 3; ++channel) {
		max_error = std::max(max_error,
				     std::fabs(read_axis(setpoint, channel) -
					       read_axis(measurement, channel)));
	}

	return max_error;
}

static int run_setpoint_case(
	const SetpointCase &test_case,
	omnisight::embedded::uav::gimbal::PIDController *controller,
	omnisight::embedded::uav::gimbal::PwmOutput *pwm,
	omnisight::embedded::uav::gimbal::HallSensorInput *hall,
	SyntheticIMUFrame *frame)
{
	const double initial_error = max_error_deg(test_case.setpoint_deg,
						  frame->orientation_deg);
	double initial_max_duty = 0.0;
	double final_max_duty_delta = 0.0;
	double final_max_rpm = 0.0;

	for (int step = 0; step < kStepsPerSetpoint; ++step) {
		const auto pid_output = controller->update(
			test_case.setpoint_deg, frame->orientation_deg, kDtS);
		const DispatchSnapshot snapshot = dispatch_pwm(pid_output, pwm, hall);

		for (size_t channel = 0; channel < 3; ++channel) {
			if (pwm->lastError().empty() == false ||
			    hall->lastError().empty() == false)
				return require_status(false,
					"PWM/Hall dispatch rejected synthetic output");
			if (step == 0) {
				initial_max_duty = std::max(initial_max_duty,
					std::fabs(snapshot.duty_cycle[channel] - 0.5));
			}
			if (step == kStepsPerSetpoint - 1) {
				final_max_duty_delta = std::max(final_max_duty_delta,
					std::fabs(snapshot.duty_cycle[channel] - 0.5));
				final_max_rpm = std::max(final_max_rpm, snapshot.rpm[channel]);
			}
		}

		*frame = advance_synthetic_imu(*frame, snapshot);
	}

	const double error = max_error_deg(test_case.setpoint_deg,
					   frame->orientation_deg);

	std::cout << "gimbal: " << test_case.name
		  << " initial_error_deg=" << initial_error
		  << " final_error_deg=" << error
		  << " initial_pwm_delta=" << initial_max_duty
		  << " final_pwm_delta=" << final_max_duty_delta
		  << " final_rpm=" << final_max_rpm << "\n";

	if (require_status(error < initial_error,
			   "synthetic IMU orientation error did not decrease") ||
	    require_status(initial_max_duty <= kMaxInitialDuty,
			   "PWM duty cycle exceeded synthetic safety bound") ||
	    require_status(final_max_duty_delta <= kSettledDutyTolerance,
			   "PWM duty cycle did not settle near neutral") ||
	    require_status(final_max_rpm <= duty_cycle_to_synthetic_rpm(
			   0.5 + kSettledDutyTolerance),
			   "Hall RPM did not settle with PWM output") ||
	    require_status(error <= kAngleToleranceDeg,
			   "synthetic IMU orientation did not converge"))
		return 1;

	std::cout << "gimbal: " << test_case.name << " converged\n";
	return 0;
}

} // namespace

int main()
{
	using omnisight::embedded::uav::gimbal::GimbalIoStatus;
	using omnisight::embedded::uav::gimbal::GimbalSoc;
	using omnisight::embedded::uav::gimbal::HallSensorInput;
	using omnisight::embedded::uav::gimbal::HallSensorInputConfig;
	using omnisight::embedded::uav::gimbal::PIDController;
	using omnisight::embedded::uav::gimbal::PIDControllerConfig;
	using omnisight::embedded::uav::gimbal::PwmOutput;
	using omnisight::embedded::uav::gimbal::PwmOutputConfig;

	PIDControllerConfig pid_config;
	pid_config.pitch.gains = {8.0, 0.0, 0.0};
	pid_config.roll.gains = {8.0, 0.0, 0.0};
	pid_config.yaw.gains = {5.6, 0.0, 0.0};
	pid_config.pitch.output = {kMinPwmUs, kMaxPwmUs};
	pid_config.roll.output = {kMinPwmUs, kMaxPwmUs};
	pid_config.yaw.output = {kMinPwmUs, kMaxPwmUs};
	pid_config.pitch.neutral_output = kNeutralPwmUs;
	pid_config.roll.neutral_output = kNeutralPwmUs;
	pid_config.yaw.neutral_output = kNeutralPwmUs;

	PwmOutputConfig pwm_config;
	pwm_config.soc = GimbalSoc::kStub;
	pwm_config.channels = 3;
	pwm_config.min_duty_cycle = kMinDutyCycle;
	pwm_config.max_duty_cycle = kMaxDutyCycle;

	HallSensorInputConfig hall_config;
	hall_config.soc = GimbalSoc::kStub;
	hall_config.channels = 3;
	hall_config.pulses_per_revolution = 6;

	PIDController controller(pid_config);
	PwmOutput pwm(pwm_config);
	HallSensorInput hall(hall_config);

	if (require_status(pwm.lastError().empty(), "PWM config rejected stub SoC") ||
	    require_status(hall.lastError().empty(), "Hall config rejected stub SoC") ||
	    require_status(pwm.setDutyCycle(0, 0.5) == GimbalIoStatus::kOk,
			   "PWM neutral dispatch failed"))
		return 1;

	const std::array<SetpointCase, 3> cases{{
		{{8.0, -6.0, 4.0}, "positive pitch negative roll"},
		{{-4.0, 5.0, -7.0}, "reverse correction"},
		{{1.5, 0.0, 2.5}, "small trim"},
	}};
	SyntheticIMUFrame frame{};

	for (const auto &test_case : cases) {
		if (run_setpoint_case(test_case, &controller, &pwm, &hall, &frame) != 0)
			return 1;
	}

	std::cout << "gimbal: synthetic IMU -> PID -> PWM/Hall smoke passed\n";
	return 0;
}
