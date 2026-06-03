// SPDX-License-Identifier: MIT
//
// Genio 1200 aarch64 stitching pipeline configuration for OP-1970.
//
// The BSP may route remap/blend work through the MediaTek APU runtime, but the
// shared source remains syntax-checkable without vendor SDK headers.
#include <cstddef>
#include <cstdint>
#include <cstring>

namespace {

constexpr std::uint32_t kMaxCameras = 4;
constexpr std::uint32_t kDefaultCameras = 4;
constexpr std::uint32_t kProcessWidth = 1280;
constexpr std::uint32_t kProcessHeight = 720;
constexpr std::uint32_t kChannels = 3;
constexpr std::uint32_t kBlendBands = 5;
constexpr char kBackendApu[] = "opencv-aarch64-mtk-apu-remap-blend";
constexpr char kBackendNeon[] = "opencv-aarch64-neon-remap-blend";

bool valid_camera_count(std::uint32_t camera_count)
{
	return camera_count >= 2 && camera_count <= kMaxCameras;
}

} // namespace

extern "C" {

struct genio1200_stitching_pipeline_config {
	std::uint32_t camera_count;
	std::uint32_t process_width;
	std::uint32_t process_height;
	std::uint32_t output_width;
	std::uint32_t output_height;
	std::uint32_t blend_bands;
	bool enable_apu;
	bool enable_neon;
	bool use_fixed_maps;
};

struct genio1200_stitching_pipeline_budget {
	std::size_t frame_bytes;
	std::size_t map_bytes;
	std::size_t blend_bytes;
	std::size_t scratch_bytes;
};

void genio1200_stitching_default_pipeline_config(
	struct genio1200_stitching_pipeline_config *config)
{
	if (!config)
		return;

	std::memset(config, 0, sizeof(*config));
	config->camera_count = kDefaultCameras;
	config->process_width = kProcessWidth;
	config->process_height = kProcessHeight;
	config->output_width = kProcessWidth * 2;
	config->output_height = kProcessHeight * 2;
	config->blend_bands = kBlendBands;
	config->enable_apu = true;
#if defined(__ARM_NEON) || defined(__ARM_NEON__)
	config->enable_neon = true;
#else
	config->enable_neon = false;
#endif
	config->use_fixed_maps = true;
}

bool genio1200_stitching_validate_pipeline_config(
	const struct genio1200_stitching_pipeline_config *config)
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
	if (config->enable_apu && !config->use_fixed_maps)
		return false;

	return true;
}

bool genio1200_stitching_estimate_pipeline_budget(
	const struct genio1200_stitching_pipeline_config *config,
	struct genio1200_stitching_pipeline_budget *budget)
{
	std::size_t pixels;

	if (!genio1200_stitching_validate_pipeline_config(config) || !budget)
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

const char *genio1200_stitching_opencv_backend(
	const struct genio1200_stitching_pipeline_config *config)
{
	if (config && config->enable_apu)
		return kBackendApu;

	return kBackendNeon;
}

} // extern "C"
