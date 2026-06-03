// SPDX-License-Identifier: MIT
/*
 * Case 5 UAC2 ALSA PCM wiring (OP-1988).
 *
 * This module owns only ALSA-side PCM exposure for the UAC2 gadget. ConfigFS
 * endpoint creation remains in C5.A.1; callers pass the selected endpoints to
 * omnisight_uac_alsa_bind().
 */
#include "alsa-wiring.h"

#include <linux/errno.h>
#include <linux/device.h>
#include <linux/jiffies.h>
#include <linux/list.h>
#include <linux/math64.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/string.h>

#include <sound/core.h>
#include <sound/initval.h>
#include <sound/pcm.h>

#define OMNISIGHT_UAC_DRIVER "omnisight-uac-alsa"
#define OMNISIGHT_UAC_PCM_NAME "uac2-duplex"

struct omnisight_uac_alsa {
	struct list_head node;
	struct device *dev;
	struct snd_card *card;
	struct snd_pcm *pcm;
	struct omnisight_uac_alsa_config config;
	const struct omnisight_uac_codec_hook *codec_hook;
	unsigned long stream_start[2];
};

static DEFINE_MUTEX(codec_hook_lock);
static const struct omnisight_uac_codec_hook *registered_codec_hook;
static DEFINE_MUTEX(state_lock);
static LIST_HEAD(state_list);

static const struct snd_pcm_hardware omnisight_uac_pcm_hw = {
	.info = SNDRV_PCM_INFO_INTERLEAVED |
		SNDRV_PCM_INFO_BLOCK_TRANSFER |
		SNDRV_PCM_INFO_MMAP |
		SNDRV_PCM_INFO_MMAP_VALID,
	.formats = SNDRV_PCM_FMTBIT_S16_LE,
	.rates = SNDRV_PCM_RATE_48000,
	.rate_min = OMNISIGHT_UAC_ALSA_DEFAULT_RATE,
	.rate_max = OMNISIGHT_UAC_ALSA_DEFAULT_RATE,
	.channels_min = OMNISIGHT_UAC_ALSA_DEFAULT_CHANNELS,
	.channels_max = OMNISIGHT_UAC_ALSA_DEFAULT_CHANNELS,
	.buffer_bytes_max = OMNISIGHT_UAC_ALSA_DEFAULT_BUFFER_BYTES,
	.period_bytes_min = OMNISIGHT_UAC_ALSA_DEFAULT_PERIOD_BYTES,
	.period_bytes_max = OMNISIGHT_UAC_ALSA_DEFAULT_PERIOD_BYTES,
	.periods_min = 2,
	.periods_max = 16,
};

static unsigned int config_rate(const struct omnisight_uac_alsa_config *config)
{
	return config->rate ? config->rate : OMNISIGHT_UAC_ALSA_DEFAULT_RATE;
}

static unsigned int config_channels(
	const struct omnisight_uac_alsa_config *config)
{
	return config->channels ? config->channels :
				  OMNISIGHT_UAC_ALSA_DEFAULT_CHANNELS;
}

static unsigned int config_sample_bits(
	const struct omnisight_uac_alsa_config *config)
{
	return config->sample_bits ? config->sample_bits :
				     OMNISIGHT_UAC_ALSA_DEFAULT_SAMPLE_BITS;
}

static enum omnisight_uac_codec config_codec(
	const struct omnisight_uac_alsa_config *config)
{
	return config->codec ? config->codec : OMNISIGHT_UAC_ALSA_DEFAULT_CODEC;
}

static bool endpoint_valid(const struct omnisight_uac_endpoint *endpoint,
			   bool capture)
{
	if (!endpoint || !endpoint->name || !endpoint->max_packet_size)
		return false;

	return endpoint->capture == capture;
}

static bool config_valid(const struct omnisight_uac_alsa_config *config)
{
	if (!config)
		return false;
	if (!endpoint_valid(config->capture_ep, true))
		return false;
	if (!endpoint_valid(config->playback_ep, false))
		return false;
	if (config_rate(config) != OMNISIGHT_UAC_ALSA_DEFAULT_RATE)
		return false;
	if (config_channels(config) != OMNISIGHT_UAC_ALSA_DEFAULT_CHANNELS)
		return false;
	if (config_sample_bits(config) != OMNISIGHT_UAC_ALSA_DEFAULT_SAMPLE_BITS)
		return false;

	return true;
}

static const struct omnisight_uac_codec_hook *find_codec_hook(
	enum omnisight_uac_codec codec)
{
	const struct omnisight_uac_codec_hook *hook;

