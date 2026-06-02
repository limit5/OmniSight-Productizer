/*
 * [OP-1938] UVC XU dispatcher userspace socket integration test.
 *
 * The test sends a LOOKUP_BY_GUID request for the FT-C600 adapter and
 * validates that the daemon response carries vendor_id=ft-c600.  It never
 * opens a video device and never issues UVC XU SET commands.
 */

#include <errno.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

#define DEFAULT_SOCKET_PATH "/run/uvc-xu-dispatcher.sock"
#define FT_C600_GUID "ft-c600-xu-guid"
#define EXPECTED_VENDOR_ID "ft-c600"
#define REQUEST_PREFIX "LOOKUP_BY_GUID "
#define RESPONSE_VENDOR_FIELD "vendor_id="
#define BUFFER_SIZE 256

struct options {
	const char *socket_path;
	const char *guid;
	const char *expected_vendor_id;
	bool self_test;
};

static void usage(const char *prog)
{
	fprintf(stderr,
		"usage: %s [--socket PATH] [--guid GUID] "
		"[--expect-vendor VENDOR] [--self-test]\n",
		prog);
}

static int parse_options(int argc, char **argv, struct options *opts)
{
	int i;

	opts->socket_path = getenv("UVC_XU_DISPATCH_SOCKET");
	if (!opts->socket_path || !opts->socket_path[0])
		opts->socket_path = DEFAULT_SOCKET_PATH;
	opts->guid = FT_C600_GUID;
	opts->expected_vendor_id = EXPECTED_VENDOR_ID;
	opts->self_test = false;

	for (i = 1; i < argc; i++) {
		if (strcmp(argv[i], "--socket") == 0) {
			if (++i >= argc)
				return -1;
			opts->socket_path = argv[i];
		} else if (strcmp(argv[i], "--guid") == 0) {
			if (++i >= argc)
				return -1;
			opts->guid = argv[i];
		} else if (strcmp(argv[i], "--expect-vendor") == 0) {
			if (++i >= argc)
				return -1;
			opts->expected_vendor_id = argv[i];
		} else if (strcmp(argv[i], "--self-test") == 0) {
			opts->self_test = true;
		} else if (strcmp(argv[i], "--help") == 0) {
			usage(argv[0]);
			exit(0);
		} else {
			return -1;
		}
	}

	return 0;
}

static int connect_unix_socket(const char *socket_path)
{
	struct sockaddr_un addr;
	int fd;

	if (strlen(socket_path) >= sizeof(addr.sun_path)) {
		fprintf(stderr, "socket path is too long: %s\n", socket_path);
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
		if (n == 0) {
			fprintf(stderr, "short write to dispatcher socket\n");
			return -1;
		}
		off += (size_t)n;
	}

	return 0;
}

static int read_response(int fd, char *buf, size_t len)
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
		if (memchr(buf, '\n', off))
			break;
	}
	buf[off] = '\0';

	if (off == 0) {
		fprintf(stderr, "empty dispatcher response\n");
		return -1;
	}

	return 0;
}

static int validate_response(const char *response, const char *expected_vendor)
{
	const char *vendor;
	size_t vendor_len = strlen(expected_vendor);

	if (strncmp(response, "OK ", 3) != 0) {
		fprintf(stderr, "dispatcher returned non-OK response: %s", response);
		return -1;
	}

	vendor = strstr(response, RESPONSE_VENDOR_FIELD);
	if (!vendor) {
		fprintf(stderr, "dispatcher response lacks vendor_id: %s", response);
		return -1;
	}
	vendor += strlen(RESPONSE_VENDOR_FIELD);

	if (strncmp(vendor, expected_vendor, vendor_len) != 0 ||
	    (vendor[vendor_len] != '\n' && vendor[vendor_len] != ' ' &&
	     vendor[vendor_len] != '\0')) {
		fprintf(stderr, "expected vendor_id=%s, got: %s",
			expected_vendor, response);
		return -1;
	}

	return 0;
}

