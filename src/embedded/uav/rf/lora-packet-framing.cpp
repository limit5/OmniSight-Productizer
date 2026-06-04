/* SPDX-License-Identifier: MIT
 *
 * Cases 6+7 LoRaWAN Class A packet framing (OP-2039).
 */
#include "lora-packet-framing.h"

#include <algorithm>
#include <array>
#include <cassert>
#include <utility>

namespace omnisight::embedded::uav::rf {
namespace {

constexpr size_t kAesBlockBytes = 16;
constexpr size_t kAesExpandedKeyBytes = 176;
constexpr size_t kLoraMicBytes = 4;
constexpr size_t kLoraMinimumDataFrameBytes = 12;
constexpr size_t kLoraMaxPhyPayloadBytes = 255;
constexpr uint8_t kLoraMajorVersion = 0x00;

constexpr std::array<uint8_t, 256> kSbox {
	0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5,
	0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
	0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0,
	0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
	0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc,
	0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
	0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a,
	0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
	0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0,
	0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
	0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b,
	0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
	0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85,
	0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
	0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5,
	0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
	0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17,
	0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
	0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88,
	0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
	0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c,
	0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
	0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9,
	0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
	0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6,
	0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
	0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e,
	0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
	0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94,
	0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
	0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68,
	0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16,
};

constexpr std::array<uint8_t, 11> kRcon {
	0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36,
};

static uint8_t xtime(uint8_t value)
{
	return static_cast<uint8_t>((value << 1U) ^
				    ((value & 0x80U) != 0U ? 0x1bU : 0x00U));
}

static void key_expansion(const std::array<uint8_t, 16> &key,
			  std::array<uint8_t, kAesExpandedKeyBytes> *round_key)
{
	for (size_t i = 0; i < key.size(); ++i)
		(*round_key)[i] = key[i];

	size_t bytes = key.size();
	uint8_t rcon_index = 1;
	std::array<uint8_t, 4> word {};

	while (bytes < round_key->size()) {
		for (size_t i = 0; i < word.size(); ++i)
			word[i] = (*round_key)[bytes - 4 + i];

		if ((bytes % key.size()) == 0) {
			const uint8_t first = word[0];

			word[0] = kSbox[word[1]] ^ kRcon[rcon_index++];
			word[1] = kSbox[word[2]];
			word[2] = kSbox[word[3]];
			word[3] = kSbox[first];
		}

		for (size_t i = 0; i < word.size(); ++i) {
			(*round_key)[bytes] =
				(*round_key)[bytes - key.size()] ^ word[i];
			++bytes;
		}
	}
}

static void add_round_key(uint8_t round,
			  const std::array<uint8_t, kAesExpandedKeyBytes> &round_key,
			  std::array<uint8_t, 16> *state)
{
	const size_t offset = static_cast<size_t>(round) * kAesBlockBytes;

	for (size_t i = 0; i < state->size(); ++i)
		(*state)[i] ^= round_key[offset + i];
}

static void sub_bytes(std::array<uint8_t, 16> *state)
{
	for (uint8_t &value : *state)
		value = kSbox[value];
}

static void shift_rows(std::array<uint8_t, 16> *state)
{
	std::array<uint8_t, 16> tmp = *state;

	(*state)[0] = tmp[0];
	(*state)[4] = tmp[4];
	(*state)[8] = tmp[8];
	(*state)[12] = tmp[12];

	(*state)[1] = tmp[5];
	(*state)[5] = tmp[9];
	(*state)[9] = tmp[13];
	(*state)[13] = tmp[1];

	(*state)[2] = tmp[10];
	(*state)[6] = tmp[14];
	(*state)[10] = tmp[2];
	(*state)[14] = tmp[6];

	(*state)[3] = tmp[15];
	(*state)[7] = tmp[3];
	(*state)[11] = tmp[7];
	(*state)[15] = tmp[11];
}

static void mix_columns(std::array<uint8_t, 16> *state)
{
	for (size_t col = 0; col < 4; ++col) {
		const size_t offset = col * 4;
		const uint8_t a0 = (*state)[offset];
		const uint8_t a1 = (*state)[offset + 1];
		const uint8_t a2 = (*state)[offset + 2];
		const uint8_t a3 = (*state)[offset + 3];
		const uint8_t x = static_cast<uint8_t>(a0 ^ a1 ^ a2 ^ a3);

		(*state)[offset] ^= x ^ xtime(static_cast<uint8_t>(a0 ^ a1));
		(*state)[offset + 1] ^= x ^ xtime(static_cast<uint8_t>(a1 ^ a2));
		(*state)[offset + 2] ^= x ^ xtime(static_cast<uint8_t>(a2 ^ a3));
		(*state)[offset + 3] ^= x ^ xtime(static_cast<uint8_t>(a3 ^ a0));
	}
}

static std::array<uint8_t, 16> aes_encrypt_block(
	const std::array<uint8_t, 16> &block,
	const std::array<uint8_t, 16> &key)
{
	std::array<uint8_t, kAesExpandedKeyBytes> round_key {};
	std::array<uint8_t, 16> state = block;

	key_expansion(key, &round_key);
	add_round_key(0, round_key, &state);

	for (uint8_t round = 1; round < 10; ++round) {
		sub_bytes(&state);
		shift_rows(&state);
		mix_columns(&state);
		add_round_key(round, round_key, &state);
	}

	sub_bytes(&state);
	shift_rows(&state);
	add_round_key(10, round_key, &state);
	return state;
}

static void xor_block(std::array<uint8_t, 16> *left,
		      const std::array<uint8_t, 16> &right)
{
	for (size_t i = 0; i < left->size(); ++i)
		(*left)[i] ^= right[i];
}

static std::array<uint8_t, 16> left_shift_one(
	const std::array<uint8_t, 16> &input)
{
	std::array<uint8_t, 16> output {};
	uint8_t carry = 0;

	for (size_t i = input.size(); i > 0; --i) {
		const uint8_t value = input[i - 1];

		output[i - 1] = static_cast<uint8_t>((value << 1U) | carry);
		carry = static_cast<uint8_t>((value & 0x80U) != 0U ? 1U : 0U);
	}
	return output;
}

static std::array<uint8_t, 16> aes_cmac(
	const std::vector<uint8_t> &message,
	const std::array<uint8_t, 16> &key)
{
	const std::array<uint8_t, 16> zero {};
	const std::array<uint8_t, 16> l = aes_encrypt_block(zero, key);
	std::array<uint8_t, 16> k1 = left_shift_one(l);

	if ((l[0] & 0x80U) != 0U)
		k1[15] ^= 0x87U;

	std::array<uint8_t, 16> k2 = left_shift_one(k1);
	if ((k1[0] & 0x80U) != 0U)
		k2[15] ^= 0x87U;

	const bool complete = !message.empty() &&
			      (message.size() % kAesBlockBytes) == 0;
	const size_t blocks = complete ?
		message.size() / kAesBlockBytes :
		(message.size() / kAesBlockBytes) + 1;
	std::array<uint8_t, 16> x {};
	std::array<uint8_t, 16> m_last {};

	if (complete) {
		const size_t start = (blocks - 1) * kAesBlockBytes;

		std::copy_n(message.begin() + static_cast<std::ptrdiff_t>(start),
			    kAesBlockBytes, m_last.begin());
		xor_block(&m_last, k1);
	} else {
		const size_t start = (blocks - 1) * kAesBlockBytes;
		const size_t tail = message.size() - start;

		for (size_t i = 0; i < tail; ++i)
			m_last[i] = message[start + i];
		m_last[tail] = 0x80;
		xor_block(&m_last, k2);
	}

	for (size_t block = 0; block + 1 < blocks; ++block) {
		for (size_t i = 0; i < kAesBlockBytes; ++i)
			x[i] ^= message[block * kAesBlockBytes + i];
		x = aes_encrypt_block(x, key);
	}

	xor_block(&x, m_last);
	return aes_encrypt_block(x, key);
}

static void write_u16_le(std::vector<uint8_t> *out, uint16_t value)
{
	out->push_back(static_cast<uint8_t>(value & 0xffU));
	out->push_back(static_cast<uint8_t>((value >> 8U) & 0xffU));
}

static void write_u32_le(std::vector<uint8_t> *out, uint32_t value)
{
	out->push_back(static_cast<uint8_t>(value & 0xffU));
	out->push_back(static_cast<uint8_t>((value >> 8U) & 0xffU));
	out->push_back(static_cast<uint8_t>((value >> 16U) & 0xffU));
	out->push_back(static_cast<uint8_t>((value >> 24U) & 0xffU));
}

static uint16_t read_u16_le(const std::vector<uint8_t> &data, size_t offset)
{
	return static_cast<uint16_t>(data[offset]) |
	       (static_cast<uint16_t>(data[offset + 1]) << 8U);
}

static uint32_t read_u32_le(const std::vector<uint8_t> &data, size_t offset)
{
	return static_cast<uint32_t>(data[offset]) |
	       (static_cast<uint32_t>(data[offset + 1]) << 8U) |
	       (static_cast<uint32_t>(data[offset + 2]) << 16U) |
	       (static_cast<uint32_t>(data[offset + 3]) << 24U);
}

static LoraFrameType frame_type_for(LoraDirection direction, bool confirmed)
{
	if (direction == LoraDirection::kUplink)
		return confirmed ? LoraFrameType::kConfirmedDataUp :
				   LoraFrameType::kUnconfirmedDataUp;
	return confirmed ? LoraFrameType::kConfirmedDataDown :
			   LoraFrameType::kUnconfirmedDataDown;
}

static bool direction_for_type(LoraFrameType type, LoraDirection *direction)
{
	switch (type) {
	case LoraFrameType::kUnconfirmedDataUp:
	case LoraFrameType::kConfirmedDataUp:
		*direction = LoraDirection::kUplink;
		return true;
	case LoraFrameType::kUnconfirmedDataDown:
	case LoraFrameType::kConfirmedDataDown:
		*direction = LoraDirection::kDownlink;
		return true;
	default:
		return false;
	}
}

static std::array<uint8_t, 16> block_a(LoraDirection direction,
				       uint32_t dev_addr,
				       uint32_t frame_counter,
				       uint8_t block_counter)
{
	std::array<uint8_t, 16> block {};

	block[0] = 0x01;
	block[5] = static_cast<uint8_t>(direction);
	block[6] = static_cast<uint8_t>(dev_addr & 0xffU);
	block[7] = static_cast<uint8_t>((dev_addr >> 8U) & 0xffU);
	block[8] = static_cast<uint8_t>((dev_addr >> 16U) & 0xffU);
	block[9] = static_cast<uint8_t>((dev_addr >> 24U) & 0xffU);
	block[10] = static_cast<uint8_t>(frame_counter & 0xffU);
	block[11] = static_cast<uint8_t>((frame_counter >> 8U) & 0xffU);
	block[12] = static_cast<uint8_t>((frame_counter >> 16U) & 0xffU);
	block[13] = static_cast<uint8_t>((frame_counter >> 24U) & 0xffU);
	block[15] = block_counter;
	return block;
}

static std::vector<uint8_t> crypt_payload(
	const std::vector<uint8_t> &payload,
	const std::array<uint8_t, 16> &key,
	LoraDirection direction,
	uint32_t dev_addr,
	uint32_t frame_counter)
{
	std::vector<uint8_t> out = payload;
	uint8_t block_counter = 1;

	for (size_t offset = 0; offset < out.size(); offset += kAesBlockBytes) {
		const std::array<uint8_t, 16> stream =
			aes_encrypt_block(block_a(direction, dev_addr,
						 frame_counter, block_counter++),
					  key);
		const size_t chunk = std::min(kAesBlockBytes, out.size() - offset);

		for (size_t i = 0; i < chunk; ++i)
			out[offset + i] ^= stream[i];
	}
	return out;
}

static std::array<uint8_t, 4> mic_for(
	const std::vector<uint8_t> &message,
	const std::array<uint8_t, 16> &nwk_skey,
	LoraDirection direction,
	uint32_t dev_addr,
	uint32_t frame_counter)
{
	std::vector<uint8_t> cmac_input;

	cmac_input.reserve(kAesBlockBytes + message.size());
	cmac_input.push_back(0x49);
	cmac_input.insert(cmac_input.end(), 4, 0x00);
	cmac_input.push_back(static_cast<uint8_t>(direction));
	write_u32_le(&cmac_input, dev_addr);
	write_u32_le(&cmac_input, frame_counter);
	cmac_input.push_back(0x00);
	cmac_input.push_back(static_cast<uint8_t>(message.size()));
	cmac_input.insert(cmac_input.end(), message.begin(), message.end());

	const std::array<uint8_t, 16> cmac = aes_cmac(cmac_input, nwk_skey);

	return { cmac[0], cmac[1], cmac[2], cmac[3] };
}

static uint32_t reconstruct_frame_counter(uint16_t low, uint32_t next_expected)
{
	uint32_t candidate = (next_expected & 0xffff0000U) | low;

	if (candidate + 0x8000U < next_expected)
		candidate += 0x10000U;
	return candidate;
}

} // namespace

class LoraFramer::Impl {
public:
	Impl() = default;
	explicit Impl(LoraSessionConfig config) : config_(std::move(config)) {}

