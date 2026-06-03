// SPDX-License-Identifier: MIT
//
// RV1126 armhf stitching pipeline configuration for OP-1951.
//
// This file intentionally keeps OpenCV behind a runtime-facing profile string:
// the BSP build supplies OpenCV, while this source remains syntax-checkable in
// the shared repository without adding a new host dependency.
#include <cstddef>
#include <cstdint>
#include <cstring>

namespace {

constexpr std::uint32_t kMaxCameras = 4;
constexpr std::uint32_t kDefaultCameras = 2;
constexpr std::uint32_t kProcessWidth = 960;
constexpr std::uint32_t kProcessHeight = 544;
constexpr std::uint32_t kChannels = 3;
constexpr std::uint32_t kBlendBands = 3;
constexpr char kBackendArmhfNeon[] = "opencv-armhf-neon-remap-blend";
constexpr char kBackendArmhfScalar[] = "opencv-armhf-scalar-remap-blend";

bool valid_camera_count(std::uint32_t camera_count)
{
	return camera_count >= 2 && camera_count <= kMaxCameras;
}

} // namespace

extern "C" {

struct rv1126_stitching_pipeline_config {
	std::uint32_t camera_count;
	std::uint32_t process_width;
	std::uint32_t process_height;
	std::uint32_t output_width;
	std::uint32_t output_height;
	std::uint32_t blend_bands;
	bool enable_neon;
	bool use_fixed_maps;
};

struct rv1126_stitching_pipeline_budget {
	std::size_t frame_bytes;
	std::size_t map_bytes;
	std::size_t blend_bytes;
	std::size_t scratch_bytes;
};

void rv1126_stitching_default_pipeline_config(
	struct rv1126_stitching_pipeline_config *config)
{
	if (!config)
		return;

	std::memset(config, 0, sizeof(*config));
	config->camera_count = kDefaultCameras;
	config->process_width = kProcessWidth;
	config->process_height = kProcessHeight;
	config->output_width = kProcessWidth * kDefaultCameras;
	config->output_height = kProcessHeight;
	config->blend_bands = kBlendBands;
#if defined(__ARM_NEON) || defined(__ARM_NEON__)
	config->enable_neon = true;
#else
	config->enable_neon = false;
#endif
	config->use_fixed_maps = true;
}

bool rv1126_stitching_validate_pipeline_config(
	const struct rv1126_stitching_pipeline_config *config)
{
	if (!config)
		return false;
	if (!valid_camera_count(config->camera_count))
		return false;
	if (config->process_width == 0 || config->process_height == 0)
		return false;
	if (config->process_width > kProcessWidth ||
	    config->process_height > kProcessHeight)
		return false;
	if (config->output_width < config->process_width ||
	    config->output_height < config->process_height)
		return false;
	if (config->blend_bands == 0 || config->blend_bands > kBlendBands)
		return false;

	return true;
}

bool rv1126_stitching_estimate_pipeline_budget(
	const struct rv1126_stitching_pipeline_config *config,
	struct rv1126_stitching_pipeline_budget *budget)
{
	std::size_t pixels;

	if (!rv1126_stitching_validate_pipeline_config(config) || !budget)
		return false;

	std::memset(budget, 0, sizeof(*budget));
	pixels = static_cast<std::size_t>(config->process_width) *
		 static_cast<std::size_t>(config->process_height);
	budget->frame_bytes = pixels * kChannels * config->camera_count;
	budget->map_bytes = pixels * sizeof(std::int16_t) * 2 *
			    config->camera_count;
	budget->blend_bytes = static_cast<std::size_t>(config->output_width) *
			      config->output_height * kChannels;
	budget->scratch_bytes = budget->frame_bytes + budget->map_bytes +
				budget->blend_bytes;
	return true;
}

const char *rv1126_stitching_opencv_backend(
	const struct rv1126_stitching_pipeline_config *config)
{
	if (config && config->enable_neon)
		return kBackendArmhfNeon;

	return kBackendArmhfScalar;
}

} // extern "C"
