/* SPDX-License-Identifier: MIT
 *
 * Case 4 RK3588 multi-camera calibration helpers (OP-1948).
 *
 * The platform build is expected to link this file with OpenCV. When OpenCV
 * headers are unavailable in a host-only smoke build, the public helpers fail
 * closed instead of emitting placeholder calibration data.
 */
#include <array>
#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <fstream>
#include <limits>
#include <sstream>
#include <string>
#include <vector>

#if __has_include(<opencv2/opencv.hpp>)
#include <opencv2/opencv.hpp>
#define OMNISIGHT_CASE4_HAS_OPENCV 1
#else
#define OMNISIGHT_CASE4_HAS_OPENCV 0
#endif

namespace omnisight::embedded::case4::rk3588::stitching {

enum class CalibrationStatus {
	kOk = 0,
	kInvalidArgument,
	kOpenCvUnavailable,
	kInsufficientFrames,
	kPatternNotFound,
	kCalibrationFailed,
	kFileError,
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

struct ChessboardSpec {
	int columns = 0;
	int rows = 0;
	double square_size_mm = 0.0;
};

struct IntrinsicCalibrationRequest {
	std::string camera_id;
	ChessboardSpec board;
#if OMNISIGHT_CASE4_HAS_OPENCV
	std::vector<cv::Mat> frames;
#else
	std::vector<int> frames;
#endif
};

static bool valid_board(const ChessboardSpec &board)
{
	return board.columns > 1 && board.rows > 1 && board.square_size_mm > 0.0;
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

static std::array<double, 9> mat_to_array_3x3(const cv::Mat &mat)
{
	std::array<double, 9> values{};

	for (int row = 0; row < 3; row++) {
		for (int col = 0; col < 3; col++)
			values[(row * 3) + col] = mat.at<double>(row, col);
	}

	return values;
}

static std::array<double, 8> distortion_to_array(const cv::Mat &mat)
{
	std::array<double, 8> values{};
	const int count = std::min(8, mat.rows * mat.cols);

	for (int i = 0; i < count; i++)
		values[i] = mat.at<double>(i);

	return values;
}

static std::vector<cv::Point3f> make_board_points(const ChessboardSpec &board)
{
	std::vector<cv::Point3f> points;

	points.reserve(static_cast<size_t>(board.columns * board.rows));
	for (int row = 0; row < board.rows; row++) {
		for (int col = 0; col < board.columns; col++) {
			points.emplace_back(
				static_cast<float>(col * board.square_size_mm),
				static_cast<float>(row * board.square_size_mm),
				0.0f);
		}
	}

	return points;
}

static double compute_reprojection_error(
	const std::vector<std::vector<cv::Point3f>> &object_points,
	const std::vector<std::vector<cv::Point2f>> &image_points,
	const std::vector<cv::Mat> &rvecs, const std::vector<cv::Mat> &tvecs,
	const cv::Mat &camera_matrix, const cv::Mat &dist_coeffs)
{
	double total_error = 0.0;
	size_t total_points = 0;

	for (size_t i = 0; i < object_points.size(); i++) {
		std::vector<cv::Point2f> projected;
		double error;

		cv::projectPoints(object_points[i], rvecs[i], tvecs[i],
				  camera_matrix, dist_coeffs, projected);
		error = cv::norm(image_points[i], projected, cv::NORM_L2);
		total_error += error * error;
		total_points += object_points[i].size();
	}

	if (total_points == 0)
		return std::numeric_limits<double>::infinity();

	return std::sqrt(total_error / static_cast<double>(total_points));
}
#endif

CalibrationStatus calibrate_intrinsics(
	const IntrinsicCalibrationRequest &request, CameraCalibration *calibration)
{
	if (!calibration || request.camera_id.empty() || !valid_board(request.board))
		return CalibrationStatus::kInvalidArgument;

#if !OMNISIGHT_CASE4_HAS_OPENCV
	(void)request;
	return CalibrationStatus::kOpenCvUnavailable;
#else
	if (request.frames.size() < 3)
		return CalibrationStatus::kInsufficientFrames;

	const cv::Size pattern_size(request.board.columns, request.board.rows);
	const std::vector<cv::Point3f> board_points =
		make_board_points(request.board);
	std::vector<std::vector<cv::Point3f>> object_points;
	std::vector<std::vector<cv::Point2f>> image_points;
	cv::Size image_size;

	for (const cv::Mat &frame : request.frames) {
		std::vector<cv::Point2f> corners;
		cv::Mat gray;

		if (frame.empty())
			continue;
		image_size = frame.size();
		if (frame.channels() == 1)
			gray = frame;
		else
			cv::cvtColor(frame, gray, cv::COLOR_BGR2GRAY);

		if (!cv::findChessboardCorners(gray, pattern_size, corners))
			continue;

		cv::cornerSubPix(gray, corners, cv::Size(11, 11), cv::Size(-1, -1),
				 cv::TermCriteria(cv::TermCriteria::EPS +
						  cv::TermCriteria::COUNT,
						  30, 0.001));
		object_points.push_back(board_points);
		image_points.push_back(corners);
	}

	if (image_points.size() < 3)
		return image_points.empty() ? CalibrationStatus::kPatternNotFound :
					      CalibrationStatus::kInsufficientFrames;

	cv::Mat camera_matrix = cv::Mat::eye(3, 3, CV_64F);
	cv::Mat dist_coeffs = cv::Mat::zeros(1, 8, CV_64F);
	std::vector<cv::Mat> rvecs;
	std::vector<cv::Mat> tvecs;
	const double rms = cv::calibrateCamera(
		object_points, image_points, image_size, camera_matrix, dist_coeffs,
		rvecs, tvecs, cv::CALIB_RATIONAL_MODEL);

	if (!std::isfinite(rms))
		return CalibrationStatus::kCalibrationFailed;

	calibration->camera_id = request.camera_id;
	calibration->intrinsic = mat_to_array_3x3(camera_matrix);
	calibration->distortion = distortion_to_array(dist_coeffs);
	calibration->rotation = {1.0, 0.0, 0.0, 0.0, 1.0,
				 0.0, 0.0, 0.0, 1.0};
	calibration->translation = {0.0, 0.0, 0.0};
	calibration->reprojection_error = compute_reprojection_error(
		object_points, image_points, rvecs, tvecs, camera_matrix,
		dist_coeffs);
	calibration->valid = true;
	return CalibrationStatus::kOk;
#endif
}

CalibrationStatus estimate_extrinsics_from_chessboard(
	const CameraCalibration &intrinsics, const ChessboardSpec &board,
#if OMNISIGHT_CASE4_HAS_OPENCV
	const cv::Mat &frame,
#else
	const int &frame,
#endif
	CameraCalibration *calibration)
{
	if (!calibration || !intrinsics.valid || !valid_board(board))
		return CalibrationStatus::kInvalidArgument;

#if !OMNISIGHT_CASE4_HAS_OPENCV
	(void)frame;
	return CalibrationStatus::kOpenCvUnavailable;
#else
	if (frame.empty())
		return CalibrationStatus::kInvalidArgument;

	cv::Mat gray;
	std::vector<cv::Point2f> corners;
	const cv::Size pattern_size(board.columns, board.rows);

	if (frame.channels() == 1)
		gray = frame;
	else
		cv::cvtColor(frame, gray, cv::COLOR_BGR2GRAY);

	if (!cv::findChessboardCorners(gray, pattern_size, corners))
		return CalibrationStatus::kPatternNotFound;

	cv::cornerSubPix(gray, corners, cv::Size(11, 11), cv::Size(-1, -1),
			 cv::TermCriteria(cv::TermCriteria::EPS +
					  cv::TermCriteria::COUNT,
					  30, 0.001));

	cv::Mat rvec;
	cv::Mat tvec;
	if (!cv::solvePnP(make_board_points(board), corners,
			  array_to_mat_3x3(intrinsics.intrinsic),
			  distortion_to_mat(intrinsics.distortion), rvec, tvec))
		return CalibrationStatus::kCalibrationFailed;

	cv::Mat rotation;
	cv::Rodrigues(rvec, rotation);

	*calibration = intrinsics;
	calibration->rotation = mat_to_array_3x3(rotation);
	calibration->translation = {tvec.at<double>(0), tvec.at<double>(1),
				    tvec.at<double>(2)};
	calibration->valid = true;
	return CalibrationStatus::kOk;
#endif
}

CalibrationStatus write_calibration_file(
	const std::string &path, const std::vector<CameraCalibration> &calibrations)
{
	if (path.empty())
		return CalibrationStatus::kInvalidArgument;

	std::ofstream out(path);

	if (!out)
		return CalibrationStatus::kFileError;

	out << "omnisight_case4_rk3588_calibration_v1\n";
	out << calibrations.size() << "\n";
	for (const CameraCalibration &calibration : calibrations) {
		out << calibration.camera_id << "\n";
		out << calibration.valid << " " << calibration.reprojection_error
		    << "\n";
		for (double value : calibration.intrinsic)
			out << value << " ";
		out << "\n";
		for (double value : calibration.distortion)
			out << value << " ";
		out << "\n";
		for (double value : calibration.rotation)
			out << value << " ";
		out << "\n";
		for (double value : calibration.translation)
			out << value << " ";
		out << "\n";
	}

	return out.good() ? CalibrationStatus::kOk : CalibrationStatus::kFileError;
}

CalibrationStatus read_calibration_file(
	const std::string &path, std::vector<CameraCalibration> *calibrations)
{
	if (!calibrations || path.empty())
		return CalibrationStatus::kInvalidArgument;

	std::ifstream in(path);
	std::string magic;
	size_t count;

	if (!in)
		return CalibrationStatus::kFileError;
	in >> magic >> count;
	if (magic != "omnisight_case4_rk3588_calibration_v1")
		return CalibrationStatus::kFileError;

	calibrations->clear();
	calibrations->reserve(count);
	for (size_t i = 0; i < count; i++) {
		CameraCalibration calibration;

		in >> calibration.camera_id >> calibration.valid >>
			calibration.reprojection_error;
		for (double &value : calibration.intrinsic)
			in >> value;
		for (double &value : calibration.distortion)
			in >> value;
		for (double &value : calibration.rotation)
			in >> value;
		for (double &value : calibration.translation)
			in >> value;
		if (!in)
			return CalibrationStatus::kFileError;
		calibrations->push_back(calibration);
	}

	return CalibrationStatus::kOk;
}

} // namespace omnisight::embedded::case4::rk3588::stitching
