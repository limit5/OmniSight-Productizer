# tests/ — OP-1799 skeleton + OP-1814 content

Dispatch/render coverage for this pack lives in the backend test suite at
`backend/tests/test_skill_android_rtsp_onvif_client.py` (discovery,
manifest validation, dispatcher routing to `backend.android_scaffolder`,
overlay resolution, and an end-to-end render asserting the full client
project = base skeleton + ONVIF + RTSP overlay). The generic overlay
mechanism is pinned in `backend/tests/test_scaffolder_base.py`
(`TestOverlayRendering`).

The ONVIF client's pure parsers (WS-Discovery `ProbeMatches`, Media
`GetProfiles`/`GetStreamUri`, and the WS-Security password digest) are
unit-tested in the rendered project itself at
`scaffolds/app/src/test/java/com/omnisight/pilot/onvif/OnvifParsingTest.kt`
— JVM tests that run under `gradle test` with no camera required. This
directory exists so the `tests` artifact declared in `skill.yaml`
validates.
