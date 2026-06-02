#include "vendor_registry.h"

#include <errno.h>
#include <pthread.h>
#include <stdlib.h>
#include <string.h>

struct vendor_registry_entry {
	const struct vendor_adapter *adapter;
	struct vendor_registry_entry *next;
};

static pthread_mutex_t registry_lock = PTHREAD_MUTEX_INITIALIZER;
static struct vendor_registry_entry *registry_head;

static int adapter_has_vid_pid(const struct vendor_adapter *adapter,
			       uint16_t vid, uint16_t pid)
{
	size_t i;

	for (i = 0; i < adapter->vid_pid_count; i++) {
		if (adapter->vid_pid[i].vid == vid &&
		    adapter->vid_pid[i].pid == pid)
			return 1;
	}

	return 0;
}

static int adapter_matches_guid(const struct vendor_adapter *adapter,
				const uint8_t *xu_guid)
{
	return memcmp(adapter->xu_guid, xu_guid,
		      VENDOR_REGISTRY_XU_GUID_SIZE) == 0;
}

static int adapter_is_valid(const struct vendor_adapter *adapter)
{
	if (!adapter || !adapter->vendor_id || !adapter->vid_pid ||
	    adapter->vid_pid_count == 0 ||
	    (adapter->cmd_type_count && !adapter->cmd_types))
		return 0;

	return 1;
}

int register_vendor(const struct vendor_adapter *adapter)
{
	struct vendor_registry_entry *entry;
	struct vendor_registry_entry *cursor;
	int rc = 0;

	if (!adapter_is_valid(adapter))
		return -EINVAL;

	entry = malloc(sizeof(*entry));
	if (!entry)
		return -ENOMEM;

	entry->adapter = adapter;
	entry->next = NULL;

	pthread_mutex_lock(&registry_lock);
	for (cursor = registry_head; cursor; cursor = cursor->next) {
		if (cursor->adapter == adapter) {
			rc = -EEXIST;
			break;
		}
	}

	if (!rc) {
		entry->next = registry_head;
		registry_head = entry;
	}
	pthread_mutex_unlock(&registry_lock);

	if (rc)
		free(entry);

	return rc;
}

const struct vendor_adapter *find_by_vid_pid(uint16_t vid, uint16_t pid)
{
	const struct vendor_adapter *adapter = NULL;
	struct vendor_registry_entry *cursor;

	pthread_mutex_lock(&registry_lock);
	for (cursor = registry_head; cursor; cursor = cursor->next) {
		if (adapter_has_vid_pid(cursor->adapter, vid, pid)) {
			adapter = cursor->adapter;
			break;
		}
	}
	pthread_mutex_unlock(&registry_lock);

	return adapter;
}

const struct vendor_adapter *find_by_guid(
	const uint8_t xu_guid[VENDOR_REGISTRY_XU_GUID_SIZE])
{
	const struct vendor_adapter *adapter = NULL;
	struct vendor_registry_entry *cursor;

	if (!xu_guid)
		return NULL;

	pthread_mutex_lock(&registry_lock);
	for (cursor = registry_head; cursor; cursor = cursor->next) {
		if (adapter_matches_guid(cursor->adapter, xu_guid)) {
			adapter = cursor->adapter;
			break;
		}
	}
	pthread_mutex_unlock(&registry_lock);

	return adapter;
}
