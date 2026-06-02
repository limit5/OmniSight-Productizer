#ifndef UVC_XU_DISPATCHER_VENDOR_REGISTRY_H
#define UVC_XU_DISPATCHER_VENDOR_REGISTRY_H

#include <stddef.h>
#include <stdint.h>

#define VENDOR_REGISTRY_XU_GUID_SIZE 16

struct vendor_vid_pid {
	uint16_t vid;
	uint16_t pid;
};

/* Registered adapters are retained by pointer and must outlive the registry. */
struct vendor_adapter {
	const char *vendor_id;
	uint8_t xu_guid[VENDOR_REGISTRY_XU_GUID_SIZE];
	const struct vendor_vid_pid *vid_pid;
	size_t vid_pid_count;
	const uint32_t *cmd_types;
	size_t cmd_type_count;
};

int register_vendor(const struct vendor_adapter *adapter);
const struct vendor_adapter *find_by_vid_pid(uint16_t vid, uint16_t pid);
const struct vendor_adapter *find_by_guid(
	const uint8_t xu_guid[VENDOR_REGISTRY_XU_GUID_SIZE]);

#endif /* UVC_XU_DISPATCHER_VENDOR_REGISTRY_H */
