/* SPDX-License-Identifier: MIT
 *
 * Case 6/7 gimbal PWM output + Hall sensor abstraction (OP-2042).
 */
#ifndef OMNISIGHT_EMBEDDED_UAV_GIMBAL_PWM_OUTPUT_ABSTRACTION_H_
#define OMNISIGHT_EMBEDDED_UAV_GIMBAL_PWM_OUTPUT_ABSTRACTION_H_

#include <array>
#include <cstddef>
#include <cstdint>
#include <string>

namespace omnisight::embedded::uav::gimbal {

enum class GimbalIoStatus {
	kOk = 0,
	kInvalidArgument,
	kUnsupportedSoc,
	kUnavailable,
};

enum class GimbalSoc {
	kStub = 0,
	kRk3588,
	kRv1126,
	kQcs6490,
	kGenio1200,
};

struct PwmOutputConfig {
	GimbalSoc soc = GimbalSoc::kStub;
	size_t channels = 3;
	double min_duty_cycle = 0.0;
	double max_duty_cycle = 1.0;
};

struct HallSensorInputConfig {
	GimbalSoc soc = GimbalSoc::kStub;
	size_t channels = 3;
	uint32_t pulses_per_revolution = 1;
};

class PwmOutput {
public:
	PwmOutput();
	explicit PwmOutput(PwmOutputConfig config);

	GimbalIoStatus configure(PwmOutputConfig config);
	GimbalIoStatus setDutyCycle(size_t channel, double value);

	double dutyCycle(size_t channel) const;
	const PwmOutputConfig &config() const;
	const std::string &lastError() const;

private:
	PwmOutputConfig config_;
	std::array<double, 8> duty_cycles_{};
	std::string last_error_;
};

class HallSensorInput {
public:
	HallSensorInput();
	explicit HallSensorInput(HallSensorInputConfig config);

	GimbalIoStatus configure(HallSensorInputConfig config);
	double readRPM(size_t channel) const;
	GimbalIoStatus setStubRPM(size_t channel, double rpm);

	const HallSensorInputConfig &config() const;
	const std::string &lastError() const;

private:
	HallSensorInputConfig config_;
	std::array<double, 8> stub_rpm_{};
	mutable std::string last_error_;
};

} // namespace omnisight::embedded::uav::gimbal

#endif // OMNISIGHT_EMBEDDED_UAV_GIMBAL_PWM_OUTPUT_ABSTRACTION_H_
