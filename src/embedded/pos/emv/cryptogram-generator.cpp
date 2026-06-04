/* SPDX-License-Identifier: MIT
 *
 * Case 7 EMV application cryptogram generator implementation (OP-2047).
 */
#include "cryptogram-generator.h"

#include <cstddef>
#include <utility>

#ifdef OMNISIGHT_EMV_CRYPTOGRAM_GENERATOR_SMOKE_MAIN
#include <cstdlib>
#endif

namespace omnisight::embedded::pos::emv {
namespace {

constexpr uint32_t kApplicationCryptogramTag = 0x9f26;
constexpr uint32_t kCryptogramInformationDataTag = 0x9f27;
constexpr uint32_t kIssuerApplicationDataTag = 0x9f10;
constexpr uint32_t kApplicationTransactionCounterTag = 0x9f36;
constexpr uint8_t kResponseMessageTemplateFormat2Tag = 0x77;

static bool valid_tag(uint32_t tag)
{
	return tag != 0 && tag <= 0xffffff;
}

static bool valid_tlv_length(std::size_t length)
{
	return length <= 0xffff;
}

static void append_u16(uint16_t value, std::vector<uint8_t> *bytes)
{
	bytes->push_back(static_cast<uint8_t>(value >> 8));
	bytes->push_back(static_cast<uint8_t>(value & 0xff));
}

static EmvCryptogramStatus append_length(std::size_t length,
					 std::vector<uint8_t> *bytes)
{
	if (!valid_tlv_length(length))
		return EmvCryptogramStatus::kEncodeError;

	if (length < 0x80) {
		bytes->push_back(static_cast<uint8_t>(length));
	} else if (length <= 0xff) {
		bytes->push_back(0x81);
		bytes->push_back(static_cast<uint8_t>(length));
	} else {
		bytes->push_back(0x82);
		bytes->push_back(static_cast<uint8_t>(length >> 8));
		bytes->push_back(static_cast<uint8_t>(length & 0xff));
	}

	return EmvCryptogramStatus::kOk;
}

static EmvCryptogramStatus append_tlv(uint32_t tag,
				      const std::vector<uint8_t> &value,
				      std::vector<uint8_t> *bytes)
{
	EmvDataElement element = {tag, value};
	std::vector<uint8_t> encoded;
	EmvCryptogramStatus status =
		EmvCryptogramGenerator::encodeTlv(element, &encoded);

	if (status != EmvCryptogramStatus::kOk)
		return status;

	bytes->insert(bytes->end(), encoded.begin(), encoded.end());
	return EmvCryptogramStatus::kOk;
}

static void append_iso9797_method2_padding(std::size_t block_size,
					   std::vector<uint8_t> *bytes)
{
	bytes->push_back(0x80);
	while ((bytes->size() % block_size) != 0)
		bytes->push_back(0x00);
}

static bool valid_cryptogram_length(EmvCryptogramAlgorithm algorithm,
				    std::size_t length)
{
	if (length == 0)
		return false;
	if (algorithm == EmvCryptogramAlgorithm::kTdesRetailMac)
		return length == EmvCryptogramGenerator::kDefaultCryptogramLength;
	return length == EmvCryptogramGenerator::kDefaultCryptogramLength ||
	       length == EmvCryptogramGenerator::kAesBlockSize;
}

static std::vector<uint8_t> atc_bytes(uint16_t atc)
{
	std::vector<uint8_t> bytes;

	append_u16(atc, &bytes);
	return bytes;
}

} // namespace

EmvCryptogramGenerator::EmvCryptogramGenerator(EmvCryptogramSignerFn signer)
	: signer_(std::move(signer))
{
}

uint8_t EmvCryptogramGenerator::cryptogramInformationData(
	EmvCryptogramType type)
{
	switch (type) {
	case EmvCryptogramType::kAac:
		return 0x00;
	case EmvCryptogramType::kTc:
		return 0x40;
	case EmvCryptogramType::kArqc:
		return 0x80;
	}

	return 0x00;
}

std::size_t
EmvCryptogramGenerator::blockSize(EmvCryptogramAlgorithm algorithm)
{
	switch (algorithm) {
	case EmvCryptogramAlgorithm::kTdesRetailMac:
		return kTdesBlockSize;
	case EmvCryptogramAlgorithm::kAesCmac:
		return kAesBlockSize;
	}

	return kTdesBlockSize;
}

EmvCryptogramStatus
EmvCryptogramGenerator::encodeTag(uint32_t tag, std::vector<uint8_t> *encoded)
{
	if (!encoded)
		return EmvCryptogramStatus::kInvalidArgument;
	if (!valid_tag(tag))
		return EmvCryptogramStatus::kEncodeError;

	std::vector<uint8_t> bytes;

	if (tag > 0xffff)
		bytes.push_back(static_cast<uint8_t>(tag >> 16));
	if (tag > 0xff)
		bytes.push_back(static_cast<uint8_t>(tag >> 8));
	bytes.push_back(static_cast<uint8_t>(tag & 0xff));

	if (bytes.size() > 1 && (bytes[0] & 0x1f) != 0x1f)
		return EmvCryptogramStatus::kEncodeError;
	if (bytes.size() == 1 && (bytes[0] & 0x1f) == 0x1f)
		return EmvCryptogramStatus::kEncodeError;

	*encoded = std::move(bytes);
	return EmvCryptogramStatus::kOk;
}

EmvCryptogramStatus
EmvCryptogramGenerator::encodeTlv(const EmvDataElement &element,
				  std::vector<uint8_t> *encoded)
{
	if (!encoded)
		return EmvCryptogramStatus::kInvalidArgument;
	if (!valid_tlv_length(element.value.size()))
		return EmvCryptogramStatus::kEncodeError;

	std::vector<uint8_t> bytes;
	EmvCryptogramStatus status = encodeTag(element.tag, &bytes);

	if (status != EmvCryptogramStatus::kOk)
		return status;

	status = append_length(element.value.size(), &bytes);
	if (status != EmvCryptogramStatus::kOk)
		return status;

	bytes.insert(bytes.end(), element.value.begin(), element.value.end());
	*encoded = std::move(bytes);
	return EmvCryptogramStatus::kOk;
}

EmvCryptogramStatus EmvCryptogramGenerator::buildSignedData(
	const EmvCryptogramRequest &request, std::vector<uint8_t> *signed_data)
{
	if (!signed_data)
		return EmvCryptogramStatus::kInvalidArgument;
	if (request.cdol_data.empty())
		return EmvCryptogramStatus::kInvalidArgument;

	const std::size_t mac_block_size = blockSize(request.algorithm);
	std::vector<uint8_t> bytes = request.cdol_data;
	const uint8_t cid = cryptogramInformationData(request.type);

	bytes.push_back(cid);
	append_u16(request.application_transaction_counter, &bytes);

	for (const EmvDataElement &element : request.icc_data) {
		std::vector<uint8_t> encoded;
		EmvCryptogramStatus status = encodeTlv(element, &encoded);

		if (status != EmvCryptogramStatus::kOk)
			return status;
		bytes.insert(bytes.end(), encoded.begin(), encoded.end());
	}

	if (request.algorithm == EmvCryptogramAlgorithm::kTdesRetailMac)
		append_iso9797_method2_padding(mac_block_size, &bytes);

	*signed_data = std::move(bytes);
	return EmvCryptogramStatus::kOk;
}

EmvCryptogramStatus EmvCryptogramGenerator::encodeResponseTemplate(
	const EmvCryptogramResult &result, std::vector<uint8_t> *response)
{
	if (!response)
		return EmvCryptogramStatus::kInvalidArgument;
	if (!valid_cryptogram_length(result.algorithm, result.cryptogram.size()))
		return EmvCryptogramStatus::kInvalidArgument;

	std::vector<uint8_t> body;
	EmvCryptogramStatus status =
		append_tlv(kApplicationCryptogramTag, result.cryptogram, &body);

	if (status != EmvCryptogramStatus::kOk)
		return status;

	status = append_tlv(kCryptogramInformationDataTag,
			    {result.cryptogram_information_data}, &body);
	if (status != EmvCryptogramStatus::kOk)
		return status;

	status = append_tlv(kApplicationTransactionCounterTag,
			    atc_bytes(result.application_transaction_counter),
			    &body);
	if (status != EmvCryptogramStatus::kOk)
		return status;

	if (!result.issuer_application_data.empty()) {
		status = append_tlv(kIssuerApplicationDataTag,
				    result.issuer_application_data, &body);
		if (status != EmvCryptogramStatus::kOk)
			return status;
	}

	EmvDataElement response_template = {
		kResponseMessageTemplateFormat2Tag,
		body,
	};

	return encodeTlv(response_template, response);
}

EmvCryptogramStatus
EmvCryptogramGenerator::generate(const EmvCryptogramRequest &request,
				 EmvCryptogramResult *result) const
{
	if (!result)
		return fail(EmvCryptogramStatus::kInvalidArgument,
			    "cryptogram result destination is null");
	if (!valid_cryptogram_length(request.algorithm,
				     request.cryptogram_length))
		return fail(EmvCryptogramStatus::kInvalidArgument,
			    "cryptogram length is invalid for algorithm");

	EmvCryptogramSignerFn signer = request.signer ? request.signer : signer_;

	if (!signer)
		return fail(EmvCryptogramStatus::kInvalidArgument,
			    "cryptogram signer is not configured");

	std::vector<uint8_t> signed_data;
	EmvCryptogramStatus status = buildSignedData(request, &signed_data);

	if (status != EmvCryptogramStatus::kOk)
		return fail(status, "cryptogram signed-data encode failed");

	EmvCryptogramSignRequest sign_request = {
		request.algorithm,
		request.type,
		signed_data,
	};
	std::vector<uint8_t> signature;

	status = signer(sign_request, &signature);
	if (status != EmvCryptogramStatus::kOk)
		return fail(status, "cryptogram signer failed");
	if (signature.size() < request.cryptogram_length)
		return fail(EmvCryptogramStatus::kSignerError,
			    "cryptogram signer returned too few bytes");

	EmvCryptogramResult generated;
	generated.type = request.type;
	generated.algorithm = request.algorithm;
	generated.cryptogram_information_data =
		cryptogramInformationData(request.type);
	generated.application_transaction_counter =
		request.application_transaction_counter;
	generated.signed_data = std::move(signed_data);
	generated.issuer_application_data = request.issuer_application_data;
	generated.cryptogram.assign(
		signature.begin(),
		signature.begin() +
			static_cast<std::ptrdiff_t>(request.cryptogram_length));

	status = encodeResponseTemplate(generated, &generated.response_template);
	if (status != EmvCryptogramStatus::kOk)
		return fail(status, "cryptogram response encode failed");

	*result = std::move(generated);
	last_error_.clear();
	return EmvCryptogramStatus::kOk;
}

const std::string &EmvCryptogramGenerator::lastError() const
{
	return last_error_;
}

EmvCryptogramStatus EmvCryptogramGenerator::fail(EmvCryptogramStatus status,
						 const std::string &error) const
{
	last_error_ = error;
	return status;
}

} // namespace omnisight::embedded::pos::emv

