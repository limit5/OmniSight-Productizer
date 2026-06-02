/* SPDX-License-Identifier: MIT
 *
 * Public customer SDK types for invoking the Case 4 UVC stitching pipeline.
 */
#ifndef OMNISIGHT_CASE4_STITCHING_H
#define OMNISIGHT_CASE4_STITCHING_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define OMNISIGHT_CASE4_MAX_CAMERAS 8
#define OMNISIGHT_CASE4_MAX_DEVICE_PATH 128
#define OMNISIGHT_CASE4_MAX_CALIBRATION_PATH 256

enum omnisight_case4_stitching_mode {
	OMNISIGHT_CASE4_STITCHING_MODE_GRID = 1,
	OMNISIGHT_CASE4_STITCHING_MODE_PANORAMA = 2,
};

struct omnisight_case4_camera {
	char device_path[OMNISIGHT_CASE4_MAX_DEVICE_PATH];
	uint32_t width;
	uint32_t height;
	uint32_t fps;
};

struct omnisight_case4_stitching_config {
	enum omnisight_case4_stitching_mode mode;
	struct omnisight_case4_camera cameras[OMNISIGHT_CASE4_MAX_CAMERAS];
	uint32_t camera_count;
	char calibration_path[OMNISIGHT_CASE4_MAX_CALIBRATION_PATH];
};

int omnisight_case4_validate_stitching_config(
	const struct omnisight_case4_stitching_config *config);

#ifdef __cplusplus
}
#endif

#endif /* OMNISIGHT_CASE4_STITCHING_H */
