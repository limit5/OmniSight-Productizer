/* SPDX-License-Identifier: MIT
 *
 * Case 7 Zebra SNAPI barcode scanner adapter (OP-2034).
 */
#include "zebra-snapi-adapter.h"

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <memory>
#include <sstream>
#include <string>
#include <utility>
#include <vector>

namespace omnisight::embedded::pos::scanner {
namespace {

constexpr uint16_t kZebraVendorId = 0x05e0;

bool read_text_file(const std::filesystem::path &path, std::string *value)
{
	if (!value)
		return false;

	std::ifstream input(path);

	if (!input)
		return false;

	std::getline(input, *value);
	return true;
}

bool parse_hex_u16(const std::string &value, uint16_t *parsed)
{
	unsigned int tmp = 0;
	std::istringstream stream(value);

	if (!parsed)
		return false;
	stream >> std::hex >> tmp;
	if (!stream || tmp > 0xffff)
		return false;

	*parsed = static_cast<uint16_t>(tmp);
	return true;
}

std::string read_optional_text(const std::filesystem::path &path)
{
	std::string value;

	(void)read_text_file(path, &value);
	return value;
}

BarcodeScannerStatus invalid_argument(std::string *last_error,
				      const char *message)
{
	if (last_error)
		*last_error = message;
	return BarcodeScannerStatus::kInvalidArgument;
}

BarcodeScannerStatus invalid_state(std::string *last_error,
				  const char *message)
{
	if (last_error)
		*last_error = message;
	return BarcodeScannerStatus::kInvalidState;
}

#if defined(OMNISIGHT_ZEBRA_SNAPI_WITH_SDK)
BarcodeSymbology map_snapi_symbology(uint32_t symbology)
{
	switch (symbology) {
	case 1:
		return BarcodeSymbology::kCode128;
	case 2:
		return BarcodeSymbology::kCode39;
	case 3:
		return BarcodeSymbology::kEan13;
	case 4:
		return BarcodeSymbology::kEan8;
	case 5:
		return BarcodeSymbology::kUpcA;
	case 6:
		return BarcodeSymbology::kQrCode;
	case 7:
		return BarcodeSymbology::kDataMatrix;
	case 8:
		return BarcodeSymbology::kPdf417;
	default:
		return BarcodeSymbology::kUnknown;
	}
}
#endif

} // namespace

class ZebraSnapiScanner::Impl {
public:
	explicit Impl(ZebraSnapiScannerConfig config)
		: config_(std::move(config))
	{
	}

	~Impl()
	{
		(void)stopCapture();
	}

	BarcodeScannerStatus discover()
	{
		devices_.clear();

		if (config_.sysfs_root.empty()) {
			last_error_ = "Zebra SNAPI sysfs root is required";
			emit(BarcodeScannerEventType::kError, {},
			     BarcodeScannerStatus::kInvalidArgument, last_error_);
			return BarcodeScannerStatus::kInvalidArgument;
		}

		std::error_code error;
		const std::filesystem::path root(config_.sysfs_root);

		if (!std::filesystem::exists(root, error)) {
			last_error_ = "Zebra SNAPI sysfs root is unavailable";
			emit(BarcodeScannerEventType::kError, {},
			     BarcodeScannerStatus::kUnavailable, last_error_);
			return BarcodeScannerStatus::kUnavailable;
		}

		for (const auto &entry :
		     std::filesystem::directory_iterator(root, error)) {
			if (error)
				break;
			probeSysfsDevice(entry.path());
		}

		if (error) {
			last_error_ = "Zebra SNAPI sysfs discovery failed";
			emit(BarcodeScannerEventType::kError, {},
			     BarcodeScannerStatus::kBackendError, last_error_);
			return BarcodeScannerStatus::kBackendError;
		}

		last_error_.clear();

		if (config_.auto_start_first_device && !devices_.empty())
			return startCapture(devices_.front());
		return BarcodeScannerStatus::kOk;
	}

	BarcodeScannerStatus startCapture(const BarcodeScannerDevice &device)
	{
		if (!device)
			return invalid_argument(&last_error_,
						"Zebra SNAPI device is empty");
		if (capturing_)
			return invalid_state(&last_error_,
					     "Zebra SNAPI capture is already active");
		if (device.vendor_id != 0 && device.vendor_id != kZebraVendorId)
			return invalid_argument(&last_error_,
						"device is not a Zebra scanner");

#if !defined(OMNISIGHT_ZEBRA_SNAPI_WITH_SDK)
		(void)device;
		last_error_ = "Zebra SNAPI SDK support is not enabled";
		emit(BarcodeScannerEventType::kError, {},
		     BarcodeScannerStatus::kUnavailable, last_error_);
		return BarcodeScannerStatus::kUnavailable;
#else
		active_device_ = device;
		capturing_ = true;
		last_error_.clear();
		emit(BarcodeScannerEventType::kCaptureStarted, active_device_,
		     BarcodeScannerStatus::kOk, "Zebra SNAPI capture started");
		return BarcodeScannerStatus::kOk;
#endif
	}

