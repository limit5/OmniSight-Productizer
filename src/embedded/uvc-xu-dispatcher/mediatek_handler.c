#include "vendor_registry.h"

#include <stdio.h>

static const struct vendor_vid_pid mediatek_vid_pid[] = {
	{ 0x0e8d, 0x0003 },
};

static const struct vendor_adapter mediatek_adapter = {
	.vendor_id = "mediatek",
	.xu_guid = {
		0xd3, 0xd3, 0xd3, 0xd3, 0x03, 0x00, 0x00, 0x40,
		0x91, 0x03, 0xf9, 0x3d, 0x7a, 0xba, 0x9d, 0x05,
	},
	/* Placeholder adapter: VID:PID entries are awaiting HIL. */
	.vid_pid = mediatek_vid_pid,
	.vid_pid_count = 1,
	.cmd_types = NULL,
	.cmd_type_count = 0,
};

static void __attribute__((constructor)) mediatek_handler_init(void)
{
	int rc = register_vendor(&mediatek_adapter);

	if (rc)
		fprintf(stderr, "mediatek: register_vendor failed rc=%d\n", rc);
}
