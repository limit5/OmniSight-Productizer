/* SPDX-License-Identifier: MIT
 *
 * Audio/video sync daemon for Case 5 audio/video pairing (OP-1996).
 */
#include "av-sync-daemon.h"

#include <algorithm>
#include <atomic>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <thread>
#include <utility>

#if defined(OMNISIGHT_AV_SYNC_WITH_NATIVE)
#include <cerrno>
#include <fcntl.h>
#include <linux/videodev2.h>
#include <poll.h>
#include <sstream>
#include <sys/ioctl.h>
#include <unistd.h>

#include <alsa/asoundlib.h>
#endif

namespace omnisight::embedded::av_pair {
namespace {

constexpr uint32_t kMinJitterDepthMs = 5;
constexpr uint32_t kMaxJitterDepthMs = 1000;

static bool valid_config(const AVSyncDaemonConfig &config)
{
	return !config.video_device.empty() && !config.audio_pcm.empty() &&
	       config.audio_sample_rate_hz != 0 &&
	       config.video_frame_interval_us != 0 &&
	       config.drift_correction_threshold_us != 0 &&
	       config.resample_threshold_us >=
		       config.drift_correction_threshold_us &&
	       config.jitter_buffer_depth_ms >= kMinJitterDepthMs &&
	       config.jitter_buffer_depth_ms <= kMaxJitterDepthMs;
}

static AVSyncCorrection correction_for_drift(
	int64_t drift_us, uint32_t correction_threshold_us,
	uint32_t resample_threshold_us)
{
	const int64_t abs_drift = std::llabs(drift_us);

	if (abs_drift < correction_threshold_us)
		return AVSyncCorrection::kNone;
	if (abs_drift < resample_threshold_us)
		return drift_us > 0 ? AVSyncCorrection::kStretchAudio :
				      AVSyncCorrection::kShrinkAudio;

	return drift_us > 0 ? AVSyncCorrection::kHoldVideoFrame :
			      AVSyncCorrection::kDropVideoFrame;
}

#if defined(OMNISIGHT_AV_SYNC_WITH_NATIVE)
static int64_t timeval_to_us(const timeval &tv)
{
	return static_cast<int64_t>(tv.tv_sec) * 1000000LL + tv.tv_usec;
}

static int64_t timespec_to_us(const timespec &ts)
{
	return static_cast<int64_t>(ts.tv_sec) * 1000000LL + ts.tv_nsec / 1000;
}

static int64_t monotonic_us()
{
	timespec ts {};

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return timespec_to_us(ts);
}
#endif

} // namespace

class AVSyncDaemon::Impl {
public:
	explicit Impl(AVSyncDaemonConfig config) : config_(std::move(config))
	{
		snapshot_.jitter_buffer_depth_ms = config_.jitter_buffer_depth_ms;
	}

	~Impl()
	{
		stop();
	}

	AVSyncStatus start()
	{
		if (!valid_config(config_)) {
			last_error_ = "AV sync configuration is incomplete";
			return AVSyncStatus::kInvalidArgument;
		}

#if !defined(OMNISIGHT_AV_SYNC_WITH_NATIVE)
		last_error_ = "native V4L2/ALSA sync support is not enabled";
		return AVSyncStatus::kUnavailable;
#else
		bool expected = false;

		if (!running_.compare_exchange_strong(expected, true))
			return AVSyncStatus::kInvalidState;

		worker_ = std::thread(&Impl::run, this);
		return AVSyncStatus::kOk;
#endif
	}

	void stop()
	{
		running_.store(false);
		if (worker_.joinable())
			worker_.join();
	}

	AVSyncStatus setJitterBufferDepthMs(uint32_t depth_ms)
	{
		if (depth_ms < kMinJitterDepthMs || depth_ms > kMaxJitterDepthMs) {
			last_error_ = "jitter buffer depth is outside supported range";
			return AVSyncStatus::kInvalidArgument;
		}

		std::lock_guard<std::mutex> lock(mutex_);

		config_.jitter_buffer_depth_ms = depth_ms;
		snapshot_.jitter_buffer_depth_ms = depth_ms;
		return AVSyncStatus::kOk;
	}

	uint32_t jitterBufferDepthMs() const
	{
		std::lock_guard<std::mutex> lock(mutex_);

		return config_.jitter_buffer_depth_ms;
	}

	AVSyncSnapshot lastSnapshot() const
	{
		std::lock_guard<std::mutex> lock(mutex_);

		return snapshot_;
	}

	bool running() const
	{
		return running_.load();
	}

	bool available() const
	{
#if defined(OMNISIGHT_AV_SYNC_WITH_NATIVE)
		return true;
#else
		return false;
#endif
	}

	const std::string &lastError() const
	{
		return last_error_;
	}

private:
#if defined(OMNISIGHT_AV_SYNC_WITH_NATIVE)
	void run()
	{
		int video_fd = -1;
		snd_pcm_t *pcm = nullptr;

		video_fd = open_video();
		if (video_fd < 0)
			goto out;

		if (open_pcm(&pcm) != 0)
			goto out;

		while (running_.load()) {
			pollfd pfd {
				.fd = video_fd,
				.events = POLLIN,
				.revents = 0,
			};
			const int rc = poll(&pfd, 1, 20);

			if (rc < 0) {
				if (errno == EINTR)
					continue;
				set_io_error("poll", errno);
				break;
			}

			if (rc > 0 && (pfd.revents & POLLIN))
				sample_video_frame(video_fd);

			sample_audio_pcm(pcm);
		}

out:
		if (pcm)
			snd_pcm_close(pcm);
		if (video_fd >= 0)
			close(video_fd);
		running_.store(false);
	}

