/* SPDX-License-Identifier: MIT
 *
 * Case 7 POS barcode scanner interface (OP-2034).
 */
#ifndef OMNISIGHT_EMBEDDED_POS_SCANNER_BARCODE_SCANNER_H_
#define OMNISIGHT_EMBEDDED_POS_SCANNER_BARCODE_SCANNER_H_

#include <cstdint>
#include <functional>
#include <string>
#include <vector>

namespace omnisight::embedded::pos::scanner {

enum class BarcodeScannerStatus {
	kOk = 0,
	kInvalidArgument,
	kUnavailable,
	kInvalidState,
	kBackendError,
};

enum class BarcodeSymbology {
	kUnknown = 0,
	kCode128,
	kCode39,
	kEan13,
	kEan8,
	kUpcA,
	kQrCode,
	kDataMatrix,
	kPdf417,
};

enum class BarcodeScannerEventType {
	kDeviceDiscovered = 0,
	kCaptureStarted,
	kCaptureStopped,
	kDataDecoded,
	kError,
};

struct BarcodeScannerDevice {
	std::string device_id;
	std::string model;
	std::string serial_number;
	std::string transport;
	uint16_t vendor_id = 0;
	uint16_t product_id = 0;
	void *vendor_specific_ptr = nullptr;

	explicit operator bool() const
	{
		return !device_id.empty() || vendor_specific_ptr != nullptr;
	}
};

struct BarcodeScanData {
	std::string text;
	std::vector<uint8_t> raw;
	BarcodeSymbology symbology = BarcodeSymbology::kUnknown;
	BarcodeScannerDevice device;
	void *vendor_specific_ptr = nullptr;
};

struct BarcodeScannerEvent {
	BarcodeScannerEventType type = BarcodeScannerEventType::kError;
	BarcodeScannerStatus status = BarcodeScannerStatus::kOk;
	BarcodeScannerDevice device;
	BarcodeScanData scan;
	std::string detail;
	void *vendor_specific_ptr = nullptr;
};

using BarcodeScannerEventCallback =
	std::function<void(const BarcodeScannerEvent &event)>;
using BarcodeDecodedCallback =
	std::function<void(const BarcodeScanData &scan)>;

class BarcodeScanner {
public:
	virtual ~BarcodeScanner() = default;

	virtual BarcodeScannerStatus discover() = 0;
	virtual BarcodeScannerStatus startCapture(
		const BarcodeScannerDevice &device) = 0;
	virtual BarcodeScannerStatus stopCapture() = 0;
	virtual void onEvent(BarcodeScannerEventCallback callback) = 0;
	virtual void onDecoded(BarcodeDecodedCallback callback) = 0;
};

const char *toString(BarcodeScannerStatus status);
const char *toString(BarcodeSymbology symbology);
const char *toString(BarcodeScannerEventType type);

} // namespace omnisight::embedded::pos::scanner

#endif // OMNISIGHT_EMBEDDED_POS_SCANNER_BARCODE_SCANNER_H_
