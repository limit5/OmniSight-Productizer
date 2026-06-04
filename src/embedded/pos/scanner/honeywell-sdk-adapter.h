/* SPDX-License-Identifier: MIT
 *
 * Case 7 Honeywell barcode scanner SDK adapter (OP-2045).
 */
#ifndef OMNISIGHT_EMBEDDED_POS_SCANNER_HONEYWELL_SDK_ADAPTER_H_
#define OMNISIGHT_EMBEDDED_POS_SCANNER_HONEYWELL_SDK_ADAPTER_H_

#include "barcode-scanner.h"

#include <memory>
#include <string>
#include <vector>

namespace omnisight::embedded::pos::scanner {

struct HoneywellSdkScannerConfig {
	std::string sdk_root;
	std::string sysfs_root = "/sys/bus/usb/devices";
	std::string device_selector;
	bool enable_code128 = true;
	bool enable_qr_code = true;
	bool auto_start_first_device = false;
};

class HoneywellSdkScanner final : public BarcodeScanner {
public:
	explicit HoneywellSdkScanner(HoneywellSdkScannerConfig config);
	~HoneywellSdkScanner() override;

	HoneywellSdkScanner(const HoneywellSdkScanner &) = delete;
	HoneywellSdkScanner &operator=(const HoneywellSdkScanner &) = delete;
	HoneywellSdkScanner(HoneywellSdkScanner &&) noexcept;
	HoneywellSdkScanner &operator=(HoneywellSdkScanner &&) noexcept;

	BarcodeScannerStatus discover() override;
	BarcodeScannerStatus startCapture(
		const BarcodeScannerDevice &device) override;
	BarcodeScannerStatus stopCapture() override;
	void onEvent(BarcodeScannerEventCallback callback) override;
	void onDecoded(BarcodeDecodedCallback callback) override;

	bool available() const;
	bool capturing() const;
	const std::vector<BarcodeScannerDevice> &devices() const;
	const BarcodeScannerDevice &activeDevice() const;
	const std::string &lastError() const;

private:
	class Impl;

	std::unique_ptr<Impl> impl_;
};

} // namespace omnisight::embedded::pos::scanner

#endif // OMNISIGHT_EMBEDDED_POS_SCANNER_HONEYWELL_SDK_ADAPTER_H_
