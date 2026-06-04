/* SPDX-License-Identifier: MIT
 *
 * Case 7 POS MSR magstripe reader driver (OP-2055).
 */
#include "msr-magstripe-driver.h"

#include <algorithm>
#include <atomic>
#include <cctype>
#include <cstring>
#include <mutex>
#include <thread>
#include <utility>

#if defined(OMNISIGHT_MSR_WITH_POSIX_TRANSPORT)
#include <cerrno>
#include <fcntl.h>
#include <poll.h>
#include <termios.h>
#include <unistd.h>
#endif

namespace omnisight::embedded::pos::msr {
namespace {

constexpr size_t kMaxSwipeBytes = 512;
constexpr size_t kTrack2MinAccountBytes = 10;

bool valid_baud_rate(uint32_t baud_rate)
{
	return baud_rate == 1200 || baud_rate == 2400 || baud_rate == 4800 ||
	       baud_rate == 9600 || baud_rate == 19200 || baud_rate == 38400 ||
	       baud_rate == 57600 || baud_rate == 115200;
}

bool is_track_start(char ch)
{
	return ch == '%' || ch == ';' || ch == '+';
}

bool is_track_payload_char(char ch)
{
	const auto raw = static_cast<unsigned char>(ch);

	return raw >= 0x20 && raw <= 0x5f;
}

MsrTrackNumber track_number_for_start(char ch)
{
	if (ch == '%')
		return MsrTrackNumber::kTrack1;
	if (ch == '+')
		return MsrTrackNumber::kTrack3;
	return MsrTrackNumber::kTrack2;
}

uint8_t track_lrc(const std::string &track_without_lrc)
{
	uint8_t lrc = 0;

	for (char ch : track_without_lrc)
		lrc ^= static_cast<uint8_t>(ch & 0x3f);

	return static_cast<uint8_t>(lrc | 0x20);
}

bool parse_track2_account(const std::string &payload,
			  MsrTrack2Account *account)
{
	if (!account)
		return false;

	*account = {};

	std::string body = payload;
	if (!body.empty() && std::isalpha(static_cast<unsigned char>(body[0])))
		body.erase(body.begin());

	const size_t separator = body.find_first_of("=D");
	if (separator == std::string::npos || separator == 0)
		return false;

	const std::string pan = body.substr(0, separator);
	if (pan.size() < kTrack2MinAccountBytes ||
	    !std::all_of(pan.begin(), pan.end(), [](char ch) {
		    return std::isdigit(static_cast<unsigned char>(ch));
	    }))
		return false;

	const std::string remainder = body.substr(separator + 1);
	if (remainder.size() < 7)
		return false;

	const std::string expiry = remainder.substr(0, 4);
	const std::string service_code = remainder.substr(4, 3);
	if (!std::all_of(expiry.begin(), expiry.end(), [](char ch) {
		    return std::isdigit(static_cast<unsigned char>(ch));
	    }) ||
	    !std::all_of(service_code.begin(), service_code.end(), [](char ch) {
		    return std::isdigit(static_cast<unsigned char>(ch));
	    }))
		return false;

	account->pan = pan;
	account->expiry_yymm = expiry;
	account->service_code = service_code;
	account->discretionary_data = remainder.substr(7);
	account->valid = true;
	return true;
}

MsrStatus parse_swipe_frame(const std::string &raw, MsrTrackData *swipe,
			    std::string *last_error)
{
	if (!swipe)
		return MsrStatus::kInvalidArgument;

	MsrTrackData parsed;
	size_t cursor = 0;

	parsed.raw = raw;
	while (cursor < raw.size()) {
		while (cursor < raw.size() && !is_track_start(raw[cursor]))
			++cursor;
		if (cursor >= raw.size())
			break;

		const char start = raw[cursor];
		const size_t end = raw.find('?', cursor + 1);
		if (end == std::string::npos) {
			if (last_error)
				*last_error = "MSR track terminator is missing";
			return MsrStatus::kInvalidFrame;
		}

		for (size_t i = cursor + 1; i < end; ++i) {
			if (!is_track_payload_char(raw[i])) {
				if (last_error)
					*last_error = "MSR track contains invalid data";
				return MsrStatus::kInvalidFrame;
			}
		}

		MsrTrack track;
		track.number = track_number_for_start(start);
		track.raw = raw.substr(cursor, end - cursor + 1);
		track.payload = raw.substr(cursor + 1, end - cursor - 1);

		if (end + 1 < raw.size() && !is_track_start(raw[end + 1]) &&
		    raw[end + 1] != '\r' && raw[end + 1] != '\n') {
			track.lrc_present = true;
			track.raw.push_back(raw[end + 1]);
			track.lrc_valid = track_lrc(raw.substr(cursor,
							       end - cursor + 1)) ==
					  static_cast<uint8_t>(raw[end + 1]);
			cursor = end + 2;
		} else {
			cursor = end + 1;
		}

		if (track.number == MsrTrackNumber::kTrack2)
			(void)parse_track2_account(track.payload,
						   &parsed.track2_account);
		parsed.tracks.push_back(std::move(track));
	}

	if (parsed.tracks.empty()) {
		if (last_error)
			*last_error = "MSR swipe did not contain ISO 7811 tracks";
		return MsrStatus::kInvalidFrame;
	}

	*swipe = std::move(parsed);
	return MsrStatus::kOk;
}

char hid_usage_to_ascii(uint8_t usage, bool shifted)
{
	if (usage >= 0x04 && usage <= 0x1d) {
		char base = static_cast<char>('a' + usage - 0x04);

		return shifted ? static_cast<char>(std::toupper(base)) : base;
	}
	if (usage >= 0x1e && usage <= 0x26) {
		static constexpr char normal[] = "123456789";
		static constexpr char shifted_digits[] = "!@#$%^&*(";

		return shifted ? shifted_digits[usage - 0x1e] :
				 normal[usage - 0x1e];
	}
	if (usage == 0x27)
		return shifted ? ')' : '0';

	switch (usage) {
	case 0x28:
		return '\n';
	case 0x2c:
		return ' ';
	case 0x2d:
		return shifted ? '_' : '-';
	case 0x2e:
		return shifted ? '+' : '=';
	case 0x33:
		return shifted ? ':' : ';';
	case 0x34:
		return shifted ? '"' : '\'';
	case 0x36:
		return shifted ? '<' : ',';
	case 0x37:
		return shifted ? '>' : '.';
	case 0x38:
		return shifted ? '?' : '/';
	default:
		return '\0';
	}
}

std::string decode_hid_keyboard_report(const uint8_t *report, size_t size)
{
	std::string decoded;

	if (!report || size < 3)
		return decoded;

	const bool shifted = (report[0] & 0x22) != 0;
	for (size_t i = 2; i < size; ++i) {
		if (report[i] == 0)
			continue;
		const char ch = hid_usage_to_ascii(report[i], shifted);

		if (ch != '\0')
			decoded.push_back(ch);
	}

	return decoded;
}

#if defined(OMNISIGHT_MSR_WITH_POSIX_TRANSPORT)
speed_t termios_baud(uint32_t baud_rate)
{
	switch (baud_rate) {
	case 1200:
		return B1200;
	case 2400:
		return B2400;
	case 4800:
		return B4800;
	case 9600:
		return B9600;
	case 19200:
		return B19200;
	case 38400:
		return B38400;
	case 57600:
		return B57600;
	case 115200:
		return B115200;
	default:
		return B9600;
	}
}
#endif

} // namespace

class MsrDriver::Impl {
public:
	Impl() = default;
	explicit Impl(MsrDriverConfig config) : config_(std::move(config)) {}

