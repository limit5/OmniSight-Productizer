#include <stddef.h>
#include <stdint.h>

struct uvc_xu_guid {
	uint32_t data1;
	uint16_t data2;
	uint16_t data3;
	uint8_t data4[8];
};

struct uvc_xu_vid_pid {
	uint16_t vid;
	uint16_t pid;
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

static const struct uvc_xu_vendor_adapter rockchip_rk35xx_adapter = {
	.vendor_id = "rockchip-rk35xx",
	.xu_guid = {
		.data1 = 0xB1B1B1B1,
		.data2 = 0x0001,
		.data3 = 0x4000,
		.data4 = { 0x91, 0x01, 0xF9, 0x3D, 0x7A, 0xBA, 0x9D, 0x05 },
	},
	/* Placeholder adapter: VID:PID entries are awaiting HIL. */
	.vid_pid = NULL,
	.vid_pid_count = 0,
	.cmd_types = NULL,
	.cmd_type_count = 0,
};

static void __attribute__((constructor)) rockchip_rk35xx_handler_init(void)
{
	register_vendor(&rockchip_rk35xx_adapter);
}