	mutex_lock(&codec_hook_lock);
	hook = registered_codec_hook;
	if (hook && hook->codec != codec)
		hook = NULL;
	mutex_unlock(&codec_hook_lock);

	return hook;
}

static struct omnisight_uac_alsa *find_state_locked(struct device *dev)
{
	struct omnisight_uac_alsa *state;

	list_for_each_entry(state, &state_list, node) {
		if (state->dev == dev)
			return state;
	}

	return NULL;
}

static int codec_startup(struct omnisight_uac_alsa *state)
{
	const struct omnisight_uac_codec_hook *hook;
	int ret;

	hook = find_codec_hook(config_codec(&state->config));
	if (!hook) {
		if (config_codec(&state->config) == OMNISIGHT_UAC_CODEC_NONE)
			return 0;
		return -ENODEV;
	}

	if (hook->startup) {
		ret = hook->startup(state->dev);
		if (ret)
			return ret;
	}

	if (hook->set_hw_params) {
		ret = hook->set_hw_params(state->dev, config_rate(&state->config),
					  config_channels(&state->config),
					  config_sample_bits(&state->config));
		if (ret) {
			if (hook->shutdown)
				hook->shutdown(state->dev);
			return ret;
		}
	}

	state->codec_hook = hook;
	return 0;
}

static void codec_shutdown(struct omnisight_uac_alsa *state)
{
	if (state->codec_hook && state->codec_hook->shutdown)
		state->codec_hook->shutdown(state->dev);
	state->codec_hook = NULL;
}

static int uac_pcm_open(struct snd_pcm_substream *substream)
{
	struct omnisight_uac_alsa *state = snd_pcm_substream_chip(substream);
	struct snd_pcm_runtime *runtime = substream->runtime;

	runtime->hw = omnisight_uac_pcm_hw;
	runtime->hw.rate_min = config_rate(&state->config);
	runtime->hw.rate_max = config_rate(&state->config);
	runtime->hw.channels_min = config_channels(&state->config);
	runtime->hw.channels_max = config_channels(&state->config);

	return snd_pcm_hw_constraint_integer(runtime,
					     SNDRV_PCM_HW_PARAM_PERIODS);
}

static int uac_pcm_hw_params(struct snd_pcm_substream *substream,
			     struct snd_pcm_hw_params *params)
{
	return snd_pcm_lib_malloc_pages(substream, params_buffer_bytes(params));
}

static int uac_pcm_hw_free(struct snd_pcm_substream *substream)
{
	return snd_pcm_lib_free_pages(substream);
}

static int uac_pcm_prepare(struct snd_pcm_substream *substream)
{
	struct omnisight_uac_alsa *state = snd_pcm_substream_chip(substream);

	state->stream_start[substream->stream] = jiffies;
	return 0;
}

static int uac_pcm_trigger(struct snd_pcm_substream *substream, int cmd)
{
	struct omnisight_uac_alsa *state = snd_pcm_substream_chip(substream);

	switch (cmd) {
	case SNDRV_PCM_TRIGGER_START:
	case SNDRV_PCM_TRIGGER_RESUME:
		state->stream_start[substream->stream] = jiffies;
		return 0;
	case SNDRV_PCM_TRIGGER_STOP:
	case SNDRV_PCM_TRIGGER_SUSPEND:
		return 0;
	default:
		return -EINVAL;
	}
}

static snd_pcm_uframes_t uac_pcm_pointer(struct snd_pcm_substream *substream)
{
	struct omnisight_uac_alsa *state = snd_pcm_substream_chip(substream);
	struct snd_pcm_runtime *runtime = substream->runtime;
	unsigned long elapsed;
	u64 frames;

	elapsed = jiffies - state->stream_start[substream->stream];
	frames = (u64)elapsed * runtime->rate;
	do_div(frames, HZ);

	return frames % runtime->buffer_size;
}

static const struct snd_pcm_ops uac_pcm_ops = {
	.open = uac_pcm_open,
	.ioctl = snd_pcm_lib_ioctl,
	.hw_params = uac_pcm_hw_params,
	.hw_free = uac_pcm_hw_free,
	.prepare = uac_pcm_prepare,
	.trigger = uac_pcm_trigger,
	.pointer = uac_pcm_pointer,
};

static void init_card_names(struct omnisight_uac_alsa *state)
{
	const char *name = state->config.card_name ?: "OmniSight UAC2";

	strscpy(state->card->driver, OMNISIGHT_UAC_DRIVER,
		sizeof(state->card->driver));
	strscpy(state->card->shortname, name, sizeof(state->card->shortname));
	snprintf(state->card->longname, sizeof(state->card->longname),
		 "%s capture=%s playback=%s",
		 name, state->config.capture_ep->name,
		 state->config.playback_ep->name);
}

