/* SPDX-License-Identifier: MIT
 *
 * Case 5 Microsoft ACS SDK placeholder (OP-2007).
 *
 * The real ACS Calling SDK headers are fetched by the platform build when
 * OMNISIGHT_ACS_WITH_SDK is enabled. This placeholder keeps the adapter
 * source buildable in open/offline worktrees without vendoring NDA material.
 */
#ifndef OMNISIGHT_EMBEDDED_CONFERENCE_VENDOR_ACS_SDK_STUB_H_
#define OMNISIGHT_EMBEDDED_CONFERENCE_VENDOR_ACS_SDK_STUB_H_

#if defined(OMNISIGHT_ACS_WITH_SDK)
extern "C" {

struct OmnisightAcsClient;
struct OmnisightAcsCall;

enum OmnisightAcsEventType {
	OMNISIGHT_ACS_EVENT_SESSION_STARTED = 0,
	OMNISIGHT_ACS_EVENT_SESSION_STOPPED,
	OMNISIGHT_ACS_EVENT_PARTICIPANT_CHANGED,
	OMNISIGHT_ACS_EVENT_MEDIA_CHANGED,
	OMNISIGHT_ACS_EVENT_ERROR,
};

using OmnisightAcsEventCallback = void (*)(
	OmnisightAcsEventType type,
	int status,
	const char *detail,
	void *user_data);

int omnisight_acs_client_create(const char *user_access_token,
				const char *display_name,
				OmnisightAcsEventCallback callback,
				void *user_data,
				OmnisightAcsClient **client);
void omnisight_acs_client_destroy(OmnisightAcsClient *client);

int omnisight_acs_join_teams_meeting(OmnisightAcsClient *client,
				     const char *teams_meeting_url,
				     bool enable_audio,
				     bool enable_video,
				     OmnisightAcsCall **call);
int omnisight_acs_call_hangup(OmnisightAcsCall *call);

} // extern "C"
#endif

#endif // OMNISIGHT_EMBEDDED_CONFERENCE_VENDOR_ACS_SDK_STUB_H_
