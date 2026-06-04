/* SPDX-License-Identifier: MIT
 *
 * Case 7 EMV application cryptogram generator interface (OP-2047).
 */
#ifndef OMNISIGHT_EMBEDDED_POS_EMV_CRYPTOGRAM_GENERATOR_H_
#define OMNISIGHT_EMBEDDED_POS_EMV_CRYPTOGRAM_GENERATOR_H_

#include <cstddef>
#include <cstdint>
#include <functional>
#include <string>
#include <vector>

namespace omnisight::embedded::pos::emv {

enum class EmvCryptogramStatus {
	kOk = 0,
	kInvalidArgument,
	kEncodeError,
	kSignerError,
};

enum class EmvCryptogramType {
	kAac = 0,
	kTc,
	kArqc,
};

enum class EmvCryptogramAlgorithm {
	kTdesRetailMac = 0,
	kAesCmac,
};

struct EmvDataElement {
	uint32_t tag = 0;
	std::vector<uint8_t> value;
};

struct EmvCryptogramSignRequest {
	EmvCryptogramAlgorithm algorithm =
		EmvCryptogramAlgorithm::kTdesRetailMac;
	EmvCryptogramType type = EmvCryptogramType::kArqc;
	std::vector<uint8_t> message;
};

using EmvCryptogramSignerFn =
	std::function<EmvCryptogramStatus(const EmvCryptogramSignRequest &request,
					  std::vector<uint8_t> *signature)>;

struct EmvCryptogramRequest {
	EmvCryptogramType type = EmvCryptogramType::kArqc;
	EmvCryptogramAlgorithm algorithm =
		EmvCryptogramAlgorithm::kTdesRetailMac;
	uint16_t application_transaction_counter = 0;
	std::vector<uint8_t> cdol_data;
	std::vector<EmvDataElement> icc_data;
	std::vector<uint8_t> issuer_application_data;
	std::size_t cryptogram_length = 8;
	EmvCryptogramSignerFn signer;
};

struct EmvCryptogramResult {
	EmvCryptogramType type = EmvCryptogramType::kArqc;
	EmvCryptogramAlgorithm algorithm =
		EmvCryptogramAlgorithm::kTdesRetailMac;
	uint8_t cryptogram_information_data = 0;
	uint16_t application_transaction_counter = 0;
	std::vector<uint8_t> signed_data;
	std::vector<uint8_t> cryptogram;
	std::vector<uint8_t> issuer_application_data;
	std::vector<uint8_t> response_template;
};

class EmvCryptogramGenerator {
public:
	static constexpr std::size_t kTdesBlockSize = 8;
	static constexpr std::size_t kAesBlockSize = 16;
	static constexpr std::size_t kDefaultCryptogramLength = 8;

	explicit EmvCryptogramGenerator(EmvCryptogramSignerFn signer = {});

	static uint8_t cryptogramInformationData(EmvCryptogramType type);
	static std::size_t blockSize(EmvCryptogramAlgorithm algorithm);
	static EmvCryptogramStatus encodeTag(uint32_t tag,
					     std::vector<uint8_t> *encoded);
	static EmvCryptogramStatus encodeTlv(const EmvDataElement &element,
					     std::vector<uint8_t> *encoded);
	static EmvCryptogramStatus buildSignedData(
		const EmvCryptogramRequest &request,
		std::vector<uint8_t> *signed_data);
	static EmvCryptogramStatus encodeResponseTemplate(
		const EmvCryptogramResult &result,
		std::vector<uint8_t> *response);

	EmvCryptogramStatus generate(const EmvCryptogramRequest &request,
				     EmvCryptogramResult *result) const;
	const std::string &lastError() const;

private:
	EmvCryptogramStatus fail(EmvCryptogramStatus status,
				 const std::string &error) const;

	EmvCryptogramSignerFn signer_;
	mutable std::string last_error_;
};

} // namespace omnisight::embedded::pos::emv

#endif // OMNISIGHT_EMBEDDED_POS_EMV_CRYPTOGRAM_GENERATOR_H_
