# tests/ — OP-1816 skeleton

Dispatch/render coverage for this pack lives in the backend test suite at
`backend/tests/test_skill_ios_map_ar.py` (discovery, manifest validation,
dispatcher routing to `backend.ios_scaffolder`, and an end-to-end render
of the iOS skeleton).

Per-pack ARKit and MapKit layer tests land with their sequenced
**follow-on** tickets in this same pack. This directory exists so the
`tests` artifact declared in `skill.yaml` validates.
