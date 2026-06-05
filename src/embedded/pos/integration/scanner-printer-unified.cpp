/* SPDX-License-Identifier: MIT
 *
 * Case 7 scanner + receipt printer unified integration (OP-2058).
 */
#include "scanner-printer-unified.h"

#include <cstdlib>
#include <iostream>
#include <utility>

namespace omnisight::embedded::pos::integration {

namespace {

const char *scanner_status_name(scanner::BarcodeScannerStatus status)
{
	switch (status) {
	case scanner::BarcodeScannerStatus::kOk:
		return "ok";
	case scanner::BarcodeScannerStatus::kInvalidArgument:
		return "invalid-argument";
	case scanner::BarcodeScannerStatus::kUnavailable:
		return "unavailable";
	case scanner::BarcodeScannerStatus::kInvalidState:
		return "invalid-state";
	case scanner::BarcodeScannerStatus::kBackendError:
		return "backend-error";
	}

	return "unknown";
}

} // namespace

ScannerPrinterPack::ScannerPrinterPack(ScannerPrinterPackConfig config)
	: config_(std::move(config))
{
	if (!config_.scanner)
		fail(ScannerPrinterPackStatus::kInvalidArgument,
		     "scanner is required");
	if (!config_.printer)
		fail(ScannerPrinterPackStatus::kInvalidArgument,
		     "printer is required");
}

ScannerPrinterPackStatus ScannerPrinterPack::discover()
{
	if (!config_.scanner)
		return fail(ScannerPrinterPackStatus::kInvalidArgument,
			    "scanner is required");

	return mapScannerStatus(config_.scanner->discover(), "discover");
}

ScannerPrinterPackStatus ScannerPrinterPack::start(
	const scanner::BarcodeScannerDevice &device)
{
	if (!config_.scanner)
		return fail(ScannerPrinterPackStatus::kInvalidArgument,
			    "scanner is required");
	if (!config_.printer)
		return fail(ScannerPrinterPackStatus::kInvalidArgument,
			    "printer is required");
	if (started_)
		return fail(ScannerPrinterPackStatus::kInvalidState,
			    "scanner capture already started");

	config_.scanner->onDecoded([this](const scanner::BarcodeScanData &scan) {
		handleDecoded(scan);
	});

	const ScannerPrinterPackStatus status =
		mapScannerStatus(config_.scanner->startCapture(device),
				 "start capture");
	if (status == ScannerPrinterPackStatus::kOk)
		started_ = true;
	return status;
}

ScannerPrinterPackStatus ScannerPrinterPack::stop()
{
	if (!config_.scanner)
		return fail(ScannerPrinterPackStatus::kInvalidArgument,
			    "scanner is required");
	if (!started_)
		return fail(ScannerPrinterPackStatus::kInvalidState,
			    "scanner capture is not started");

	const ScannerPrinterPackStatus status =
		mapScannerStatus(config_.scanner->stopCapture(), "stop capture");
	if (status == ScannerPrinterPackStatus::kOk)
		started_ = false;
	return status;
}

void ScannerPrinterPack::onScan(UnifiedScanCallback callback)
{
	scan_callback_ = std::move(callback);
}

ScannerPrinterPackStatus ScannerPrinterPack::printReceipt()
{
	if (!config_.printer)
		return fail(ScannerPrinterPackStatus::kInvalidArgument,
			    "printer is required");
	if (pending_receipt_.lines.empty())
		return fail(ScannerPrinterPackStatus::kInvalidState,
			    "receipt has no lines");

	const ScannerPrinterPackStatus status =
		config_.printer->printReceipt(pending_receipt_);
	if (status != ScannerPrinterPackStatus::kOk)
		return fail(ScannerPrinterPackStatus::kPrinterError,
			    "receipt printer rejected document");

	last_error_.clear();
	last_status_ = ScannerPrinterPackStatus::kOk;
	return last_status_;
}

ScannerPrinterPackStatus ScannerPrinterPack::clearReceipt()
{
	pending_receipt_ = {};
	last_error_.clear();
	last_status_ = ScannerPrinterPackStatus::kOk;
	return last_status_;
}

const ReceiptDocument &ScannerPrinterPack::pendingReceipt() const
{
	return pending_receipt_;
}

ScannerPrinterPackStatus ScannerPrinterPack::lastStatus() const
{
	return last_status_;
}

const std::string &ScannerPrinterPack::lastError() const
{
	return last_error_;
}

ScannerPrinterPackStatus ScannerPrinterPack::fail(
	ScannerPrinterPackStatus status, const std::string &error)
{
	last_status_ = status;
	last_error_ = error;
	return status;
}

ScannerPrinterPackStatus ScannerPrinterPack::mapScannerStatus(
	scanner::BarcodeScannerStatus status, const char *operation)
{
	if (status == scanner::BarcodeScannerStatus::kOk) {
		last_error_.clear();
		last_status_ = ScannerPrinterPackStatus::kOk;
		return last_status_;
	}

	return fail(ScannerPrinterPackStatus::kScannerError,
		    std::string(operation) + " failed: " +
			    scanner_status_name(status));
}

void ScannerPrinterPack::handleDecoded(const scanner::BarcodeScanData &scan)
{
	ScannerPrinterPackStatus status = ScannerPrinterPackStatus::kOk;

	if (config_.auto_append_lookup) {
		if (!config_.lookup_item) {
			status = fail(ScannerPrinterPackStatus::kInvalidArgument,
				      "item lookup callback is required");
		} else {
			ReceiptLine line;
			status = config_.lookup_item(scan, &line);
			if (status == ScannerPrinterPackStatus::kOk) {
				pending_receipt_.lines.push_back(std::move(line));
				last_error_.clear();
				last_status_ = status;
			} else if (status == ScannerPrinterPackStatus::kLookupMiss) {
				fail(status, "item lookup missed barcode");
			} else {
				fail(status, "item lookup failed");
			}
		}
	}

	if (scan_callback_)
		scan_callback_(scan, status);
}

const char *toString(ScannerPrinterPackStatus status)
{
	switch (status) {
	case ScannerPrinterPackStatus::kOk:
		return "ok";
	case ScannerPrinterPackStatus::kInvalidArgument:
		return "invalid-argument";
	case ScannerPrinterPackStatus::kInvalidState:
		return "invalid-state";
	case ScannerPrinterPackStatus::kLookupMiss:
		return "lookup-miss";
	case ScannerPrinterPackStatus::kScannerError:
		return "scanner-error";
	case ScannerPrinterPackStatus::kPrinterError:
		return "printer-error";
	}

	return "unknown";
}

} // namespace omnisight::embedded::pos::integration

