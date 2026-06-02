/* SPDX-License-Identifier: MIT
 *
 * uvc-xu-dispatcher daemon skeleton (OP-1933).
 *
 * Phase 0 only discovers UVC video nodes and exposes a Unix-socket protocol
 * stub. Vendor-specific XU GUID and dispatch logic belongs to later P0.B.3*
 * tickets.
 */
#include "main.h"

#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

static volatile sig_atomic_t stop_requested;

static void handle_signal(int signal_number)
{
	(void)signal_number;
	stop_requested = 1;
}

static bool has_video_prefix(const char *name)
{
	size_t i;

	if (strncmp(name, "video", 5) != 0)
		return false;

	for (i = 5; name[i] != '\0'; i++) {
		if (!isdigit((unsigned char)name[i]))
			return false;
	}

	return i > 5;
}

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

static int read_device_id(const char *video_path, const char *id_name,
			  char *buf, size_t buf_len)
{
	char path[UVC_XU_DISPATCHER_MAX_PATH];
	const char *candidates[] = {
		"device/idVendor",
		"device/../idVendor",
		"device/../../idVendor",
	};
	size_t i;

	if (strcmp(id_name, "idProduct") == 0) {
		candidates[0] = "device/idProduct";
		candidates[1] = "device/../idProduct";
		candidates[2] = "device/../../idProduct";
	}

	for (i = 0; i < sizeof(candidates) / sizeof(candidates[0]); i++) {
		if (make_child_path(path, sizeof(path), video_path,
				    candidates[i]) != 0)
			continue;
		if (copy_text_file(path, buf, buf_len) == 0)
			return 0;
	}

	snprintf(buf, buf_len, "unknown");
	return -ENOENT;
}

int uvc_xu_scan_sysfs(const char *root, struct uvc_xu_device_list *list)
{
	DIR *dir;
	struct dirent *entry;

	if (!root || !list)
		return -EINVAL;

	memset(list, 0, sizeof(*list));

	dir = opendir(root);
	if (!dir) {
		fprintf(stderr, "uvc-xu-dispatcher: sysfs root %s unavailable: %s\n",
			root, strerror(errno));
		return -errno;
	}

	while ((entry = readdir(dir)) != NULL) {
		struct uvc_xu_device *device;
		size_t name_len;

		if (!has_video_prefix(entry->d_name))
			continue;
		if (list->count >= UVC_XU_DISPATCHER_MAX_DEVICES) {
			fprintf(stderr,
				"uvc-xu-dispatcher: device limit reached at %zu\n",
				list->count);
			break;
		}
		device = &list->devices[list->count];
		name_len = strlen(entry->d_name);
		if (name_len >= sizeof(device->name)) {
			fprintf(stderr,
				"uvc-xu-dispatcher: skipping overlong node %s\n",
				entry->d_name);
			continue;
		}

		memcpy(device->name, entry->d_name, name_len + 1);
		if (make_child_path(device->sysfs_path, sizeof(device->sysfs_path),
				    root, entry->d_name) != 0)
			continue;

		read_device_id(device->sysfs_path, "idVendor", device->vendor_id,
			       sizeof(device->vendor_id));
		read_device_id(device->sysfs_path, "idProduct", device->product_id,
			       sizeof(device->product_id));

		fprintf(stderr,
			"uvc-xu-dispatcher: detected %s vid=%s pid=%s path=%s\n",
			device->name, device->vendor_id, device->product_id,
			device->sysfs_path);
		list->count++;
	}

	closedir(dir);
	return 0;
}

static int install_signal_handlers(void)
{
	struct sigaction action;

	memset(&action, 0, sizeof(action));
	action.sa_handler = handle_signal;

	if (sigaction(SIGINT, &action, NULL) != 0)
		return -errno;
	if (sigaction(SIGTERM, &action, NULL) != 0)
		return -errno;

	return 0;
}

static int make_server_socket(const char *socket_path)
{
	struct sockaddr_un addr;
	int fd;

	if (strlen(socket_path) >= sizeof(addr.sun_path))
		return -ENAMETOOLONG;

	fd = socket(AF_UNIX, SOCK_STREAM, 0);
	if (fd < 0)
		return -errno;

	memset(&addr, 0, sizeof(addr));
	addr.sun_family = AF_UNIX;
	snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", socket_path);

	(void)unlink(socket_path);

	if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
		int saved_errno = errno;

		close(fd);
		return -saved_errno;
	}

	if (listen(fd, 8) != 0) {
		int saved_errno = errno;

		close(fd);
		unlink(socket_path);
		return -saved_errno;
	}

	return fd;
}

static void write_response(int client_fd, const char *response)
{
	size_t len = strlen(response);

	while (len > 0) {
		ssize_t written = write(client_fd, response, len);

		if (written < 0) {
			if (errno == EINTR)
				continue;
			return;
		}
		response += written;
		len -= (size_t)written;
	}
}

static void handle_client(int client_fd, const struct uvc_xu_device_list *list)
{
	char request[64];
	ssize_t got;

	got = read(client_fd, request, sizeof(request) - 1);
	if (got < 0) {
		if (errno != EINTR)
			write_response(client_fd, "ERR read\n");
		return;
	}

	request[got > 0 ? got : 0] = '\0';

	if (strncmp(request, "PING", 4) == 0) {
		write_response(client_fd, "OK uvc-xu-dispatcher\n");
		return;
	}

	if (strncmp(request, "LIST", 4) == 0) {
		char line[128];
		size_t i;

		for (i = 0; i < list->count; i++) {
			snprintf(line, sizeof(line), "%s %s:%s\n",
				 list->devices[i].name,
				 list->devices[i].vendor_id,
				 list->devices[i].product_id);
			write_response(client_fd, line);
		}
		write_response(client_fd, "END\n");
		return;
	}

	write_response(client_fd, "ERR unsupported\n");
}

int uvc_xu_run_daemon(const char *socket_path, const char *sysfs_root)
{
	struct uvc_xu_device_list list;
	int server_fd;
	int ret;

	ret = install_signal_handlers();
	if (ret != 0)
		return ret;

	ret = uvc_xu_scan_sysfs(sysfs_root, &list);
	if (ret != 0)
		fprintf(stderr, "uvc-xu-dispatcher: continuing without devices\n");

	server_fd = make_server_socket(socket_path);
	if (server_fd < 0)
		return server_fd;
	ret = 0;

	fprintf(stderr, "uvc-xu-dispatcher: listening on %s\n", socket_path);

	while (!stop_requested) {
		int client_fd = accept(server_fd, NULL, NULL);

		if (client_fd < 0) {
			if (errno == EINTR)
				continue;
			ret = -errno;
			break;
		}

		handle_client(client_fd, &list);
		close(client_fd);
	}

	close(server_fd);
	unlink(socket_path);
	return ret;
}

int main(int argc, char **argv)
{
	const char *socket_path = UVC_XU_DISPATCHER_SOCKET_PATH;
	const char *sysfs_root = UVC_XU_DISPATCHER_SYSFS_ROOT;
	int ret;

	if (argc > 1)
		socket_path = argv[1];
	if (argc > 2)
		sysfs_root = argv[2];

	ret = uvc_xu_run_daemon(socket_path, sysfs_root);
	if (ret != 0) {
		fprintf(stderr, "uvc-xu-dispatcher: exit: %s\n", strerror(-ret));
		return EXIT_FAILURE;
	}

	return EXIT_SUCCESS;
}
