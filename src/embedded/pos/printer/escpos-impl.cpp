/* SPDX-License-Identifier: MIT
 *
 * Case 7 ESC/POS thermal printer protocol implementation (OP-2052).
 */
#include "escpos-impl.h"

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <utility>

#ifdef OMNISIGHT_POS_PRINTER_ESCPOS_IMPL_SMOKE_MAIN
#include <cstdlib>
#endif

namespace omnisight::embedded::pos::printer {
namespace {

constexpr uint8_t kEsc = 0x1b;
constexpr uint8_t kGs = 0x1d;
constexpr std::size_t kMaxNativePayload = 255;
constexpr std::size_t kMaxTwoByteStorePayload = 65532;

static uint8_t alignment_byte(EscPosAlignment alignment)
{
	switch (alignment) {
	case EscPosAlignment::kLeft:
		return 0;
	case EscPosAlignment::kCenter:
		return 1;
	case EscPosAlignment::kRight:
		return 2;
	}

	return 0;
}

static uint8_t underline_byte(EscPosUnderline underline)
{
	switch (underline) {
	case EscPosUnderline::kOff:
		return 0;
	case EscPosUnderline::kSingle:
		return 1;
	case EscPosUnderline::kDouble:
		return 2;
	}

	return 0;
}

static uint8_t qr_error_correction_byte(EscPosQrErrorCorrection level)
{
	switch (level) {
	case EscPosQrErrorCorrection::kLow:
		return 48;
	case EscPosQrErrorCorrection::kMedium:
		return 49;
	case EscPosQrErrorCorrection::kQuartile:
		return 50;
	case EscPosQrErrorCorrection::kHigh:
		return 51;
	}

	return 49;
}

static uint8_t cut_mode_byte(EscPosCutMode mode)
{
	return mode == EscPosCutMode::kPartial ? 1 : 0;
}

static uint8_t drawer_pin_byte(EscPosDrawerPin pin)
{
	return pin == EscPosDrawerPin::kPin5 ? 1 : 0;
}

static bool valid_scale(uint8_t scale)
{
	return scale >= 1 && scale <= 8;
}

static bool valid_text_payload(const std::string &data)
{
	return !data.empty() && data.size() <= kMaxNativePayload;
}

static void append_gs_k_command(uint8_t cn, uint8_t fn,
				const std::vector<uint8_t> &payload,
				std::vector<uint8_t> *bytes)
{
	const uint16_t length = static_cast<uint16_t>(payload.size() + 2);

	bytes->push_back(kGs);
	bytes->push_back('(');
	bytes->push_back('k');
	bytes->push_back(static_cast<uint8_t>(length & 0xff));
	bytes->push_back(static_cast<uint8_t>(length >> 8));
	bytes->push_back(cn);
	bytes->push_back(fn);
	bytes->insert(bytes->end(), payload.begin(), payload.end());
}

static std::vector<uint8_t> text_payload(const std::string &data)
{
	return std::vector<uint8_t>(data.begin(), data.end());
}

static std::vector<uint8_t> store_payload(const std::string &data)
{
	std::vector<uint8_t> payload;

	payload.reserve(data.size() + 1);
	payload.push_back(48);
	payload.insert(payload.end(), data.begin(), data.end());
	return payload;
}

} // namespace

EscPosPrinter::EscPosPrinter(EscPosTransportConfig transport,
			     EscPosWriteFn write_fn)
	: transport_(std::move(transport)), write_fn_(std::move(write_fn))
{
}

EscPosTransportConfig::operator bool() const
{
	if (kind == EscPosTransportKind::kTcp)
		return !host.empty() && port != 0;
	if (kind == EscPosTransportKind::kSerial)
		return !device_path.empty() && baud_rate != 0;
	return !device_path.empty();
}

void EscPosPrinter::setTransport(EscPosTransportConfig transport)
{
	transport_ = std::move(transport);
}

void EscPosPrinter::setWriteFn(EscPosWriteFn write_fn)
{
	write_fn_ = std::move(write_fn);
}

const EscPosTransportConfig &EscPosPrinter::transport() const
{
	return transport_;
}

const std::string &EscPosPrinter::lastError() const
{
	return last_error_;
}

EscPosStatus EscPosPrinter::initialize()
{
	return writeBytes({kEsc, '@'});
}

EscPosStatus EscPosPrinter::setTextStyle(const EscPosTextStyle &style)
{
	std::vector<uint8_t> bytes;
	const EscPosStatus status = buildTextStyle(style, &bytes);

	if (status != EscPosStatus::kOk)
		return fail(status, "invalid text style");

	return writeBytes(bytes);
}

EscPosStatus EscPosPrinter::writeText(const std::string &text)
{
	if (text.empty())
		return fail(EscPosStatus::kInvalidArgument, "empty text");

	return writeBytes(text_payload(text));
}

EscPosStatus EscPosPrinter::feedLines(uint8_t lines)
{
	if (lines == 0)
		return fail(EscPosStatus::kInvalidArgument, "zero feed lines");

	return writeBytes({kEsc, 'd', lines});
}

EscPosStatus EscPosPrinter::cutPaper(EscPosCutMode mode)
{
	return writeBytes({kGs, 'V', cut_mode_byte(mode)});
}

EscPosStatus EscPosPrinter::kickCashDrawer(EscPosDrawerPin pin,
					   uint8_t on_time, uint8_t off_time)
{
	if (on_time == 0 || off_time == 0)
		return fail(EscPosStatus::kInvalidArgument,
			    "cash drawer pulse duration is zero");

	return writeBytes({kEsc, 'p', drawer_pin_byte(pin), on_time, off_time});
}

EscPosStatus EscPosPrinter::printBarcode(EscPosBarcodeType type,
					 const std::string &data)
{
	std::vector<uint8_t> bytes;
	const EscPosStatus status = buildBarcode(type, data, &bytes);

	if (status != EscPosStatus::kOk)
		return fail(status, "invalid barcode request");

	return writeBytes(bytes);
}

EscPosStatus EscPosPrinter::printQrCode(const std::string &data,
					const EscPosQrOptions &options)
{
	std::vector<uint8_t> bytes;
	const EscPosStatus status = buildQrCode(data, options, &bytes);

	if (status != EscPosStatus::kOk)
		return fail(status, "invalid QR request");

	return writeBytes(bytes);
}

EscPosStatus EscPosPrinter::printPdf417(const std::string &data,
					const EscPosPdf417Options &options)
{
	std::vector<uint8_t> bytes;
	const EscPosStatus status = buildPdf417(data, options, &bytes);

	if (status != EscPosStatus::kOk)
		return fail(status, "invalid PDF417 request");

	return writeBytes(bytes);
}

EscPosStatus EscPosPrinter::buildTextStyle(const EscPosTextStyle &style,
					   std::vector<uint8_t> *bytes)
{
	if (!bytes || !valid_scale(style.width_scale) ||
	    !valid_scale(style.height_scale))
		return EscPosStatus::kInvalidArgument;

	const uint8_t size =
		static_cast<uint8_t>(((style.width_scale - 1) << 4) |
				     (style.height_scale - 1));

	bytes->clear();
	bytes->reserve(12);
	bytes->insert(bytes->end(), {kEsc, 'a', alignment_byte(style.alignment)});
	bytes->insert(bytes->end(),
		      {kEsc, 'E', static_cast<uint8_t>(style.bold ? 1 : 0)});
	bytes->insert(bytes->end(),
		      {kEsc, '-', underline_byte(style.underline)});
	bytes->insert(bytes->end(), {kGs, '!', size});
	return EscPosStatus::kOk;
}

EscPosStatus EscPosPrinter::buildBarcode(EscPosBarcodeType type,
					 const std::string &data,
					 std::vector<uint8_t> *bytes)
{
	if (!bytes || !valid_text_payload(data))
		return EscPosStatus::kInvalidArgument;
	if (type != EscPosBarcodeType::kCode128)
		return EscPosStatus::kInvalidArgument;

	bytes->clear();
	bytes->reserve(data.size() + 4);
	bytes->insert(bytes->end(), {kGs, 'k', 73,
				     static_cast<uint8_t>(data.size())});
	bytes->insert(bytes->end(), data.begin(), data.end());
	return EscPosStatus::kOk;
}

EscPosStatus EscPosPrinter::buildQrCode(const std::string &data,
					const EscPosQrOptions &options,
					std::vector<uint8_t> *bytes)
{
	if (!bytes || data.empty() || data.size() > kMaxTwoByteStorePayload ||
	    options.module_size < 1 || options.module_size > 16)
		return EscPosStatus::kInvalidArgument;

	bytes->clear();
	append_gs_k_command(49, 65, {49, 0}, bytes);
	append_gs_k_command(49, 67, {options.module_size}, bytes);
	append_gs_k_command(49, 69,
			    {qr_error_correction_byte(options.error_correction)},
			    bytes);
	append_gs_k_command(49, 80, store_payload(data), bytes);
	append_gs_k_command(49, 81, {48}, bytes);
	return EscPosStatus::kOk;
}

EscPosStatus EscPosPrinter::buildPdf417(const std::string &data,
					const EscPosPdf417Options &options,
					std::vector<uint8_t> *bytes)
{
	if (!bytes || data.empty() || data.size() > kMaxTwoByteStorePayload ||
	    options.columns > 30 || options.rows > 90 ||
	    options.module_width < 2 || options.module_width > 8 ||
	    options.row_height < 2 || options.row_height > 8 ||
	    options.error_correction_level > 8)
		return EscPosStatus::kInvalidArgument;

	bytes->clear();
	append_gs_k_command(48, 65, {options.columns}, bytes);
	append_gs_k_command(48, 66, {options.rows}, bytes);
	append_gs_k_command(48, 67, {options.module_width}, bytes);
	append_gs_k_command(48, 68, {options.row_height}, bytes);
	append_gs_k_command(48, 69, {options.error_correction_level}, bytes);
	append_gs_k_command(48, 80, store_payload(data), bytes);
	append_gs_k_command(48, 81, {48}, bytes);
	return EscPosStatus::kOk;
}

EscPosStatus EscPosPrinter::writeBytes(const std::vector<uint8_t> &bytes)
{
	if (!transport_)
		return fail(EscPosStatus::kInvalidState,
			    "printer transport is not configured");
	if (!write_fn_)
		return fail(EscPosStatus::kInvalidState,
			    "printer write callback is not configured");
	if (bytes.empty())
		return fail(EscPosStatus::kInvalidArgument, "empty write");

	const EscPosStatus status = write_fn_(transport_, bytes);
	if (status != EscPosStatus::kOk)
		return fail(EscPosStatus::kTransportError,
			    "printer transport write failed");

	last_error_.clear();
	return EscPosStatus::kOk;
}

EscPosStatus EscPosPrinter::fail(EscPosStatus status,
				 const std::string &error) const
{
	last_error_ = error;
	return status;
}

const char *toString(EscPosStatus status)
{
	switch (status) {
	case EscPosStatus::kOk:
		return "ok";
	case EscPosStatus::kInvalidArgument:
		return "invalid-argument";
	case EscPosStatus::kInvalidState:
		return "invalid-state";
	case EscPosStatus::kTransportError:
		return "transport-error";
	}

	return "unknown";
}

const char *toString(EscPosTransportKind kind)
{
	switch (kind) {
	case EscPosTransportKind::kUsb:
		return "usb";
	case EscPosTransportKind::kSerial:
		return "serial";
	case EscPosTransportKind::kTcp:
		return "tcp";
	}

	return "unknown";
}

const char *toString(EscPosAlignment alignment)
{
	switch (alignment) {
	case EscPosAlignment::kLeft:
		return "left";
	case EscPosAlignment::kCenter:
		return "center";
	case EscPosAlignment::kRight:
		return "right";
	}

	return "unknown";
}

} // namespace omnisight::embedded::pos::printer

