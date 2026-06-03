/* SPDX-License-Identifier: MIT
 *
 * USB UAC2 gadget configfs validator (OP-1985).
 */
#ifndef OMNISIGHT_UAC2_CONFIG_H
#define OMNISIGHT_UAC2_CONFIG_H

#include <stddef.h>

#define OMNISIGHT_UAC2_DEFAULT_CONFIGFS_ROOT "/sys/kernel/config/usb_gadget"
#define OMNISIGHT_UAC2_DEFAULT_GADGET_NAME "omnisight-uac2"
#define OMNISIGHT_UAC2_DEFAULT_FUNCTION_NAME "uac2.usb0"
#define OMNISIGHT_UAC2_DEFAULT_CONFIG_NAME "c.1"
#define OMNISIGHT_UAC2_DEFAULT_SAMPLE_RATE "48000"
#define OMNISIGHT_UAC2_DEFAULT_SAMPLE_SIZE "2"
#define OMNISIGHT_UAC2_DEFAULT_CHANNEL_MASK "0x3"
#define OMNISIGHT_UAC2_MAX_PATH 512
#define OMNISIGHT_UAC2_MAX_TEXT 128

struct omnisight_uac2_config {
	const char *configfs_root;
	const char *gadget_name;
	const char *function_name;
	const char *config_name;
	const char *sample_rate;
	const char *sample_size;
	const char *channel_mask;
	int require_bound_udc;
};

int omnisight_uac2_default_config(struct omnisight_uac2_config *config);
int omnisight_uac2_validate(const struct omnisight_uac2_config *config);

#endif /* OMNISIGHT_UAC2_CONFIG_H */
