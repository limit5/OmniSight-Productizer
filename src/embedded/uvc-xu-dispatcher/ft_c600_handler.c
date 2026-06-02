#include <stddef.h>
#include <stdint.h>

struct uvc_xu_guid {
	uint32_t data1;
	uint16_t data2;
	uint16_t data3;
	uint8_t data4[8];
};

struct uvc_xu_vendor_adapter {
	const char *vendor_id;
	struct uvc_xu_guid xu_guid;
	const void *vid_pids;
	size_t vid_pid_count;
	const void *cmd_types;
	size_t cmd_type_count;
};

extern int register_vendor(const struct uvc_xu_vendor_adapter *adapter);

static const struct uvc_xu_vendor_adapter ft_c600_adapter = {
	.vendor_id = "ft-c600",
	.xu_guid = {
		.data1 = 0x20209E96,
		.data2 = 0x90F1,
		.data3 = 0xA540,
		.data4 = { 0x90, 0x75, 0xF9, 0x3D, 0x7A, 0xBA, 0x9D, 0x05 },
	},
	/* VID:PID and command maps are awaiting HIL validation. */
	.vid_pids = NULL,
	.vid_pid_count = 0,
	.cmd_types = NULL,
	.cmd_type_count = 0,
};

static void __attribute__((constructor)) register_ft_c600_handler(void)
{
	(void)register_vendor(&ft_c600_adapter);
}
