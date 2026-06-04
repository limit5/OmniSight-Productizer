/* SPDX-License-Identifier: MIT
 *
 * Cases 6+7 UAV gyro-assisted video stabilization (OP-2046).
 */
#ifndef OMNISIGHT_EMBEDDED_UAV_CV_VIDEO_STABILIZATION_H_
#define OMNISIGHT_EMBEDDED_UAV_CV_VIDEO_STABILIZATION_H_

#include <array>
#include <memory>
#include <string>

#if __has_include(<opencv2/core.hpp>)
#include <opencv2/core.hpp>
#define OMNISIGHT_UAV_CV_VIDEO_STABILIZATION_HEADER_HAS_OPENCV 1
#else
#define OMNISIGHT_UAV_CV_VIDEO_STABILIZATION_HEADER_HAS_OPENCV 0
namespace cv {
class Mat;
}
#endif

namespace omnisight::embedded::uav::cv {

enum class VideoStabilizerStatus {
	kOk = 0,
	kInvalidArgument,
	kOpenCvUnavailable,
};

struct VideoStabilizerConfig {
	double focal_length_px = 800.0;
	double max_correction_rad = 0.35;
	int border_value = 0;
};

class VideoStabilizer {
public:
	VideoStabilizer();
	explicit VideoStabilizer(VideoStabilizerConfig config);
	~VideoStabilizer();

	VideoStabilizer(const VideoStabilizer &) = delete;
	VideoStabilizer &operator=(const VideoStabilizer &) = delete;
	VideoStabilizer(VideoStabilizer &&) noexcept;
	VideoStabilizer &operator=(VideoStabilizer &&) noexcept;

	VideoStabilizerStatus stabilize(const ::cv::Mat &frame,
					const std::array<double, 4> &orientation_quat,
					::cv::Mat *corrected_frame);
#if OMNISIGHT_UAV_CV_VIDEO_STABILIZATION_HEADER_HAS_OPENCV
	::cv::Mat stabilize(const ::cv::Mat &frame,
			    const std::array<double, 4> &orientation_quat);
#endif

	const VideoStabilizerConfig &config() const;
	VideoStabilizerStatus lastStatus() const;
	const std::string &lastError() const;

private:
	class Impl;

	std::unique_ptr<Impl> impl_;
};

const char *toString(VideoStabilizerStatus status);

} // namespace omnisight::embedded::uav::cv

#endif // OMNISIGHT_EMBEDDED_UAV_CV_VIDEO_STABILIZATION_H_
