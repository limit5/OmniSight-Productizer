/* SPDX-License-Identifier: MIT
 *
 * RK3588 userspace charging daemon (OP-2000).
 *
 * The kernel TCPM/UCSI stack owns the low-level PD state machine. This daemon
 * consumes the /sys/class/typec view, applies a deterministic contract policy
 * for exported partner source PDOs, requests role swaps through writable sysfs
 * attributes when present, and reports state over a Unix socket.
 */
#include "charging-daemon.h"

#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#ifdef __linux__
#include <linux/netlink.h>
#endif

static volatile sig_atomic_t stop_requested;

static void handle_signal(int signal_number)
{
	(void)signal_number;
	stop_requested = 1;
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

static bool has_port_prefix(const char *name)
{
	size_t i;

	if (strncmp(name, "port", 4) != 0)
		return false;

	for (i = 4; name[i] != '\0'; i++) {
		if (!isdigit((unsigned char)name[i]))
			return false;
	}

	return i > 4;
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

static int write_text_file(const char *path, const char *value)
{
	int fd;
	size_t len = strlen(value);
	ssize_t written;

	fd = open(path, O_WRONLY | O_CLOEXEC);
	if (fd < 0)
		return -errno;

	written = write(fd, value, len);
	if (written < 0) {
		int saved_errno = errno;

		close(fd);
		return -saved_errno;
	}

	close(fd);
	return written == (ssize_t)len ? 0 : -EIO;
}

static int read_attr(const char *base, const char *name, char *buf,
		     size_t buf_len)
{
	char path[CHARGING_DAEMON_MAX_PATH];
	int ret;

	ret = make_child_path(path, sizeof(path), base, name);
	if (ret != 0)
		return ret;

	ret = copy_text_file(path, buf, buf_len);
	if (ret != 0)
		snprintf(buf, buf_len, "unknown");

	return ret;
}

static int write_attr_if_present(const char *base, const char *name,
				 const char *value)
{
	char path[CHARGING_DAEMON_MAX_PATH];
	int ret;

	ret = make_child_path(path, sizeof(path), base, name);
	if (ret != 0)
		return ret;

	if (access(path, W_OK) != 0)
		return -errno;

	return write_text_file(path, value);
}

static bool parse_pdo_numbers(const char *line, unsigned int *mv,
			      unsigned int *ma)
{
	const char *cursor = line;
	unsigned long values[2];
	size_t count = 0;

	while (*cursor != '\0' && count < 2) {
		char *end;

		while (*cursor != '\0' && !isdigit((unsigned char)*cursor))
			cursor++;
		if (*cursor == '\0')
			break;

		errno = 0;
		values[count] = strtoul(cursor, &end, 10);
		if (errno != 0 || end == cursor)
			return false;

		count++;
		cursor = end;
	}

	if (count < 2 || values[0] == 0 || values[1] == 0)
		return false;

	*mv = values[0] <= 100 ? (unsigned int)(values[0] * 1000) :
				  (unsigned int)values[0];
	*ma = values[1] <= 100 ? (unsigned int)(values[1] * 1000) :
				  (unsigned int)values[1];
	return true;
}

static int read_source_pdos_from_file(const char *path,
				      struct charging_daemon_port *port)
{
	FILE *file;
	char line[128];

	file = fopen(path, "r");
	if (!file)
		return -errno;

	while (fgets(line, sizeof(line), file)) {
		struct charging_daemon_pdo *pdo;
		unsigned int mv;
		unsigned int ma;

		if (!parse_pdo_numbers(line, &mv, &ma))
			continue;
		if (port->source_pdo_count >= CHARGING_DAEMON_MAX_PDOS)
			break;

		pdo = &port->source_pdos[port->source_pdo_count++];
		pdo->millivolts = mv;
		pdo->milliamps = ma;
	}

	fclose(file);
	return port->source_pdo_count > 0 ? 0 : -ENODATA;
}

static int read_source_pdos(const char *partner_path,
			    struct charging_daemon_port *port)
{
	const char *candidates[] = {
		"source_capabilities",
		"source-pdos",
		"source_pdos",
		"pdos",
	};
	char path[CHARGING_DAEMON_MAX_PATH];
	size_t i;

	port->source_pdo_count = 0;

	for (i = 0; i < sizeof(candidates) / sizeof(candidates[0]); i++) {
		if (make_child_path(path, sizeof(path), partner_path,
				    candidates[i]) != 0)
			continue;
		if (read_source_pdos_from_file(path, port) == 0)
			return 0;
	}

	return -ENOENT;
}

static void choose_contract(struct charging_daemon_port *port)
{
	size_t i;
	size_t best = 0;
	unsigned long best_mw = 0;

	port->contract.millivolts = 0;
	port->contract.milliamps = 0;

	for (i = 0; i < port->source_pdo_count; i++) {
		unsigned long mw;

		if (port->source_pdos[i].millivolts > 20000)
			continue;
		mw = ((unsigned long)port->source_pdos[i].millivolts *
		      port->source_pdos[i].milliamps) / 1000;
		if (mw > best_mw) {
			best = i;
			best_mw = mw;
		}
	}

	if (best_mw == 0)
		return;

	port->contract = port->source_pdos[best];
}

static void request_contract(const struct charging_daemon_port *port)
{
	char value[32];

	if (port->contract.millivolts == 0 || port->contract.milliamps == 0)
		return;

	snprintf(value, sizeof(value), "%u\n", port->contract.millivolts);
	(void)write_attr_if_present(port->sysfs_path, "request_voltage", value);
	(void)write_attr_if_present(port->sysfs_path, "voltage_request", value);

	snprintf(value, sizeof(value), "%u\n", port->contract.milliamps);
	(void)write_attr_if_present(port->sysfs_path, "request_current", value);
	(void)write_attr_if_present(port->sysfs_path, "current_request", value);
}

static void request_role_swap(const struct charging_daemon_port *port,
			      enum charging_daemon_role_preference preference)
{
	const char *role = NULL;

	if (!port->role_swap_supported)
		return;

	switch (preference) {
	case CHARGING_DAEMON_ROLE_SINK:
		role = "sink\n";
		break;
	case CHARGING_DAEMON_ROLE_SOURCE:
		role = "source\n";
		break;
	case CHARGING_DAEMON_ROLE_AUTO:
	default:
		return;
	}

	(void)write_attr_if_present(port->sysfs_path, "power_role", role);
}

static bool attr_is_writable(const char *base, const char *name)
{
	char path[CHARGING_DAEMON_MAX_PATH];

	if (make_child_path(path, sizeof(path), base, name) != 0)
		return false;

	return access(path, W_OK) == 0;
}

static int discover_partner(const char *root, struct charging_daemon_port *port)
{
	char partner_name[CHARGING_DAEMON_MAX_NAME];
	char pd_capable[CHARGING_DAEMON_MAX_TEXT];
	struct stat st;
	int ret;

	ret = snprintf(partner_name, sizeof(partner_name), "%s-partner",
		       port->name);
	if (ret < 0 || (size_t)ret >= sizeof(partner_name))
		return -ENAMETOOLONG;

	ret = make_child_path(port->partner_path, sizeof(port->partner_path),
			      root, partner_name);
	if (ret != 0)
		return ret;

	if (stat(port->partner_path, &st) != 0) {
		port->partner_path[0] = '\0';
		return -errno;
	}

	port->has_partner = 1;
	read_attr(port->partner_path, "usb_power_delivery_revision",
		  port->pd_revision, sizeof(port->pd_revision));
	if (read_attr(port->partner_path, "supports_usb_power_delivery",
		      pd_capable, sizeof(pd_capable)) == 0 &&
	    (!strcmp(pd_capable, "yes") || !strcmp(pd_capable, "1")))
		port->pd_capable = 1;

	if (read_source_pdos(port->partner_path, port) == 0)
		port->pd_capable = 1;

	return 0;
}

static void apply_port_policy(struct charging_daemon_port *port,
			      enum charging_daemon_role_preference preference)
{
	choose_contract(port);
	request_role_swap(port, preference);
	request_contract(port);
}

int charging_daemon_scan_sysfs(const char *root,
			       struct charging_daemon_state *state)
{
	DIR *dir;
	struct dirent *entry;
	enum charging_daemon_role_preference preference;

	if (!root || !state)
		return -EINVAL;

	preference = state->role_preference;
	memset(state, 0, sizeof(*state));
	state->role_preference = preference;

	dir = opendir(root);
	if (!dir) {
		fprintf(stderr, "charging-daemon: sysfs root %s unavailable: %s\n",
			root, strerror(errno));
		return -errno;
	}

	while ((entry = readdir(dir)) != NULL) {
		struct charging_daemon_port *port;
		size_t name_len;

		if (!has_port_prefix(entry->d_name))
			continue;
		if (state->count >= CHARGING_DAEMON_MAX_PORTS) {
			fprintf(stderr,
				"charging-daemon: port limit reached at %zu\n",
				state->count);
			break;
		}

		port = &state->ports[state->count];
		name_len = strlen(entry->d_name);
		if (name_len >= sizeof(port->name)) {
			fprintf(stderr,
				"charging-daemon: skipping overlong port %s\n",
				entry->d_name);
			continue;
		}

		memcpy(port->name, entry->d_name, name_len + 1);
		if (make_child_path(port->sysfs_path, sizeof(port->sysfs_path),
				    root, entry->d_name) != 0)
			continue;

		read_attr(port->sysfs_path, "power_role", port->power_role,
			  sizeof(port->power_role));
		read_attr(port->sysfs_path, "data_role", port->data_role,
			  sizeof(port->data_role));
		read_attr(port->sysfs_path, "port_type", port->port_type,
			  sizeof(port->port_type));
		port->role_swap_supported =
			attr_is_writable(port->sysfs_path, "power_role") ||
			!strcmp(port->port_type, "dual") ||
			!strcmp(port->port_type, "dual [drp]");

		(void)discover_partner(root, port);
		apply_port_policy(port, state->role_preference);
		read_attr(port->sysfs_path, "power_role", port->power_role,
			  sizeof(port->power_role));

		fprintf(stderr,
			"charging-daemon: %s role=%s type=%s pd=%s contract=%umV/%umA\n",
			port->name, port->power_role, port->port_type,
			port->pd_capable ? "yes" : "no",
			port->contract.millivolts, port->contract.milliamps);
		state->count++;
	}

	closedir(dir);
	return 0;
}

static int make_server_socket(const char *socket_path)
{
	struct sockaddr_un addr;
	int fd;

	if (strlen(socket_path) >= sizeof(addr.sun_path))
		return -ENAMETOOLONG;

	fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
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

static int make_uevent_socket(void)
{
#ifdef __linux__
	struct sockaddr_nl addr;
	int fd;
	int enable = 1;

	fd = socket(AF_NETLINK, SOCK_DGRAM | SOCK_CLOEXEC, NETLINK_KOBJECT_UEVENT);
	if (fd < 0)
		return -errno;

	memset(&addr, 0, sizeof(addr));
	addr.nl_family = AF_NETLINK;
	addr.nl_groups = 1;

	if (setsockopt(fd, SOL_SOCKET, SO_PASSCRED, &enable,
		       sizeof(enable)) != 0) {
		int saved_errno = errno;

		close(fd);
		return -saved_errno;
	}

	if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
		int saved_errno = errno;

		close(fd);
		return -saved_errno;
	}

	return fd;
#else
	return -ENOSYS;
#endif
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

static void write_port_state(int client_fd,
			     const struct charging_daemon_port *port)
{
	char line[512];

	snprintf(line, sizeof(line),
		 "%s role=%s data=%s type=%s partner=%s pd=%s contract=%umV/%umA pdo_count=%zu\n",
		 port->name, port->power_role, port->data_role, port->port_type,
		 port->has_partner ? "yes" : "no",
		 port->pd_capable ? "yes" : "no",
		 port->contract.millivolts, port->contract.milliamps,
		 port->source_pdo_count);
	write_response(client_fd, line);
}

static enum charging_daemon_role_preference parse_role_preference(
	const char *request)
{
	if (strncmp(request, "ROLE sink", 9) == 0)
		return CHARGING_DAEMON_ROLE_SINK;
	if (strncmp(request, "ROLE source", 11) == 0)
		return CHARGING_DAEMON_ROLE_SOURCE;
	return CHARGING_DAEMON_ROLE_AUTO;
}

static void handle_client(int client_fd, struct charging_daemon_state *state,
			  const char *sysfs_root)
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
		write_response(client_fd, "OK charging-daemon\n");
		return;
	}

	if (strncmp(request, "STATE", 5) == 0) {
		size_t i;

		for (i = 0; i < state->count; i++)
			write_port_state(client_fd, &state->ports[i]);
		write_response(client_fd, "END\n");
		return;
	}

	if (strncmp(request, "RESCAN", 6) == 0) {
		(void)charging_daemon_scan_sysfs(sysfs_root, state);
		write_response(client_fd, "OK rescan\n");
		return;
	}

	if (strncmp(request, "ROLE ", 5) == 0) {
		state->role_preference = parse_role_preference(request);
		(void)charging_daemon_scan_sysfs(sysfs_root, state);
		write_response(client_fd, "OK role\n");
		return;
	}

	write_response(client_fd, "ERR unsupported\n");
}

static bool uevent_mentions_typec(const char *buf, ssize_t len)
{
	ssize_t i;

	for (i = 0; i < len; i++) {
		if (i + 5 <= len && memcmp(buf + i, "typec", 5) == 0)
			return true;
	}

	return false;
}

static void drain_uevent_socket(int fd, const char *sysfs_root,
				struct charging_daemon_state *state)
{
	char buf[2048];

	for (;;) {
		ssize_t got = recv(fd, buf, sizeof(buf), MSG_DONTWAIT);

		if (got < 0) {
			if (errno == EAGAIN || errno == EWOULDBLOCK ||
			    errno == EINTR)
				return;
			return;
		}
		if (got == 0)
			return;
		if (uevent_mentions_typec(buf, got))
			(void)charging_daemon_scan_sysfs(sysfs_root, state);
	}
}

int charging_daemon_run(const char *socket_path, const char *sysfs_root)
{
	struct charging_daemon_state state;
	int server_fd;
	int uevent_fd;
	int ret;

	memset(&state, 0, sizeof(state));

	ret = install_signal_handlers();
	if (ret != 0)
		return ret;

	ret = charging_daemon_scan_sysfs(sysfs_root, &state);
	if (ret != 0)
		fprintf(stderr, "charging-daemon: continuing without ports\n");

	server_fd = make_server_socket(socket_path);
	if (server_fd < 0)
		return server_fd;

	uevent_fd = make_uevent_socket();
	if (uevent_fd < 0)
		fprintf(stderr, "charging-daemon: uevent socket unavailable: %s\n",
			strerror(-uevent_fd));

	fprintf(stderr, "charging-daemon: listening on %s\n", socket_path);
	ret = 0;

	while (!stop_requested) {
		struct pollfd fds[2];
		nfds_t nfds = 1;
		int ready;

		fds[0].fd = server_fd;
		fds[0].events = POLLIN;
		if (uevent_fd >= 0) {
			fds[1].fd = uevent_fd;
			fds[1].events = POLLIN;
			nfds = 2;
		}

		ready = poll(fds, nfds, -1);
		if (ready < 0) {
			if (errno == EINTR)
				continue;
			ret = -errno;
			break;
		}

		if (uevent_fd >= 0 && (fds[1].revents & POLLIN))
			drain_uevent_socket(uevent_fd, sysfs_root, &state);

		if (fds[0].revents & POLLIN) {
			int client_fd = accept4(server_fd, NULL, NULL,
						SOCK_CLOEXEC);

			if (client_fd < 0) {
				if (errno == EINTR)
					continue;
				ret = -errno;
				break;
			}
			handle_client(client_fd, &state, sysfs_root);
			close(client_fd);
		}
	}

	if (uevent_fd >= 0)
		close(uevent_fd);
	close(server_fd);
	unlink(socket_path);
	return ret;
}

int main(int argc, char **argv)
{
	const char *socket_path = CHARGING_DAEMON_SOCKET_PATH;
	const char *sysfs_root = CHARGING_DAEMON_SYSFS_ROOT;
	int ret;

	if (argc > 1)
		socket_path = argv[1];
	if (argc > 2)
		sysfs_root = argv[2];

	ret = charging_daemon_run(socket_path, sysfs_root);
	if (ret != 0) {
		fprintf(stderr, "charging-daemon: exit: %s\n", strerror(-ret));
		return EXIT_FAILURE;
	}

	return EXIT_SUCCESS;
}