#ifdef OMNISIGHT_POS_PRINTER_ESCPOS_IMPL_SMOKE_MAIN
int main()
{
	using omnisight::embedded::pos::printer::EscPosAlignment;
	using omnisight::embedded::pos::printer::EscPosBarcodeType;
	using omnisight::embedded::pos::printer::EscPosCutMode;
	using omnisight::embedded::pos::printer::EscPosDrawerPin;
	using omnisight::embedded::pos::printer::EscPosPdf417Options;
	using omnisight::embedded::pos::printer::EscPosPrinter;
	using omnisight::embedded::pos::printer::EscPosQrOptions;
	using omnisight::embedded::pos::printer::EscPosStatus;
	using omnisight::embedded::pos::printer::EscPosTextStyle;
	using omnisight::embedded::pos::printer::EscPosTransportConfig;
	using omnisight::embedded::pos::printer::EscPosTransportKind;
	using omnisight::embedded::pos::printer::EscPosUnderline;

	std::vector<uint8_t> bytes;
	EscPosTextStyle style;
	style.alignment = EscPosAlignment::kCenter;
	style.bold = true;
	style.underline = EscPosUnderline::kSingle;
	style.width_scale = 2;
	style.height_scale = 3;
	if (EscPosPrinter::buildTextStyle(style, &bytes) != EscPosStatus::kOk)
		return EXIT_FAILURE;
	const std::vector<uint8_t> expected_style = {
		0x1b, 'a', 1, 0x1b, 'E', 1, 0x1b, '-', 1, 0x1d, '!', 0x12,
	};
	if (bytes != expected_style)
		return EXIT_FAILURE;

	if (EscPosPrinter::buildBarcode(EscPosBarcodeType::kCode128, "{B1234",
					&bytes) != EscPosStatus::kOk)
		return EXIT_FAILURE;
	const std::vector<uint8_t> expected_code128 = {
		0x1d, 'k', 73, 6, '{', 'B', '1', '2', '3', '4',
	};
	if (bytes != expected_code128)
		return EXIT_FAILURE;

	EscPosQrOptions qr_options;
	qr_options.module_size = 5;
	if (EscPosPrinter::buildQrCode("sale-42", qr_options, &bytes) !=
	    EscPosStatus::kOk)
		return EXIT_FAILURE;
	if (bytes.size() < 30 || bytes[0] != 0x1d || bytes[1] != '(' ||
	    bytes[2] != 'k' || bytes[5] != 49 || bytes.back() != 48)
		return EXIT_FAILURE;

	EscPosPdf417Options pdf417_options;
	pdf417_options.columns = 4;
	if (EscPosPrinter::buildPdf417("receipt", pdf417_options, &bytes) !=
	    EscPosStatus::kOk)
		return EXIT_FAILURE;
	if (bytes.size() < 40 || bytes[5] != 48 || bytes[6] != 65 ||
	    bytes.back() != 48)
		return EXIT_FAILURE;

	std::vector<uint8_t> written;
	EscPosTransportConfig tcp;
	tcp.kind = EscPosTransportKind::kTcp;
	tcp.host = "127.0.0.1";
	tcp.port = 9100;
	EscPosPrinter printer(
		tcp, [&written](const EscPosTransportConfig &transport,
				const std::vector<uint8_t> &chunk) {
			if (transport.kind != EscPosTransportKind::kTcp)
				return EscPosStatus::kTransportError;
			written.insert(written.end(), chunk.begin(), chunk.end());
			return EscPosStatus::kOk;
		});
	if (printer.initialize() != EscPosStatus::kOk ||
	    printer.cutPaper(EscPosCutMode::kPartial) != EscPosStatus::kOk ||
	    printer.kickCashDrawer(EscPosDrawerPin::kPin5) !=
		    EscPosStatus::kOk)
		return EXIT_FAILURE;
	const std::vector<uint8_t> expected_written = {
		0x1b, '@', 0x1d, 'V', 1, 0x1b, 'p', 1, 50, 50,
	};
	if (written != expected_written)
		return EXIT_FAILURE;

	return EXIT_SUCCESS;
}
#endif
