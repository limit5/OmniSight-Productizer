#include "vendor_registry.h"

#include <stdio.h>

static const struct vendor_vid_pid rockchip_rk35xx_vid_pid[] = {
	{ 0x2207, 0x350a },
};

static const struct vendor_adapter rockchip_rk35xx_adapter = {
	.vendor_id = "rockchip-rk35xx",
	.xu_guid = {
		0xb1, 0xb1, 0xb1, 0xb1, 0x01, 0x00, 0x00, 0x40,
		0x91, 0x01, 0xf9, 0x3d, 0x7a, 0xba, 0x9d, 0x05,
	},
	/* Placeholder adapter: VID:PID entries are awaiting HIL. */
	.vid_pid = rockchip_rk35xx_vid_pid,
	.vid_pid_count = 1,
	.cmd_types = NULL,
	.cmd_type_count = 0,
};

static void __attribute__((constructor)) rockchip_rk35xx_handler_init(void)
{
	int rc = register_vendor(&rockchip_rk35xx_adapter);

	if (rc)
		fprintf(stderr, "rockchip-rk35xx: register_vendor failed rc=%d\n", rc);
}
