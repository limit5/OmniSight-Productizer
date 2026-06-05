#include "vendor_registry.h"

#include <stdio.h>

static const struct vendor_vid_pid qualcomm_vid_pid[] = {
	{ 0x18d1, 0xd00d },
};

static const struct vendor_adapter qualcomm_adapter = {
	.vendor_id = "qualcomm",
	.xu_guid = {
		0xc2, 0xc2, 0xc2, 0xc2, 0x02, 0x00, 0x00, 0x40,
		0x91, 0x02, 0xf9, 0x3d, 0x7a, 0xba, 0x9d, 0x05,
	},
	/* Placeholder mapping awaiting Radxa Dragon Q6A HIL validation. */
	.vid_pid = qualcomm_vid_pid,
	.vid_pid_count = 1,
	.cmd_types = NULL,
	.cmd_type_count = 0,
};

static void __attribute__((constructor)) register_qualcomm_handler(void)
{
	int rc = register_vendor(&qualcomm_adapter);

	if (rc)
		fprintf(stderr, "qualcomm: register_vendor failed rc=%d\n", rc);
}