int omnisight_uac_alsa_register_codec_hook(
	const struct omnisight_uac_codec_hook *hook)
{
	int ret = 0;

	if (!hook || hook->codec == OMNISIGHT_UAC_CODEC_NONE)
		return -EINVAL;

	mutex_lock(&codec_hook_lock);
	if (registered_codec_hook)
		ret = -EBUSY;
	else
		registered_codec_hook = hook;
	mutex_unlock(&codec_hook_lock);

	return ret;
}
EXPORT_SYMBOL_GPL(omnisight_uac_alsa_register_codec_hook);

void omnisight_uac_alsa_unregister_codec_hook(
	const struct omnisight_uac_codec_hook *hook)
{
	mutex_lock(&codec_hook_lock);
	if (registered_codec_hook == hook)
		registered_codec_hook = NULL;
	mutex_unlock(&codec_hook_lock);
}
EXPORT_SYMBOL_GPL(omnisight_uac_alsa_unregister_codec_hook);

int omnisight_uac_alsa_bind(struct device *dev,
			    const struct omnisight_uac_alsa_config *config)
{
	struct omnisight_uac_alsa *state;
	struct snd_card *card;
	int ret;

	if (!dev || !config_valid(config))
		return -EINVAL;

	mutex_lock(&state_lock);
	if (find_state_locked(dev)) {
		mutex_unlock(&state_lock);
		return -EBUSY;
	}
	mutex_unlock(&state_lock);

	ret = snd_card_new(dev, -1, NULL, THIS_MODULE, sizeof(*state), &card);
	if (ret)
		return ret;

	state = card->private_data;
	INIT_LIST_HEAD(&state->node);
	state->dev = dev;
	state->card = card;
	state->config = *config;

	ret = codec_startup(state);
	if (ret)
		goto free_card;

	init_card_names(state);

	ret = snd_pcm_new(state->card, OMNISIGHT_UAC_PCM_NAME, 0, 1, 1,
			  &state->pcm);
	if (ret)
		goto shutdown_codec;

	state->pcm->private_data = state;
	strscpy(state->pcm->name, OMNISIGHT_UAC_PCM_NAME,
		sizeof(state->pcm->name));
	snd_pcm_set_ops(state->pcm, SNDRV_PCM_STREAM_PLAYBACK, &uac_pcm_ops);
	snd_pcm_set_ops(state->pcm, SNDRV_PCM_STREAM_CAPTURE, &uac_pcm_ops);
	snd_pcm_set_managed_buffer_all(state->pcm, SNDRV_DMA_TYPE_VMALLOC,
				       NULL,
				       OMNISIGHT_UAC_ALSA_DEFAULT_BUFFER_BYTES,
				       OMNISIGHT_UAC_ALSA_DEFAULT_BUFFER_BYTES);

	ret = snd_card_register(state->card);
	if (ret)
		goto shutdown_codec;

	mutex_lock(&state_lock);
	list_add(&state->node, &state_list);
	mutex_unlock(&state_lock);
	dev_info(dev, "registered UAC2 ALSA PCM at %u Hz/%u-bit/%u ch\n",
		 config_rate(&state->config),
		 config_sample_bits(&state->config),
		 config_channels(&state->config));
	return 0;

shutdown_codec:
	codec_shutdown(state);
free_card:
	snd_card_free(state->card);
	return ret;
}
EXPORT_SYMBOL_GPL(omnisight_uac_alsa_bind);

void omnisight_uac_alsa_unbind(struct device *dev)
{
	struct omnisight_uac_alsa *state;

	if (!dev)
		return;

	mutex_lock(&state_lock);
	state = find_state_locked(dev);
	if (state)
		list_del_init(&state->node);
	mutex_unlock(&state_lock);
	if (!state)
		return;

	codec_shutdown(state);
	snd_card_free(state->card);
}
EXPORT_SYMBOL_GPL(omnisight_uac_alsa_unbind);

MODULE_DESCRIPTION("OmniSight UAC2 ALSA PCM wiring");
MODULE_AUTHOR("GPT-5.5 (codex-cli)");
MODULE_LICENSE("Dual MIT/GPL");

static int __init omnisight_uac_alsa_init(void)
{
	pr_info("%s: ALSA PCM wiring loaded\n", OMNISIGHT_UAC_DRIVER);
	return 0;
}

static void __exit omnisight_uac_alsa_exit(void)
{
	pr_info("%s: ALSA PCM wiring unloaded\n", OMNISIGHT_UAC_DRIVER);
}

module_init(omnisight_uac_alsa_init);
module_exit(omnisight_uac_alsa_exit);
