/* SPDX-License-Identifier: MIT
 *
 * Case 7 POS MSR magstripe reader driver (OP-2055).
 */
#ifndef OMNISIGHT_EMBEDDED_POS_MSR_MAGSTRIPE_DRIVER_H_
#define OMNISIGHT_EMBEDDED_POS_MSR_MAGSTRIPE_DRIVER_H_

#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <vector>

namespace omnisight::embedded::pos::msr {

enum class MsrStatus {
	kOk = 0,
	kInvalidArgument,
	kInvalidFrame,
	kUnavailable,
	kIoError,
	kInvalidState,
};

enum class MsrTransportMode {
	kUsbHid = 0,
	kSerial,
};

enum class MsrTrackNumber {
	kTrack1 = 1,
	kTrack2 = 2,
	kTrack3 = 3,
};

struct MsrTrack {
	MsrTrackNumber number = MsrTrackNumber::kTrack1;
	std::string raw;
	std::string payload;
	bool lrc_present = false;
	bool lrc_valid = false;
};

struct MsrTrack2Account {
	std::string pan;
	std::string expiry_yymm;
	std::string service_code;
	std::string discretionary_data;
	bool valid = false;
};

struct MsrTrackData {
	std::string raw;
	std::vector<MsrTrack> tracks;
	MsrTrack2Account track2_account;

	bool empty() const
	{
		return tracks.empty();
	}
};

struct MsrDriverConfig {
	MsrTransportMode transport = MsrTransportMode::kUsbHid;
	std::string device_path;
	uint32_t baud_rate = 9600;
	size_t hid_report_size = 8;
};

class MsrDriver {
public:
	using SwipeCallback = std::function<void(const MsrTrackData &)>;

	MsrDriver();
	explicit MsrDriver(MsrDriverConfig config);
	~MsrDriver();

	MsrDriver(const MsrDriver &) = delete;
	MsrDriver &operator=(const MsrDriver &) = delete;
	MsrDriver(MsrDriver &&) noexcept;
	MsrDriver &operator=(MsrDriver &&) noexcept;

	MsrStatus configure(MsrDriverConfig config);
	void onSwipe(SwipeCallback callback);

	MsrStatus start();
	void stop();
	bool running() const;
	bool available() const;

	MsrStatus ingest(const uint8_t *data, size_t size);
	MsrStatus ingest(const std::string &data);
	MsrStatus ingestHidReport(const uint8_t *report, size_t size);

	MsrTrackData lastSwipe() const;
	const std::string &lastError() const;

private:
	class Impl;

	std::unique_ptr<Impl> impl_;
};

const char *toString(MsrStatus status);
const char *toString(MsrTransportMode mode);
const char *toString(MsrTrackNumber number);

} // namespace omnisight::embedded::pos::msr

#endif // OMNISIGHT_EMBEDDED_POS_MSR_MAGSTRIPE_DRIVER_H_
