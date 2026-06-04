/* SPDX-License-Identifier: MIT
 *
 * Case 7 ESC/POS thermal printer protocol interface (OP-2052).
 */
#ifndef OMNISIGHT_EMBEDDED_POS_PRINTER_ESCPOS_IMPL_H_
#define OMNISIGHT_EMBEDDED_POS_PRINTER_ESCPOS_IMPL_H_

#include <cstddef>
#include <cstdint>
#include <functional>
#include <string>
#include <vector>

namespace omnisight::embedded::pos::printer {

enum class EscPosStatus {
	kOk = 0,
	kInvalidArgument,
	kInvalidState,
	kTransportError,
};

enum class EscPosTransportKind {
	kUsb = 0,
	kSerial,
	kTcp,
};

enum class EscPosAlignment {
	kLeft = 0,
	kCenter,
	kRight,
};

enum class EscPosUnderline {
	kOff = 0,
	kSingle,
	kDouble,
};

enum class EscPosCutMode {
	kFull = 0,
	kPartial,
};

enum class EscPosDrawerPin {
	kPin2 = 0,
	kPin5,
};

enum class EscPosBarcodeType {
	kCode128 = 0,
};

enum class EscPosQrErrorCorrection {
	kLow = 0,
	kMedium,
	kQuartile,
	kHigh,
};

struct EscPosTransportConfig {
	EscPosTransportKind kind = EscPosTransportKind::kUsb;
	std::string device_path;
	std::string host;
	uint16_t port = 0;
	uint32_t baud_rate = 0;

	explicit operator bool() const;
};

struct EscPosTextStyle {
	EscPosAlignment alignment = EscPosAlignment::kLeft;
	bool bold = false;
	EscPosUnderline underline = EscPosUnderline::kOff;
	uint8_t width_scale = 1;
	uint8_t height_scale = 1;
};

struct EscPosQrOptions {
	uint8_t module_size = 4;
	EscPosQrErrorCorrection error_correction = EscPosQrErrorCorrection::kMedium;
};

struct EscPosPdf417Options {
	uint8_t columns = 0;
	uint8_t rows = 0;
	uint8_t module_width = 3;
	uint8_t row_height = 3;
	uint8_t error_correction_level = 3;
};

using EscPosWriteFn =
	std::function<EscPosStatus(const EscPosTransportConfig &transport,
				  const std::vector<uint8_t> &bytes)>;

class EscPosPrinter {
public:
	explicit EscPosPrinter(EscPosTransportConfig transport = {},
			       EscPosWriteFn write_fn = {});
	virtual ~EscPosPrinter() = default;

	void setTransport(EscPosTransportConfig transport);
	void setWriteFn(EscPosWriteFn write_fn);

	const EscPosTransportConfig &transport() const;
	const std::string &lastError() const;

	virtual EscPosStatus initialize();
	virtual EscPosStatus setTextStyle(const EscPosTextStyle &style);
	virtual EscPosStatus writeText(const std::string &text);
	virtual EscPosStatus feedLines(uint8_t lines);
	virtual EscPosStatus cutPaper(EscPosCutMode mode = EscPosCutMode::kFull);
	virtual EscPosStatus kickCashDrawer(
		EscPosDrawerPin pin = EscPosDrawerPin::kPin2,
		uint8_t on_time = 50, uint8_t off_time = 50);
	virtual EscPosStatus printBarcode(EscPosBarcodeType type,
					  const std::string &data);
	virtual EscPosStatus printQrCode(const std::string &data,
					 const EscPosQrOptions &options = {});
	virtual EscPosStatus printPdf417(const std::string &data,
					 const EscPosPdf417Options &options = {});

	static EscPosStatus buildTextStyle(const EscPosTextStyle &style,
					   std::vector<uint8_t> *bytes);
	static EscPosStatus buildBarcode(EscPosBarcodeType type,
					 const std::string &data,
					 std::vector<uint8_t> *bytes);
	static EscPosStatus buildQrCode(const std::string &data,
					const EscPosQrOptions &options,
					std::vector<uint8_t> *bytes);
	static EscPosStatus buildPdf417(const std::string &data,
					const EscPosPdf417Options &options,
					std::vector<uint8_t> *bytes);

protected:
	EscPosStatus writeBytes(const std::vector<uint8_t> &bytes);
	EscPosStatus fail(EscPosStatus status, const std::string &error) const;

private:
	EscPosTransportConfig transport_;
	EscPosWriteFn write_fn_;
	mutable std::string last_error_;
};

const char *toString(EscPosStatus status);
const char *toString(EscPosTransportKind kind);
const char *toString(EscPosAlignment alignment);

} // namespace omnisight::embedded::pos::printer

#endif // OMNISIGHT_EMBEDDED_POS_PRINTER_ESCPOS_IMPL_H_
