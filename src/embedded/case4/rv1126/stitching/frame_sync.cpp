// SPDX-License-Identifier: MIT
//
// Fixed-storage frame timestamp alignment for OP-1951 RV1126 stitching.
#include <cstddef>
#include <cstdint>
#include <cstring>

namespace {

constexpr std::size_t kMaxCameras = 4;
constexpr std::size_t kQueueDepth = 3;
constexpr std::uint64_t kDefaultToleranceNs = 33333333ULL;

std::uint64_t delta_ns(std::uint64_t left, std::uint64_t right)
{
	return left > right ? left - right : right - left;
}

} // namespace

extern "C" {

struct rv1126_stitching_frame {
	std::uint32_t camera_id;
	std::uint32_t sequence;
	std::uint64_t monotonic_ns;
	void *user;
};

struct rv1126_stitching_frame_sync {
	std::uint32_t camera_count;
	std::uint64_t tolerance_ns;
	rv1126_stitching_frame slots[kMaxCameras][kQueueDepth];
	std::uint8_t counts[kMaxCameras];
};

void rv1126_stitching_frame_sync_init(
	struct rv1126_stitching_frame_sync *sync,
	std::uint32_t camera_count,
	std::uint64_t tolerance_ns)
{
	if (!sync)
		return;

	std::memset(sync, 0, sizeof(*sync));
	if (camera_count == 0 || camera_count > kMaxCameras)
		camera_count = kMaxCameras;

	sync->camera_count = camera_count;
	sync->tolerance_ns = tolerance_ns ? tolerance_ns : kDefaultToleranceNs;
}

bool rv1126_stitching_frame_sync_push(
	struct rv1126_stitching_frame_sync *sync,
	const struct rv1126_stitching_frame *frame)
{
	std::uint8_t *count;
	rv1126_stitching_frame *queue;

	if (!sync || !frame)
		return false;
	if (frame->camera_id >= sync->camera_count)
		return false;

	count = &sync->counts[frame->camera_id];
	queue = sync->slots[frame->camera_id];
	if (*count == kQueueDepth) {
		for (std::size_t i = 1; i < kQueueDepth; i++)
			queue[i - 1] = queue[i];
		*count = kQueueDepth - 1;
	}

	queue[*count] = *frame;
	(*count)++;
	return true;
}

bool rv1126_stitching_frame_sync_pop_aligned(
	struct rv1126_stitching_frame_sync *sync,
	struct rv1126_stitching_frame *frames,
	std::size_t frames_len)
{
	std::uint64_t anchor_ns;

	if (!sync || !frames)
		return false;
	if (frames_len < sync->camera_count)
		return false;

	for (std::uint32_t camera = 0; camera < sync->camera_count; camera++) {
		if (sync->counts[camera] == 0)
			return false;
	}

	anchor_ns = sync->slots[0][0].monotonic_ns;
	for (std::uint32_t camera = 1; camera < sync->camera_count; camera++) {
		std::uint64_t candidate = sync->slots[camera][0].monotonic_ns;

		if (candidate > anchor_ns)
			anchor_ns = candidate;
	}

	for (std::uint32_t camera = 0; camera < sync->camera_count; camera++) {
		rv1126_stitching_frame *queue = sync->slots[camera];
		std::uint8_t *count = &sync->counts[camera];
		std::uint8_t selected = 0;

		for (std::uint8_t i = 1; i < *count; i++) {
			if (delta_ns(queue[i].monotonic_ns, anchor_ns) <
			    delta_ns(queue[selected].monotonic_ns, anchor_ns))
				selected = i;
		}

		if (delta_ns(queue[selected].monotonic_ns, anchor_ns) >
		    sync->tolerance_ns)
			return false;

		frames[camera] = queue[selected];
		for (std::uint8_t i = selected + 1; i < *count; i++)
			queue[i - 1] = queue[i];
		(*count)--;
	}

	return true;
}

} // extern "C"
