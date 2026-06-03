/* SPDX-License-Identifier: MIT
 *
 * Case 5 Zoom Linux Meeting SDK adapter placeholder (OP-2005).
 *
 * The production Zoom SDK headers are NDA-gated and are expected to be
 * supplied by the OP-491-style mirror during embedded builds. This header
 * keeps the adapter buildable in source-only environments and defines the
 * narrow bridge that the real SDK binding must satisfy.
 */
#ifndef OMNISIGHT_EMBEDDED_CONFERENCE_VENDOR_ZOOM_SDK_STUB_H_
#define OMNISIGHT_EMBEDDED_CONFERENCE_VENDOR_ZOOM_SDK_STUB_H_

#ifdef __cplusplus
extern "C" {
#endif

typedef struct omnisight_zoom_sdk omnisight_zoom_sdk;

struct omnisight_zoom_sdk_config {
	const char *sdk_root;
	const char *app_key;
	const char *app_secret;
	const char *glibc_abi;
};

struct omnisight_zoom_meeting_options {
	const char *meeting_number;
	const char *password;
	const char *display_name;
	const char *zak_token;
	const char *join_token;
};

enum omnisight_zoom_sdk_status {
	OMNISIGHT_ZOOM_SDK_OK = 0,
	OMNISIGHT_ZOOM_SDK_INVALID_ARGUMENT = 1,
	OMNISIGHT_ZOOM_SDK_UNAVAILABLE = 2,
	OMNISIGHT_ZOOM_SDK_INVALID_STATE = 3,
	OMNISIGHT_ZOOM_SDK_BACKEND_ERROR = 4,
};

#if defined(OMNISIGHT_ZOOM_WITH_LINUX_SDK)
enum omnisight_zoom_sdk_status
omnisight_zoom_sdk_create(const struct omnisight_zoom_sdk_config *config,
			  omnisight_zoom_sdk **sdk);
void omnisight_zoom_sdk_destroy(omnisight_zoom_sdk *sdk);
const char *omnisight_zoom_sdk_last_error(const omnisight_zoom_sdk *sdk);
enum omnisight_zoom_sdk_status
omnisight_zoom_sdk_start_meeting(
	omnisight_zoom_sdk *sdk,
	const struct omnisight_zoom_meeting_options *options);
enum omnisight_zoom_sdk_status
omnisight_zoom_sdk_join_meeting(
	omnisight_zoom_sdk *sdk,
	const struct omnisight_zoom_meeting_options *options);
enum omnisight_zoom_sdk_status
omnisight_zoom_sdk_leave_meeting(omnisight_zoom_sdk *sdk);
enum omnisight_zoom_sdk_status
omnisight_zoom_sdk_start_media(omnisight_zoom_sdk *sdk, int audio, int video);
enum omnisight_zoom_sdk_status
omnisight_zoom_sdk_stop_media(omnisight_zoom_sdk *sdk, int audio, int video);
#else
static inline enum omnisight_zoom_sdk_status
omnisight_zoom_sdk_create(const struct omnisight_zoom_sdk_config *config,
			  omnisight_zoom_sdk **sdk)
{
	(void)config;
	if (sdk)
		*sdk = 0;
	return OMNISIGHT_ZOOM_SDK_UNAVAILABLE;
}

static inline void omnisight_zoom_sdk_destroy(omnisight_zoom_sdk *sdk)
{
	(void)sdk;
}

static inline const char *
omnisight_zoom_sdk_last_error(const omnisight_zoom_sdk *sdk)
{
	(void)sdk;
	return "Zoom Linux Meeting SDK bridge is not linked";
}

static inline enum omnisight_zoom_sdk_status
omnisight_zoom_sdk_start_meeting(
	omnisight_zoom_sdk *sdk,
	const struct omnisight_zoom_meeting_options *options)
{
	(void)sdk;
	(void)options;
	return OMNISIGHT_ZOOM_SDK_UNAVAILABLE;
}

static inline enum omnisight_zoom_sdk_status
omnisight_zoom_sdk_join_meeting(
	omnisight_zoom_sdk *sdk,
	const struct omnisight_zoom_meeting_options *options)
{
	(void)sdk;
	(void)options;
	return OMNISIGHT_ZOOM_SDK_UNAVAILABLE;
}

static inline enum omnisight_zoom_sdk_status
omnisight_zoom_sdk_leave_meeting(omnisight_zoom_sdk *sdk)
{
	(void)sdk;
	return OMNISIGHT_ZOOM_SDK_UNAVAILABLE;
}

static inline enum omnisight_zoom_sdk_status
omnisight_zoom_sdk_start_media(omnisight_zoom_sdk *sdk, int audio, int video)
{
	(void)sdk;
	(void)audio;
	(void)video;
	return OMNISIGHT_ZOOM_SDK_UNAVAILABLE;
}

static inline enum omnisight_zoom_sdk_status
omnisight_zoom_sdk_stop_media(omnisight_zoom_sdk *sdk, int audio, int video)
{
	(void)sdk;
	(void)audio;
	(void)video;
	return OMNISIGHT_ZOOM_SDK_UNAVAILABLE;
}
#endif

#ifdef __cplusplus
}
#endif

#endif // OMNISIGHT_EMBEDDED_CONFERENCE_VENDOR_ZOOM_SDK_STUB_H_
