// SPDX-License-Identifier: GPL-2.0-only
/*
 * RK3588 Type-C port controller binding shim (OP-1989).
 *
 * This module intentionally does not reimplement USB-C policy handling. It
 * instantiates the upstream Linux Type-C controller I2C client selected for
 * the board, then the upstream driver owns TCPM registration and
 * /sys/class/typec exposure.
 */

#include <linux/err.h>
#include <linux/i2c.h>
#include <linux/init.h>
#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/string.h>

#include "tcpc-driver.h"

static const struct omnisight_rk3588_tcpc_binding tcpc_bindings[] = {
	{
		.kind = OMNISIGHT_RK3588_TCPC_FUSB302,
		.name = "fusb302",
		.i2c_type = "fusb302",
		.default_addr = OMNISIGHT_RK3588_TCPC_FUSB302_ADDR,
		.exposes_tcpm = true,
	},
	{
		.kind = OMNISIGHT_RK3588_TCPC_WUSB3801,
		.name = "wusb3801",
		.i2c_type = "wusb3801",
		.default_addr = OMNISIGHT_RK3588_TCPC_WUSB3801_ADDR,
		.exposes_tcpm = false,
	},
	{
		.kind = OMNISIGHT_RK3588_TCPC_TCPM_TCPCI,
		.name = "tcpci",
		.i2c_type = "tcpci",
		.default_addr = OMNISIGHT_RK3588_TCPC_FUSB302_ADDR,
		.exposes_tcpm = true,
	},
};

static char controller[16] = "fusb302";
module_param_string(controller, controller, sizeof(controller), 0444);
MODULE_PARM_DESC(controller,
		 "upstream controller driver: fusb302, wusb3801, or tcpci");

static int bus_num = OMNISIGHT_RK3588_TCPC_DEFAULT_BUS;
module_param(bus_num, int, 0444);
MODULE_PARM_DESC(bus_num,
		 "RK3588 I2C bus number carrying the Type-C controller");

static unsigned short i2c_addr;
module_param(i2c_addr, ushort, 0444);
MODULE_PARM_DESC(i2c_addr,
		 "Type-C controller I2C address; 0 keeps controller default");

static struct i2c_client *tcpc_client;

const struct omnisight_rk3588_tcpc_binding *
omnisight_rk3588_tcpc_binding_by_name(const char *name)
{
	size_t i;

	for (i = 0; i < ARRAY_SIZE(tcpc_bindings); i++) {
		if (!strcmp(name, tcpc_bindings[i].name))
			return &tcpc_bindings[i];
	}

	return NULL;
}
EXPORT_SYMBOL_GPL(omnisight_rk3588_tcpc_binding_by_name);

static int omnisight_rk3588_tcpc_register_client(
	const struct omnisight_rk3588_tcpc_binding *binding)
{
	struct i2c_board_info info = {};
	struct i2c_adapter *adapter;
	unsigned short addr = i2c_addr ?: binding->default_addr;

	strscpy(info.type, binding->i2c_type, sizeof(info.type));
	info.addr = addr;

	adapter = i2c_get_adapter(bus_num);
	if (!adapter) {
		pr_info("omnisight-rk3588-tcpc: i2c-%d unavailable; defer to DTS or real hardware\n",
			bus_num);
		return 0;
	}

	tcpc_client = i2c_new_client_device(adapter, &info);
	i2c_put_adapter(adapter);
	if (IS_ERR(tcpc_client)) {
		int ret = PTR_ERR(tcpc_client);

		tcpc_client = NULL;
		pr_err("omnisight-rk3588-tcpc: failed to bind %s at i2c-%d/0x%02x: %d\n",
		       binding->i2c_type, bus_num, addr, ret);
		return ret;
	}

	pr_info("omnisight-rk3588-tcpc: bound upstream %s at i2c-%d/0x%02x\n",
		binding->i2c_type, bus_num, addr);
	pr_info("omnisight-rk3588-tcpc: expect %s after TCPM probe\n",
		OMNISIGHT_RK3588_TCPC_SYSFS_CLASS);
	if (!binding->exposes_tcpm)
		pr_warn("omnisight-rk3588-tcpc: %s lacks FUSB302-class TCPM PD\n",
			binding->name);

	return 0;
}

static int __init omnisight_rk3588_tcpc_init(void)
{
	const struct omnisight_rk3588_tcpc_binding *binding;

	binding = omnisight_rk3588_tcpc_binding_by_name(controller);
	if (!binding) {
		pr_err("omnisight-rk3588-tcpc: unsupported controller '%s'\n",
		       controller);
		pr_err("omnisight-rk3588-tcpc: HUSB311/FUSB304 need vendor follow-up\n");
		return -EINVAL;
	}

	return omnisight_rk3588_tcpc_register_client(binding);
}

static void __exit omnisight_rk3588_tcpc_exit(void)
{
	if (tcpc_client)
		i2c_unregister_device(tcpc_client);
}

module_init(omnisight_rk3588_tcpc_init);
module_exit(omnisight_rk3588_tcpc_exit);

MODULE_DESCRIPTION("OmniSight RK3588 Type-C TCPC upstream-driver binding");
MODULE_AUTHOR("GPT-5.5 (codex-cli) <noreply@openai.com>");
MODULE_LICENSE("GPL");
MODULE_SOFTDEP("pre: fusb302");