	BarcodeScannerStatus stopCapture()
	{
		if (!capturing_)
			return BarcodeScannerStatus::kOk;

		BarcodeScannerDevice stopped = active_device_;

		capturing_ = false;
		active_device_ = {};
		last_error_.clear();
		emit(BarcodeScannerEventType::kCaptureStopped, stopped,
		     BarcodeScannerStatus::kOk, "Zebra SNAPI capture stopped");
		return BarcodeScannerStatus::kOk;
	}

	void onEvent(BarcodeScannerEventCallback callback)
	{
		event_callback_ = std::move(callback);
	}

	void onDecoded(BarcodeDecodedCallback callback)
	{
		decoded_callback_ = std::move(callback);
	}

	bool available() const
	{
#if defined(OMNISIGHT_ZEBRA_SNAPI_WITH_SDK)
		return true;
#else
		return false;
#endif
	}

	bool capturing() const
	{
		return capturing_;
	}

	const std::vector<BarcodeScannerDevice> &devices() const
	{
		return devices_;
	}

	const BarcodeScannerDevice &activeDevice() const
	{
		return active_device_;
	}

	const std::string &lastError() const
	{
		return last_error_;
	}

#if defined(OMNISIGHT_ZEBRA_SNAPI_WITH_SDK)
	void handleDecodedData(const char *text, const uint8_t *raw,
			       std::size_t raw_len, uint32_t symbology,
			       void *vendor_specific_ptr)
	{
		if (!capturing_)
			return;

		BarcodeScanData scan;

		scan.text = text ? text : "";
		if (raw && raw_len > 0)
			scan.raw.assign(raw, raw + raw_len);
		scan.symbology = map_snapi_symbology(symbology);
		scan.device = active_device_;
		scan.vendor_specific_ptr = vendor_specific_ptr;

		if (decoded_callback_)
			decoded_callback_(scan);
		emit(BarcodeScannerEventType::kDataDecoded, active_device_,
		     BarcodeScannerStatus::kOk, "Zebra SNAPI barcode decoded",
		     scan, vendor_specific_ptr);
	}
#endif

private:
	void probeSysfsDevice(const std::filesystem::path &path)
	{
		uint16_t vendor_id = 0;
		uint16_t product_id = 0;
		std::string encoded_vendor;
		std::string encoded_product;

		if (!read_text_file(path / "idVendor", &encoded_vendor) ||
		    !parse_hex_u16(encoded_vendor, &vendor_id) ||
		    vendor_id != kZebraVendorId)
			return;
		if (!read_text_file(path / "idProduct", &encoded_product) ||
		    !parse_hex_u16(encoded_product, &product_id))
			product_id = 0;

		BarcodeScannerDevice device = {
			path.filename().string(),
			read_optional_text(path / "product"),
			read_optional_text(path / "serial"),
			"usb-snapi",
			vendor_id,
			product_id,
			nullptr,
		};

		if (device.model.empty())
			device.model = "Zebra SNAPI scanner";
		if (!config_.device_selector.empty() &&
		    device.device_id != config_.device_selector &&
		    device.serial_number != config_.device_selector)
			return;

		devices_.push_back(device);
		emit(BarcodeScannerEventType::kDeviceDiscovered, device,
		     BarcodeScannerStatus::kOk, "Zebra SNAPI scanner discovered");
	}

	void emit(BarcodeScannerEventType type,
		  const BarcodeScannerDevice &device,
		  BarcodeScannerStatus status, const std::string &detail)
	{
		emit(type, device, status, detail, {}, device.vendor_specific_ptr);
	}

	void emit(BarcodeScannerEventType type,
		  const BarcodeScannerDevice &device,
		  BarcodeScannerStatus status, const std::string &detail,
		  const BarcodeScanData &scan, void *vendor_specific_ptr)
	{
		if (!event_callback_)
			return;

		event_callback_({
			type,
			status,
			device,
			scan,
			detail,
			vendor_specific_ptr,
		});
	}

