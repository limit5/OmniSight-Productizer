/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * UCSI binding wrapper for Case 5 Type-C exposure (OP-1998).
 *
 * Board or transport-specific code supplies the low-level UCSI operations.
 * This layer owns registration with the upstream Linux UCSI core so the
 * kernel exposes connectors through /sys/class/typec.
 */
#ifndef OMNISIGHT_UCSI_BINDINGS_H
#define OMNISIGHT_UCSI_BINDINGS_H

#include <linux/device.h>
#include <linux/types.h>

#define OMNISIGHT_UCSI_DEFAULT_CONNECTORS 1
#define OMNISIGHT_UCSI_SYSFS_CLASS "/sys/class/typec"

struct ucsi_operations;
struct ucsi_handler;

struct ucsi_handler_config {
	const char *name;
	const struct ucsi_operations *ops;
	unsigned int connector_count;
	bool supports_pd_contract;
	bool supports_alt_modes;
};

struct ucsi_handler *
ucsi_register_handler(struct device *dev,
		      const struct ucsi_handler_config *config);
void ucsi_cleanup_handler(struct ucsi_handler *handler);

#endif /* OMNISIGHT_UCSI_BINDINGS_H */
