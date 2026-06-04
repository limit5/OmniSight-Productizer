/* SPDX-License-Identifier: MIT
 *
 * Cases 6+7 UAV Lucas-Kanade optical flow (OP-2031).
 */
#ifndef OMNISIGHT_EMBEDDED_UAV_CV_OPTICAL_FLOW_H_
#define OMNISIGHT_EMBEDDED_UAV_CV_OPTICAL_FLOW_H_

#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace cv {
class Mat;
}

namespace omnisight::embedded::uav::cv {

enum class OpticalFlowStatus {
	kOk = 0,
	kInvalidArgument,
	kOpenCvUnavailable,
	kFeatureDetectionFailed,
	kTrackingFailed,
};

struct OpticalFlowConfig {
	size_t max_corners = 200;
	double quality_level = 0.01;
	double min_distance_px = 8.0;
	int window_size_px = 21;
	int pyramid_levels = 3;
	double max_tracking_error = 20.0;
};

struct MotionVector {
	float start_x_px = 0.0F;
	float start_y_px = 0.0F;
	float end_x_px = 0.0F;
	float end_y_px = 0.0F;
	float dx_px = 0.0F;
	float dy_px = 0.0F;
	float tracking_error = 0.0F;
};

class OpticalFlow {
public:
	OpticalFlow();
	explicit OpticalFlow(OpticalFlowConfig config);
	~OpticalFlow();

	OpticalFlow(const OpticalFlow &) = delete;
	OpticalFlow &operator=(const OpticalFlow &) = delete;
	OpticalFlow(OpticalFlow &&) noexcept;
	OpticalFlow &operator=(OpticalFlow &&) noexcept;

	OpticalFlowStatus computeFlow(const ::cv::Mat &prev_frame,
				      const ::cv::Mat &curr_frame,
				      std::vector<MotionVector> *motion_vectors);
	std::vector<MotionVector> computeFlow(const ::cv::Mat &prev_frame,
					      const ::cv::Mat &curr_frame);

	const OpticalFlowConfig &config() const;
	const std::string &lastError() const;

private:
	class Impl;

	std::unique_ptr<Impl> impl_;
};

const char *toString(OpticalFlowStatus status);

} // namespace omnisight::embedded::uav::cv

#endif // OMNISIGHT_EMBEDDED_UAV_CV_OPTICAL_FLOW_H_