	LoraStatus frame(const LoraFrameRequest &request,
			 std::vector<uint8_t> *phy_payload)
	{
		if (phy_payload == nullptr) {
			last_error_ = "phy_payload is required";
			return LoraStatus::kInvalidArgument;
		}
		if (config_.dev_addr == 0) {
			last_error_ = "dev_addr is required";
			return LoraStatus::kInvalidArgument;
		}
		if (request.fopts.size() > 15) {
			last_error_ = "fopts length exceeds LoRaWAN FHDR limit";
			return LoraStatus::kInvalidArgument;
		}

		const LoraFrameType type =
			frame_type_for(request.direction, request.confirmed);
		const uint32_t frame_counter = next_counter(request.direction);
		const std::array<uint8_t, 16> &payload_key =
			request.fport == 0 ? config_.nwk_skey : config_.app_skey;
		const std::vector<uint8_t> encrypted_payload =
			crypt_payload(request.payload, payload_key,
				      request.direction, config_.dev_addr,
				      frame_counter);

		std::vector<uint8_t> frame;

		frame.reserve(1 + 7 + request.fopts.size() +
			      (request.payload.empty() ? 0 : 1 + request.payload.size()) +
			      kLoraMicBytes);
		frame.push_back(static_cast<uint8_t>(
			(static_cast<uint8_t>(type) << 5U) | kLoraMajorVersion));
		write_u32_le(&frame, config_.dev_addr);
		frame.push_back(static_cast<uint8_t>((request.fctrl & 0xf0U) |
						     request.fopts.size()));
		write_u16_le(&frame, static_cast<uint16_t>(frame_counter & 0xffffU));
		frame.insert(frame.end(), request.fopts.begin(), request.fopts.end());
		if (!request.payload.empty()) {
			frame.push_back(request.fport);
			frame.insert(frame.end(), encrypted_payload.begin(),
				     encrypted_payload.end());
		}
		if (frame.size() + kLoraMicBytes > kLoraMaxPhyPayloadBytes) {
			last_error_ = "LoRaWAN PHYPayload exceeds 255 bytes";
			return LoraStatus::kInvalidArgument;
		}

		const std::array<uint8_t, 4> mic =
			mic_for(frame, config_.nwk_skey, request.direction,
				config_.dev_addr, frame_counter);
		frame.insert(frame.end(), mic.begin(), mic.end());

		*phy_payload = std::move(frame);
		advance_counter(request.direction);
		last_error_.clear();
		return LoraStatus::kOk;
	}

