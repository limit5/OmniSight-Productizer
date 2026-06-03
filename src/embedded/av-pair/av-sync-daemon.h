/* SPDX-License-Identifier: MIT
 *
 * Audio/video sync daemon for Case 5 audio/video pairing (OP-1996).
 */
#ifndef OMNISIGHT_EMBEDDED_AV_PAIR_AV_SYNC_DAEMON_H_
#define OMNISIGHT_EMBEDDED_AV_PAIR_AV_SYNC_DAEMON_H_

#include <cstdint>
#include <memory>
#include <string>

namespace omnisight::embedded::av_pair {

enum class AVSyncStatus {
	kOk = 0,
	kInvalidArgument,
	kUnavailable,
	kInvalidState,
	kIoError,
};

enum class AVSyncCorrection {
	kNone = 0,
	kHoldVideoFrame,
	kDropVideoFrame,
	kStretchAudio,
	kShrinkAudio,
};

struct AVSyncDaemonConfig {
	std::string video_device;
	std::string audio_pcm;
	uint32_t audio_sample_rate_hz = 48000;
	uint32_t video_frame_interval_us = 33333;
	uint32_t jitter_buffer_depth_ms = 80;
	uint32_t drift_correction_threshold_us = 2000;
	uint32_t resample_threshold_us = 10000;
};

struct AVSyncSnapshot {
	int64_t video_timestamp_us = 0;
	int64_t audio_timestamp_us = 0;
	int64_t audio_position_frames = 0;
	int64_t drift_us = 0;
	uint32_t jitter_buffer_depth_ms = 0;
	uint32_t underrun_count = 0;
	AVSyncCorrection correction = AVSyncCorrection::kNone;
};

class AVSyncDaemon {
public:
	explicit AVSyncDaemon(AVSyncDaemonConfig config);
	~AVSyncDaemon();

	AVSyncDaemon(const AVSyncDaemon &) = delete;
	AVSyncDaemon &operator=(const AVSyncDaemon &) = delete;
	AVSyncDaemon(AVSyncDaemon &&) noexcept;
	AVSyncDaemon &operator=(AVSyncDaemon &&) noexcept;

	AVSyncStatus start();
	void stop();

	AVSyncStatus setJitterBufferDepthMs(uint32_t depth_ms);
	uint32_t jitterBufferDepthMs() const;
	AVSyncSnapshot lastSnapshot() const;

	bool running() const;
	bool available() const;
	const std::string &lastError() const;

private:
	class Impl;

	std::unique_ptr<Impl> impl_;
};

} // namespace omnisight::embedded::av_pair

#endif // OMNISIGHT_EMBEDDED_AV_PAIR_AV_SYNC_DAEMON_H_