#ifdef OMNISIGHT_EMV_CRYPTOGRAM_GENERATOR_SMOKE_MAIN
int main()
{
	using omnisight::embedded::pos::emv::EmvCryptogramAlgorithm;
	using omnisight::embedded::pos::emv::EmvCryptogramGenerator;
	using omnisight::embedded::pos::emv::EmvCryptogramRequest;
	using omnisight::embedded::pos::emv::EmvCryptogramResult;
	using omnisight::embedded::pos::emv::EmvCryptogramSignRequest;
	using omnisight::embedded::pos::emv::EmvCryptogramStatus;
	using omnisight::embedded::pos::emv::EmvCryptogramType;

	EmvCryptogramGenerator generator(
		[](const EmvCryptogramSignRequest &request,
		   std::vector<uint8_t> *signature) {
			if (!signature || request.message.empty())
				return EmvCryptogramStatus::kInvalidArgument;
			signature->assign(16, request.message.back());
			(*signature)[0] =
				request.algorithm ==
						EmvCryptogramAlgorithm::kAesCmac ?
					0xa5 :
					0x3d;
			return EmvCryptogramStatus::kOk;
		});

	EmvCryptogramRequest request;
	request.type = EmvCryptogramType::kArqc;
	request.algorithm = EmvCryptogramAlgorithm::kTdesRetailMac;
	request.application_transaction_counter = 0x1234;
	request.cdol_data = {0x00, 0x00, 0x00, 0x00, 0x10, 0x00,
			     0x00, 0x00, 0x08, 0x40, 0x25, 0x06,
			     0x04, 0x00, 0x00, 0x00};
	request.icc_data = {{0x9f37, {0xde, 0xad, 0xbe, 0xef}}};

	EmvCryptogramResult result;
	if (generator.generate(request, &result) != EmvCryptogramStatus::kOk)
		return EXIT_FAILURE;
	if (result.cryptogram_information_data != 0x80)
		return EXIT_FAILURE;
	if (result.cryptogram.size() != 8 || result.cryptogram[0] != 0x3d)
		return EXIT_FAILURE;
	if (result.response_template.empty() ||
	    result.response_template[0] != 0x77)
		return EXIT_FAILURE;
	if ((result.signed_data.size() %
	     EmvCryptogramGenerator::kTdesBlockSize) != 0)
		return EXIT_FAILURE;

	request.type = EmvCryptogramType::kTc;
	request.algorithm = EmvCryptogramAlgorithm::kAesCmac;
	request.cryptogram_length = EmvCryptogramGenerator::kAesBlockSize;
	if (generator.generate(request, &result) != EmvCryptogramStatus::kOk)
		return EXIT_FAILURE;
	if (result.cryptogram_information_data != 0x40)
		return EXIT_FAILURE;
	if (result.cryptogram.size() != EmvCryptogramGenerator::kAesBlockSize ||
	    result.cryptogram[0] != 0xa5)
		return EXIT_FAILURE;

	request.type = EmvCryptogramType::kAac;
	request.algorithm = EmvCryptogramAlgorithm::kTdesRetailMac;
	request.cryptogram_length =
		EmvCryptogramGenerator::kDefaultCryptogramLength;
	if (generator.generate(request, &result) != EmvCryptogramStatus::kOk)
		return EXIT_FAILURE;
	if (result.cryptogram_information_data != 0x00)
		return EXIT_FAILURE;

	return EXIT_SUCCESS;
}
#endif