	LoraStatus parse(const std::vector<uint8_t> &phy_payload,
			 LoraFrame *parsed)
	{
		if (parsed == nullptr) {
			last_error_ = "frame is required";
			return LoraStatus::kInvalidArgument;
		}
		if (phy_payload.size() < kLoraMinimumDataFrameBytes) {
			last_error_ = "PHYPayload is too short";
			return LoraStatus::kFrameTooShort;
		}

		const uint8_t mhdr = phy_payload[0];
		const LoraFrameType type =
			static_cast<LoraFrameType>((mhdr >> 5U) & 0x07U);
		LoraDirection direction {};

		if ((mhdr & 0x03U) != kLoraMajorVersion ||
		    !direction_for_type(type, &direction)) {
			last_error_ = "unsupported LoRaWAN frame type";
			return LoraStatus::kUnsupportedFrame;
		}

		const uint8_t fctrl = phy_payload[5];
		const size_t fopts_len = fctrl & 0x0fU;
		const size_t fopts_end = 8 + fopts_len;

		if (phy_payload.size() < fopts_end + kLoraMicBytes) {
			last_error_ = "PHYPayload FHDR is truncated";
			return LoraStatus::kFrameTooShort;
		}

		const uint32_t dev_addr = read_u32_le(phy_payload, 1);
		const uint16_t fcnt16 = read_u16_le(phy_payload, 6);
		const uint32_t frame_counter =
			reconstruct_frame_counter(fcnt16, next_counter(direction));
		const size_t mic_offset = phy_payload.size() - kLoraMicBytes;
		const std::vector<uint8_t> message(phy_payload.begin(),
						   phy_payload.begin() +
							   static_cast<std::ptrdiff_t>(mic_offset));
		const std::array<uint8_t, 4> expected =
			mic_for(message, config_.nwk_skey, direction, dev_addr,
				frame_counter);

		if (!std::equal(expected.begin(), expected.end(),
				phy_payload.begin() + static_cast<std::ptrdiff_t>(mic_offset))) {
			last_error_ = "LoRaWAN MIC mismatch";
			return LoraStatus::kMicMismatch;
		}

		LoraFrame out;

		out.direction = direction;
		out.type = type;
		out.dev_addr = dev_addr;
		out.frame_counter = frame_counter;
		out.fctrl = static_cast<uint8_t>(fctrl & 0xf0U);
		out.fopts.assign(phy_payload.begin() + 8,
				 phy_payload.begin() +
					 static_cast<std::ptrdiff_t>(fopts_end));
		if (fopts_end < mic_offset) {
			out.has_fport = true;
			out.fport = phy_payload[fopts_end];
			const std::array<uint8_t, 16> &payload_key =
				out.fport == 0 ? config_.nwk_skey : config_.app_skey;
			const std::vector<uint8_t> encrypted_payload(
				phy_payload.begin() +
					static_cast<std::ptrdiff_t>(fopts_end + 1),
				phy_payload.begin() +
					static_cast<std::ptrdiff_t>(mic_offset));

			out.payload = crypt_payload(encrypted_payload, payload_key,
						    direction, dev_addr,
						    frame_counter);
		}

		*parsed = std::move(out);
		set_next_counter(direction, frame_counter + 1);
		last_error_.clear();
		return LoraStatus::kOk;
	}

