/* SPDX-License-Identifier: MIT
 *
 * Cases 6+7 UAV gyro-assisted video stabilization (OP-2046).
 */
#include "video-stabilization.h"

#include <algorithm>
#include <cmath>
#include <utility>

#if __has_include(<opencv2/core.hpp>) && __has_include(<opencv2/imgproc.hpp>)
#include <opencv2/core.hpp>
#include <opencv2/imgproc.hpp>
#define OMNISIGHT_UAV_CV_HAS_STABILIZATION_OPENCV 1
#else
#define OMNISIGHT_UAV_CV_HAS_STABILIZATION_OPENCV 0
#endif

namespace omnisight::embedded::uav::cv {
namespace {

bool valid_config(const VideoStabilizerConfig &config)
{
	if (!std::isfinite(config.focal_length_px) || config.focal_length_px <= 0.0)
		return false;
	if (!std::isfinite(config.max_correction_rad) ||
	    config.max_correction_rad <= 0.0)
		return false;
	return true;
}

bool finite_quaternion(const std::array<double, 4> &q)
{
	return std::all_of(q.begin(), q.end(),
			   [](double value) { return std::isfinite(value); });
}

#if OMNISIGHT_UAV_CV_HAS_STABILIZATION_OPENCV
constexpr double kPi = 3.14159265358979323846;
constexpr double kRadiansToDegrees = 180.0 / kPi;

std::array<double, 4> normalize_quaternion(std::array<double, 4> q)
{
	const double norm = std::sqrt(q[0] * q[0] + q[1] * q[1] +
				      q[2] * q[2] + q[3] * q[3]);

	if (norm < 1.0e-12 || !std::isfinite(norm))
		return { 1.0, 0.0, 0.0, 0.0 };
	for (double &value : q)
		value /= norm;
	return q;
}

std::array<double, 3> roll_pitch_yaw_from_quaternion(std::array<double, 4> q)
{
	q = normalize_quaternion(q);

	const double w = q[0];
	const double x = q[1];
	const double y = q[2];
	const double z = q[3];

	const double sinr = 2.0 * (w * x + y * z);
	const double cosr = 1.0 - 2.0 * (x * x + y * y);
	const double roll = std::atan2(sinr, cosr);

	const double sinp = 2.0 * (w * y - z * x);
	const double pitch = std::abs(sinp) >= 1.0 ?
				     std::copysign(kPi / 2.0, sinp) :
				     std::asin(sinp);

	const double siny = 2.0 * (w * z + x * y);
	const double cosy = 1.0 - 2.0 * (y * y + z * z);
	const double yaw = std::atan2(siny, cosy);

	return { roll, pitch, yaw };
}
#endif

} // namespace

class VideoStabilizer::Impl {
public:
	explicit Impl(VideoStabilizerConfig config) : config_(std::move(config))
	{
	}

	VideoStabilizerStatus stabilize(
		const ::cv::Mat &frame, const std::array<double, 4> &orientation_quat,
		::cv::Mat *corrected_frame)
	{
		if (corrected_frame == nullptr)
			return fail(VideoStabilizerStatus::kInvalidArgument,
				    "corrected frame output is null");
		if (!valid_config(config_))
			return fail(VideoStabilizerStatus::kInvalidArgument,
				    "video stabilizer configuration is invalid");
		if (!finite_quaternion(orientation_quat))
			return fail(VideoStabilizerStatus::kInvalidArgument,
				    "orientation quaternion contains a non-finite value");

#if !OMNISIGHT_UAV_CV_HAS_STABILIZATION_OPENCV
		(void)frame;
		last_error_ = "OpenCV warpAffine headers are unavailable";
		last_status_ = VideoStabilizerStatus::kOpenCvUnavailable;
		return last_status_;
#else
		if (frame.empty())
			return fail(VideoStabilizerStatus::kInvalidArgument,
				    "input frame must be non-empty");

		const auto rpy = roll_pitch_yaw_from_quaternion(orientation_quat);
		const double roll =
			std::clamp(rpy[0], -config_.max_correction_rad,
				   config_.max_correction_rad);
		const double pitch =
			std::clamp(rpy[1], -config_.max_correction_rad,
				   config_.max_correction_rad);
		const double yaw =
			std::clamp(rpy[2], -config_.max_correction_rad,
				   config_.max_correction_rad);

		const ::cv::Point2f center((static_cast<float>(frame.cols) - 1.0F) *
						   0.5F,
					   (static_cast<float>(frame.rows) - 1.0F) *
						   0.5F);
		::cv::Mat transform =
			::cv::getRotationMatrix2D(center, -yaw * kRadiansToDegrees,
						  1.0);

		transform.at<double>(0, 2) += -pitch * config_.focal_length_px;
		transform.at<double>(1, 2) += roll * config_.focal_length_px;

		::cv::warpAffine(frame, *corrected_frame, transform, frame.size(),
				 ::cv::INTER_LINEAR, ::cv::BORDER_CONSTANT,
				 ::cv::Scalar(config_.border_value));

		last_error_.clear();
		last_status_ = VideoStabilizerStatus::kOk;
		return last_status_;
#endif
	}

#if OMNISIGHT_UAV_CV_HAS_STABILIZATION_OPENCV
	::cv::Mat stabilize(const ::cv::Mat &frame,
			    const std::array<double, 4> &orientation_quat)
	{
		::cv::Mat corrected_frame;

		(void)stabilize(frame, orientation_quat, &corrected_frame);
		return corrected_frame;
	}
#endif