	~Impl()
	{
		stop();
	}

	MsrStatus configure(MsrDriverConfig config)
	{
		if (config.hid_report_size == 0)
			return fail(MsrStatus::kInvalidArgument,
				    "MSR HID report size is required");
		if (config.transport == MsrTransportMode::kSerial &&
		    !valid_baud_rate(config.baud_rate))
			return fail(MsrStatus::kInvalidArgument,
				    "unsupported MSR serial baud rate");
		if (running_.load())
			return fail(MsrStatus::kInvalidState,
				    "cannot configure a running MSR driver");

		std::lock_guard<std::mutex> lock(mutex_);

		config_ = std::move(config);
		return MsrStatus::kOk;
	}

	void onSwipe(SwipeCallback callback)
	{
		std::lock_guard<std::mutex> lock(mutex_);

		callback_ = std::move(callback);
	}

	MsrStatus start()
	{
		if (config_.device_path.empty())
			return fail(MsrStatus::kInvalidArgument,
				    "MSR device path is required");
		if (config_.transport == MsrTransportMode::kSerial &&
		    !valid_baud_rate(config_.baud_rate))
			return fail(MsrStatus::kInvalidArgument,
				    "unsupported MSR serial baud rate");

#if !defined(OMNISIGHT_MSR_WITH_POSIX_TRANSPORT)
		return fail(MsrStatus::kUnavailable,
			    "MSR POSIX transport support is not enabled");
#else
		bool expected = false;

		if (!running_.compare_exchange_strong(expected, true))
			return MsrStatus::kOk;

		worker_ = std::thread(&Impl::run, this);
		return MsrStatus::kOk;
#endif
	}

