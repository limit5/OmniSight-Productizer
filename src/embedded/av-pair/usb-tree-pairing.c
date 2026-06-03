/* SPDX-License-Identifier: MIT
 *
 * USB topology pairing for Case 5 audio/video devices (OP-1993).
 *
 * This walker consumes already-probed V4L2 and ALSA nodes. Enumeration stays
 * in C5.E.1; this file only decides whether the supplied nodes are co-located
 * in the USB topology.
 */
#define _XOPEN_SOURCE 700

#include "usb-tree-pairing.h"

#include <dirent.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#define USB_TREE_PAIRING_MAX_DEPTH 12

struct usb_tree_location {
	char node_path[USB_TREE_PAIRING_MAX_PATH];
	char usb_device[USB_TREE_PAIRING_MAX_USB_ID];
	char usb_parent[USB_TREE_PAIRING_MAX_USB_ID];
};

static const char *path_basename(const char *path)
{
	const char *slash;

	if (!path)
		return NULL;

	slash = strrchr(path, '/');
	return slash ? slash + 1 : path;
}

static int copy_string(char *dst, size_t dst_len, const char *src)
{
	int written;

	if (!dst || dst_len == 0 || !src)
		return -EINVAL;

	written = snprintf(dst, dst_len, "%s", src);
	if (written < 0 || (size_t)written >= dst_len)
		return -ENAMETOOLONG;

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

static int has_parent_component(const char *path, const char *component)
{
	const char *cursor = path;
	size_t len = strlen(component);

	while ((cursor = strstr(cursor, component)) != NULL) {
		if ((cursor == path || cursor[-1] == '/') &&
		    (cursor[len] == '/' || cursor[len] == '\0'))
			return 1;
		cursor += len;
	}

	return 0;
}

static int is_usb_device_name(const char *name)
{
	const char *dash;

	if (!name || strncmp(name, "usb", 3) == 0)
		return 0;

	dash = strchr(name, '-');
	return dash && dash != name;
}

static void strip_interface_suffix(char *name)
{
	char *colon = strchr(name, ':');

	if (colon)
		*colon = '\0';
}

static void usb_parent_name(const char *usb_device, char *parent,
			    size_t parent_len)
{
	const char *dash;
	const char *dot;
	size_t len;

	if (!usb_device || parent_len == 0)
		return;

	dot = strrchr(usb_device, '.');
	if (dot) {
		len = (size_t)(dot - usb_device);
		if (len >= parent_len)
			len = parent_len - 1;
		memcpy(parent, usb_device, len);
		parent[len] = '\0';
		return;
	}

	dash = strchr(usb_device, '-');
	if (!dash || dash == usb_device) {
		parent[0] = '\0';
		return;
	}

	len = (size_t)(dash - usb_device);
	if (len + 3 >= parent_len)
		len = parent_len - 4;
	snprintf(parent, parent_len, "usb%.*s", (int)len, usb_device);
}

static int extract_usb_location(const char *usb_sysfs_root,
				const char *node_path,
				struct usb_tree_location *location)
{
	const char *rel;
	const char *scan_path = node_path;
	char resolved[USB_TREE_PAIRING_MAX_PATH];
	char work[USB_TREE_PAIRING_MAX_PATH];
	char *cursor;
	int rc;

	if (!usb_sysfs_root || !node_path || !location)
		return -EINVAL;

	memset(location, 0, sizeof(*location));
	rc = copy_string(location->node_path, sizeof(location->node_path),
			 node_path);
	if (rc != 0)
		return rc;

	if (realpath(node_path, resolved))
		scan_path = resolved;

	if (strncmp(scan_path, usb_sysfs_root, strlen(usb_sysfs_root)) == 0) {
		rel = scan_path + strlen(usb_sysfs_root);
		if (*rel == '/')
			rel++;
	} else {
		rel = scan_path;
	}

	rc = copy_string(work, sizeof(work), rel);
	if (rc != 0)
		return rc;

	cursor = strtok(work, "/");
	while (cursor) {
		char candidate[USB_TREE_PAIRING_MAX_USB_ID];

		rc = copy_string(candidate, sizeof(candidate), cursor);
		if (rc != 0)
			return rc;
		strip_interface_suffix(candidate);
		if (is_usb_device_name(candidate))
			copy_string(location->usb_device,
				    sizeof(location->usb_device), candidate);

		cursor = strtok(NULL, "/");
	}

	if (location->usb_device[0] == '\0')
		return -ENOENT;

	usb_parent_name(location->usb_device, location->usb_parent,
			sizeof(location->usb_parent));
	return 0;
}

static int node_matches(const char *path, const char *name,
			const char *required_parent)
{
	const char *base = path_basename(path);

	if (!base || strcmp(base, name) != 0)
		return 0;
	if (!required_parent)
		return 1;

	return has_parent_component(path, required_parent);
}

static int find_named_node(const char *base, const char *name,
			   const char *required_parent, unsigned int depth,
			   char *result, size_t result_len)
{
	DIR *dir;
	struct dirent *entry;
	int saved_errno = ENOENT;

	if (depth > USB_TREE_PAIRING_MAX_DEPTH)
		return -ENOENT;

	if (node_matches(base, name, required_parent))
		return copy_string(result, result_len, base);

	dir = opendir(base);
	if (!dir)
		return -errno;

	while ((entry = readdir(dir)) != NULL) {
		char child[USB_TREE_PAIRING_MAX_PATH];
		struct stat st;
		int rc;

		if (strcmp(entry->d_name, ".") == 0 ||
		    strcmp(entry->d_name, "..") == 0)
			continue;

		rc = make_child_path(child, sizeof(child), base, entry->d_name);
		if (rc != 0) {
			saved_errno = -rc;
			continue;
		}
		if (stat(child, &st) != 0) {
			saved_errno = errno;
			continue;
		}
		if (!S_ISDIR(st.st_mode))
			continue;

		rc = find_named_node(child, name, required_parent, depth + 1,
				     result, result_len);
		if (rc == 0) {
			closedir(dir);
			return 0;
		}
		if (rc != -ENOENT)
			saved_errno = -rc;
	}

	closedir(dir);
	return -saved_errno;
}

static int find_video_location(const char *usb_sysfs_root,
			       const struct usb_tree_video_device *video,
			       struct usb_tree_location *location)
{
	const char *video_name;
	char path[USB_TREE_PAIRING_MAX_PATH];
	int rc;

	if (!video || video->video_node[0] == '\0')
		return -EINVAL;

	video_name = path_basename(video->video_node);
	if (!video_name || video_name[0] == '\0')
		return -EINVAL;

	if (video->sysfs_path[0] != '\0') {
		return extract_usb_location(usb_sysfs_root, video->sysfs_path,
					    location);
	}

	rc = find_named_node(usb_sysfs_root, video_name, "video4linux", 0,
			     path, sizeof(path));
	if (rc != 0)
		return rc;

	return extract_usb_location(usb_sysfs_root, path, location);
}

static int find_audio_location(const char *usb_sysfs_root,
			       const struct probe_alsa_device *audio,
			       struct usb_tree_location *location)
{
	char path[USB_TREE_PAIRING_MAX_PATH];
	int rc;

	if (!audio || audio->card_id[0] == '\0')
		return -EINVAL;

	rc = find_named_node(usb_sysfs_root, audio->card_id, "sound", 0,
			     path, sizeof(path));
	if (rc != 0)
		return rc;

	return extract_usb_location(usb_sysfs_root, path, location);
}

static void init_pairing(const struct usb_tree_video_device *video,
			 const struct probe_alsa_device *audio,
			 paired_devices_t *pairing)
{
	memset(pairing, 0, sizeof(*pairing));
	if (video)
		copy_string(pairing->video_node, sizeof(pairing->video_node),
			    video->video_node);
	if (audio && audio->pcm_capture_node[0] != '\0')
		copy_string(pairing->audio_node, sizeof(pairing->audio_node),
			    audio->pcm_capture_node);
	else if (audio)
		copy_string(pairing->audio_node, sizeof(pairing->audio_node),
			    audio->card_id);
}

int usb_tree_pair_audio_video_at(const char *usb_sysfs_root,
				 const struct usb_tree_video_device *video,
				 const struct probe_alsa_device *audio,
				 paired_devices_t *pairing)
{
	struct usb_tree_location video_location;
	struct usb_tree_location audio_location;
	int rc;

	if (!usb_sysfs_root || !video || !audio || !pairing)
		return -EINVAL;

	init_pairing(video, audio, pairing);

	rc = find_video_location(usb_sysfs_root, video, &video_location);
	if (rc != 0) {
		snprintf(pairing->reason, sizeof(pairing->reason),
			 "video USB location not found");
		return rc;
	}

	rc = find_audio_location(usb_sysfs_root, audio, &audio_location);
	if (rc != 0) {
		snprintf(pairing->reason, sizeof(pairing->reason),
			 "audio USB location not found");
		return rc;
	}

	if (strcmp(video_location.usb_device, audio_location.usb_device) == 0) {
		pairing->paired = 1;
		copy_string(pairing->usb_device, sizeof(pairing->usb_device),
			    video_location.usb_device);
		copy_string(pairing->usb_parent, sizeof(pairing->usb_parent),
			    video_location.usb_parent);
		snprintf(pairing->reason, sizeof(pairing->reason),
			 "same USB device");
		return 0;
	}

	if (video_location.usb_parent[0] != '\0' &&
	    strcmp(video_location.usb_parent, audio_location.usb_parent) == 0) {
		pairing->paired = 1;
		copy_string(pairing->usb_parent, sizeof(pairing->usb_parent),
			    video_location.usb_parent);
		snprintf(pairing->reason, sizeof(pairing->reason),
			 "same USB parent");
		return 0;
	}

	copy_string(pairing->usb_device, sizeof(pairing->usb_device),
		    video_location.usb_device);
	copy_string(pairing->usb_parent, sizeof(pairing->usb_parent),
		    video_location.usb_parent);
	snprintf(pairing->reason, sizeof(pairing->reason),
		 "different USB topology");
	return 0;
}

int usb_tree_pair_audio_video(const struct usb_tree_video_device *video,
			      const struct probe_alsa_device *audio,
			      paired_devices_t *pairing)
{
	return usb_tree_pair_audio_video_at(USB_TREE_PAIRING_SYSFS_ROOT, video,
					    audio, pairing);
}