	const VideoStabilizerConfig &config() const
	{
		return config_;
	}

	VideoStabilizerStatus lastStatus() const
	{
		return last_status_;
	}

	const std::string &lastError() const
	{
		return last_error_;
	}

private:
	VideoStabilizerStatus fail(VideoStabilizerStatus status,
				   const std::string &error)
	{
		last_error_ = error;
		last_status_ = status;
		return last_status_;
	}

	VideoStabilizerConfig config_;
	VideoStabilizerStatus last_status_ =
		VideoStabilizerStatus::kInvalidArgument;
	std::string last_error_;
};

VideoStabilizer::VideoStabilizer()
	: impl_(std::make_unique<Impl>(VideoStabilizerConfig {}))
{
}

VideoStabilizer::VideoStabilizer(VideoStabilizerConfig config)
	: impl_(std::make_unique<Impl>(std::move(config)))
{
}

VideoStabilizer::~VideoStabilizer() = default;
VideoStabilizer::VideoStabilizer(VideoStabilizer &&) noexcept = default;
VideoStabilizer &VideoStabilizer::operator=(VideoStabilizer &&) noexcept = default;

VideoStabilizerStatus VideoStabilizer::stabilize(
	const ::cv::Mat &frame, const std::array<double, 4> &orientation_quat,
	::cv::Mat *corrected_frame)
{
	return impl_->stabilize(frame, orientation_quat, corrected_frame);
}

#if OMNISIGHT_UAV_CV_HAS_STABILIZATION_OPENCV
::cv::Mat VideoStabilizer::stabilize(
	const ::cv::Mat &frame, const std::array<double, 4> &orientation_quat)
{
	return impl_->stabilize(frame, orientation_quat);
}
#endif

const VideoStabilizerConfig &VideoStabilizer::config() const
{
	return impl_->config();
}

VideoStabilizerStatus VideoStabilizer::lastStatus() const
{
	return impl_->lastStatus();
}

const std::string &VideoStabilizer::lastError() const
{
	return impl_->lastError();
}

const char *toString(VideoStabilizerStatus status)
{
	switch (status) {
	case VideoStabilizerStatus::kOk:
		return "ok";
	case VideoStabilizerStatus::kInvalidArgument:
		return "invalid_argument";
	case VideoStabilizerStatus::kOpenCvUnavailable:
		return "opencv_unavailable";
	}

	return "unknown";
}

} // namespace omnisight::embedded::uav::cv

#if defined(OMNISIGHT_UAV_CV_VIDEO_STABILIZATION_SMOKE_MAIN)
int main()
{
	omnisight::embedded::uav::cv::VideoStabilizer stabilizer;

	if (stabilizer.config().focal_length_px <= 0.0)
		return 1;

#if OMNISIGHT_UAV_CV_HAS_STABILIZATION_OPENCV
	::cv::Mat frame = ::cv::Mat::zeros(64, 64, CV_8UC1);
	::cv::line(frame, ::cv::Point(8, 32), ::cv::Point(56, 32),
		   ::cv::Scalar(255), 2);

	::cv::Mat corrected;
	const auto status = stabilizer.stabilize(frame, { 1.0, 0.0, 0.0, 0.0 },
						 &corrected);

	return status ==
			       omnisight::embedded::uav::cv::VideoStabilizerStatus::kOk &&
		       corrected.size() == frame.size() && !corrected.empty() ?
		       0 :
		       1;
#else
	return 0;
#endif
}
#endif
