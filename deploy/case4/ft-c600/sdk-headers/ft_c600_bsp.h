#ifndef OMNISIGHT_CASE4_FT_C600_BSP_H
#define OMNISIGHT_CASE4_FT_C600_BSP_H

#include <stddef.h>
#include <stdint.h>

#define OMNISIGHT_FT_C600_VENDOR_ID "ft-c600"
#define OMNISIGHT_FT_C600_SOC_NAME "Fullhan MC6358"
#define OMNISIGHT_FT_C600_CASE2_REPO "https://github.com/limit5/UVCCamera_Qt.git"
#define OMNISIGHT_FT_C600_XU_GUID_SIZE 16

static const uint8_t omnisight_ft_c600_xu_guid[OMNISIGHT_FT_C600_XU_GUID_SIZE] = {
	0x96, 0x9e, 0x20, 0x20,
	0xf1, 0x90,
	0x40, 0xa5,
	0x90, 0x75, 0xf9, 0x3d, 0x7a, 0xba, 0x9d, 0x05,
};

struct omnisight_ft_c600_vid_pid {
	uint16_t vid;
	uint16_t pid;
};

struct omnisight_ft_c600_bsp_descriptor {
	const char *vendor_id;
	const char *soc_name;
	const char *case2_repo;
	const uint8_t *xu_guid;
	size_t xu_guid_size;
	const struct omnisight_ft_c600_vid_pid *vid_pid;
	size_t vid_pid_count;
};

static inline struct omnisight_ft_c600_bsp_descriptor
omnisight_ft_c600_bsp_descriptor(void)
{
	struct omnisight_ft_c600_bsp_descriptor desc = {
		.vendor_id = OMNISIGHT_FT_C600_VENDOR_ID,
		.soc_name = OMNISIGHT_FT_C600_SOC_NAME,
		.case2_repo = OMNISIGHT_FT_C600_CASE2_REPO,
		.xu_guid = omnisight_ft_c600_xu_guid,
		.xu_guid_size = OMNISIGHT_FT_C600_XU_GUID_SIZE,
		.vid_pid = NULL,
		.vid_pid_count = 0,
	};

	return desc;
}

#endif /* OMNISIGHT_CASE4_FT_C600_BSP_H */
