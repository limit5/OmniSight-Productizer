# tests/ — OP-1799 skeleton

Dispatch/render coverage for this pack lives in the backend test suite at
`backend/tests/test_skill_android_rtsp_onvif_client.py` (discovery,
manifest validation, dispatcher routing to `backend.android_scaffolder`,
and an end-to-end render of the Android skeleton).

Per-pack ONVIF-discovery and RTSP-playback client tests land with their
sequenced **follow-on** tickets in this same pack. This directory exists
so the `tests` artifact declared in `skill.yaml` validates.
