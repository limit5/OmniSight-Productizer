/* SPDX-License-Identifier: MIT
 *
 * Case 4 QCS6490 V4L2 frame synchronization helpers (OP-1967).
 *
 * Frames are aligned by monotonic V4L2 buffer timestamps. Camera drivers must
 * queue buffers with V4L2_BUF_FLAG_TIMESTAMP_MONOTONIC for deterministic
 * cross-camera matching.
 */
#include <algorithm>
#include <cerrno>
#include <cstdlib>
#include <cstdint>
#include <map>
#include <string>
#include <sys/time.h>
#include <vector>

#if __has_include(<linux/videodev2.h>)
#include <linux/videodev2.h>
#define OMNISIGHT_CASE4_HAS_V4L2 1
#else
#define OMNISIGHT_CASE4_HAS_V4L2 0
#endif

#if __has_include(<opencv2/opencv.hpp>)
#include <opencv2/opencv.hpp>
#define OMNISIGHT_CASE4_HAS_OPENCV 1
#else
#define OMNISIGHT_CASE4_HAS_OPENCV 0
#endif

namespace omnisight::embedded::case4::qcs6490::stitching {

enum class FrameSyncStatus {
	kOk = 0,
	kInvalidArgument,
	kTimestampNotMonotonic,
	kQueueIncomplete,
	kNoAlignedFrameSet,
};

struct CapturedFrame {
	std::string camera_id;
	uint32_t buffer_index = 0;
	uint32_t sequence = 0;
	int64_t timestamp_ns = 0;
	bool timestamp_monotonic = false;
#if OMNISIGHT_CASE4_HAS_OPENCV
	cv::Mat image;
#endif
};

struct FrameSet {
	std::vector<CapturedFrame> frames;
	int64_t aligned_timestamp_ns = 0;
	int64_t max_skew_ns = 0;
};

struct FrameSyncConfig {
	std::vector<std::string> camera_ids;
	int64_t tolerance_ns = 5000000;
};

static bool valid_config(const FrameSyncConfig &config)
{
	if (config.camera_ids.size() < 2 || config.tolerance_ns < 0)
		return false;

	for (const std::string &camera_id : config.camera_ids) {
		if (camera_id.empty())
			return false;
	}

	return true;
}

static int64_t timestamp_to_ns(const timeval &timestamp)
{
	return (static_cast<int64_t>(timestamp.tv_sec) * 1000000000LL) +
	       (static_cast<int64_t>(timestamp.tv_usec) * 1000LL);
}

#if OMNISIGHT_CASE4_HAS_V4L2
CapturedFrame frame_from_v4l2_buffer(const std::string &camera_id,
				     const v4l2_buffer &buffer)
{
	CapturedFrame frame;

	frame.camera_id = camera_id;
	frame.buffer_index = buffer.index;
	frame.sequence = buffer.sequence;
	frame.timestamp_ns = timestamp_to_ns(buffer.timestamp);
	frame.timestamp_monotonic =
		(buffer.flags & V4L2_BUF_FLAG_TIMESTAMP_MASK) ==
		V4L2_BUF_FLAG_TIMESTAMP_MONOTONIC;
	return frame;
}
#endif

FrameSyncStatus align_frame_set(const FrameSyncConfig &config,
				const std::vector<CapturedFrame> &queued_frames,
				FrameSet *frame_set)
{
	if (!frame_set || !valid_config(config))
		return FrameSyncStatus::kInvalidArgument;

	std::map<std::string, std::vector<CapturedFrame>> by_camera;

	for (const CapturedFrame &frame : queued_frames) {
		if (!frame.timestamp_monotonic)
			return FrameSyncStatus::kTimestampNotMonotonic;
		by_camera[frame.camera_id].push_back(frame);
	}

	for (const std::string &camera_id : config.camera_ids) {
		auto it = by_camera.find(camera_id);

		if (it == by_camera.end() || it->second.empty())
			return FrameSyncStatus::kQueueIncomplete;
		std::sort(it->second.begin(), it->second.end(),
			  [](const CapturedFrame &left,
			     const CapturedFrame &right) {
				  return left.timestamp_ns < right.timestamp_ns;
			  });
	}

	int64_t best_skew = config.tolerance_ns + 1;
	FrameSet best;
	const std::vector<CapturedFrame> &anchor_frames =
		by_camera[config.camera_ids.front()];

	for (const CapturedFrame &anchor : anchor_frames) {
		FrameSet candidate;
		int64_t min_ts = anchor.timestamp_ns;
		int64_t max_ts = anchor.timestamp_ns;
		bool complete = true;

		candidate.frames.push_back(anchor);
		for (size_t i = 1; i < config.camera_ids.size(); i++) {
			const auto &frames = by_camera[config.camera_ids[i]];
			auto nearest = std::min_element(
				frames.begin(), frames.end(),
				[&anchor](const CapturedFrame &left,
					  const CapturedFrame &right) {
					const int64_t left_delta =
						std::llabs(left.timestamp_ns -
							   anchor.timestamp_ns);
					const int64_t right_delta =
						std::llabs(right.timestamp_ns -
							   anchor.timestamp_ns);

					return left_delta < right_delta;
				});

			if (nearest == frames.end()) {
				complete = false;
				break;
			}
			min_ts = std::min(min_ts, nearest->timestamp_ns);
			max_ts = std::max(max_ts, nearest->timestamp_ns);
			candidate.frames.push_back(*nearest);
		}

		if (!complete)
			continue;

		const int64_t skew = max_ts - min_ts;
		if (skew <= config.tolerance_ns && skew < best_skew) {
			best_skew = skew;
			best = candidate;
			best.aligned_timestamp_ns = min_ts + (skew / 2);
			best.max_skew_ns = skew;
		}
	}

	if (best.frames.empty())
		return FrameSyncStatus::kNoAlignedFrameSet;

	*frame_set = best;
	return FrameSyncStatus::kOk;
}

std::vector<CapturedFrame> prune_frames_older_than(
	const std::vector<CapturedFrame> &queued_frames, int64_t timestamp_ns)
{
	std::vector<CapturedFrame> retained;

	retained.reserve(queued_frames.size());
	for (const CapturedFrame &frame : queued_frames) {
		if (frame.timestamp_ns >= timestamp_ns)
			retained.push_back(frame);
	}

	return retained;
}

} // namespace omnisight::embedded::case4::qcs6490::stitching
