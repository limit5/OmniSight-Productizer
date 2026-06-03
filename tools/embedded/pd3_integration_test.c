/*
 * [OP-2003] PD3.0/UCSI userspace integration verifier.
 *
 * The shell wrapper owns qemu execution and synthetic sysfs fixture creation.
 * This helper verifies the /sys/class/typec view and the charging-daemon
 * socket state that should result from a stub PD contract negotiation.
 */

#include <errno.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#define DEFAULT_SYSFS_ROOT "/sys/class/typec"
#define DEFAULT_SOCKET_PATH "/run/charging-daemon.sock"
#define DEFAULT_PORT "port0"
#define DEFAULT_CONTRACT "contract=20000mV/5000mA"
#define DEFAULT_EVENT "pd-contract-negotiated"
#define BUFFER_SIZE 2048

struct options {
	const char *sysfs_root;
	const char *socket_path;
	const char *port;
	const char *expected_contract;
	const char *expected_event;
};

static void usage(const char *prog)
{
	fprintf(stderr,
		"usage: %s [--sysfs-root PATH] [--socket PATH] [--port NAME] "
		"[--expect-contract TEXT] [--expect-event TEXT]\n",
		prog);
}

static int make_path(char *buf, size_t len, const char *a, const char *b)
{
	int n = snprintf(buf, len, "%s/%s", a, b);

	if (n < 0 || (size_t)n >= len)
		return -1;
	return 0;
}

static int read_text_file(const char *path, char *buf, size_t len)
{
	FILE *file = fopen(path, "r");
	size_t used;

	if (!file) {
		fprintf(stderr, "pd3-integration: cannot open %s: %s\n",
			path, strerror(errno));
		return -1;
	}
	if (!fgets(buf, len, file)) {
		fprintf(stderr, "pd3-integration: cannot read %s\n", path);
		fclose(file);
		return -1;
	}
	fclose(file);

	used = strcspn(buf, "\r\n");
	buf[used] = '\0';
	return 0;
}

static int expect_file_contains(const char *path, const char *needle)
{
	char buf[BUFFER_SIZE];

	if (read_text_file(path, buf, sizeof(buf)) < 0)
		return -1;
	if (!strstr(buf, needle)) {
		fprintf(stderr, "pd3-integration: %s lacks %s (got %s)\n",
			path, needle, buf);
		return -1;
	}
	return 0;
}

static int verify_typec_sysfs(const struct options *opts)
{
	char port_path[256];
	char partner_name[128];
	char partner_path[256];
	char attr_path[256];
	struct stat st;

	if (make_path(port_path, sizeof(port_path), opts->sysfs_root,
		      opts->port) < 0)
		return -1;
	if (stat(port_path, &st) != 0 || !S_ISDIR(st.st_mode)) {
		fprintf(stderr, "pd3-integration: missing TypeC port %s\n",
			port_path);
		return -1;
	}

	snprintf(partner_name, sizeof(partner_name), "%s-partner", opts->port);
	if (make_path(partner_path, sizeof(partner_path), opts->sysfs_root,
		      partner_name) < 0)
		return -1;
	if (stat(partner_path, &st) != 0 || !S_ISDIR(st.st_mode)) {
		fprintf(stderr, "pd3-integration: missing TypeC partner %s\n",
			partner_path);
		return -1;
	}

	if (make_path(attr_path, sizeof(attr_path), port_path, "power_role") < 0 ||
	    expect_file_contains(attr_path, "sink") < 0)
		return -1;
	if (make_path(attr_path, sizeof(attr_path), port_path, "data_role") < 0 ||
	    expect_file_contains(attr_path, "device") < 0)
		return -1;
	if (make_path(attr_path, sizeof(attr_path), partner_path,
		      "supports_usb_power_delivery") < 0 ||
	    expect_file_contains(attr_path, "yes") < 0)
		return -1;
	if (make_path(attr_path, sizeof(attr_path), partner_path,
		      "usb_power_delivery_revision") < 0 ||
	    expect_file_contains(attr_path, "3.0") < 0)
		return -1;
	if (make_path(attr_path, sizeof(attr_path), port_path, "ucsi_event") < 0 ||
	    expect_file_contains(attr_path, opts->expected_event) < 0)
		return -1;

	return 0;
}