static int run_lookup_client(const struct options *opts)
{
	char request[BUFFER_SIZE];
	char response[BUFFER_SIZE];
	int fd;
	int n;
	int rc = -1;

	n = snprintf(request, sizeof(request), "%s%s\n", REQUEST_PREFIX,
		     opts->guid);
	if (n < 0 || (size_t)n >= sizeof(request)) {
		fprintf(stderr, "lookup request is too long\n");
		return -1;
	}

	fd = connect_unix_socket(opts->socket_path);
	if (fd < 0)
		return -1;

	if (write_all(fd, request, (size_t)n) < 0)
		goto out;
	if (read_response(fd, response, sizeof(response)) < 0)
		goto out;
	if (validate_response(response, opts->expected_vendor_id) < 0)
		goto out;

	printf("LOOKUP_BY_GUID %s returned vendor_id=%s\n", opts->guid,
	       opts->expected_vendor_id);
	rc = 0;

out:
	close(fd);
	return rc;
}

static int make_listening_socket(const char *socket_path)
{
	struct sockaddr_un addr;
	int fd;

	if (strlen(socket_path) >= sizeof(addr.sun_path)) {
		fprintf(stderr, "socket path is too long: %s\n", socket_path);
		return -1;
	}

	unlink(socket_path);

	fd = socket(AF_UNIX, SOCK_STREAM, 0);
	if (fd < 0) {
		perror("socket");
		return -1;
	}

	memset(&addr, 0, sizeof(addr));
	addr.sun_family = AF_UNIX;
	strncpy(addr.sun_path, socket_path, sizeof(addr.sun_path) - 1);

	if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
		perror("bind");
		close(fd);
		return -1;
	}
	if (listen(fd, 1) < 0) {
		perror("listen");
		close(fd);
		return -1;
	}

	return fd;
}

static int run_stub_server(int listen_fd, const struct options *opts)
{
	char expected[BUFFER_SIZE];
	char request[BUFFER_SIZE];
	char response[BUFFER_SIZE];
	int client_fd;
	ssize_t nread;
	int n;

	client_fd = accept(listen_fd, NULL, NULL);
	if (client_fd < 0) {
		perror("accept");
		return 1;
	}

	nread = read(client_fd, request, sizeof(request) - 1);
	if (nread < 0) {
		perror("read");
		close(client_fd);
		return 1;
	}
	request[nread] = '\0';

	n = snprintf(expected, sizeof(expected), "%s%s\n", REQUEST_PREFIX,
		     opts->guid);
	if (n < 0 || (size_t)n >= sizeof(expected) ||
	    strcmp(request, expected) != 0) {
		fprintf(stderr, "unexpected lookup request: %s", request);
		close(client_fd);
		return 1;
	}

	n = snprintf(response, sizeof(response), "OK vendor_id=%s\n",
		     opts->expected_vendor_id);
	if (n < 0 || (size_t)n >= sizeof(response) ||
	    write_all(client_fd, response, (size_t)n) < 0) {
		close(client_fd);
		return 1;
	}

	close(client_fd);
	return 0;
}

static int run_self_test(const struct options *opts)
{
	pid_t child;
	int listen_fd;
	int status;
	int rc;

	listen_fd = make_listening_socket(opts->socket_path);
	if (listen_fd < 0)
		return -1;

	child = fork();
	if (child < 0) {
		perror("fork");
		close(listen_fd);
		unlink(opts->socket_path);
		return -1;
	}

	if (child == 0) {
		int child_rc = run_stub_server(listen_fd, opts);

		close(listen_fd);
		_exit(child_rc);
	}

	rc = run_lookup_client(opts);
	close(listen_fd);

	if (waitpid(child, &status, 0) < 0) {
		perror("waitpid");
		unlink(opts->socket_path);
		return -1;
	}

	unlink(opts->socket_path);

	if (!WIFEXITED(status) || WEXITSTATUS(status) != 0)
		return -1;

	return rc;
}

int main(int argc, char **argv)
{
	struct options opts;

	if (parse_options(argc, argv, &opts) < 0) {
		usage(argv[0]);
		return 2;
	}

	if (opts.self_test)
		return run_self_test(&opts) == 0 ? 0 : 1;

	return run_lookup_client(&opts) == 0 ? 0 : 1;
}
