/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * RK3588 Type-C port controller binding shim (OP-1989).
 *
 * The ATK-DLRK3588 path defaults to the upstream FUSB302 TCPM driver. This
 * header keeps the board-level binding constants separate from the module
 * glue so downstream DTS or initramfs probes use one source of truth.
 */
#ifndef OMNISIGHT_RK3588_TCPC_DRIVER_H
#define OMNISIGHT_RK3588_TCPC_DRIVER_H

#include <linux/types.h>

#define OMNISIGHT_RK3588_TCPC_DEFAULT_BUS 4
#define OMNISIGHT_RK3588_TCPC_FUSB302_ADDR 0x22
#define OMNISIGHT_RK3588_TCPC_WUSB3801_ADDR 0x60

#define OMNISIGHT_RK3588_TCPC_SYSFS_CLASS "/sys/class/typec"

enum omnisight_rk3588_tcpc_kind {
	OMNISIGHT_RK3588_TCPC_FUSB302 = 0,
	OMNISIGHT_RK3588_TCPC_WUSB3801,
	OMNISIGHT_RK3588_TCPC_TCPM_TCPCI,
};

struct omnisight_rk3588_tcpc_binding {
	enum omnisight_rk3588_tcpc_kind kind;
	const char *name;
	const char *i2c_type;
	unsigned short default_addr;
	bool exposes_tcpm;
};

const struct omnisight_rk3588_tcpc_binding *
omnisight_rk3588_tcpc_binding_by_name(const char *name);

#endif /* OMNISIGHT_RK3588_TCPC_DRIVER_H */