static int connect_socket(const char *socket_path)
{
	struct sockaddr_un addr;
	int fd;

	if (strlen(socket_path) >= sizeof(addr.sun_path)) {
		fprintf(stderr, "pd3-integration: socket path too long: %s\n",
			socket_path);
		return -1;
	}

	fd = socket(AF_UNIX, SOCK_STREAM, 0);
	if (fd < 0) {
		perror("socket");
		return -1;
	}

	memset(&addr, 0, sizeof(addr));
	addr.sun_family = AF_UNIX;
	strncpy(addr.sun_path, socket_path, sizeof(addr.sun_path) - 1);

	if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
		perror("connect");
		close(fd);
		return -1;
	}

	return fd;
}

static int write_all(int fd, const char *buf, size_t len)
{
	size_t off = 0;

	while (off < len) {
		ssize_t n = write(fd, buf + off, len - off);

		if (n < 0) {
			if (errno == EINTR)
				continue;
			perror("write");
			return -1;
		}
		off += (size_t)n;
	}

	return 0;
}

static int read_all(int fd, char *buf, size_t len)
{
	size_t off = 0;

	while (off + 1 < len) {
		ssize_t n = read(fd, buf + off, len - 1 - off);

		if (n < 0) {
			if (errno == EINTR)
				continue;
			perror("read");
			return -1;
		}
		if (n == 0)
			break;
		off += (size_t)n;
		buf[off] = '\0';
		if (strstr(buf, "\nEND\n") || strcmp(buf, "END\n") == 0)
			break;
	}
	buf[off] = '\0';
	return off > 0 ? 0 : -1;
}

static int verify_daemon_state(const struct options *opts)
{
	char response[BUFFER_SIZE];
	int fd;
	int rc = -1;

	fd = connect_socket(opts->socket_path);
	if (fd < 0)
		return -1;

	if (write_all(fd, "STATE\n", strlen("STATE\n")) < 0)
		goto out;
	if (read_all(fd, response, sizeof(response)) < 0)
		goto out;
	if (!strstr(response, opts->port) ||
	    !strstr(response, "partner=yes") ||
	    !strstr(response, "pd=yes") ||
	    !strstr(response, opts->expected_contract)) {
		fprintf(stderr, "pd3-integration: unexpected daemon state:\n%s",
			response);
		goto out;
	}

	printf("pd3-integration: verified %s %s\n", opts->port,
	       opts->expected_contract);
	rc = 0;

out:
	close(fd);
	return rc;
}

static int parse_options(int argc, char **argv, struct options *opts)
{
	int i;

	opts->sysfs_root = DEFAULT_SYSFS_ROOT;
	opts->socket_path = DEFAULT_SOCKET_PATH;
	opts->port = DEFAULT_PORT;
	opts->expected_contract = DEFAULT_CONTRACT;
	opts->expected_event = DEFAULT_EVENT;

	for (i = 1; i < argc; i++) {
		if (strcmp(argv[i], "--sysfs-root") == 0) {
			if (++i >= argc)
				return -1;
			opts->sysfs_root = argv[i];
		} else if (strcmp(argv[i], "--socket") == 0) {
			if (++i >= argc)
				return -1;
			opts->socket_path = argv[i];
		} else if (strcmp(argv[i], "--port") == 0) {
			if (++i >= argc)
				return -1;
			opts->port = argv[i];
		} else if (strcmp(argv[i], "--expect-contract") == 0) {
			if (++i >= argc)
				return -1;
			opts->expected_contract = argv[i];
		} else if (strcmp(argv[i], "--expect-event") == 0) {
			if (++i >= argc)
				return -1;
			opts->expected_event = argv[i];
		} else if (strcmp(argv[i], "--help") == 0) {
			usage(argv[0]);
			exit(0);
		} else {
			return -1;
		}
	}

	return 0;
}

int main(int argc, char **argv)
{
	struct options opts;

	if (parse_options(argc, argv, &opts) < 0) {
		usage(argv[0]);
		return 2;
	}

	if (verify_typec_sysfs(&opts) < 0)
		return 1;
	if (verify_daemon_state(&opts) < 0)
		return 1;

	return 0;
}