	const LoraSessionConfig &config() const { return config_; }
	const std::string &last_error() const { return last_error_; }

private:
	uint32_t next_counter(LoraDirection direction) const
	{
		return direction == LoraDirection::kUplink ?
			config_.uplink_frame_counter :
			config_.downlink_frame_counter;
	}

	void set_next_counter(LoraDirection direction, uint32_t value)
	{
		if (direction == LoraDirection::kUplink)
			config_.uplink_frame_counter = value;
		else
			config_.downlink_frame_counter = value;
	}

	void advance_counter(LoraDirection direction)
	{
		set_next_counter(direction, next_counter(direction) + 1);
	}

	LoraSessionConfig config_;
	std::string last_error_;
};

LoraFramer::LoraFramer() : impl_(std::make_unique<Impl>())
{
}

LoraFramer::LoraFramer(LoraSessionConfig config)
	: impl_(std::make_unique<Impl>(std::move(config)))
{
}

LoraFramer::~LoraFramer() = default;
LoraFramer::LoraFramer(LoraFramer &&) noexcept = default;
LoraFramer &LoraFramer::operator=(LoraFramer &&) noexcept = default;

LoraStatus LoraFramer::frame(const LoraFrameRequest &request,
			     std::vector<uint8_t> *phy_payload)
{
	return impl_->frame(request, phy_payload);
}

LoraStatus LoraFramer::parse(const std::vector<uint8_t> &phy_payload,
			     LoraFrame *frame)
{
	return impl_->parse(phy_payload, frame);
}

const LoraSessionConfig &LoraFramer::config() const
{
	return impl_->config();
}

const std::string &LoraFramer::lastError() const
{
	return impl_->last_error();
}

const char *toString(LoraStatus status)
{
	switch (status) {
	case LoraStatus::kOk:
		return "ok";
	case LoraStatus::kInvalidArgument:
		return "invalid_argument";
	case LoraStatus::kFrameTooShort:
		return "frame_too_short";
	case LoraStatus::kUnsupportedFrame:
		return "unsupported_frame";
	case LoraStatus::kMicMismatch:
		return "mic_mismatch";
	}

	return "unknown";
}

#if defined(OMNISIGHT_UAV_RF_LORA_PACKET_FRAMING_SMOKE_MAIN)
static bool smoke_crypto_vectors()
{
	const std::array<uint8_t, 16> aes_key {
		0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07,
		0x08, 0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x0e, 0x0f,
	};
	const std::array<uint8_t, 16> aes_plain {
		0x00, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77,
		0x88, 0x99, 0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff,
	};
	const std::array<uint8_t, 16> aes_expected {
		0x69, 0xc4, 0xe0, 0xd8, 0x6a, 0x7b, 0x04, 0x30,
		0xd8, 0xcd, 0xb7, 0x80, 0x70, 0xb4, 0xc5, 0x5a,
	};
	const std::array<uint8_t, 16> cmac_key {
		0x2b, 0x7e, 0x15, 0x16, 0x28, 0xae, 0xd2, 0xa6,
		0xab, 0xf7, 0x15, 0x88, 0x09, 0xcf, 0x4f, 0x3c,
	};
	const std::vector<uint8_t> cmac_message {
		0x6b, 0xc1, 0xbe, 0xe2, 0x2e, 0x40, 0x9f, 0x96,
		0xe9, 0x3d, 0x7e, 0x11, 0x73, 0x93, 0x17, 0x2a,
	};
	const std::array<uint8_t, 16> cmac_expected {
		0x07, 0x0a, 0x16, 0xb4, 0x6b, 0x4d, 0x41, 0x44,
		0xf7, 0x9b, 0xdd, 0x9d, 0xd0, 0x4a, 0x28, 0x7c,
	};

	return aes_encrypt_block(aes_plain, aes_key) == aes_expected &&
	       aes_cmac(cmac_message, cmac_key) == cmac_expected;
}
#endif

} // namespace omnisight::embedded::uav::rf

