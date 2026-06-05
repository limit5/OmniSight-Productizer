/*
 * [OP-2056] Cases 6+7 UAV CV integration helper.
 *
 * The shell wrapper owns qemu execution. This helper owns the synthetic
 * frame stream and verifies optical-flow, stubbed NPU object detection,
 * and video stabilization without requiring camera or accelerator hardware.
 */

#include "object-detection.h"
#include "optical-flow.h"
#include "video-stabilization.h"

#include <opencv2/core.hpp>
#include <opencv2/imgproc.hpp>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <vector>

namespace {

constexpr int kWidth = 96;
constexpr int kHeight = 96;
constexpr int kBoxX = 24;
constexpr int kBoxY = 24;
constexpr int kBoxWidth = 20;
constexpr int kBoxHeight = 16;
constexpr int kDx = 4;
constexpr int kDy = 3;
constexpr float kFlowTolerancePx = 0.75F;
constexpr float kBoxTolerancePx = 1.0F;
constexpr double kFrameToleranceMeanAbsDiff = 0.25;

static int require_status(bool ok, const char *message)
{
	if (ok)
		return 0;
	std::cerr << "uav-cv: " << message << "\n";
	return 1;
}

static cv::Mat synthetic_frame(int dx, int dy)
{
	cv::Mat frame = cv::Mat::zeros(kHeight, kWidth, CV_8UC3);

	cv::rectangle(frame, cv::Rect(kBoxX + dx, kBoxY + dy, kBoxWidth,
				      kBoxHeight),
		      cv::Scalar(255, 255, 255), -1);
	cv::circle(frame, cv::Point(16 + dx, 70 + dy), 5,
		   cv::Scalar(220, 220, 220), -1);
	cv::line(frame, cv::Point(62 + dx, 18 + dy),
		 cv::Point(82 + dx, 38 + dy), cv::Scalar(200, 200, 200), 2);
	return frame;
}

static float median_delta(std::vector<float> values)
{
	std::sort(values.begin(), values.end());
	return values[values.size() / 2];
}

static omnisight::embedded::uav::cv::UAVFrame frame_view(const cv::Mat &frame)
{
	return {
		.data = frame.data,
		.size = frame.total() * frame.elemSize(),
		.width = static_cast<uint32_t>(frame.cols),
		.height = static_cast<uint32_t>(frame.rows),
		.stride = static_cast<uint32_t>(frame.step[0]),
		.format = omnisight::embedded::uav::cv::UAVFrameFormat::kBgr888,
	};
}

static omnisight::embedded::uav::cv::UAVObjectDetectionStatus stub_detect(
	const omnisight::embedded::uav::cv::UAVObjectDetectorConfig &config,
	const omnisight::embedded::uav::cv::UAVFrame &frame,
	std::vector<omnisight::embedded::uav::cv::UAVBoundingBox> *boxes)
{
	if (boxes == nullptr || frame.data == nullptr ||
	    frame.format != omnisight::embedded::uav::cv::UAVFrameFormat::kBgr888)
		return omnisight::embedded::uav::cv::UAVObjectDetectionStatus::
			kInvalidArgument;

	uint32_t min_x = frame.width;
	uint32_t min_y = frame.height;
	uint32_t max_x = 0;
	uint32_t max_y = 0;
	for (uint32_t y = 0; y < frame.height; y++) {
		const uint8_t *row = frame.data + y * frame.stride;
		for (uint32_t x = 0; x < frame.width; x++) {
			const uint8_t *px = row + x * 3U;
			if (px[0] < 240U || px[1] < 240U || px[2] < 240U)
				continue;
			min_x = std::min(min_x, x);
			min_y = std::min(min_y, y);
			max_x = std::max(max_x, x);
			max_y = std::max(max_y, y);
		}
	}

	if (min_x >= frame.width || min_y >= frame.height)
		return omnisight::embedded::uav::cv::UAVObjectDetectionStatus::
			kInferenceError;

	boxes->push_back({
		.x = static_cast<float>(min_x),
		.y = static_cast<float>(min_y),
		.width = static_cast<float>(max_x - min_x + 1U),
		.height = static_cast<float>(max_y - min_y + 1U),
		.confidence = std::max(0.98F, config.confidence_threshold),
		.class_id = 1,
		.label = "synthetic-target",
	});
	return omnisight::embedded::uav::cv::UAVObjectDetectionStatus::kOk;
}

static double mean_abs_diff(const cv::Mat &lhs, const cv::Mat &rhs)
{
	return cv::norm(lhs, rhs, cv::NORM_L1) /
	       static_cast<double>(lhs.total() * lhs.channels());
}

} // namespace

