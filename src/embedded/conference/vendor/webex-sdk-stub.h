/* SPDX-License-Identifier: MIT
 *
 * Case 5 Cisco Webex Calling SDK adapter placeholder (OP-2008).
 *
 * The production Webex Calling SDK for Linux is NDA-gated and is expected to
 * be supplied by the OP-491-style mirror during embedded builds. This header
 * keeps the adapter buildable in source-only environments and defines the
 * narrow bridge that the real SDK binding must satisfy.
 */
#ifndef OMNISIGHT_EMBEDDED_CONFERENCE_VENDOR_WEBEX_SDK_STUB_H_
#define OMNISIGHT_EMBEDDED_CONFERENCE_VENDOR_WEBEX_SDK_STUB_H_

#ifdef __cplusplus
extern "C" {
#endif

typedef struct omnisight_webex_sdk omnisight_webex_sdk;
typedef struct omnisight_webex_call omnisight_webex_call;

struct omnisight_webex_sdk_config {
	const char *sdk_root;
	const char *glibc_abi;
};

struct omnisight_webex_call_options {
	const char *meeting_url;
	const char *display_name;
};

enum omnisight_webex_sdk_status {
	OMNISIGHT_WEBEX_SDK_OK = 0,
	OMNISIGHT_WEBEX_SDK_INVALID_ARGUMENT = 1,
	OMNISIGHT_WEBEX_SDK_UNAVAILABLE = 2,
	OMNISIGHT_WEBEX_SDK_INVALID_STATE = 3,
	OMNISIGHT_WEBEX_SDK_BACKEND_ERROR = 4,
};

#if defined(OMNISIGHT_WEBEX_WITH_CALLING_SDK)
enum omnisight_webex_sdk_status
omnisight_webex_sdk_create(const struct omnisight_webex_sdk_config *config,
			   omnisight_webex_sdk **sdk);
void omnisight_webex_sdk_destroy(omnisight_webex_sdk *sdk);
const char *omnisight_webex_sdk_last_error(const omnisight_webex_sdk *sdk);
enum omnisight_webex_sdk_status
omnisight_webex_sdk_authenticate(omnisight_webex_sdk *sdk,
				 const char *access_token);
enum omnisight_webex_sdk_status
omnisight_webex_sdk_start_call(
	omnisight_webex_sdk *sdk,
	const struct omnisight_webex_call_options *options,
	omnisight_webex_call **call);
enum omnisight_webex_sdk_status
omnisight_webex_sdk_stop_call(omnisight_webex_sdk *sdk,
			      omnisight_webex_call *call);
enum omnisight_webex_sdk_status
omnisight_webex_sdk_start_media(omnisight_webex_sdk *sdk,
				omnisight_webex_call *call, int audio,
				int video);
enum omnisight_webex_sdk_status
omnisight_webex_sdk_stop_media(omnisight_webex_sdk *sdk,
			       omnisight_webex_call *call, int audio,
			       int video);
#else
static inline enum omnisight_webex_sdk_status
omnisight_webex_sdk_create(const struct omnisight_webex_sdk_config *config,
			   omnisight_webex_sdk **sdk)
{
	(void)config;
	if (sdk)
		*sdk = 0;
	return OMNISIGHT_WEBEX_SDK_UNAVAILABLE;
}

static inline void omnisight_webex_sdk_destroy(omnisight_webex_sdk *sdk)
{
	(void)sdk;
}

static inline const char *
omnisight_webex_sdk_last_error(const omnisight_webex_sdk *sdk)
{
	(void)sdk;
	return "Cisco Webex Calling SDK bridge is not linked";
}

static inline enum omnisight_webex_sdk_status
omnisight_webex_sdk_authenticate(omnisight_webex_sdk *sdk,
				 const char *access_token)
{
	(void)sdk;
	(void)access_token;
	return OMNISIGHT_WEBEX_SDK_UNAVAILABLE;
}

static inline enum omnisight_webex_sdk_status
omnisight_webex_sdk_start_call(
	omnisight_webex_sdk *sdk,
	const struct omnisight_webex_call_options *options,
	omnisight_webex_call **call)
{
	(void)sdk;
	(void)options;
	if (call)
		*call = 0;
	return OMNISIGHT_WEBEX_SDK_UNAVAILABLE;
}

static inline enum omnisight_webex_sdk_status
omnisight_webex_sdk_stop_call(omnisight_webex_sdk *sdk,
			      omnisight_webex_call *call)
{
	(void)sdk;
	(void)call;
	return OMNISIGHT_WEBEX_SDK_UNAVAILABLE;
}

static inline enum omnisight_webex_sdk_status
omnisight_webex_sdk_start_media(omnisight_webex_sdk *sdk,
				omnisight_webex_call *call, int audio,
				int video)
{
	(void)sdk;
	(void)call;
	(void)audio;
	(void)video;
	return OMNISIGHT_WEBEX_SDK_UNAVAILABLE;
}

static inline enum omnisight_webex_sdk_status
omnisight_webex_sdk_stop_media(omnisight_webex_sdk *sdk,
			       omnisight_webex_call *call, int audio,
			       int video)
{
	(void)sdk;
	(void)call;
	(void)audio;
	(void)video;
	return OMNISIGHT_WEBEX_SDK_UNAVAILABLE;
}
#endif

#ifdef __cplusplus
}
#endif

#endif // OMNISIGHT_EMBEDDED_CONFERENCE_VENDOR_WEBEX_SDK_STUB_H_
