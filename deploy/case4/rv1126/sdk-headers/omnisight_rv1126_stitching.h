/* SPDX-License-Identifier: MIT
 *
 * Customer SDK header for the ATK-DLRV1126 uvc-stitching runtime.
 */
#ifndef OMNISIGHT_RV1126_STITCHING_H
#define OMNISIGHT_RV1126_STITCHING_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define OMNISIGHT_RV1126_SDK_VERSION_MAJOR 1
#define OMNISIGHT_RV1126_SDK_VERSION_MINOR 0
#define OMNISIGHT_RV1126_MAX_CAMERAS 4
#define OMNISIGHT_RV1126_MAX_DEVICE_PATH 128

enum omnisight_rv1126_pixel_format {
	OMNISIGHT_RV1126_PIXEL_FORMAT_NV12 = 0,
	OMNISIGHT_RV1126_PIXEL_FORMAT_YUYV = 1,
};

enum omnisight_rv1126_stitch_mode {
	OMNISIGHT_RV1126_STITCH_MODE_GRID = 0,
	OMNISIGHT_RV1126_STITCH_MODE_PANORAMA = 1,
};

struct omnisight_rv1126_camera {
	char device_path[OMNISIGHT_RV1126_MAX_DEVICE_PATH];
	uint32_t width;
	uint32_t height;
	uint32_t fps;
	enum omnisight_rv1126_pixel_format pixel_format;
};

struct omnisight_rv1126_stitch_config {
	enum omnisight_rv1126_stitch_mode mode;
	uint32_t camera_count;
	struct omnisight_rv1126_camera cameras[OMNISIGHT_RV1126_MAX_CAMERAS];
	uint32_t output_width;
	uint32_t output_height;
};

struct omnisight_rv1126_frame {
	void *data;
	uint32_t length;
	uint64_t monotonic_timestamp_ns;
};

int omnisight_rv1126_stitch_open(
	const struct omnisight_rv1126_stitch_config *config,
	void **handle);
int omnisight_rv1126_stitch_frame(
	void *handle,
	const struct omnisight_rv1126_frame *input_frames,
	uint32_t input_frame_count,
	struct omnisight_rv1126_frame *output_frame);
void omnisight_rv1126_stitch_close(void *handle);

#ifdef __cplusplus
}
#endif

#endif /* OMNISIGHT_RV1126_STITCHING_H */