#if defined(OMNISIGHT_POS_SCANNER_PRINTER_UNIFIED_SMOKE_MAIN)
namespace {

class SmokeScanner final :
	public omnisight::embedded::pos::scanner::BarcodeScanner {
public:
	omnisight::embedded::pos::scanner::BarcodeScannerStatus discover() override
	{
		return omnisight::embedded::pos::scanner::BarcodeScannerStatus::kOk;
	}

	omnisight::embedded::pos::scanner::BarcodeScannerStatus startCapture(
		const omnisight::embedded::pos::scanner::BarcodeScannerDevice &device)
		override
	{
		active_device_ = device;
		capturing_ = true;
		return omnisight::embedded::pos::scanner::BarcodeScannerStatus::kOk;
	}

	omnisight::embedded::pos::scanner::BarcodeScannerStatus stopCapture()
		override
	{
		capturing_ = false;
		return omnisight::embedded::pos::scanner::BarcodeScannerStatus::kOk;
	}

	void onEvent(
		omnisight::embedded::pos::scanner::BarcodeScannerEventCallback
			callback) override
	{
		event_callback_ = std::move(callback);
	}

	void onDecoded(
		omnisight::embedded::pos::scanner::BarcodeDecodedCallback callback)
		override
	{
		decoded_callback_ = std::move(callback);
	}

	void emit(const std::string &text)
	{
		omnisight::embedded::pos::scanner::BarcodeScanData scan;
		scan.text = text;
		scan.symbology =
			omnisight::embedded::pos::scanner::BarcodeSymbology::kCode128;
		scan.device = active_device_;
		if (decoded_callback_)
			decoded_callback_(scan);
	}

	bool capturing() const
	{
		return capturing_;
	}

private:
	omnisight::embedded::pos::scanner::BarcodeDecodedCallback
		decoded_callback_;
	omnisight::embedded::pos::scanner::BarcodeScannerEventCallback
		event_callback_;
	omnisight::embedded::pos::scanner::BarcodeScannerDevice active_device_;
	bool capturing_ = false;
};

class SmokePrinter final :
	public omnisight::embedded::pos::integration::ReceiptPrinter {
public:
	omnisight::embedded::pos::integration::ScannerPrinterPackStatus
	printReceipt(
		const omnisight::embedded::pos::integration::ReceiptDocument
			&receipt) override
	{
		printed_ = receipt;
		return omnisight::embedded::pos::integration::
			ScannerPrinterPackStatus::kOk;
	}

	const omnisight::embedded::pos::integration::ReceiptDocument &printed()
		const
	{
		return printed_;
	}

private:
	omnisight::embedded::pos::integration::ReceiptDocument printed_;
};

} // namespace

