#include "vendor_registry.h"

#include <stdio.h>

static const struct vendor_vid_pid rockchip_rv1126_vid_pid[] = {
	{ 0x2207, 0x110c },
};

static const struct vendor_adapter rockchip_rv1126_adapter = {
	.vendor_id = "rockchip-rv1126",
	.xu_guid = {
		0xb2, 0xb1, 0xb1, 0xb1, 0x01, 0x00, 0x00, 0x40,
		0x91, 0x01, 0xf9, 0x3d, 0x7a, 0xba, 0x9d, 0x06,
	},
	/* Placeholder mapping awaiting HIL: do not ship real VID:PID yet. */
	.vid_pid = rockchip_rv1126_vid_pid,
	.vid_pid_count = 1,
	.cmd_types = NULL,
	.cmd_type_count = 0,
};

__attribute__((constructor))
static void register_rockchip_rv1126_handler(void)
{
	int rc = register_vendor(&rockchip_rv1126_adapter);

	if (rc)
		fprintf(stderr, "rockchip-rv1126: register_vendor failed rc=%d\n", rc);
}
