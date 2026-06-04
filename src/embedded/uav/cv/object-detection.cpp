/* SPDX-License-Identifier: MIT
 *
 * Cases 6+7 UAV NPU object-detection wrapper (OP-2037).
 */
#include "object-detection.h"

#include <utility>

namespace omnisight::embedded::uav::cv {
namespace {

static bool valid_threshold(float value)
{
	return value >= 0.0F && value <= 1.0F;
}

static UAVObjectDetectionStatus empty_model_loader(
	const UAVObjectDetectorConfig &, const UAVFrame &,
	std::vector<UAVBoundingBox> *)
{
	return UAVObjectDetectionStatus::kOk;
}

} // namespace

UAVObjectDetector::UAVObjectDetector()
	: UAVObjectDetector(UAVObjectDetectorBackend {})
{
}

UAVObjectDetector::UAVObjectDetector(UAVObjectDetectorBackend backend)
	: backend_(std::move(backend))
{
	if (!backend_.load_model)
		backend_.load_model = empty_model_loader;
}

UAVObjectDetectionStatus
UAVObjectDetector::loadModel(const UAVObjectDetectorConfig &config)
{
	if (!validConfig(config))
		return fail(UAVObjectDetectionStatus::kInvalidArgument,
			    "object detector config is invalid");

	std::vector<UAVBoundingBox> unused;
	const UAVFrame no_frame;
	const UAVObjectDetectionStatus status =
		backend_.load_model(config, no_frame, &unused);

	if (status != UAVObjectDetectionStatus::kOk) {
		loaded_ = false;
		return fail(status, "NPU object-detection model load failed");
	}

	config_ = config;
	loaded_ = true;
	last_status_ = UAVObjectDetectionStatus::kOk;
	last_error_.clear();
	return last_status_;
}

std::vector<UAVBoundingBox> UAVObjectDetector::detect(const UAVFrame &frame)
{
	std::vector<UAVBoundingBox> bounding_boxes;

	if (!loaded_) {
		(void)fail(UAVObjectDetectionStatus::kInvalidState,
			   "object detector model is not loaded");
		return bounding_boxes;
	}
	if (!validFrame(frame)) {
		(void)fail(UAVObjectDetectionStatus::kInvalidArgument,
			   "object detector frame is invalid");
		return bounding_boxes;
	}

	if (frame.size == 0 && config_.allow_stub_frames) {
		last_status_ = UAVObjectDetectionStatus::kOk;
		last_error_.clear();
		return bounding_boxes;
	}

	if (!backend_.detect) {
		(void)fail(UAVObjectDetectionStatus::kInvalidState,
			   "NPU object-detection backend is not configured");
		return bounding_boxes;
	}

	const UAVObjectDetectionStatus status =
		backend_.detect(config_, frame, &bounding_boxes);

	if (status != UAVObjectDetectionStatus::kOk) {
		bounding_boxes.clear();
		(void)fail(status, "NPU object-detection inference failed");
		return bounding_boxes;
	}

	last_status_ = UAVObjectDetectionStatus::kOk;
	last_error_.clear();
	return bounding_boxes;
}

bool UAVObjectDetector::loaded() const
{
	return loaded_;
}

UAVObjectDetectionStatus UAVObjectDetector::lastStatus() const
{
	return last_status_;
}

const std::string &UAVObjectDetector::lastError() const
{
	return last_error_;
}

UAVObjectDetectionStatus
UAVObjectDetector::fail(UAVObjectDetectionStatus status,
			const std::string &error) const
{
	last_status_ = status;
	last_error_ = error;
	return status;
}

bool UAVObjectDetector::validConfig(const UAVObjectDetectorConfig &config) const
{
	if (config.skill_name != "npu-detection")
		return false;
	if (config.model_path.empty())
		return false;
	if (!valid_threshold(config.confidence_threshold) ||
	    !valid_threshold(config.iou_threshold))
		return false;
	return true;
}

bool UAVObjectDetector::validFrame(const UAVFrame &frame) const
{
	if (frame.width == 0 || frame.height == 0)
		return false;
	if (frame.stride != 0 && frame.stride < frame.width)
		return false;
	if (frame.size == 0)
		return config_.allow_stub_frames;
	return frame.data != nullptr;
}

} // namespace omnisight::embedded::uav::cv
