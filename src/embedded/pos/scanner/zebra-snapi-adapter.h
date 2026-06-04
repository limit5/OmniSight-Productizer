/* SPDX-License-Identifier: MIT
 *
 * Case 7 Zebra SNAPI barcode scanner adapter (OP-2034).
 */
#ifndef OMNISIGHT_EMBEDDED_POS_SCANNER_ZEBRA_SNAPI_ADAPTER_H_
#define OMNISIGHT_EMBEDDED_POS_SCANNER_ZEBRA_SNAPI_ADAPTER_H_

#include "barcode-scanner.h"

#include <memory>
#include <string>
#include <vector>

namespace omnisight::embedded::pos::scanner {

struct ZebraSnapiScannerConfig {
	std::string sdk_root;
	std::string sysfs_root = "/sys/bus/usb/devices";
	std::string device_selector;
	bool enable_code128 = true;
	bool enable_qr_code = true;
	bool auto_start_first_device = false;
};

class ZebraSnapiScanner final : public BarcodeScanner {
public:
	explicit ZebraSnapiScanner(ZebraSnapiScannerConfig config);
	~ZebraSnapiScanner() override;

	ZebraSnapiScanner(const ZebraSnapiScanner &) = delete;
	ZebraSnapiScanner &operator=(const ZebraSnapiScanner &) = delete;
	ZebraSnapiScanner(ZebraSnapiScanner &&) noexcept;
	ZebraSnapiScanner &operator=(ZebraSnapiScanner &&) noexcept;

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

#endif // OMNISIGHT_EMBEDDED_POS_SCANNER_ZEBRA_SNAPI_ADAPTER_H_
