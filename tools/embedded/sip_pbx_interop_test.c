/*
 * [OP-2006] SIP PBX interop helper for Asterisk/FreeSWITCH smoke tests.
 *
 * This dependency-free userspace helper drives a minimal SIP UAC:
 * REGISTER, REGISTER refresh, INVITE, RTP media packets, BYE.  The shell
 * wrapper owns qemu/docker orchestration; this binary owns protocol checks.
 */

#define _POSIX_C_SOURCE 200809L

#include <arpa/inet.h>
#include <errno.h>
#include <netdb.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define DEFAULT_SERVER_HOST "127.0.0.1"
#define DEFAULT_SERVER_PORT "5060"
#define DEFAULT_USER "1001"
#define DEFAULT_DOMAIN "127.0.0.1"
#define DEFAULT_CALLEE "600"
#define DEFAULT_EXPIRES "60"
#define DEFAULT_TIMEOUT_MS 3000
#define DEFAULT_RTP_PACKETS 6
#define SIP_BUFSZ 4096
#define SDP_ADDRSZ 64
#define TAGSZ 32
#define BRANCHSZ 48
#define CALLIDSZ 64
#define RTP_PAYLOAD_BYTES 160
#define STUB_SIP_PORT 15060
#define STUB_RTP_PORT 14000

struct options {
	const char *server_host;
	const char *server_port;
	const char *user;
	const char *domain;
	const char *callee;
	const char *expires;
	int timeout_ms;
	int rtp_packets;
	bool self_test;
	bool require_rtp_rx;
};

struct sip_client {
	int sip_fd;
	int rtp_fd;
	struct sockaddr_storage server_addr;
	socklen_t server_len;
	char local_ip[SDP_ADDRSZ];
	uint16_t local_sip_port;
	uint16_t local_rtp_port;
	char tag[TAGSZ];
	char call_id[CALLIDSZ];
	int cseq;
};

static void usage(const char *prog)
{
	fprintf(stderr,
		"usage: %s [--server-host HOST] [--server-port PORT] "
		"[--user USER] [--domain DOMAIN] [--callee EXT] "
		"[--expires SECONDS] [--timeout-ms MS] [--rtp-packets N] "
		"[--require-rtp-rx] [--self-test]\n",
		prog);
}

