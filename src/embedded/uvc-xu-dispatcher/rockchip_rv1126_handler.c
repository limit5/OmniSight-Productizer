#include <stddef.h>
#include <stdint.h>

struct uvc_xu_guid {
	uint32_t data1;
	uint16_t data2;
	uint16_t data3;
	uint8_t data4[8];
};

struct uvc_xu_vid_pid {
	uint16_t vid, pid;
};

struct uvc_xu_cmd_type {
	uint8_t selector;
	const char *name;
};

struct uvc_xu_vendor_adapter {
	const char *vendor_id;
	struct uvc_xu_guid xu_guid;
	const struct uvc_xu_vid_pid *vid_pid;
	size_t vid_pid_count;
	const struct uvc_xu_cmd_type *cmd_types;
	size_t cmd_type_count;
};

extern void register_vendor(const struct uvc_xu_vendor_adapter *adapter);

static const struct uvc_xu_vendor_adapter rockchip_rv1126_adapter = {
	.vendor_id = "rockchip-rv1126",
	.xu_guid = {
		.data1 = 0xB1B1B1B2,
		.data2 = 0x0001,
		.data3 = 0x4000,
		.data4 = { 0x91, 0x01, 0xF9, 0x3D, 0x7A, 0xBA, 0x9D, 0x06 },
	},
	/* Placeholder mapping awaiting HIL: do not ship real VID:PID yet. */
	.vid_pid = NULL,
	.vid_pid_count = 0,
	.cmd_types = NULL,
	.cmd_type_count = 0,
};

__attribute__((constructor))
static void register_rockchip_rv1126_handler(void)
{
	register_vendor(&rockchip_rv1126_adapter);
}