int main()
{
	using namespace omnisight::embedded::pos::integration;
	using namespace omnisight::embedded::pos::scanner;

	SmokeScanner scanner;
	SmokePrinter printer;
	int scan_callbacks = 0;

	ScannerPrinterPack pack({
		&scanner,
		&printer,
		[](const BarcodeScanData &scan, ReceiptLine *line) {
			if (!line || scan.text != "012345678905")
				return ScannerPrinterPackStatus::kLookupMiss;
			line->sku = scan.text;
			line->description = "Smoke item";
			line->quantity = 1;
			line->unit_price_minor = 1299;
			line->currency = "USD";
			return ScannerPrinterPackStatus::kOk;
		},
		true,
	});

	pack.onScan([&scan_callbacks](const BarcodeScanData &scan,
				      ScannerPrinterPackStatus status) {
		if (scan.text == "012345678905" &&
		    status == ScannerPrinterPackStatus::kOk)
			++scan_callbacks;
	});

	if (pack.discover() != ScannerPrinterPackStatus::kOk) {
		std::cerr << "discover failed: " << pack.lastError() << '\n';
		return EXIT_FAILURE;
	}

	BarcodeScannerDevice device = {
		"smoke-scanner",
		"Smoke Scanner",
		"SMOKE001",
		"loopback",
		0,
		0,
		nullptr,
	};
	if (pack.start(device) != ScannerPrinterPackStatus::kOk ||
	    !scanner.capturing()) {
		std::cerr << "start failed: " << pack.lastError() << '\n';
		return EXIT_FAILURE;
	}

	scanner.emit("012345678905");
	if (scan_callbacks != 1 || pack.pendingReceipt().lines.size() != 1) {
		std::cerr << "scan callback or lookup append failed\n";
		return EXIT_FAILURE;
	}

	if (pack.printReceipt() != ScannerPrinterPackStatus::kOk ||
	    printer.printed().lines.size() != 1 ||
	    printer.printed().lines[0].sku != "012345678905") {
		std::cerr << "receipt print failed: " << pack.lastError() << '\n';
		return EXIT_FAILURE;
	}

	if (pack.stop() != ScannerPrinterPackStatus::kOk ||
	    scanner.capturing()) {
		std::cerr << "stop failed: " << pack.lastError() << '\n';
		return EXIT_FAILURE;
	}

	return EXIT_SUCCESS;
}
#endif
