/* SPDX-License-Identifier: MIT
 *
 * Cases 6+7 UAV Lucas-Kanade optical flow (OP-2031).
 */
#include "optical-flow.h"

#include <algorithm>
#include <cmath>
#include <utility>

#if __has_include(<opencv2/core.hpp>) && \
	__has_include(<opencv2/imgproc.hpp>) && \
	__has_include(<opencv2/video/tracking.hpp>)
#include <opencv2/core.hpp>
#include <opencv2/imgproc.hpp>
#include <opencv2/video/tracking.hpp>
#define OMNISIGHT_UAV_CV_HAS_OPENCV 1
#else
#define OMNISIGHT_UAV_CV_HAS_OPENCV 0
#endif

namespace omnisight::embedded::uav::cv {
namespace {

bool valid_config(const OpticalFlowConfig &config)
{
	if (config.max_corners == 0)
		return false;
	if (config.quality_level <= 0.0 || config.quality_level >= 1.0)
		return false;
	if (config.min_distance_px <= 0.0)
		return false;
	if (config.window_size_px < 3 || (config.window_size_px % 2) == 0)
		return false;
	if (config.pyramid_levels < 0)
		return false;
	return config.max_tracking_error > 0.0;
}

#if OMNISIGHT_UAV_CV_HAS_OPENCV
::cv::Mat to_gray(const ::cv::Mat &frame)
{
	if (frame.channels() == 1)
		return frame;

	::cv::Mat gray;
	if (frame.channels() == 3)
		::cv::cvtColor(frame, gray, ::cv::COLOR_BGR2GRAY);
	else if (frame.channels() == 4)
		::cv::cvtColor(frame, gray, ::cv::COLOR_BGRA2GRAY);

	return gray;
}
#endif

} // namespace

class OpticalFlow::Impl {
public:
	explicit Impl(OpticalFlowConfig config) : config_(std::move(config))
	{
	}

	OpticalFlowStatus computeFlow(const ::cv::Mat &prev_frame,
				      const ::cv::Mat &curr_frame,
				      std::vector<MotionVector> *motion_vectors)
	{
		if (motion_vectors == nullptr)
			return invalid_argument("motion vector output is null");
		motion_vectors->clear();
		if (!valid_config(config_))
			return invalid_argument("optical flow configuration is invalid");

#if !OMNISIGHT_UAV_CV_HAS_OPENCV
		(void)prev_frame;
		(void)curr_frame;
		last_error_ = "OpenCV optical flow headers are unavailable";
		return OpticalFlowStatus::kOpenCvUnavailable;
#else
		if (prev_frame.empty() || curr_frame.empty())
			return invalid_argument("input frames must be non-empty");
		if (prev_frame.size() != curr_frame.size())
			return invalid_argument("input frames must have matching dimensions");

		::cv::Mat prev_gray = to_gray(prev_frame);
		::cv::Mat curr_gray = to_gray(curr_frame);
		if (prev_gray.empty() || curr_gray.empty())
			return invalid_argument("input frames must be grayscale, BGR, or BGRA");

		std::vector<::cv::Point2f> prev_points;
		::cv::goodFeaturesToTrack(prev_gray, prev_points,
					  static_cast<int>(config_.max_corners),
					  config_.quality_level,
					  config_.min_distance_px);
		if (prev_points.empty()) {
			last_error_ = "no trackable image corners found";
			return OpticalFlowStatus::kFeatureDetectionFailed;
		}

		std::vector<::cv::Point2f> curr_points;
		std::vector<unsigned char> status;
		std::vector<float> errors;
		::cv::calcOpticalFlowPyrLK(
			prev_gray, curr_gray, prev_points, curr_points, status,
			errors, ::cv::Size(config_.window_size_px,
					   config_.window_size_px),
			config_.pyramid_levels);

		const size_t count = std::min(prev_points.size(), curr_points.size());
		motion_vectors->reserve(count);
		for (size_t i = 0; i < count; i++) {
			const bool tracked = i < status.size() && status[i] != 0;
			const float error = i < errors.size() ? errors[i] : 0.0F;

			if (!tracked || !std::isfinite(error) ||
			    error > config_.max_tracking_error)
				continue;

			MotionVector vector;
			vector.start_x_px = prev_points[i].x;
			vector.start_y_px = prev_points[i].y;
			vector.end_x_px = curr_points[i].x;
			vector.end_y_px = curr_points[i].y;
			vector.dx_px = vector.end_x_px - vector.start_x_px;
			vector.dy_px = vector.end_y_px - vector.start_y_px;
			vector.tracking_error = error;
			motion_vectors->push_back(vector);
		}

		if (motion_vectors->empty()) {
			last_error_ = "Lucas-Kanade tracking produced no valid vectors";
			return OpticalFlowStatus::kTrackingFailed;
		}

		last_error_.clear();
		return OpticalFlowStatus::kOk;
#endif
	}