	void stop()
	{
		running_.store(false);
		if (worker_.joinable())
			worker_.join();
	}

	bool running() const
	{
		return running_.load();
	}

	bool available() const
	{
#if defined(OMNISIGHT_MSR_WITH_POSIX_TRANSPORT)
		return true;
#else
		return false;
#endif
	}

	MsrStatus ingest(const uint8_t *data, size_t size)
	{
		if (!data && size != 0)
			return fail(MsrStatus::kInvalidArgument,
				    "MSR ingest data pointer is null");
		if (size == 0)
			return MsrStatus::kOk;

		std::string ascii;

		ascii.reserve(size);
		for (size_t i = 0; i < size; ++i) {
			const char ch = static_cast<char>(data[i]);

			if (ch == '\r' || ch == '\n' || is_track_start(ch) ||
			    ch == '?' || is_track_payload_char(ch))
				ascii.push_back(ch);
		}

		return ingest(ascii);
	}

	MsrStatus ingest(const std::string &data)
	{
		if (data.empty())
			return MsrStatus::kOk;

		std::vector<MsrTrackData> swipes;
		MsrStatus status = MsrStatus::kOk;

		{
			std::lock_guard<std::mutex> lock(mutex_);

			pending_.append(data);
			if (pending_.size() > kMaxSwipeBytes)
				pending_.erase(0, pending_.size() - kMaxSwipeBytes);
			status = drainPending(&swipes);
		}

		for (const auto &swipe : swipes)
			emit(swipe);

		return status;
	}

	MsrStatus ingestHidReport(const uint8_t *report, size_t size)
	{
		if (!report && size != 0)
			return fail(MsrStatus::kInvalidArgument,
				    "MSR HID report pointer is null");
		if (size == 0)
			return MsrStatus::kOk;

		const std::string decoded = decode_hid_keyboard_report(report, size);

		if (!decoded.empty())
			return ingest(decoded);
		return ingest(report, size);
	}

	MsrTrackData lastSwipe() const
	{
		std::lock_guard<std::mutex> lock(mutex_);

		return last_swipe_;
	}

	const std::string &lastError() const
	{
		return last_error_;
	}

private:
	MsrStatus drainPending(std::vector<MsrTrackData> *swipes)
	{
		while (true) {
			const size_t start = pending_.find_first_of("%;+");

			if (start == std::string::npos) {
				pending_.clear();
				return MsrStatus::kOk;
			}
			if (start != 0)
				pending_.erase(0, start);

			size_t cursor = 0;
			bool found_track = false;
			do {
				const size_t end = pending_.find('?', cursor + 1);

				if (end == std::string::npos)
					return MsrStatus::kOk;

				found_track = true;
				cursor = end + 1;
				if (cursor < pending_.size() &&
				    !is_track_start(pending_[cursor]) &&
				    pending_[cursor] != '\r' &&
				    pending_[cursor] != '\n')
					++cursor;
			} while (cursor < pending_.size() &&
				 is_track_start(pending_[cursor]));

			if (!found_track)
				return MsrStatus::kOk;

			const std::string frame = pending_.substr(0, cursor);
			MsrTrackData swipe;
			const MsrStatus status = parse_swipe_frame(frame, &swipe,
								   &last_error_);

			pending_.erase(0, cursor);
			if (status != MsrStatus::kOk)
				return status;

			last_swipe_ = swipe;
			last_error_.clear();
			if (swipes)
				swipes->push_back(std::move(swipe));
		}
	}

	void emit(const MsrTrackData &swipe)
	{
		SwipeCallback callback;

		{
			std::lock_guard<std::mutex> lock(mutex_);

			callback = callback_;
		}
		if (callback)
			callback(swipe);
	}

