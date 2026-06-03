/* SPDX-License-Identifier: MIT
 *
 * USB topology pairing for Case 5 audio/video devices (OP-1993).
 */
#ifndef AV_PAIR_USB_TREE_PAIRING_H
#define AV_PAIR_USB_TREE_PAIRING_H

#include <stddef.h>

#include "probe_alsa_devices.h"

#define USB_TREE_PAIRING_SYSFS_ROOT "/sys/bus/usb/devices"
#define USB_TREE_PAIRING_MAX_NODE 64
#define USB_TREE_PAIRING_MAX_PATH 512
#define USB_TREE_PAIRING_MAX_USB_ID 64
#define USB_TREE_PAIRING_MAX_REASON 128

struct usb_tree_video_device {
	char video_node[USB_TREE_PAIRING_MAX_NODE];
	char sysfs_path[USB_TREE_PAIRING_MAX_PATH];
};

typedef struct paired_devices {
	int paired;
	char video_node[USB_TREE_PAIRING_MAX_NODE];
	char audio_node[USB_TREE_PAIRING_MAX_NODE];
	char usb_device[USB_TREE_PAIRING_MAX_USB_ID];
	char usb_parent[USB_TREE_PAIRING_MAX_USB_ID];
	char reason[USB_TREE_PAIRING_MAX_REASON];
} paired_devices_t;

int usb_tree_pair_audio_video(const struct usb_tree_video_device *video,
			      const struct probe_alsa_device *audio,
			      paired_devices_t *pairing);
int usb_tree_pair_audio_video_at(const char *usb_sysfs_root,
				 const struct usb_tree_video_device *video,
				 const struct probe_alsa_device *audio,
				 paired_devices_t *pairing);

#endif /* AV_PAIR_USB_TREE_PAIRING_H */
