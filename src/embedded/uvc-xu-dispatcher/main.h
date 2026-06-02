/* SPDX-License-Identifier: MIT
 *
 * uvc-xu-dispatcher daemon skeleton (OP-1933).
 */
#ifndef UVC_XU_DISPATCHER_MAIN_H
#define UVC_XU_DISPATCHER_MAIN_H

#include <stddef.h>

#define UVC_XU_DISPATCHER_SOCKET_PATH "/run/uvc-xu-dispatcher.sock"
#define UVC_XU_DISPATCHER_SYSFS_ROOT "/sys/class/video4linux"
#define UVC_XU_DISPATCHER_MAX_DEVICES 32
#define UVC_XU_DISPATCHER_MAX_NAME 64
#define UVC_XU_DISPATCHER_MAX_PATH 256
#define UVC_XU_DISPATCHER_MAX_ID 16

struct uvc_xu_device {
	char name[UVC_XU_DISPATCHER_MAX_NAME];
	char sysfs_path[UVC_XU_DISPATCHER_MAX_PATH];
	char vendor_id[UVC_XU_DISPATCHER_MAX_ID];
	char product_id[UVC_XU_DISPATCHER_MAX_ID];
};

struct uvc_xu_device_list {
	struct uvc_xu_device devices[UVC_XU_DISPATCHER_MAX_DEVICES];
	size_t count;
};

int uvc_xu_scan_sysfs(const char *root, struct uvc_xu_device_list *list);
int uvc_xu_run_daemon(const char *socket_path, const char *sysfs_root);

#endif /* UVC_XU_DISPATCHER_MAIN_H */
