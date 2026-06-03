/* SPDX-License-Identifier: MIT
 *
 * audio-router daemon skeleton (OP-1991).
 */
#ifndef UAC_GADGET_AUDIO_ROUTER_DAEMON_H
#define UAC_GADGET_AUDIO_ROUTER_DAEMON_H

#include "probe_alsa_devices.h"

#define AUDIO_ROUTER_SOCKET_PATH "/run/audio-router.sock"
#define AUDIO_ROUTER_SYSFS_ROOT PROBE_ALSA_SYSFS_ROOT

int audio_router_probe_devices(const char *sysfs_root,
			       struct probe_alsa_device_list *list);
int audio_router_run_daemon(const char *socket_path, const char *sysfs_root);

#endif /* UAC_GADGET_AUDIO_ROUTER_DAEMON_H */