int main()
{
	using omnisight::embedded::uav::cv::OpticalFlow;
	using omnisight::embedded::uav::cv::OpticalFlowConfig;
	using omnisight::embedded::uav::cv::OpticalFlowStatus;
	using omnisight::embedded::uav::cv::UAVBoundingBox;
	using omnisight::embedded::uav::cv::UAVObjectDetectionStatus;
	using omnisight::embedded::uav::cv::UAVObjectDetector;
	using omnisight::embedded::uav::cv::UAVObjectDetectorBackend;
	using omnisight::embedded::uav::cv::UAVObjectDetectorConfig;
	using omnisight::embedded::uav::cv::VideoStabilizer;
	using omnisight::embedded::uav::cv::VideoStabilizerStatus;

	const cv::Mat prev = synthetic_frame(0, 0);
	const cv::Mat curr = synthetic_frame(kDx, kDy);

	OpticalFlowConfig flow_config;
	flow_config.max_corners = 32;
	flow_config.min_distance_px = 4.0;
	flow_config.max_tracking_error = 30.0;
	OpticalFlow flow(flow_config);

	std::vector<omnisight::embedded::uav::cv::MotionVector> vectors;
	if (flow.computeFlow(prev, curr, &vectors) != OpticalFlowStatus::kOk ||
	    require_status(!vectors.empty(), "optical flow produced no vectors"))
		return 1;

	std::vector<float> dx_values;
	std::vector<float> dy_values;
	for (const auto &vector : vectors) {
		dx_values.push_back(vector.dx_px);
		dy_values.push_back(vector.dy_px);
	}
	const float flow_dx = median_delta(dx_values);
	const float flow_dy = median_delta(dy_values);
	if (require_status(std::fabs(flow_dx - static_cast<float>(kDx)) <=
				   kFlowTolerancePx,
			   "optical flow dx exceeded tolerance") ||
	    require_status(std::fabs(flow_dy - static_cast<float>(kDy)) <=
				   kFlowTolerancePx,
			   "optical flow dy exceeded tolerance"))
		return 1;

	UAVObjectDetectorBackend backend;
	backend.detect = stub_detect;
	UAVObjectDetector detector(backend);
	UAVObjectDetectorConfig detector_config;
	detector_config.model_path = "stub://qemu-synthetic-target";
	if (detector.loadModel(detector_config) != UAVObjectDetectionStatus::kOk)
		return require_status(false, "object detector failed to load stub model");

	const std::vector<UAVBoundingBox> boxes = detector.detect(frame_view(curr));
	if (require_status(boxes.size() == 1,
			   "object detector did not return one target"))
		return 1;
	const UAVBoundingBox &box = boxes.front();
	if (require_status(std::fabs(box.x - static_cast<float>(kBoxX + kDx)) <=
				   kBoxTolerancePx,
			   "detected box x exceeded tolerance") ||
	    require_status(std::fabs(box.y - static_cast<float>(kBoxY + kDy)) <=
				   kBoxTolerancePx,
			   "detected box y exceeded tolerance") ||
	    require_status(std::fabs(box.width - static_cast<float>(kBoxWidth)) <=
				   kBoxTolerancePx,
			   "detected box width exceeded tolerance") ||
	    require_status(std::fabs(box.height - static_cast<float>(kBoxHeight)) <=
				   kBoxTolerancePx,
			   "detected box height exceeded tolerance"))
		return 1;

	VideoStabilizer stabilizer;
	cv::Mat stabilized;
	if (stabilizer.stabilize(curr, { 1.0, 0.0, 0.0, 0.0 }, &stabilized) !=
		    VideoStabilizerStatus::kOk ||
	    require_status(!stabilized.empty(), "stabilizer returned empty frame") ||
	    require_status(stabilized.size() == curr.size(),
			   "stabilized frame dimensions changed"))
		return 1;
	if (require_status(mean_abs_diff(curr, stabilized) <=
				   kFrameToleranceMeanAbsDiff,
			   "identity stabilized frame exceeded tolerance"))
		return 1;

	std::cout << "uav-cv: flow_dx=" << flow_dx << " flow_dy=" << flow_dy
		  << " boxes=" << boxes.size()
		  << " stabilized_mean_abs_diff="
		  << mean_abs_diff(curr, stabilized) << "\n";
	return 0;
}
