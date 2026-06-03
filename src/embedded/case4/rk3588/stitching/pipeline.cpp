/* SPDX-License-Identifier: MIT
 *
 * Case 4 RK3588 OpenCV stitching pipeline (OP-1948).
 *
 * Supports panorama mode through OpenCV Stitcher and a deterministic grid
 * fallback for customer bring-up captures where pairwise features are sparse.
 */
#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

#if __has_include(<opencv2/opencv.hpp>)
#include <opencv2/opencv.hpp>
#include <opencv2/stitching.hpp>
#define OMNISIGHT_CASE4_HAS_OPENCV 1
#else
#define OMNISIGHT_CASE4_HAS_OPENCV 0
#endif

namespace omnisight::embedded::case4::rk3588::stitching {

enum class StitchMode {
	kPanorama = 0,
	kGrid,
};

enum class PipelineStatus {
	kOk = 0,
	kInvalidArgument,
	kOpenCvUnavailable,
	kCalibrationMissing,
	kFrameSetIncomplete,
	kStitchFailed,
};

struct CameraCalibration {
	std::string camera_id;
	std::array<double, 9> intrinsic{};
	std::array<double, 8> distortion{};
	std::array<double, 9> rotation{1.0, 0.0, 0.0, 0.0, 1.0,
				       0.0, 0.0, 0.0, 1.0};
	std::array<double, 3> translation{};
	double reprojection_error = 0.0;
	bool valid = false;
};

struct StitchingConfig {
	std::vector<std::string> camera_ids;
	StitchMode mode = StitchMode::kPanorama;
	int grid_columns = 0;
	bool undistort = true;
};

struct StitchingFrame {
	std::string camera_id;
	int64_t timestamp_ns = 0;
#if OMNISIGHT_CASE4_HAS_OPENCV
	cv::Mat image;
#endif
};

struct StitchingResult {
	int64_t timestamp_ns = 0;
#if OMNISIGHT_CASE4_HAS_OPENCV
	cv::Mat image;
#endif
};

static bool valid_config(const StitchingConfig &config)
{
	if (config.camera_ids.size() < 2)
		return false;
	for (const std::string &camera_id : config.camera_ids) {
		if (camera_id.empty())
			return false;
	}
	if (config.mode == StitchMode::kGrid && config.grid_columns < 1)
		return false;
	return true;
}

static const CameraCalibration *find_calibration(
	const std::vector<CameraCalibration> &calibrations,
	const std::string &camera_id)
{
	for (const CameraCalibration &calibration : calibrations) {
		if (calibration.camera_id == camera_id)
			return calibration.valid ? &calibration : nullptr;
	}

	return nullptr;
}

#if OMNISIGHT_CASE4_HAS_OPENCV
static cv::Mat array_to_mat_3x3(const std::array<double, 9> &values)
{
	cv::Mat mat(3, 3, CV_64F);

	for (int row = 0; row < 3; row++) {
		for (int col = 0; col < 3; col++)
			mat.at<double>(row, col) = values[(row * 3) + col];
	}

	return mat;
}

static cv::Mat distortion_to_mat(const std::array<double, 8> &values)
{
	cv::Mat mat(1, 8, CV_64F);

	for (int col = 0; col < 8; col++)
		mat.at<double>(0, col) = values[col];

	return mat;
}

static const StitchingFrame *find_frame(const std::vector<StitchingFrame> &frames,
					const std::string &camera_id)
{
	for (const StitchingFrame &frame : frames) {
		if (frame.camera_id == camera_id)
			return frame.image.empty() ? nullptr : &frame;
	}

	return nullptr;
}

static cv::Mat prepare_frame(const StitchingFrame &frame,
			     const CameraCalibration &calibration,
			     bool undistort)
{
	if (!undistort)
		return frame.image;

	cv::Mat prepared;

	cv::undistort(frame.image, prepared, array_to_mat_3x3(calibration.intrinsic),
		      distortion_to_mat(calibration.distortion));
	return prepared;
}

static PipelineStatus stitch_panorama(const std::vector<cv::Mat> &images,
				      cv::Mat *output)
{
	cv::Ptr<cv::Stitcher> stitcher =
		cv::Stitcher::create(cv::Stitcher::PANORAMA);
	const cv::Stitcher::Status status = stitcher->stitch(images, *output);

	return status == cv::Stitcher::OK ? PipelineStatus::kOk :
					    PipelineStatus::kStitchFailed;
}

static PipelineStatus stitch_grid(const std::vector<cv::Mat> &images,
				  int columns, cv::Mat *output)
{
	if (images.empty() || columns < 1)
		return PipelineStatus::kInvalidArgument;

	int tile_width = images.front().cols;
	int tile_height = images.front().rows;

	for (const cv::Mat &image : images) {
		tile_width = std::min(tile_width, image.cols);
		tile_height = std::min(tile_height, image.rows);
	}

	if (tile_width < 1 || tile_height < 1)
		return PipelineStatus::kInvalidArgument;

	const int rows = static_cast<int>((images.size() + columns - 1) / columns);
	*output = cv::Mat::zeros(rows * tile_height, columns * tile_width,
				 images.front().type());

	for (size_t i = 0; i < images.size(); i++) {
		cv::Mat resized;
		const int row = static_cast<int>(i) / columns;
		const int col = static_cast<int>(i) % columns;
		cv::Rect roi(col * tile_width, row * tile_height, tile_width,
			     tile_height);

		cv::resize(images[i], resized, cv::Size(tile_width, tile_height));
		resized.copyTo((*output)(roi));
	}

	return PipelineStatus::kOk;
}
#endif

PipelineStatus run_stitching_pipeline(
	const StitchingConfig &config,
	const std::vector<CameraCalibration> &calibrations,
	const std::vector<StitchingFrame> &frames, StitchingResult *result)
{
	if (!result || !valid_config(config))
		return PipelineStatus::kInvalidArgument;

#if !OMNISIGHT_CASE4_HAS_OPENCV
	(void)calibrations;
	(void)frames;
	return PipelineStatus::kOpenCvUnavailable;
#else
	std::vector<cv::Mat> prepared_frames;
	int64_t timestamp_ns = 0;

	prepared_frames.reserve(config.camera_ids.size());
	for (const std::string &camera_id : config.camera_ids) {
		const CameraCalibration *calibration =
			find_calibration(calibrations, camera_id);
		const StitchingFrame *frame = find_frame(frames, camera_id);

		if (!calibration)
			return PipelineStatus::kCalibrationMissing;
		if (!frame)
			return PipelineStatus::kFrameSetIncomplete;

		prepared_frames.push_back(
			prepare_frame(*frame, *calibration, config.undistort));
		timestamp_ns = std::max(timestamp_ns, frame->timestamp_ns);
	}

	cv::Mat stitched;
	PipelineStatus status;

	if (config.mode == StitchMode::kGrid)
		status = stitch_grid(prepared_frames, config.grid_columns, &stitched);
	else
		status = stitch_panorama(prepared_frames, &stitched);

	if (status != PipelineStatus::kOk)
		return status;

	result->timestamp_ns = timestamp_ns;
	result->image = stitched;
	return PipelineStatus::kOk;
#endif
}

} // namespace omnisight::embedded::case4::rk3588::stitching
