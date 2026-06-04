/* SPDX-License-Identifier: MIT
 *
 * Case 6/7 gimbal PWM output + Hall sensor abstraction (OP-2042).
 */
#include "pwm-output-abstraction.h"

#include <cassert>

namespace omnisight::embedded::uav::gimbal {
namespace {

constexpr size_t kMaxGimbalChannels = 8;

struct SocHook {
	const char *pwm_controller;
	const char *hall_counter;
};

const SocHook *soc_hook(GimbalSoc soc)
{
	static constexpr SocHook kStubHook = {
		"stub-pwm", "stub-hall",
	};
	static constexpr SocHook kRk3588Hook = {
		"rk3588-pwm", "rk3588-hall-counter",
	};
	static constexpr SocHook kRv1126Hook = {
		"rv1126-pwm", "rv1126-hall-counter",
	};
	static constexpr SocHook kQcs6490Hook = {
		"qcs6490-pwm", "qcs6490-hall-counter",
	};
	static constexpr SocHook kGenio1200Hook = {
		"genio1200-pwm", "genio1200-hall-counter",
	};

	switch (soc) {
	case GimbalSoc::kStub:
		return &kStubHook;
	case GimbalSoc::kRk3588:
		return &kRk3588Hook;
	case GimbalSoc::kRv1126:
		return &kRv1126Hook;
	case GimbalSoc::kQcs6490:
		return &kQcs6490Hook;
	case GimbalSoc::kGenio1200:
		return &kGenio1200Hook;
	}

	return nullptr;
}

bool valid_channel_count(size_t channels)
{
	return channels > 0 && channels <= kMaxGimbalChannels;
}

GimbalIoStatus validate_pwm_config(const PwmOutputConfig &config,
				   std::string *error)
{
	if (soc_hook(config.soc) == nullptr) {
		*error = "unsupported gimbal SoC";
		return GimbalIoStatus::kUnsupportedSoc;
	}
	if (!valid_channel_count(config.channels)) {
		*error = "PWM channel count must be 1..8";
		return GimbalIoStatus::kInvalidArgument;
	}
	if (config.min_duty_cycle > config.max_duty_cycle) {
		*error = "PWM duty-cycle range is inverted";
		return GimbalIoStatus::kInvalidArgument;
	}

	error->clear();
	return GimbalIoStatus::kOk;
}

GimbalIoStatus validate_hall_config(const HallSensorInputConfig &config,
				    std::string *error)
{
	if (soc_hook(config.soc) == nullptr) {
		*error = "unsupported gimbal SoC";
		return GimbalIoStatus::kUnsupportedSoc;
	}
	if (!valid_channel_count(config.channels)) {
		*error = "Hall sensor channel count must be 1..8";
		return GimbalIoStatus::kInvalidArgument;
	}
	if (config.pulses_per_revolution == 0) {
		*error = "pulses_per_revolution must be greater than zero";
		return GimbalIoStatus::kInvalidArgument;
	}

	error->clear();
	return GimbalIoStatus::kOk;
}

bool valid_channel(size_t channel, size_t channels)
{
	return channel < channels;
}

} // namespace

PwmOutput::PwmOutput() : PwmOutput(PwmOutputConfig{})
{
}

PwmOutput::PwmOutput(PwmOutputConfig config)
{
	(void)configure(config);
}

GimbalIoStatus PwmOutput::configure(PwmOutputConfig config)
{
	const GimbalIoStatus status = validate_pwm_config(config, &last_error_);

	if (status != GimbalIoStatus::kOk)
		return status;

	config_ = config;
	duty_cycles_.fill(config_.min_duty_cycle);
	return GimbalIoStatus::kOk;
}

GimbalIoStatus PwmOutput::setDutyCycle(size_t channel, double value)
{
	const SocHook *hook = soc_hook(config_.soc);

	if (hook == nullptr) {
		last_error_ = "unsupported gimbal SoC";
		return GimbalIoStatus::kUnsupportedSoc;
	}
	if (!valid_channel(channel, config_.channels)) {
		last_error_ = "PWM channel is out of range";
		return GimbalIoStatus::kInvalidArgument;
	}
	if (value < config_.min_duty_cycle || value > config_.max_duty_cycle) {
		last_error_ = "PWM duty cycle is out of range";
		return GimbalIoStatus::kInvalidArgument;
	}

	(void)hook->pwm_controller;
	duty_cycles_[channel] = value;
	last_error_.clear();
	return GimbalIoStatus::kOk;
}

double PwmOutput::dutyCycle(size_t channel) const
{
	if (!valid_channel(channel, config_.channels))
		return 0.0;
	return duty_cycles_[channel];
}

const PwmOutputConfig &PwmOutput::config() const
{
	return config_;
}

const std::string &PwmOutput::lastError() const
{
	return last_error_;
}

HallSensorInput::HallSensorInput() : HallSensorInput(HallSensorInputConfig{})
{
}

HallSensorInput::HallSensorInput(HallSensorInputConfig config)
{
	(void)configure(config);
}

GimbalIoStatus HallSensorInput::configure(HallSensorInputConfig config)
{
	const GimbalIoStatus status = validate_hall_config(config, &last_error_);

	if (status != GimbalIoStatus::kOk)
		return status;

	config_ = config;
	stub_rpm_.fill(0.0);
	return GimbalIoStatus::kOk;
}

double HallSensorInput::readRPM(size_t channel) const
{
	const SocHook *hook = soc_hook(config_.soc);

	if (hook == nullptr) {
		last_error_ = "unsupported gimbal SoC";
		return 0.0;
	}
	if (!valid_channel(channel, config_.channels)) {
		last_error_ = "Hall sensor channel is out of range";
		return 0.0;
	}

	(void)hook->hall_counter;
	last_error_.clear();
	return stub_rpm_[channel];
}

GimbalIoStatus HallSensorInput::setStubRPM(size_t channel, double rpm)
{
	if (!valid_channel(channel, config_.channels)) {
		last_error_ = "Hall sensor channel is out of range";
		return GimbalIoStatus::kInvalidArgument;
	}
	if (rpm < 0.0) {
		last_error_ = "Hall sensor RPM must be non-negative";
		return GimbalIoStatus::kInvalidArgument;
	}

	stub_rpm_[channel] = rpm;
	last_error_.clear();
	return GimbalIoStatus::kOk;
}

const HallSensorInputConfig &HallSensorInput::config() const
{
	return config_;
}

const std::string &HallSensorInput::lastError() const
{
	return last_error_;
}

} // namespace omnisight::embedded::uav::gimbal