	int open_video()
	{
		const int fd = open(config_.video_device.c_str(), O_RDWR | O_NONBLOCK);

		if (fd < 0)
			set_io_error("open video device", errno);

		return fd;
	}

	int open_pcm(snd_pcm_t **pcm)
	{
		const int rc = snd_pcm_open(pcm, config_.audio_pcm.c_str(),
					    SND_PCM_STREAM_PLAYBACK, 0);

		if (rc < 0) {
			std::lock_guard<std::mutex> lock(mutex_);

			last_error_ = std::string("open ALSA PCM: ") +
				      snd_strerror(rc);
			return rc;
		}

		return 0;
	}

	void sample_video_frame(int video_fd)
	{
		v4l2_buffer buffer {};

		buffer.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
		buffer.memory = V4L2_MEMORY_MMAP;

		if (ioctl(video_fd, VIDIOC_DQBUF, &buffer) != 0) {
			if (errno != EAGAIN && errno != EINVAL)
				set_io_error("VIDIOC_DQBUF", errno);
			return;
		}

		update_video_timestamp(timeval_to_us(buffer.timestamp));

		if (ioctl(video_fd, VIDIOC_QBUF, &buffer) != 0 &&
		    errno != EINVAL && errno != EIO) {
			set_io_error("VIDIOC_QBUF", errno);
		}
	}

	void sample_audio_pcm(snd_pcm_t *pcm)
	{
		snd_pcm_sframes_t delay = 0;
		snd_pcm_uframes_t avail = 0;
		snd_pcm_state_t state;
		timespec ts {};
		int rc;

		state = snd_pcm_state(pcm);
		if (state == SND_PCM_STATE_XRUN) {
			std::lock_guard<std::mutex> lock(mutex_);

			snapshot_.underrun_count++;
		}

		rc = snd_pcm_htimestamp(pcm, &avail, &ts);
		if (rc < 0) {
			if (rc != -EPIPE) {
				std::lock_guard<std::mutex> lock(mutex_);

				last_error_ = std::string("snd_pcm_htimestamp: ") +
					      snd_strerror(rc);
			}
			return;
		}

		rc = snd_pcm_delay(pcm, &delay);
		if (rc < 0 && rc != -EPIPE) {
			std::lock_guard<std::mutex> lock(mutex_);

			last_error_ = std::string("snd_pcm_delay: ") +
				      snd_strerror(rc);
			return;
		}

		update_audio_timestamp(ts.tv_sec == 0 && ts.tv_nsec == 0 ?
					       monotonic_us() :
					       timespec_to_us(ts),
				       std::max<snd_pcm_sframes_t>(0, delay),
				       avail);
	}

	void update_video_timestamp(int64_t timestamp_us)
	{
		std::lock_guard<std::mutex> lock(mutex_);

		snapshot_.video_timestamp_us = timestamp_us;
		update_drift_locked();
	}

	void update_audio_timestamp(int64_t timestamp_us,
				    snd_pcm_sframes_t delay_frames,
				    snd_pcm_uframes_t avail_frames)
	{
		const int64_t played_frames =
			timestamp_us * config_.audio_sample_rate_hz / 1000000LL -
			delay_frames;
		std::lock_guard<std::mutex> lock(mutex_);

		(void)avail_frames;
		snapshot_.audio_timestamp_us = timestamp_us;
		snapshot_.audio_position_frames = std::max<int64_t>(0, played_frames);
		update_drift_locked();
	}

	void set_io_error(const char *operation, int error)
	{
		std::lock_guard<std::mutex> lock(mutex_);
		std::ostringstream stream;

		stream << operation << ": " << std::strerror(error);
		last_error_ = stream.str();
	}
#endif

	void update_drift_locked()
	{
		if (snapshot_.video_timestamp_us == 0 ||
		    snapshot_.audio_timestamp_us == 0)
			return;

		const int64_t jitter_us =
			static_cast<int64_t>(config_.jitter_buffer_depth_ms) *
			1000LL;

		snapshot_.drift_us = snapshot_.video_timestamp_us + jitter_us -
				     snapshot_.audio_timestamp_us;
		snapshot_.correction = correction_for_drift(
			snapshot_.drift_us, config_.drift_correction_threshold_us,
			config_.resample_threshold_us);
	}

	AVSyncDaemonConfig config_;
	mutable std::mutex mutex_;
	std::atomic<bool> running_ { false };
	std::thread worker_;
	AVSyncSnapshot snapshot_;
	std::string last_error_;
};

AVSyncDaemon::AVSyncDaemon(AVSyncDaemonConfig config)
	: impl_(std::make_unique<Impl>(std::move(config)))
{
}

AVSyncDaemon::~AVSyncDaemon() = default;

AVSyncDaemon::AVSyncDaemon(AVSyncDaemon &&) noexcept = default;

AVSyncDaemon &AVSyncDaemon::operator=(AVSyncDaemon &&) noexcept = default;

AVSyncStatus AVSyncDaemon::start()
{
	return impl_->start();
}

void AVSyncDaemon::stop()
{
	impl_->stop();
}

AVSyncStatus AVSyncDaemon::setJitterBufferDepthMs(uint32_t depth_ms)
{
	return impl_->setJitterBufferDepthMs(depth_ms);
}

uint32_t AVSyncDaemon::jitterBufferDepthMs() const
{
	return impl_->jitterBufferDepthMs();
}

AVSyncSnapshot AVSyncDaemon::lastSnapshot() const
{
	return impl_->lastSnapshot();
}

bool AVSyncDaemon::running() const
{
	return impl_->running();
}

bool AVSyncDaemon::available() const
{
	return impl_->available();
}

const std::string &AVSyncDaemon::lastError() const
{
	return impl_->lastError();
}

} // namespace omnisight::embedded::av_pair
