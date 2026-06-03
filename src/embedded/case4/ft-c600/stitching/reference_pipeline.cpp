// SPDX-License-Identifier: MIT
/*
 * FT-C600 Case 4 stitching reference pipeline (OP-1954).
 *
 * FT-C600 reuses the existing Case 2 single-camera hardware path.  Case 4
 * still expects every EVK bundle to expose a stitching-shaped pipeline, so
 * this reference implementation treats FT-C600 as a degenerate one-camera
 * topology and forwards the single input frame unchanged.
 */
#include <stddef.h>
#include <stdint.h>
#include <string.h>

struct ft_c600_frame {
	const uint8_t *data;
	size_t size;
	uint32_t width;
	uint32_t height;
	uint64_t timestamp_ns;
};

struct ft_c600_output_frame {
	uint8_t *data;
	size_t capacity;
	size_t size;
	uint32_t width;
	uint32_t height;
	uint64_t timestamp_ns;
};

struct ft_c600_stitching_pipeline {
	int initialized;
	size_t camera_count;
};

namespace {

constexpr size_t kFtC600CameraCount = 1;

bool frame_shape_is_valid(const struct ft_c600_frame &frame)
{
	return frame.data != nullptr && frame.size > 0 && frame.width > 0 &&
	       frame.height > 0;
}

bool output_has_capacity(const struct ft_c600_output_frame &output,
			 size_t size)
{
	return output.data != nullptr && output.capacity >= size;
}

} // namespace

extern "C" {

int ft_c600_reference_pipeline_init(
	struct ft_c600_stitching_pipeline *pipeline)
{
	if (pipeline == nullptr)
		return -1;

	pipeline->initialized = 1;
	pipeline->camera_count = kFtC600CameraCount;
	return 0;
}

size_t ft_c600_reference_pipeline_camera_count(
	const struct ft_c600_stitching_pipeline *pipeline)
{
	if (pipeline == nullptr || !pipeline->initialized)
		return 0;

	return pipeline->camera_count;
}

int ft_c600_reference_pipeline_stitch(
	const struct ft_c600_stitching_pipeline *pipeline,
	const struct ft_c600_frame *input_frames,
	size_t input_frame_count,
	struct ft_c600_output_frame *output_frame)
{
	const struct ft_c600_frame *input_frame;

	if (pipeline == nullptr || !pipeline->initialized ||
	    input_frames == nullptr || output_frame == nullptr)
		return -1;
	if (input_frame_count != kFtC600CameraCount)
		return -2;

	input_frame = &input_frames[0];
	if (!frame_shape_is_valid(*input_frame))
		return -3;
	if (!output_has_capacity(*output_frame, input_frame->size))
		return -4;

	memcpy(output_frame->data, input_frame->data, input_frame->size);
	output_frame->size = input_frame->size;
	output_frame->width = input_frame->width;
	output_frame->height = input_frame->height;
	output_frame->timestamp_ns = input_frame->timestamp_ns;
	return 0;
}

void ft_c600_reference_pipeline_reset(
	struct ft_c600_stitching_pipeline *pipeline)
{
	if (pipeline == nullptr)
		return;

	pipeline->initialized = 0;
	pipeline->camera_count = 0;
}

}
