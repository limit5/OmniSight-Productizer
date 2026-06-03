/* SPDX-License-Identifier: MIT
 *
 * ALSA device enumeration for Case 5 audio/video pairing (OP-1990).
 *
 * This mirrors the existing sysfs-first V4L2 discovery style: enumerate
 * sound cards, attach capture/playback PCM nodes, and copy USB VID/PID when
 * sysfs exposes them. Pairing with V4L2 devices belongs to C5.E.2.
 */
#include "probe_alsa_devices.h"

#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int copy_text_file(const char *path, char *buf, size_t buf_len)
{
	FILE *file;
	size_t len;

	if (buf_len == 0)
		return -EINVAL;

	file = fopen(path, "r");
	if (!file)
		return -errno;

	if (!fgets(buf, buf_len, file)) {
		int saved_errno = ferror(file) ? errno : ENODATA;

		fclose(file);
		return -saved_errno;
	}

	fclose(file);

	len = strcspn(buf, "\r\n");
	buf[len] = '\0';
	return 0;
}

static int make_child_path(char *buf, size_t buf_len, const char *base,
			   const char *child)
{
	int written;

	written = snprintf(buf, buf_len, "%s/%s", base, child);
	if (written < 0 || (size_t)written >= buf_len)
		return -ENAMETOOLONG;

	return 0;
}

static int make_dev_node(char *buf, size_t buf_len, const char *node)
{
	return make_child_path(buf, buf_len, PROBE_ALSA_DEV_NODE_ROOT, node);
}

static int has_card_prefix(const char *name)
{
	size_t i;

	if (strncmp(name, "card", 4) != 0)
		return 0;

	for (i = 4; name[i] != '\0'; i++) {
		if (!isdigit((unsigned char)name[i]))
			return 0;
	}

	return i > 4;
}

static int parse_pcm_node(const char *name, char *card_id, size_t card_id_len,
			  char *direction)
{
	const char *cursor;
	const char *card_start;
	size_t card_digits;
	int written;

	if (strncmp(name, "pcmC", 4) != 0)
		return -EINVAL;

	cursor = name + 4;
	card_start = cursor;
	while (isdigit((unsigned char)*cursor))
		cursor++;
	card_digits = (size_t)(cursor - card_start);
	if (card_digits == 0 || *cursor != 'D')
		return -EINVAL;

	cursor++;
	if (!isdigit((unsigned char)*cursor))
		return -EINVAL;
	while (isdigit((unsigned char)*cursor))
		cursor++;

	if ((*cursor != 'c' && *cursor != 'p') || cursor[1] != '\0')
		return -EINVAL;

	written = snprintf(card_id, card_id_len, "card%.*s",
			   (int)card_digits, card_start);
	if (written < 0 || (size_t)written >= card_id_len)
		return -ENAMETOOLONG;

	*direction = *cursor;
	return 0;
}

static int find_device_index(const struct probe_alsa_device_list *list,
			     const char *card_id)
{
	size_t i;

	for (i = 0; i < list->count; i++) {
		if (strcmp(list->devices[i].card_id, card_id) == 0)
			return (int)i;
	}

	return -ENOENT;
}

static struct probe_alsa_device *add_device(struct probe_alsa_device_list *list,
					    const char *card_id)
{
	struct probe_alsa_device *device;
	size_t card_len;

	if (list->count >= PROBE_ALSA_MAX_DEVICES) {
		fprintf(stderr,
			"probe_alsa_devices: device limit reached at %zu\n",
			list->count);
		return NULL;
	}

	card_len = strlen(card_id);
	if (card_len >= PROBE_ALSA_MAX_CARD_ID) {
		fprintf(stderr,
			"probe_alsa_devices: skipping overlong card %s\n",
			card_id);
		return NULL;
	}

	device = &list->devices[list->count];
	memset(device, 0, sizeof(*device));
	memcpy(device->card_id, card_id, card_len + 1);
	snprintf(device->vendor_id, sizeof(device->vendor_id), "unknown");
	snprintf(device->product_id, sizeof(device->product_id), "unknown");
	list->count++;

	return device;
}

