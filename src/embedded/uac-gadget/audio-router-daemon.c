/* SPDX-License-Identifier: MIT
 *
 * audio-router daemon skeleton (OP-1991).
 *
 * Case 5 Phase 1B only exposes a userspace control plane: enumerate ALSA
 * cards and serve a Unix-socket protocol stub. PCM routing and audio I/O
 * belong to later application/provider leaves.
 */
#include "audio-router-daemon.h"

#include <errno.h>
#include <signal.h>
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

int audio_router_probe_devices(const char *sysfs_root,
			       struct probe_alsa_device_list *list)
{
	if (!sysfs_root || !list)
		return -EINVAL;

	return probe_alsa_devices_at(sysfs_root, list);
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

static void handle_list(int client_fd, const struct probe_alsa_device_list *list)
{
	char line[256];
	size_t i;

	for (i = 0; i < list->count; i++) {
		const struct probe_alsa_device *device = &list->devices[i];
		const char *capture = device->pcm_capture_node;
		const char *playback = device->pcm_playback_node;

		if (capture[0] == '\0')
			capture = "-";
		if (playback[0] == '\0')
			playback = "-";

		snprintf(line, sizeof(line),
			 "%s capture=%s playback=%s vid=%s pid=%s\n",
			 device->card_id, capture, playback,
			 device->vendor_id, device->product_id);
		write_response(client_fd, line);
	}
	write_response(client_fd, "END\n");
}

static void handle_client(int client_fd,
			  const struct probe_alsa_device_list *list)
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
		write_response(client_fd, "OK audio-router\n");
		return;
	}

	if (strncmp(request, "LIST", 4) == 0) {
		handle_list(client_fd, list);
		return;
	}

	write_response(client_fd, "ERR unsupported\n");
}

int audio_router_run_daemon(const char *socket_path, const char *sysfs_root)
{
	struct probe_alsa_device_list list;
	int server_fd;
	int ret;

	ret = install_signal_handlers();
	if (ret != 0)
		return ret;

	ret = audio_router_probe_devices(sysfs_root, &list);
	if (ret != 0)
		fprintf(stderr, "audio-router: continuing without devices\n");

	server_fd = make_server_socket(socket_path);
	if (server_fd < 0)
		return server_fd;
	ret = 0;

	fprintf(stderr, "audio-router: listening on %s\n", socket_path);

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
	const char *socket_path = AUDIO_ROUTER_SOCKET_PATH;
	const char *sysfs_root = AUDIO_ROUTER_SYSFS_ROOT;
	int ret;

	if (argc > 1)
		socket_path = argv[1];
	if (argc > 2)
		sysfs_root = argv[2];

	ret = audio_router_run_daemon(socket_path, sysfs_root);
	if (ret != 0) {
		fprintf(stderr, "audio-router: exit: %s\n", strerror(-ret));
		return EXIT_FAILURE;
	}

	return EXIT_SUCCESS;
}