#if defined(OMNISIGHT_GIMBAL_PWM_HALL_SMOKE_MAIN)
int main()
{
	using omnisight::embedded::uav::gimbal::GimbalIoStatus;
	using omnisight::embedded::uav::gimbal::GimbalSoc;
	using omnisight::embedded::uav::gimbal::HallSensorInput;
	using omnisight::embedded::uav::gimbal::HallSensorInputConfig;
	using omnisight::embedded::uav::gimbal::PwmOutput;
	using omnisight::embedded::uav::gimbal::PwmOutputConfig;

	PwmOutputConfig pwm_config;
	pwm_config.soc = GimbalSoc::kRk3588;
	pwm_config.channels = 3;
	pwm_config.min_duty_cycle = 0.05;
	pwm_config.max_duty_cycle = 0.95;

	PwmOutput pwm(pwm_config);
	assert(pwm.setDutyCycle(0, 0.25) == GimbalIoStatus::kOk);
	assert(pwm.setDutyCycle(1, 0.50) == GimbalIoStatus::kOk);
	assert(pwm.setDutyCycle(2, 0.75) == GimbalIoStatus::kOk);
	assert(pwm.dutyCycle(0) == 0.25);
	assert(pwm.setDutyCycle(3, 0.50) == GimbalIoStatus::kInvalidArgument);

	HallSensorInputConfig hall_config;
	hall_config.soc = GimbalSoc::kRk3588;
	hall_config.channels = 3;
	hall_config.pulses_per_revolution = 6;

	HallSensorInput hall(hall_config);
	assert(hall.setStubRPM(0, 1200.0) == GimbalIoStatus::kOk);
	assert(hall.setStubRPM(1, 900.0) == GimbalIoStatus::kOk);
	assert(hall.readRPM(0) == 1200.0);
	assert(hall.readRPM(1) == 900.0);
	assert(hall.setStubRPM(2, -1.0) == GimbalIoStatus::kInvalidArgument);

	return 0;
}
#endif