	const OpticalFlowConfig &config() const
	{
		return config_;
	}

	const std::string &lastError() const
	{
		return last_error_;
	}

private:
	OpticalFlowStatus invalid_argument(const std::string &message)
	{
		last_error_ = message;
		return OpticalFlowStatus::kInvalidArgument;
	}

	OpticalFlowConfig config_;
	std::string last_error_;
};

OpticalFlow::OpticalFlow() : impl_(std::make_unique<Impl>(OpticalFlowConfig{}))
{
}

OpticalFlow::OpticalFlow(OpticalFlowConfig config)
	: impl_(std::make_unique<Impl>(std::move(config)))
{
}

OpticalFlow::~OpticalFlow() = default;
OpticalFlow::OpticalFlow(OpticalFlow &&) noexcept = default;
OpticalFlow &OpticalFlow::operator=(OpticalFlow &&) noexcept = default;

OpticalFlowStatus OpticalFlow::computeFlow(
	const ::cv::Mat &prev_frame, const ::cv::Mat &curr_frame,
	std::vector<MotionVector> *motion_vectors)
{
	return impl_->computeFlow(prev_frame, curr_frame, motion_vectors);
}

std::vector<MotionVector> OpticalFlow::computeFlow(const ::cv::Mat &prev_frame,
						   const ::cv::Mat &curr_frame)
{
	std::vector<MotionVector> motion_vectors;

	(void)impl_->computeFlow(prev_frame, curr_frame, &motion_vectors);
	return motion_vectors;
}

const OpticalFlowConfig &OpticalFlow::config() const
{
	return impl_->config();
}

const std::string &OpticalFlow::lastError() const
{
	return impl_->lastError();
}

const char *toString(OpticalFlowStatus status)
{
	switch (status) {
	case OpticalFlowStatus::kOk:
		return "ok";
	case OpticalFlowStatus::kInvalidArgument:
		return "invalid_argument";
	case OpticalFlowStatus::kOpenCvUnavailable:
		return "opencv_unavailable";
	case OpticalFlowStatus::kFeatureDetectionFailed:
		return "feature_detection_failed";
	case OpticalFlowStatus::kTrackingFailed:
		return "tracking_failed";
	}

	return "unknown";
}

} // namespace omnisight::embedded::uav::cv

#if defined(OMNISIGHT_UAV_OPTICAL_FLOW_SMOKE_MAIN)
int main()
{
	omnisight::embedded::uav::cv::OpticalFlow flow;

	if (flow.config().max_corners == 0)
		return 1;

#if OMNISIGHT_UAV_CV_HAS_OPENCV
	::cv::Mat prev = ::cv::Mat::zeros(80, 80, CV_8UC1);
	::cv::Mat curr = ::cv::Mat::zeros(80, 80, CV_8UC1);
	::cv::rectangle(prev, ::cv::Rect(20, 20, 20, 20), ::cv::Scalar(255),
			-1);
	::cv::rectangle(curr, ::cv::Rect(24, 22, 20, 20), ::cv::Scalar(255),
			-1);

	std::vector<omnisight::embedded::uav::cv::MotionVector> vectors;
	const auto status = flow.computeFlow(prev, curr, &vectors);

	return status == omnisight::embedded::uav::cv::OpticalFlowStatus::kOk &&
		       !vectors.empty() ?
		       0 :
		       1;
#else
	return 0;
#endif
}
#endif
