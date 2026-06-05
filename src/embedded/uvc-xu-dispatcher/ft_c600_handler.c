#include "vendor_registry.h"

#include <stdio.h>

static const struct vendor_vid_pid ft_c600_vid_pid[] = {
	{ 0x2207, 0xc600 },
};

static const struct vendor_adapter ft_c600_adapter = {
	.vendor_id = "ft-c600",
	.xu_guid = {
		0x96, 0x9e, 0x20, 0x20, 0xf1, 0x90, 0x40, 0xa5,
		0x90, 0x75, 0xf9, 0x3d, 0x7a, 0xba, 0x9d, 0x05,
	},
	/* VID:PID and command maps are awaiting HIL validation. */
	.vid_pid = ft_c600_vid_pid,
	.vid_pid_count = 1,
	.cmd_types = NULL,
	.cmd_type_count = 0,
};

static void __attribute__((constructor)) register_ft_c600_handler(void)
{
	int rc = register_vendor(&ft_c600_adapter);

	if (rc)
		fprintf(stderr, "ft-c600: register_vendor failed rc=%d\n", rc);
}
