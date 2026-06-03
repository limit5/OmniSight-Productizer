// SPDX-License-Identifier: MIT
//
// Genio 1200 stitching calibration defaults for OP-1970.
//
// Phase 1A keeps the MediaTek EVK calibration payload host-checkable while the
// BSP build links the actual OpenCV/MediaTek camera stack. The layout mirrors
// the wave-1 Case 4 fixed-storage calibration helpers.
#include <cmath>
#include <cstddef>
#include <cstring>

namespace {

constexpr int kMaxCameras = 4;
constexpr int kMinCameras = 2;
constexpr int kDefaultCameras = 4;
constexpr int kProcessWidth = 1280;
constexpr int kProcessHeight = 720;
constexpr float kDefaultFocal = 920.0F;
constexpr float kDefaultCx = 640.0F;
constexpr float kDefaultCy = 360.0F;
constexpr float kIdentity3x3[9] = {
	1.0F, 0.0F, 0.0F,
	0.0F, 1.0F, 0.0F,
	0.0F, 0.0F, 1.0F,
};

bool finite_matrix(const float *values, std::size_t count)
{
	for (std::size_t i = 0; i < count; i++) {
		if (!std::isfinite(values[i]))
			return false;
	}

	return true;
}

} // namespace

extern "C" {

struct genio1200_stitching_calibration {
	int camera_count;
	int process_width;
	int process_height;
	float intrinsics[kMaxCameras][9];
	float distortion[kMaxCameras][5];
	float homographies[kMaxCameras][9];
};

int genio1200_stitching_calibration_max_cameras(void)
{
	return kMaxCameras;
}

std::size_t genio1200_stitching_calibration_bytes(void)
{
	return sizeof(genio1200_stitching_calibration);
}

void genio1200_stitching_default_calibration(
	struct genio1200_stitching_calibration *calibration)
{
	if (!calibration)
		return;

	std::memset(calibration, 0, sizeof(*calibration));
	calibration->camera_count = kDefaultCameras;
	calibration->process_width = kProcessWidth;
	calibration->process_height = kProcessHeight;

	for (int camera = 0; camera < kMaxCameras; camera++) {
		std::memcpy(calibration->intrinsics[camera], kIdentity3x3,
			    sizeof(kIdentity3x3));
		std::memcpy(calibration->homographies[camera], kIdentity3x3,
			    sizeof(kIdentity3x3));

		calibration->intrinsics[camera][0] = kDefaultFocal;
		calibration->intrinsics[camera][4] = kDefaultFocal;
		calibration->intrinsics[camera][2] = kDefaultCx;
		calibration->intrinsics[camera][5] = kDefaultCy;
	}
}

bool genio1200_stitching_validate_calibration(
	const struct genio1200_stitching_calibration *calibration)
{
	if (!calibration)
		return false;
	if (calibration->camera_count < kMinCameras ||
	    calibration->camera_count > kMaxCameras)
		return false;
	if (calibration->process_width <= 0 || calibration->process_height <= 0)
		return false;
	if (calibration->process_width > kProcessWidth ||
	    calibration->process_height > kProcessHeight)
		return false;

	for (int camera = 0; camera < calibration->camera_count; camera++) {
		if (!finite_matrix(calibration->intrinsics[camera], 9))
			return false;
		if (!finite_matrix(calibration->distortion[camera], 5))
			return false;
		if (!finite_matrix(calibration->homographies[camera], 9))
			return false;
		if (calibration->intrinsics[camera][0] <= 0.0F ||
		    calibration->intrinsics[camera][4] <= 0.0F)
			return false;
	}

	return true;
}

} // extern "C"
