#include "../../src/embedded/uvc-xu-dispatcher/vendor_registry.h"

#include <stdio.h>

struct handler_probe {
	const char *vendor_id;
	uint16_t vid;
	uint16_t pid;
};

static int assert_handler_registered(const struct handler_probe *probe)
{
	const struct vendor_adapter *adapter;

	adapter = find_by_vid_pid(probe->vid, probe->pid);
	if (!adapter) {
		fprintf(stderr, "%s handler was not registered\n",
			probe->vendor_id);
		return 1;
	}

	if (!adapter->vendor_id || adapter->vendor_id[0] == '\0') {
		fprintf(stderr, "%s handler has empty vendor_id\n",
			probe->vendor_id);
		return 1;
	}

	return 0;
}

int main(void)
{
	static const struct handler_probe probes[] = {
		{ "ft-c600", 0x2207, 0xc600 },
		{ "qualcomm", 0x18d1, 0xd00d },
		{ "mediatek", 0x0e8d, 0x0003 },
		{ "rockchip-rk35xx", 0x2207, 0x350a },
		{ "rockchip-rv1126", 0x2207, 0x110c },
	};
	size_t i;

	for (i = 0; i < sizeof(probes) / sizeof(probes[0]); i++) {
		if (assert_handler_registered(&probes[i]))
			return 1;
	}

	return 0;
}