	MsrStatus fail(MsrStatus status, const std::string &message)
	{
		last_error_ = message;
		return status;
	}

#if defined(OMNISIGHT_MSR_WITH_POSIX_TRANSPORT)
	void run()
	{
		const int fd = ::open(config_.device_path.c_str(), O_RDONLY | O_CLOEXEC);

		if (fd < 0) {
			fail(MsrStatus::kIoError, std::strerror(errno));
			running_.store(false);
			return;
		}

		if (config_.transport == MsrTransportMode::kSerial &&
		    !configureSerial(fd)) {
			::close(fd);
			running_.store(false);
			return;
		}

		std::vector<uint8_t> buffer(std::max(config_.hid_report_size,
						    static_cast<size_t>(64)));

		while (running_.load()) {
			struct pollfd pfd = {
				fd,
				POLLIN,
				0,
			};
			const int rc = ::poll(&pfd, 1, 100);

			if (rc < 0) {
				if (errno == EINTR)
					continue;
				fail(MsrStatus::kIoError, std::strerror(errno));
				break;
			}
			if (rc == 0 || (pfd.revents & POLLIN) == 0)
				continue;

			const ssize_t got = ::read(fd, buffer.data(), buffer.size());

			if (got < 0) {
				if (errno == EINTR || errno == EAGAIN)
					continue;
				fail(MsrStatus::kIoError, std::strerror(errno));
				break;
			}
			if (got == 0)
				continue;

			if (config_.transport == MsrTransportMode::kUsbHid)
				(void)ingestHidReport(buffer.data(),
						       static_cast<size_t>(got));
			else
				(void)ingest(buffer.data(), static_cast<size_t>(got));
		}

		::close(fd);
		running_.store(false);
	}

	bool configureSerial(int fd)
	{
		struct termios tty {};

		if (::tcgetattr(fd, &tty) != 0) {
			fail(MsrStatus::kIoError, std::strerror(errno));
			return false;
		}

		cfmakeraw(&tty);
		cfsetispeed(&tty, termios_baud(config_.baud_rate));
		cfsetospeed(&tty, termios_baud(config_.baud_rate));
		tty.c_cflag |= static_cast<tcflag_t>(CLOCAL | CREAD);
		tty.c_cflag &= static_cast<tcflag_t>(~PARENB);
		tty.c_cflag &= static_cast<tcflag_t>(~CSTOPB);
		tty.c_cflag &= static_cast<tcflag_t>(~CSIZE);
		tty.c_cflag |= CS8;

		if (::tcsetattr(fd, TCSANOW, &tty) != 0) {
			fail(MsrStatus::kIoError, std::strerror(errno));
			return false;
		}
		return true;
	}
#endif

	MsrDriverConfig config_;
	SwipeCallback callback_;
	MsrTrackData last_swipe_;
	std::string pending_;
	mutable std::mutex mutex_;
	std::atomic<bool> running_ {false};
	std::thread worker_;
	std::string last_error_;
};

MsrDriver::MsrDriver() : impl_(std::make_unique<Impl>()) {}

MsrDriver::MsrDriver(MsrDriverConfig config)
	: impl_(std::make_unique<Impl>(std::move(config)))
{
}

MsrDriver::~MsrDriver() = default;
MsrDriver::MsrDriver(MsrDriver &&) noexcept = default;
MsrDriver &MsrDriver::operator=(MsrDriver &&) noexcept = default;

MsrStatus MsrDriver::configure(MsrDriverConfig config)
{
	return impl_->configure(std::move(config));
}

void MsrDriver::onSwipe(SwipeCallback callback)
{
	impl_->onSwipe(std::move(callback));
}

MsrStatus MsrDriver::start()
{
	return impl_->start();
}

void MsrDriver::stop()
{
	impl_->stop();
}

bool MsrDriver::running() const
{
	return impl_->running();
}

bool MsrDriver::available() const
{
	return impl_->available();
}

MsrStatus MsrDriver::ingest(const uint8_t *data, size_t size)
{
	return impl_->ingest(data, size);
}

MsrStatus MsrDriver::ingest(const std::string &data)
{
	return impl_->ingest(data);
}

MsrStatus MsrDriver::ingestHidReport(const uint8_t *report, size_t size)
{
	return impl_->ingestHidReport(report, size);
}

MsrTrackData MsrDriver::lastSwipe() const
{
	return impl_->lastSwipe();
}

const std::string &MsrDriver::lastError() const
{
	return impl_->lastError();
}

const char *toString(MsrStatus status)
{
	switch (status) {
	case MsrStatus::kOk:
		return "ok";
	case MsrStatus::kInvalidArgument:
		return "invalid-argument";
	case MsrStatus::kInvalidFrame:
		return "invalid-frame";
	case MsrStatus::kUnavailable:
		return "unavailable";
	case MsrStatus::kIoError:
		return "io-error";
	case MsrStatus::kInvalidState:
		return "invalid-state";
	}

	return "unknown";
}

const char *toString(MsrTransportMode mode)
{
	switch (mode) {
	case MsrTransportMode::kUsbHid:
		return "usb-hid";
	case MsrTransportMode::kSerial:
		return "serial";
	}

	return "unknown";
}

const char *toString(MsrTrackNumber number)
{
	switch (number) {
	case MsrTrackNumber::kTrack1:
		return "track-1";
	case MsrTrackNumber::kTrack2:
		return "track-2";
	case MsrTrackNumber::kTrack3:
		return "track-3";
	}

	return "unknown";
}

} // namespace omnisight::embedded::pos::msr

