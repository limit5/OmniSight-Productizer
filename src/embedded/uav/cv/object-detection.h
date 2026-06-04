/* SPDX-License-Identifier: MIT
 *
 * Cases 6+7 UAV NPU object-detection wrapper (OP-2037).
 */
#ifndef OMNISIGHT_EMBEDDED_UAV_CV_OBJECT_DETECTION_H_
#define OMNISIGHT_EMBEDDED_UAV_CV_OBJECT_DETECTION_H_

#include <cstddef>
#include <cstdint>
#include <functional>
#include <string>
#include <vector>

namespace omnisight::embedded::uav::cv {

enum class UAVObjectDetectionStatus {
	kOk = 0,
	kInvalidArgument,
	kInvalidState,
	kModelLoadError,
	kInferenceError,
};

enum class UAVObjectDetectionModel {
	kYolov5 = 0,
	kYolov8,
};

enum class UAVFrameFormat {
	kRgb888 = 0,
	kBgr888,
	kNv12,
};

struct UAVFrame {
	const uint8_t *data = nullptr;
	std::size_t size = 0;
	uint32_t width = 0;
	uint32_t height = 0;
	uint32_t stride = 0;
	UAVFrameFormat format = UAVFrameFormat::kRgb888;
};

struct UAVBoundingBox {
	float x = 0.0F;
	float y = 0.0F;
	float width = 0.0F;
	float height = 0.0F;
	float confidence = 0.0F;
	uint32_t class_id = 0;
	std::string label;
};

struct UAVObjectDetectorConfig {
	UAVObjectDetectionModel model = UAVObjectDetectionModel::kYolov8;
	std::string skill_name = "npu-detection";
	std::string model_path;
	std::string labels_path;
	float confidence_threshold = 0.25F;
	float iou_threshold = 0.45F;
	bool allow_stub_frames = false;
};

using UAVObjectDetectionInferenceFn =
	std::function<UAVObjectDetectionStatus(
		const UAVObjectDetectorConfig &config, const UAVFrame &frame,
		std::vector<UAVBoundingBox> *bounding_boxes)>;

struct UAVObjectDetectorBackend {
	UAVObjectDetectionInferenceFn load_model;
	UAVObjectDetectionInferenceFn detect;
};

class UAVObjectDetector {
public:
	UAVObjectDetector();
	explicit UAVObjectDetector(UAVObjectDetectorBackend backend);

	UAVObjectDetectionStatus loadModel(const UAVObjectDetectorConfig &config);
	std::vector<UAVBoundingBox> detect(const UAVFrame &frame);

	bool loaded() const;
	UAVObjectDetectionStatus lastStatus() const;
	const std::string &lastError() const;

private:
	UAVObjectDetectionStatus fail(UAVObjectDetectionStatus status,
				      const std::string &error) const;
	bool validConfig(const UAVObjectDetectorConfig &config) const;
	bool validFrame(const UAVFrame &frame) const;

	UAVObjectDetectorBackend backend_;
	UAVObjectDetectorConfig config_;
	bool loaded_ = false;
	mutable UAVObjectDetectionStatus last_status_ =
		UAVObjectDetectionStatus::kInvalidState;
	mutable std::string last_error_;
};

} // namespace omnisight::embedded::uav::cv

#endif // OMNISIGHT_EMBEDDED_UAV_CV_OBJECT_DETECTION_H_