static struct probe_alsa_device *get_or_add_device(
	struct probe_alsa_device_list *list, const char *card_id)
{
	int index;

	index = find_device_index(list, card_id);
	if (index >= 0)
		return &list->devices[index];

	return add_device(list, card_id);
}

static int read_device_id_from_base(const char *base, const char *id_name,
				    char *buf, size_t buf_len)
{
	char path[PROBE_ALSA_MAX_PATH];
	const char *candidates[] = {
		"device/idVendor",
		"device/../idVendor",
		"device/../../idVendor",
		"device/../../../idVendor",
	};
	size_t i;

	if (strcmp(id_name, "idProduct") == 0) {
		candidates[0] = "device/idProduct";
		candidates[1] = "device/../idProduct";
		candidates[2] = "device/../../idProduct";
		candidates[3] = "device/../../../idProduct";
	}

	for (i = 0; i < sizeof(candidates) / sizeof(candidates[0]); i++) {
		if (make_child_path(path, sizeof(path), base, candidates[i]) != 0)
			continue;
		if (copy_text_file(path, buf, buf_len) == 0)
			return 0;
	}

	return -ENOENT;
}

static void read_device_ids(const char *sysfs_root,
			    struct probe_alsa_device *device)
{
	char base[PROBE_ALSA_MAX_PATH];

	if (make_child_path(base, sizeof(base), sysfs_root,
			    device->card_id) == 0) {
		read_device_id_from_base(base, "idVendor", device->vendor_id,
					 sizeof(device->vendor_id));
		read_device_id_from_base(base, "idProduct", device->product_id,
					 sizeof(device->product_id));
	}
}

static void attach_pcm_node(struct probe_alsa_device *device,
			    const char *node_name, char direction)
{
	char *target;

	if (direction == 'c') {
		if (device->pcm_capture_node[0] != '\0')
			return;
		target = device->pcm_capture_node;
	} else {
		if (device->pcm_playback_node[0] != '\0')
			return;
		target = device->pcm_playback_node;
	}

	if (make_dev_node(target, PROBE_ALSA_MAX_NODE, node_name) != 0) {
		fprintf(stderr,
			"probe_alsa_devices: skipping overlong PCM node %s\n",
			node_name);
	}
}

int probe_alsa_devices_at(const char *sysfs_root,
			  struct probe_alsa_device_list *list)
{
	DIR *dir;
	struct dirent *entry;
	int rc;

	if (!sysfs_root || !list)
		return -EINVAL;

	memset(list, 0, sizeof(*list));

	dir = opendir(sysfs_root);
	if (!dir) {
		fprintf(stderr, "probe_alsa_devices: sysfs root %s unavailable: %s\n",
			sysfs_root, strerror(errno));
		return -errno;
	}

	while ((entry = readdir(dir)) != NULL) {
		struct probe_alsa_device *device;

		if (!has_card_prefix(entry->d_name))
			continue;

		device = get_or_add_device(list, entry->d_name);
		if (!device)
			continue;

		read_device_ids(sysfs_root, device);
	}

	rewinddir(dir);
	while ((entry = readdir(dir)) != NULL) {
		struct probe_alsa_device *device;
		char card_id[PROBE_ALSA_MAX_CARD_ID];
		char direction = '\0';

		rc = parse_pcm_node(entry->d_name, card_id, sizeof(card_id),
				    &direction);
		if (rc != 0)
			continue;

		device = get_or_add_device(list, card_id);
		if (!device)
			continue;

		attach_pcm_node(device, entry->d_name, direction);
		read_device_ids(sysfs_root, device);
	}

	closedir(dir);
	return 0;
}

int probe_alsa_devices(struct probe_alsa_device_list *list)
{
	return probe_alsa_devices_at(PROBE_ALSA_SYSFS_ROOT, list);
}
