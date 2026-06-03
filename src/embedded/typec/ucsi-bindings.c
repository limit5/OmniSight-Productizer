// SPDX-License-Identifier: GPL-2.0-only
/*
 * UCSI binding wrapper for Case 5 Type-C exposure (OP-1998).
 *
 * The Linux UCSI core already translates connector status, PD contracts,
 * cable identity, and alternate modes into Type-C class devices. Keep this
 * file as a narrow registration layer over drivers/usb/typec/ucsi/.
 */

#include <linux/err.h>
#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/slab.h>

#include "ucsi-bindings.h"

struct ucsi;

struct ucsi *ucsi_create(struct device *dev, const struct ucsi_operations *ops);
void ucsi_destroy(struct ucsi *ucsi);
int ucsi_register(struct ucsi *ucsi);
void ucsi_unregister(struct ucsi *ucsi);

struct ucsi_handler {
	struct device *dev;
	struct ucsi *ucsi;
	unsigned int connector_count;
	bool supports_pd_contract;
	bool supports_alt_modes;
	bool registered;
};

static int ucsi_validate_config(const struct ucsi_handler_config *config)
{
	if (!config || !config->ops)
		return -EINVAL;

	if (config->connector_count == 0)
		return -EINVAL;

	return 0;
}

struct ucsi_handler *
ucsi_register_handler(struct device *dev, const struct ucsi_handler_config *config)
{
	struct ucsi_handler *handler;
	const char *name;
	int ret;

	ret = ucsi_validate_config(config);
	if (ret)
		return ERR_PTR(ret);

	if (!dev)
		return ERR_PTR(-EINVAL);

	handler = kzalloc(sizeof(*handler), GFP_KERNEL);
	if (!handler)
		return ERR_PTR(-ENOMEM);

	handler->dev = dev;
	handler->connector_count = config->connector_count;
	handler->supports_pd_contract = config->supports_pd_contract;
	handler->supports_alt_modes = config->supports_alt_modes;

	handler->ucsi = ucsi_create(dev, config->ops);
	if (IS_ERR(handler->ucsi)) {
		ret = PTR_ERR(handler->ucsi);
		kfree(handler);
		return ERR_PTR(ret);
	}

	ret = ucsi_register(handler->ucsi);
	if (ret) {
		ucsi_destroy(handler->ucsi);
		kfree(handler);
		return ERR_PTR(ret);
	}

	handler->registered = true;
	name = config->name ?: dev_name(dev);
	dev_info(dev,
		 "omnisight-ucsi: registered %s with %u connector(s); sysfs=%s pd=%s altmodes=%s\n",
		 name, handler->connector_count, OMNISIGHT_UCSI_SYSFS_CLASS,
		 handler->supports_pd_contract ? "yes" : "no",
		 handler->supports_alt_modes ? "yes" : "no");

	return handler;
}
EXPORT_SYMBOL_GPL(ucsi_register_handler);

void ucsi_cleanup_handler(struct ucsi_handler *handler)
{
	if (!handler)
		return;

	if (handler->registered)
		ucsi_unregister(handler->ucsi);

	ucsi_destroy(handler->ucsi);
	kfree(handler);
}
EXPORT_SYMBOL_GPL(ucsi_cleanup_handler);

MODULE_DESCRIPTION("OmniSight UCSI binding wrapper for Type-C sysfs exposure");
MODULE_AUTHOR("GPT-5.5 (codex-cli) <noreply@openai.com>");
MODULE_LICENSE("GPL");
