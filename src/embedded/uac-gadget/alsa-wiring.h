/* SPDX-License-Identifier: MIT
 *
 * Case 5 UAC2 ALSA PCM wiring (OP-1988).
 */
#ifndef OMNISIGHT_UAC_GADGET_ALSA_WIRING_H
#define OMNISIGHT_UAC_GADGET_ALSA_WIRING_H

#include <linux/kconfig.h>
#include <linux/types.h>

#define OMNISIGHT_UAC_ALSA_DEFAULT_RATE 48000U
#define OMNISIGHT_UAC_ALSA_DEFAULT_CHANNELS 2U
#define OMNISIGHT_UAC_ALSA_DEFAULT_SAMPLE_BITS 16U
#define OMNISIGHT_UAC_ALSA_DEFAULT_BUFFER_BYTES (64U * 1024U)
#define OMNISIGHT_UAC_ALSA_DEFAULT_PERIOD_BYTES 4096U
#if IS_ENABLED(CONFIG_OMNISIGHT_UAC_GADGET_CODEC_ES8388)
#define OMNISIGHT_UAC_ALSA_DEFAULT_CODEC OMNISIGHT_UAC_CODEC_ES8388
#elif IS_ENABLED(CONFIG_OMNISIGHT_UAC_GADGET_CODEC_RT5640)
#define OMNISIGHT_UAC_ALSA_DEFAULT_CODEC OMNISIGHT_UAC_CODEC_RT5640
#else
#define OMNISIGHT_UAC_ALSA_DEFAULT_CODEC OMNISIGHT_UAC_CODEC_NONE
#endif

enum omnisight_uac_codec {
	OMNISIGHT_UAC_CODEC_NONE = 0,
	OMNISIGHT_UAC_CODEC_ES8388,
	OMNISIGHT_UAC_CODEC_RT5640,
	OMNISIGHT_UAC_CODEC_CUSTOM,
};

struct device;

struct omnisight_uac_endpoint {
	const char *name;
	u8 address;
	u16 max_packet_size;
	bool capture;
};

struct omnisight_uac_codec_hook {
	enum omnisight_uac_codec codec;
	int (*startup)(struct device *dev);
	void (*shutdown)(struct device *dev);
	int (*set_hw_params)(struct device *dev, unsigned int rate,
			     unsigned int channels, unsigned int sample_bits);
};

struct omnisight_uac_alsa_config {
	const char *card_name;
	const struct omnisight_uac_endpoint *capture_ep;
	const struct omnisight_uac_endpoint *playback_ep;
	enum omnisight_uac_codec codec;
	unsigned int rate;
	unsigned int channels;
	unsigned int sample_bits;
};

int omnisight_uac_alsa_register_codec_hook(
	const struct omnisight_uac_codec_hook *hook);
void omnisight_uac_alsa_unregister_codec_hook(
	const struct omnisight_uac_codec_hook *hook);
int omnisight_uac_alsa_bind(struct device *dev,
			    const struct omnisight_uac_alsa_config *config);
void omnisight_uac_alsa_unbind(struct device *dev);

#endif /* OMNISIGHT_UAC_GADGET_ALSA_WIRING_H */