	ZebraSnapiScannerConfig config_;
	BarcodeScannerEventCallback event_callback_;
	BarcodeDecodedCallback decoded_callback_;
	std::vector<BarcodeScannerDevice> devices_;
	BarcodeScannerDevice active_device_;
	std::string last_error_;
	bool capturing_ = false;
};

ZebraSnapiScanner::ZebraSnapiScanner(ZebraSnapiScannerConfig config)
	: impl_(std::make_unique<Impl>(std::move(config)))
{
}

ZebraSnapiScanner::~ZebraSnapiScanner() = default;

ZebraSnapiScanner::ZebraSnapiScanner(ZebraSnapiScanner &&) noexcept = default;

ZebraSnapiScanner &ZebraSnapiScanner::operator=(
	ZebraSnapiScanner &&) noexcept = default;

BarcodeScannerStatus ZebraSnapiScanner::discover()
{
	return impl_->discover();
}

BarcodeScannerStatus ZebraSnapiScanner::startCapture(
	const BarcodeScannerDevice &device)
{
	return impl_->startCapture(device);
}

BarcodeScannerStatus ZebraSnapiScanner::stopCapture()
{
	return impl_->stopCapture();
}

void ZebraSnapiScanner::onEvent(BarcodeScannerEventCallback callback)
{
	impl_->onEvent(std::move(callback));
}

void ZebraSnapiScanner::onDecoded(BarcodeDecodedCallback callback)
{
	impl_->onDecoded(std::move(callback));
}

bool ZebraSnapiScanner::available() const
{
	return impl_->available();
}

bool ZebraSnapiScanner::capturing() const
{
	return impl_->capturing();
}

const std::vector<BarcodeScannerDevice> &ZebraSnapiScanner::devices() const
{
	return impl_->devices();
}

const BarcodeScannerDevice &ZebraSnapiScanner::activeDevice() const
{
	return impl_->activeDevice();
}

const std::string &ZebraSnapiScanner::lastError() const
{
	return impl_->lastError();
}

const char *toString(BarcodeScannerStatus status)
{
	switch (status) {
	case BarcodeScannerStatus::kOk:
		return "ok";
	case BarcodeScannerStatus::kInvalidArgument:
		return "invalid-argument";
	case BarcodeScannerStatus::kUnavailable:
		return "unavailable";
	case BarcodeScannerStatus::kInvalidState:
		return "invalid-state";
	case BarcodeScannerStatus::kBackendError:
		return "backend-error";
	}

	return "unknown";
}

const char *toString(BarcodeSymbology symbology)
{
	switch (symbology) {
	case BarcodeSymbology::kUnknown:
		return "unknown";
	case BarcodeSymbology::kCode128:
		return "code128";
	case BarcodeSymbology::kCode39:
		return "code39";
	case BarcodeSymbology::kEan13:
		return "ean13";
	case BarcodeSymbology::kEan8:
		return "ean8";
	case BarcodeSymbology::kUpcA:
		return "upc-a";
	case BarcodeSymbology::kQrCode:
		return "qr-code";
	case BarcodeSymbology::kDataMatrix:
		return "data-matrix";
	case BarcodeSymbology::kPdf417:
		return "pdf417";
	}

	return "unknown";
}

const char *toString(BarcodeScannerEventType type)
{
	switch (type) {
	case BarcodeScannerEventType::kDeviceDiscovered:
		return "device-discovered";
	case BarcodeScannerEventType::kCaptureStarted:
		return "capture-started";
	case BarcodeScannerEventType::kCaptureStopped:
		return "capture-stopped";
	case BarcodeScannerEventType::kDataDecoded:
		return "data-decoded";
	case BarcodeScannerEventType::kError:
		return "error";
	}

	return "unknown";
}

} // namespace omnisight::embedded::pos::scanner

#if defined(OMNISIGHT_ZEBRA_SNAPI_ADAPTER_SMOKE_MAIN)
#include <iostream>
#include <vector>

int main()
{
	using namespace omnisight::embedded::pos::scanner;

	ZebraSnapiScanner adapter({
		"",
		"/sys/bus/usb/devices",
		"",
		true,
		true,
		false,
	});
	std::vector<BarcodeScannerEventType> events;

	adapter.onEvent([&events](const BarcodeScannerEvent &event) {
		events.push_back(event.type);
	});

	BarcodeScannerDevice wrong_vendor = {
		"other",
		"Not Zebra",
		"",
		"usb",
		0xffff,
		0,
		nullptr,
	};
	if (adapter.startCapture(wrong_vendor) !=
	    BarcodeScannerStatus::kInvalidArgument) {
		std::cerr << "Zebra SNAPI adapter accepted a non-Zebra device\n";
		return 1;
	}

	const BarcodeScannerStatus status = adapter.discover();
	if (status != BarcodeScannerStatus::kOk &&
	    status != BarcodeScannerStatus::kUnavailable) {
		std::cerr << "Zebra SNAPI discovery returned "
			  << toString(status) << ": " << adapter.lastError()
			  << '\n';
		return 1;
	}
	if (!adapter.available()) {
		if (adapter.capturing()) {
			std::cerr << "Zebra SNAPI adapter captured without SDK\n";
			return 1;
		}
		std::cout << "Zebra SNAPI adapter smoke skipped: "
			  << adapter.lastError() << '\n';
		return 0;
	}
	if (status != BarcodeScannerStatus::kOk) {
		std::cerr << "Zebra SNAPI discovery failed: "
			  << adapter.lastError() << '\n';
		return 1;
	}

	return 0;
}
#endif
