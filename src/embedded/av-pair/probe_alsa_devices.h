/* SPDX-License-Identifier: MIT
 *
 * ALSA device enumeration for Case 5 audio/video pairing (OP-1990).
 */
#ifndef AV_PAIR_PROBE_ALSA_DEVICES_H
#define AV_PAIR_PROBE_ALSA_DEVICES_H

#include <stddef.h>

#define PROBE_ALSA_SYSFS_ROOT "/sys/class/sound"
#define PROBE_ALSA_DEV_NODE_ROOT "/dev/snd"
#define PROBE_ALSA_MAX_DEVICES 32
#define PROBE_ALSA_MAX_CARD_ID 32
#define PROBE_ALSA_MAX_NODE 64
#define PROBE_ALSA_MAX_PATH 256
#define PROBE_ALSA_MAX_ID 16

struct probe_alsa_device {
	char card_id[PROBE_ALSA_MAX_CARD_ID];
	char pcm_capture_node[PROBE_ALSA_MAX_NODE];
	char pcm_playback_node[PROBE_ALSA_MAX_NODE];
	char vendor_id[PROBE_ALSA_MAX_ID];
	char product_id[PROBE_ALSA_MAX_ID];
};

struct probe_alsa_device_list {
	struct probe_alsa_device devices[PROBE_ALSA_MAX_DEVICES];
	size_t count;
};

int probe_alsa_devices(struct probe_alsa_device_list *list);
int probe_alsa_devices_at(const char *sysfs_root,
			  struct probe_alsa_device_list *list);

#endif /* AV_PAIR_PROBE_ALSA_DEVICES_H */