static int parse_options(int argc, char **argv, struct options *opts)
{
	int i;

	opts->server_host = DEFAULT_SERVER_HOST;
	opts->server_port = DEFAULT_SERVER_PORT;
	opts->user = DEFAULT_USER;
	opts->domain = DEFAULT_DOMAIN;
	opts->callee = DEFAULT_CALLEE;
	opts->expires = DEFAULT_EXPIRES;
	opts->timeout_ms = DEFAULT_TIMEOUT_MS;
	opts->rtp_packets = DEFAULT_RTP_PACKETS;
	opts->self_test = false;
	opts->require_rtp_rx = false;

	for (i = 1; i < argc; i++) {
		if (strcmp(argv[i], "--server-host") == 0) {
			if (++i >= argc)
				return -1;
			opts->server_host = argv[i];
		} else if (strcmp(argv[i], "--server-port") == 0) {
			if (++i >= argc)
				return -1;
			opts->server_port = argv[i];
		} else if (strcmp(argv[i], "--user") == 0) {
			if (++i >= argc)
				return -1;
			opts->user = argv[i];
		} else if (strcmp(argv[i], "--domain") == 0) {
			if (++i >= argc)
				return -1;
			opts->domain = argv[i];
		} else if (strcmp(argv[i], "--callee") == 0) {
			if (++i >= argc)
				return -1;
			opts->callee = argv[i];
		} else if (strcmp(argv[i], "--expires") == 0) {
			if (++i >= argc)
				return -1;
			opts->expires = argv[i];
		} else if (strcmp(argv[i], "--timeout-ms") == 0) {
			if (++i >= argc)
				return -1;
			opts->timeout_ms = atoi(argv[i]);
			if (opts->timeout_ms <= 0)
				return -1;
		} else if (strcmp(argv[i], "--rtp-packets") == 0) {
			if (++i >= argc)
				return -1;
			opts->rtp_packets = atoi(argv[i]);
			if (opts->rtp_packets <= 0)
				return -1;
		} else if (strcmp(argv[i], "--require-rtp-rx") == 0) {
			opts->require_rtp_rx = true;
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

static void make_token(char *buf, size_t len, const char *prefix)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	snprintf(buf, len, "%s%ld%ld%ld", prefix, (long)getpid(),
		 (long)ts.tv_sec, (long)ts.tv_nsec);
}

static int wait_readable(int fd, int timeout_ms)
{
	struct timeval tv;
	fd_set rfds;
	int rc;

	FD_ZERO(&rfds);
	FD_SET(fd, &rfds);
	tv.tv_sec = timeout_ms / 1000;
	tv.tv_usec = (timeout_ms % 1000) * 1000;

	do {
		rc = select(fd + 1, &rfds, NULL, NULL, &tv);
	} while (rc < 0 && errno == EINTR);

	return rc;
}

static int bind_udp_socket(uint16_t port, uint16_t *bound_port)
{
	struct sockaddr_in addr;
	socklen_t len = sizeof(addr);
	int fd;

	fd = socket(AF_INET, SOCK_DGRAM, 0);
	if (fd < 0) {
		perror("socket");
		return -1;
	}

	memset(&addr, 0, sizeof(addr));
	addr.sin_family = AF_INET;
	addr.sin_addr.s_addr = htonl(INADDR_ANY);
	addr.sin_port = htons(port);

	if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
		perror("bind");
		close(fd);
		return -1;
	}

	if (getsockname(fd, (struct sockaddr *)&addr, &len) < 0) {
		perror("getsockname");
		close(fd);
		return -1;
	}
	*bound_port = ntohs(addr.sin_port);
	return fd;
}

static int resolve_server(const char *host, const char *port,
			  struct sockaddr_storage *addr, socklen_t *addr_len)
{
	struct addrinfo hints;
	struct addrinfo *res;
	int rc;

	memset(&hints, 0, sizeof(hints));
	hints.ai_family = AF_INET;
	hints.ai_socktype = SOCK_DGRAM;

	rc = getaddrinfo(host, port, &hints, &res);
	if (rc != 0) {
		fprintf(stderr, "sip-interop: resolve %s:%s failed: %s\n",
			host, port, gai_strerror(rc));
		return -1;
	}

	memcpy(addr, res->ai_addr, res->ai_addrlen);
	*addr_len = (socklen_t)res->ai_addrlen;
	freeaddrinfo(res);
	return 0;
}

static int init_client(struct sip_client *client, const struct options *opts)
{
	struct sockaddr_in probe;
	socklen_t probe_len = sizeof(probe);

	memset(client, 0, sizeof(*client));
	if (resolve_server(opts->server_host, opts->server_port,
			   &client->server_addr, &client->server_len) < 0)
		return -1;

	client->sip_fd = bind_udp_socket(0, &client->local_sip_port);
	if (client->sip_fd < 0)
		return -1;
	client->rtp_fd = bind_udp_socket(0, &client->local_rtp_port);
	if (client->rtp_fd < 0) {
		close(client->sip_fd);
		return -1;
	}

	if (connect(client->sip_fd, (struct sockaddr *)&client->server_addr,
		    client->server_len) < 0) {
		perror("connect");
		close(client->sip_fd);
		close(client->rtp_fd);
		return -1;
	}
	if (getsockname(client->sip_fd, (struct sockaddr *)&probe,
			&probe_len) < 0) {
		perror("getsockname");
		close(client->sip_fd);
		close(client->rtp_fd);
		return -1;
	}

	if (!inet_ntop(AF_INET, &probe.sin_addr, client->local_ip,
		       sizeof(client->local_ip))) {
		perror("inet_ntop");
		close(client->sip_fd);
		close(client->rtp_fd);
		return -1;
	}
	if (strcmp(client->local_ip, "0.0.0.0") == 0)
		snprintf(client->local_ip, sizeof(client->local_ip), "127.0.0.1");

	make_token(client->tag, sizeof(client->tag), "tag");
	make_token(client->call_id, sizeof(client->call_id), "call");
	client->cseq = 1;
	return 0;
}

static int send_sip(struct sip_client *client, const char *msg)
{
	size_t len = strlen(msg);
	ssize_t written = send(client->sip_fd, msg, len, 0);

	if (written < 0) {
		perror("send");
		return -1;
	}
	if ((size_t)written != len) {
		fprintf(stderr, "sip-interop: short SIP send\n");
		return -1;
	}
	return 0;
}

static int recv_sip(struct sip_client *client, char *buf, size_t len,
		    int timeout_ms)
{
	ssize_t nread;

	if (wait_readable(client->sip_fd, timeout_ms) <= 0) {
		fprintf(stderr, "sip-interop: timed out waiting for SIP response\n");
		return -1;
	}

	nread = recv(client->sip_fd, buf, len - 1, 0);
	if (nread < 0) {
		perror("recv");
		return -1;
	}
	buf[nread] = '\0';
	return 0;
}

static int parse_status_code(const char *msg)
{
	int code = 0;

	if (sscanf(msg, "SIP/2.0 %d", &code) != 1)
		return -1;
	return code;
}

static int wait_for_status(struct sip_client *client, int min_code, int max_code,
			   int timeout_ms, char *last, size_t last_len)
{
	int code;

	do {
		if (recv_sip(client, last, last_len, timeout_ms) < 0)
			return -1;
		code = parse_status_code(last);
		if (code < 0) {
			fprintf(stderr, "sip-interop: non-response SIP packet: %.40s\n",
				last);
			return -1;
		}
	} while (code < min_code);

	if (code > max_code) {
		fprintf(stderr, "sip-interop: unexpected SIP status %d\n", code);
		return -1;
	}

	return code;
}

static const char *find_header(const char *msg, const char *name)
{
	const char *p = msg;
	size_t name_len = strlen(name);

	while (*p) {
		if (strncasecmp(p, name, name_len) == 0 && p[name_len] == ':') {
			p += name_len + 1;
			while (*p == ' ' || *p == '\t')
				p++;
			return p;
		}
		p = strstr(p, "\n");
		if (!p)
			break;
		p++;
		if (*p == '\r' || *p == '\n')
			break;
	}
	return NULL;
}

static int copy_header_line(const char *msg, const char *name, char *out,
			    size_t out_len)
{
	const char *start = find_header(msg, name);
	const char *end;
	size_t len;

	if (!start)
		return -1;
	end = strpbrk(start, "\r\n");
	if (!end)
		return -1;
	len = (size_t)(end - start);
	if (len >= out_len)
		len = out_len - 1;
	memcpy(out, start, len);
	out[len] = '\0';
	return 0;
}

static int parse_sdp_media(const char *msg, char *addr, size_t addr_len,
			   uint16_t *port)
{
	const char *conn = strstr(msg, "\nc=IN IP4 ");
	const char *media = strstr(msg, "\nm=audio ");
	unsigned int rtp_port;

	if (!conn || !media)
		return -1;
	conn += strlen("\nc=IN IP4 ");
	if (sscanf(conn, "%63s", addr) != 1)
		return -1;
	addr[addr_len - 1] = '\0';
	(void)addr_len;

	media += strlen("\nm=audio ");
	if (sscanf(media, "%u", &rtp_port) != 1 || rtp_port > UINT16_MAX)
		return -1;
	*port = (uint16_t)rtp_port;
	return 0;
}

static int build_register(char *buf, size_t len, const struct options *opts,
			  const struct sip_client *client, const char *branch)
{
	return snprintf(buf, len,
		"REGISTER sip:%s SIP/2.0\r\n"
		"Via: SIP/2.0/UDP %s:%u;branch=%s;rport\r\n"
		"Max-Forwards: 70\r\n"
		"From: <sip:%s@%s>;tag=%s\r\n"
		"To: <sip:%s@%s>\r\n"
		"Call-ID: %s\r\n"
		"CSeq: %d REGISTER\r\n"
		"Contact: <sip:%s@%s:%u>\r\n"
		"Expires: %s\r\n"
		"Content-Length: 0\r\n\r\n",
		opts->domain, client->local_ip, client->local_sip_port, branch,
		opts->user, opts->domain, client->tag, opts->user,
		opts->domain, client->call_id, client->cseq, opts->user,
		client->local_ip, client->local_sip_port, opts->expires);
}

static int do_register(struct sip_client *client, const struct options *opts)
{
	char msg[SIP_BUFSZ];
	char resp[SIP_BUFSZ];
	char branch[BRANCHSZ];
	int n;

	make_token(branch, sizeof(branch), "z9hG4bKreg");
	n = build_register(msg, sizeof(msg), opts, client, branch);
	if (n < 0 || (size_t)n >= sizeof(msg))
		return -1;
	if (send_sip(client, msg) < 0)
		return -1;
	if (wait_for_status(client, 200, 299, opts->timeout_ms, resp,
			    sizeof(resp)) < 0)
		return -1;
	printf("sip-interop: REGISTER cseq=%d accepted\n", client->cseq);
	client->cseq++;
	return 0;
}

static int build_invite(char *buf, size_t len, const struct options *opts,
			const struct sip_client *client, const char *branch)
{
	char sdp[512];
	int sdp_len;

	sdp_len = snprintf(sdp, sizeof(sdp),
		"v=0\r\n"
		"o=omnisight 1 1 IN IP4 %s\r\n"
		"s=omnisight-sip-interop\r\n"
		"c=IN IP4 %s\r\n"
		"t=0 0\r\n"
		"m=audio %u RTP/AVP 0\r\n"
		"a=rtpmap:0 PCMU/8000\r\n"
		"a=sendrecv\r\n",
		client->local_ip, client->local_ip, client->local_rtp_port);
	if (sdp_len < 0 || (size_t)sdp_len >= sizeof(sdp))
		return -1;

	return snprintf(buf, len,
		"INVITE sip:%s@%s SIP/2.0\r\n"
		"Via: SIP/2.0/UDP %s:%u;branch=%s;rport\r\n"
		"Max-Forwards: 70\r\n"
		"From: <sip:%s@%s>;tag=%s\r\n"
		"To: <sip:%s@%s>\r\n"
		"Call-ID: %s-call\r\n"
		"CSeq: %d INVITE\r\n"
		"Contact: <sip:%s@%s:%u>\r\n"
		"Content-Type: application/sdp\r\n"
		"Content-Length: %d\r\n\r\n%s",
		opts->callee, opts->domain, client->local_ip,
		client->local_sip_port, branch, opts->user, opts->domain,
		client->tag, opts->callee, opts->domain, client->call_id,
		client->cseq, opts->user, client->local_ip,
		client->local_sip_port, sdp_len, sdp);
}

static int send_ack(struct sip_client *client, const struct options *opts,
		    const char *to_header)
{
	char msg[SIP_BUFSZ];
	char branch[BRANCHSZ];
	int n;

	make_token(branch, sizeof(branch), "z9hG4bKack");
	n = snprintf(msg, sizeof(msg),
		"ACK sip:%s@%s SIP/2.0\r\n"
		"Via: SIP/2.0/UDP %s:%u;branch=%s;rport\r\n"
		"Max-Forwards: 70\r\n"
		"From: <sip:%s@%s>;tag=%s\r\n"
		"To: %s\r\n"
		"Call-ID: %s-call\r\n"
		"CSeq: %d ACK\r\n"
		"Contact: <sip:%s@%s:%u>\r\n"
		"Content-Length: 0\r\n\r\n",
		opts->callee, opts->domain, client->local_ip,
		client->local_sip_port, branch, opts->user, opts->domain,
		client->tag, to_header, client->call_id, client->cseq,
		opts->user, client->local_ip, client->local_sip_port);
	if (n < 0 || (size_t)n >= sizeof(msg))
		return -1;
	return send_sip(client, msg);
}

static int send_bye(struct sip_client *client, const struct options *opts,
		    const char *to_header)
{
	char msg[SIP_BUFSZ];
	char resp[SIP_BUFSZ];
	char branch[BRANCHSZ];
	int n;

	make_token(branch, sizeof(branch), "z9hG4bKbye");
	client->cseq++;
	n = snprintf(msg, sizeof(msg),
		"BYE sip:%s@%s SIP/2.0\r\n"
		"Via: SIP/2.0/UDP %s:%u;branch=%s;rport\r\n"
		"Max-Forwards: 70\r\n"
		"From: <sip:%s@%s>;tag=%s\r\n"
		"To: %s\r\n"
		"Call-ID: %s-call\r\n"
		"CSeq: %d BYE\r\n"
		"Content-Length: 0\r\n\r\n",
		opts->callee, opts->domain, client->local_ip,
		client->local_sip_port, branch, opts->user, opts->domain,
		client->tag, to_header, client->call_id, client->cseq);
	if (n < 0 || (size_t)n >= sizeof(msg))
		return -1;
	if (send_sip(client, msg) < 0)
		return -1;
	if (wait_for_status(client, 200, 299, opts->timeout_ms, resp,
			    sizeof(resp)) < 0)
		return -1;
	printf("sip-interop: BYE accepted, media stopped\n");
	client->cseq++;
	return 0;
}

static int run_media(struct sip_client *client, const struct options *opts,
		     const char *remote_ip, uint16_t remote_port)
{
	struct sockaddr_in rtp_addr;
	unsigned char packet[12 + RTP_PAYLOAD_BYTES];
	uint16_t seq = 1000;
	uint32_t timestamp = 16000;
	int received = 0;
	int i;

	memset(&rtp_addr, 0, sizeof(rtp_addr));
	rtp_addr.sin_family = AF_INET;
	rtp_addr.sin_port = htons(remote_port);
	if (inet_pton(AF_INET, remote_ip, &rtp_addr.sin_addr) != 1) {
		fprintf(stderr, "sip-interop: invalid remote RTP IP %s\n",
			remote_ip);
		return -1;
	}

	for (i = 0; i < opts->rtp_packets; i++) {
		ssize_t sent;

		memset(packet, 0xd5, sizeof(packet));
		packet[0] = 0x80;
		packet[1] = 0x00;
		packet[2] = (unsigned char)(seq >> 8);
		packet[3] = (unsigned char)(seq & 0xff);
		packet[4] = (unsigned char)(timestamp >> 24);
		packet[5] = (unsigned char)(timestamp >> 16);
		packet[6] = (unsigned char)(timestamp >> 8);
		packet[7] = (unsigned char)(timestamp & 0xff);
		packet[8] = 0x4f;
		packet[9] = 0x50;
		packet[10] = 0x20;
		packet[11] = 0x06;

		sent = sendto(client->rtp_fd, packet, sizeof(packet), 0,
			      (struct sockaddr *)&rtp_addr, sizeof(rtp_addr));
		if (sent < 0) {
			perror("sendto");
			return -1;
		}
		seq++;
		timestamp += RTP_PAYLOAD_BYTES;

		if (wait_readable(client->rtp_fd, 150) > 0) {
			unsigned char rx[512];

			if (recv(client->rtp_fd, rx, sizeof(rx), 0) > 0)
				received++;
		}
	}

	if (opts->require_rtp_rx && received == 0) {
		fprintf(stderr, "sip-interop: no RTP received from PBX\n");
		return -1;
	}

	printf("sip-interop: media started, sent %d RTP packet(s), received %d\n",
	       opts->rtp_packets, received);
	return 0;
}

static int do_call_cycle(struct sip_client *client, const struct options *opts)
{
	char msg[SIP_BUFSZ];
	char resp[SIP_BUFSZ];
	char branch[BRANCHSZ];
	char remote_ip[SDP_ADDRSZ];
	char to_header[512];
	uint16_t remote_rtp_port;
	int code;
	int n;

	make_token(branch, sizeof(branch), "z9hG4bKinv");
	n = build_invite(msg, sizeof(msg), opts, client, branch);
	if (n < 0 || (size_t)n >= sizeof(msg))
		return -1;
	if (send_sip(client, msg) < 0)
		return -1;

	do {
		code = wait_for_status(client, 100, 699, opts->timeout_ms, resp,
				       sizeof(resp));
		if (code < 0)
			return -1;
	} while (code < 200);

	if (code < 200 || code > 299) {
		fprintf(stderr, "sip-interop: INVITE rejected with %d\n", code);
		return -1;
	}

	if (copy_header_line(resp, "To", to_header, sizeof(to_header)) < 0 ||
	    parse_sdp_media(resp, remote_ip, sizeof(remote_ip),
			    &remote_rtp_port) < 0) {
		fprintf(stderr, "sip-interop: 200 OK missing To/SDP media\n");
		return -1;
	}

	if (send_ack(client, opts, to_header) < 0)
		return -1;
	printf("sip-interop: INVITE answered, remote RTP %s:%u\n", remote_ip,
	       remote_rtp_port);

	if (run_media(client, opts, remote_ip, remote_rtp_port) < 0)
		return -1;
	if (send_bye(client, opts, to_header) < 0)
		return -1;
	client->cseq++;
	return 0;
}

static int run_client(const struct options *opts)
{
	struct sip_client client;
	int rc = 1;

	if (init_client(&client, opts) < 0)
		return 1;

	if (do_register(&client, opts) < 0)
		goto out;
	if (do_call_cycle(&client, opts) < 0)
		goto out;
	if (do_register(&client, opts) < 0)
		goto out;

	printf("sip-interop: REGISTER refresh, INVITE, media, BYE passed\n");
	rc = 0;

out:
	close(client.sip_fd);
	close(client.rtp_fd);
	return rc;
}

static int stub_send(int fd, const struct sockaddr *addr, socklen_t addr_len,
		     const char *msg)
{
	if (sendto(fd, msg, strlen(msg), 0, addr, addr_len) < 0) {
		perror("sendto");
		return -1;
	}
	return 0;
}

static int stub_read(int fd, char *buf, size_t len, struct sockaddr *addr,
		     socklen_t *addr_len)
{
	ssize_t nread;

	if (wait_readable(fd, DEFAULT_TIMEOUT_MS) <= 0)
		return -1;
	nread = recvfrom(fd, buf, len - 1, 0, addr, addr_len);
	if (nread < 0) {
		perror("recvfrom");
		return -1;
	}
	buf[nread] = '\0';
	return 0;
}

static int stub_extract_call_headers(const char *req, char *via, size_t via_len,
				     char *from, size_t from_len,
				     char *to, size_t to_len,
				     char *call_id, size_t call_id_len,
				     char *cseq, size_t cseq_len)
{
	return copy_header_line(req, "Via", via, via_len) == 0 &&
	       copy_header_line(req, "From", from, from_len) == 0 &&
	       copy_header_line(req, "To", to, to_len) == 0 &&
	       copy_header_line(req, "Call-ID", call_id, call_id_len) == 0 &&
	       copy_header_line(req, "CSeq", cseq, cseq_len) == 0 ? 0 : -1;
}

static int stub_send_simple_response(int fd, const char *req,
				     const struct sockaddr *addr,
				     socklen_t addr_len, int code,
				     const char *reason)
{
	char via[512], from[512], to[512], call_id[256], cseq[128];
	char resp[SIP_BUFSZ];
	int n;

	if (stub_extract_call_headers(req, via, sizeof(via), from, sizeof(from),
				      to, sizeof(to), call_id, sizeof(call_id),
				      cseq, sizeof(cseq)) < 0)
		return -1;

	n = snprintf(resp, sizeof(resp),
		"SIP/2.0 %d %s\r\n"
		"Via: %s\r\n"
		"From: %s\r\n"
		"To: %s;tag=stub\r\n"
		"Call-ID: %s\r\n"
		"CSeq: %s\r\n"
		"Content-Length: 0\r\n\r\n",
		code, reason, via, from, to, call_id, cseq);
	if (n < 0 || (size_t)n >= sizeof(resp))
		return -1;
	return stub_send(fd, addr, addr_len, resp);
}

static int stub_send_invite_ok(int fd, const char *req,
			       const struct sockaddr *addr, socklen_t addr_len)
{
	char via[512], from[512], to[512], call_id[256], cseq[128];
	char sdp[512];
	char resp[SIP_BUFSZ];
	int sdp_len;
	int n;

	if (stub_extract_call_headers(req, via, sizeof(via), from, sizeof(from),
				      to, sizeof(to), call_id, sizeof(call_id),
				      cseq, sizeof(cseq)) < 0)
		return -1;

	sdp_len = snprintf(sdp, sizeof(sdp),
		"v=0\r\n"
		"o=stub 1 1 IN IP4 127.0.0.1\r\n"
		"s=stub\r\n"
		"c=IN IP4 127.0.0.1\r\n"
		"t=0 0\r\n"
		"m=audio %u RTP/AVP 0\r\n"
		"a=rtpmap:0 PCMU/8000\r\n"
		"a=sendrecv\r\n",
		STUB_RTP_PORT);
	if (sdp_len < 0 || (size_t)sdp_len >= sizeof(sdp))
		return -1;

	n = snprintf(resp, sizeof(resp),
		"SIP/2.0 200 OK\r\n"
		"Via: %s\r\n"
		"From: %s\r\n"
		"To: %s;tag=stub\r\n"
		"Call-ID: %s\r\n"
		"CSeq: %s\r\n"
		"Contact: <sip:stub@127.0.0.1:%u>\r\n"
		"Content-Type: application/sdp\r\n"
		"Content-Length: %d\r\n\r\n%s",
		via, from, to, call_id, cseq, STUB_SIP_PORT, sdp_len, sdp);
	if (n < 0 || (size_t)n >= sizeof(resp))
		return -1;
	return stub_send(fd, addr, addr_len, resp);
}

static int run_stub_rtp(void)
{
	struct sockaddr_in peer;
	socklen_t peer_len = sizeof(peer);
	unsigned char buf[512];
	uint16_t bound_port;
	int fd;
	int i;

	fd = bind_udp_socket(STUB_RTP_PORT, &bound_port);
	if (fd < 0)
		return 1;

	for (i = 0; i < DEFAULT_RTP_PACKETS; i++) {
		ssize_t nread;

		if (wait_readable(fd, DEFAULT_TIMEOUT_MS) <= 0)
			break;
		nread = recvfrom(fd, buf, sizeof(buf), 0,
				 (struct sockaddr *)&peer, &peer_len);
		if (nread > 0)
			(void)sendto(fd, buf, (size_t)nread, 0,
				     (struct sockaddr *)&peer, peer_len);
	}

	close(fd);
	return 0;
}

static int run_stub_pbx(void)
{
	struct sockaddr_storage peer;
	socklen_t peer_len;
	char req[SIP_BUFSZ];
	uint16_t bound_port;
	pid_t rtp_child = -1;
	int fd;
	int status;
	int rc = 1;

	fd = bind_udp_socket(STUB_SIP_PORT, &bound_port);
	if (fd < 0)
		return 1;

	peer_len = sizeof(peer);
	if (stub_read(fd, req, sizeof(req), (struct sockaddr *)&peer,
		      &peer_len) < 0 || strncmp(req, "REGISTER ", 9) != 0)
		goto out;
	if (stub_send_simple_response(fd, req, (struct sockaddr *)&peer,
				      peer_len, 200, "OK") < 0)
		goto out;

	peer_len = sizeof(peer);
	if (stub_read(fd, req, sizeof(req), (struct sockaddr *)&peer,
		      &peer_len) < 0 || strncmp(req, "INVITE ", 7) != 0)
		goto out;
	if (stub_send_simple_response(fd, req, (struct sockaddr *)&peer,
				      peer_len, 100, "Trying") < 0 ||
	    stub_send_invite_ok(fd, req, (struct sockaddr *)&peer,
				peer_len) < 0)
		goto out;

	rtp_child = fork();
	if (rtp_child < 0) {
		perror("fork");
		goto out;
	}
	if (rtp_child == 0)
		_exit(run_stub_rtp());

	peer_len = sizeof(peer);
	if (stub_read(fd, req, sizeof(req), (struct sockaddr *)&peer,
		      &peer_len) < 0 || strncmp(req, "ACK ", 4) != 0)
		goto out;
	peer_len = sizeof(peer);
	if (stub_read(fd, req, sizeof(req), (struct sockaddr *)&peer,
		      &peer_len) < 0 || strncmp(req, "BYE ", 4) != 0)
		goto out;
	if (stub_send_simple_response(fd, req, (struct sockaddr *)&peer,
				      peer_len, 200, "OK") < 0)
		goto out;

	peer_len = sizeof(peer);
	if (stub_read(fd, req, sizeof(req), (struct sockaddr *)&peer,
		      &peer_len) < 0 || strncmp(req, "REGISTER ", 9) != 0)
		goto out;
	if (stub_send_simple_response(fd, req, (struct sockaddr *)&peer,
				      peer_len, 200, "OK") < 0)
		goto out;
	rc = 0;

out:
	close(fd);
	if (rtp_child > 0)
		(void)waitpid(rtp_child, &status, 0);
	return rc;
}

static int run_self_test(struct options *opts)
{
	struct timespec startup_delay;
	pid_t child;
	int status;
	int rc;

	opts->server_host = "127.0.0.1";
	opts->server_port = "15060";
	opts->domain = "127.0.0.1";
	opts->require_rtp_rx = true;

	child = fork();
	if (child < 0) {
		perror("fork");
		return 1;
	}
	if (child == 0)
		_exit(run_stub_pbx());

	startup_delay.tv_sec = 0;
	startup_delay.tv_nsec = 100000000;
	(void)nanosleep(&startup_delay, NULL);
	rc = run_client(opts);
	if (waitpid(child, &status, 0) < 0) {
		perror("waitpid");
		return 1;
	}
	if (!WIFEXITED(status) || WEXITSTATUS(status) != 0)
		return 1;
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
		return run_self_test(&opts);

	return run_client(&opts);
}