#if defined(OMNISIGHT_POS_MSR_MAGSTRIPE_DRIVER_SMOKE_MAIN)
#include <cstdlib>
#include <iostream>

namespace {

using omnisight::embedded::pos::msr::MsrDriver;
using omnisight::embedded::pos::msr::MsrStatus;
using omnisight::embedded::pos::msr::MsrTrackData;
using omnisight::embedded::pos::msr::MsrTrackNumber;

bool expect(bool value, const char *message)
{
	if (value)
		return true;

	std::cerr << message << '\n';
	return false;
}

bool expect_track(const MsrTrackData &swipe, MsrTrackNumber number,
		  const std::string &payload)
{
	const auto it = std::find_if(swipe.tracks.begin(), swipe.tracks.end(),
				    [number](const auto &track) {
					    return track.number == number;
				    });

	return it != swipe.tracks.end() && it->payload == payload;
}

} // namespace

int main()
{
	MsrDriver driver;
	MsrTrackData callback_swipe;
	bool callback_seen = false;

	driver.onSwipe([&callback_seen, &callback_swipe](const MsrTrackData &swipe) {
		callback_seen = true;
		callback_swipe = swipe;
	});

	const std::string swipe =
		"%B4111111111111111^CARDHOLDER/TEST^25121010000000000000?"
		";4111111111111111=25121010000000000000?"
		"+001234567890?";

	if (driver.ingest(swipe) != MsrStatus::kOk)
		return EXIT_FAILURE;
	if (!expect(callback_seen, "MSR swipe callback was not emitted"))
		return EXIT_FAILURE;
	if (!expect(callback_swipe.tracks.size() == 3,
		    "MSR swipe did not parse three tracks"))
		return EXIT_FAILURE;
	if (!expect(expect_track(callback_swipe, MsrTrackNumber::kTrack1,
				"B4111111111111111^CARDHOLDER/TEST^25121010000000000000"),
		    "MSR track 1 payload mismatch"))
		return EXIT_FAILURE;
	if (!expect(expect_track(callback_swipe, MsrTrackNumber::kTrack2,
				"4111111111111111=25121010000000000000"),
		    "MSR track 2 payload mismatch"))
		return EXIT_FAILURE;
	if (!expect(expect_track(callback_swipe, MsrTrackNumber::kTrack3,
				"001234567890"),
		    "MSR track 3 payload mismatch"))
		return EXIT_FAILURE;
	if (!expect(callback_swipe.track2_account.valid,
		    "MSR track 2 account fields were not parsed"))
		return EXIT_FAILURE;
	if (!expect(callback_swipe.track2_account.pan == "4111111111111111",
		    "MSR track 2 PAN mismatch"))
		return EXIT_FAILURE;
	if (!expect(callback_swipe.track2_account.expiry_yymm == "2512",
		    "MSR track 2 expiry mismatch"))
		return EXIT_FAILURE;
	if (!expect(callback_swipe.track2_account.service_code == "101",
		    "MSR track 2 service code mismatch"))
		return EXIT_FAILURE;

	callback_seen = false;
	if (driver.ingest("%B123?;4111111111111111=2512") != MsrStatus::kOk)
		return EXIT_FAILURE;
	if (callback_seen)
		return EXIT_FAILURE;
	if (driver.ingest("101000000000000?") != MsrStatus::kOk)
		return EXIT_FAILURE;
	if (!callback_seen)
		return EXIT_FAILURE;

	return EXIT_SUCCESS;
}
#endif
