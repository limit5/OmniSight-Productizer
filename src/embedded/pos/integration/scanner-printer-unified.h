/* SPDX-License-Identifier: MIT
 *
 * Case 7 scanner + receipt printer unified integration (OP-2058).
 */
#ifndef OMNISIGHT_EMBEDDED_POS_INTEGRATION_SCANNER_PRINTER_UNIFIED_H_
#define OMNISIGHT_EMBEDDED_POS_INTEGRATION_SCANNER_PRINTER_UNIFIED_H_

#include "../scanner/barcode-scanner.h"

#include <cstdint>
#include <functional>
#include <string>
#include <vector>

namespace omnisight::embedded::pos::integration {

enum class ScannerPrinterPackStatus {
	kOk = 0,
	kInvalidArgument,
	kInvalidState,
	kLookupMiss,
	kScannerError,
	kPrinterError,
};

struct ReceiptLine {
	std::string sku;
	std::string description;
	uint32_t quantity = 1;
	int64_t unit_price_minor = 0;
	std::string currency = "USD";
};

struct ReceiptDocument {
	std::string transaction_id;
	std::vector<ReceiptLine> lines;
};

class ReceiptPrinter {
public:
	virtual ~ReceiptPrinter() = default;

	virtual ScannerPrinterPackStatus printReceipt(
		const ReceiptDocument &receipt) = 0;
};

struct ScannerPrinterPackConfig {
	scanner::BarcodeScanner *scanner = nullptr;
	ReceiptPrinter *printer = nullptr;
	std::function<ScannerPrinterPackStatus(
		const scanner::BarcodeScanData &scan, ReceiptLine *line)>
		lookup_item;
	bool auto_append_lookup = true;
};

using UnifiedScanCallback =
	std::function<void(const scanner::BarcodeScanData &scan,
			   ScannerPrinterPackStatus status)>;

class ScannerPrinterPack {
public:
	explicit ScannerPrinterPack(ScannerPrinterPackConfig config);

	ScannerPrinterPack(const ScannerPrinterPack &) = delete;
	ScannerPrinterPack &operator=(const ScannerPrinterPack &) = delete;
	ScannerPrinterPack(ScannerPrinterPack &&) = delete;
	ScannerPrinterPack &operator=(ScannerPrinterPack &&) = delete;

	ScannerPrinterPackStatus discover();
	ScannerPrinterPackStatus start(
		const scanner::BarcodeScannerDevice &device);
	ScannerPrinterPackStatus stop();
	void onScan(UnifiedScanCallback callback);
	ScannerPrinterPackStatus printReceipt();
	ScannerPrinterPackStatus clearReceipt();

	const ReceiptDocument &pendingReceipt() const;
	ScannerPrinterPackStatus lastStatus() const;
	const std::string &lastError() const;

private:
	ScannerPrinterPackStatus fail(ScannerPrinterPackStatus status,
				      const std::string &error);
	ScannerPrinterPackStatus mapScannerStatus(
		scanner::BarcodeScannerStatus status, const char *operation);
	void handleDecoded(const scanner::BarcodeScanData &scan);

	ScannerPrinterPackConfig config_;
	UnifiedScanCallback scan_callback_;
	ReceiptDocument pending_receipt_;
	ScannerPrinterPackStatus last_status_ = ScannerPrinterPackStatus::kOk;
	std::string last_error_;
	bool started_ = false;
};

const char *toString(ScannerPrinterPackStatus status);

} // namespace omnisight::embedded::pos::integration

#endif // OMNISIGHT_EMBEDDED_POS_INTEGRATION_SCANNER_PRINTER_UNIFIED_H_
