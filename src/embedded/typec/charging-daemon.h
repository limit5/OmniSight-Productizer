/* SPDX-License-Identifier: MIT
 *
 * RK3588 userspace charging daemon (OP-2000).
 */
#ifndef OMNISIGHT_CHARGING_DAEMON_H
#define OMNISIGHT_CHARGING_DAEMON_H

#include <stddef.h>

#define CHARGING_DAEMON_SOCKET_PATH "/run/charging-daemon.sock"
#define CHARGING_DAEMON_SYSFS_ROOT "/sys/class/typec"
#define CHARGING_DAEMON_MAX_PORTS 8
#define CHARGING_DAEMON_MAX_NAME 64
#define CHARGING_DAEMON_MAX_PATH 256
#define CHARGING_DAEMON_MAX_TEXT 64
#define CHARGING_DAEMON_MAX_PDOS 16

enum charging_daemon_role_preference {
	CHARGING_DAEMON_ROLE_AUTO = 0,
	CHARGING_DAEMON_ROLE_SINK,
	CHARGING_DAEMON_ROLE_SOURCE,
};

struct charging_daemon_pdo {
	unsigned int millivolts;
	unsigned int milliamps;
};

struct charging_daemon_port {
	char name[CHARGING_DAEMON_MAX_NAME];
	char sysfs_path[CHARGING_DAEMON_MAX_PATH];
	char partner_path[CHARGING_DAEMON_MAX_PATH];
	char power_role[CHARGING_DAEMON_MAX_TEXT];
	char data_role[CHARGING_DAEMON_MAX_TEXT];
	char port_type[CHARGING_DAEMON_MAX_TEXT];
	char pd_revision[CHARGING_DAEMON_MAX_TEXT];
	struct charging_daemon_pdo source_pdos[CHARGING_DAEMON_MAX_PDOS];
	size_t source_pdo_count;
	struct charging_daemon_pdo contract;
	int has_partner;
	int pd_capable;
	int role_swap_supported;
};

struct charging_daemon_state {
	struct charging_daemon_port ports[CHARGING_DAEMON_MAX_PORTS];
	size_t count;
	enum charging_daemon_role_preference role_preference;
};

int charging_daemon_scan_sysfs(const char *root,
			       struct charging_daemon_state *state);
int charging_daemon_run(const char *socket_path, const char *sysfs_root);

#endif /* OMNISIGHT_CHARGING_DAEMON_H */