#if defined(OMNISIGHT_UAV_RF_LORA_PACKET_FRAMING_SMOKE_MAIN)
int main()
{
	using omnisight::embedded::uav::rf::LoraDirection;
	using omnisight::embedded::uav::rf::LoraFrame;
	using omnisight::embedded::uav::rf::LoraFrameRequest;
	using omnisight::embedded::uav::rf::LoraFramer;
	using omnisight::embedded::uav::rf::LoraSessionConfig;
	using omnisight::embedded::uav::rf::LoraStatus;

	assert(omnisight::embedded::uav::rf::smoke_crypto_vectors());

	LoraSessionConfig config;
	config.dev_addr = 0x26011bda;
	config.nwk_skey = { 0x2b, 0x7e, 0x15, 0x16, 0x28, 0xae, 0xd2, 0xa6,
			    0xab, 0xf7, 0x15, 0x88, 0x09, 0xcf, 0x4f, 0x3c };
	config.app_skey = { 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07,
			    0x08, 0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x0e, 0x0f };

	LoraFramer uplink(config);
	LoraFrameRequest request;
	request.direction = LoraDirection::kUplink;
	request.fport = 2;
	request.payload = { 0x10, 0x42, 0x7f, 0x00, 0x55 };

	std::vector<uint8_t> phy_payload;
	assert(uplink.frame(request, &phy_payload) == LoraStatus::kOk);
	assert(phy_payload.size() == 1 + 7 + 1 + request.payload.size() + 4);
	assert(phy_payload[0] == 0x40);
	assert(uplink.config().uplink_frame_counter == 1);

	LoraFramer parser(config);
	LoraFrame parsed;
	assert(parser.parse(phy_payload, &parsed) == LoraStatus::kOk);
	assert(parsed.direction == LoraDirection::kUplink);
	assert(parsed.dev_addr == config.dev_addr);
	assert(parsed.frame_counter == 0);
	assert(parsed.has_fport);
	assert(parsed.fport == request.fport);
	assert(parsed.payload == request.payload);

	phy_payload.back() ^= 0x01;
	assert(parser.parse(phy_payload, &parsed) == LoraStatus::kMicMismatch);
	return 0;
}
#endif
